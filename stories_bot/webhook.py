"""Render Free entry point: authenticated webhook + Supabase-backed durable queue."""
import asyncio
from contextlib import suppress
import hmac
import json
import logging
import os
from pathlib import Path
import tempfile
import uuid
from aiohttp import web
import httpx
from .brain import Brain
from .cloud import Cloud,snapshot,restore_snapshot
from .config import Config,UserError
from .service import Service,command
from .source import Source
from .store import Store
from .telegram import Telegram

log=logging.getLogger('stories_bot')

def actionable(update,username):
    m=update.get('message')
    if not isinstance(m,dict) or m.get('from',{}).get('is_bot'):return False
    text=m.get('text') or m.get('caption','')
    if not isinstance(text,str):return False
    first=text.split(maxsplit=1)[0] if text.strip() else ''
    if first.startswith('/') and '@' in first and first.split('@',1)[1].lower()!=username.lower():return False
    op,_=command(text)
    return bool(op or text.strip().lower() in ('логотип','шрифт','шрифт заголовка','шрифт текста','пример','не надо'))

async def receive(request):
    secret=request.app['secret']
    provided=request.headers.get('X-Telegram-Bot-Api-Secret-Token','')
    if not hmac.compare_digest(secret.encode(),provided.encode()):raise web.HTTPForbidden()
    try:payload=await request.json()
    except (ValueError,UnicodeError):raise web.HTTPBadRequest()
    if not isinstance(payload,dict) or type(payload.get('update_id')) is not int:raise web.HTTPBadRequest()
    if not actionable(payload,request.app['username']):return web.json_response({'ok':True})
    try:await request.app['cloud'].rpc('stories_enqueue',p_bot=request.app['cloud'].bot,p_update=payload)
    except UserError:raise web.HTTPServiceUnavailable()
    request.app['wake'].set()
    # Telegram is acknowledged only after durable insertion, never after an in-memory task.
    return web.json_response({'ok':True})

async def lease_guard(cloud,owner,job):
    while True:
        await asyncio.sleep(20)
        try:ok=await cloud.rpc('stories_renew',p_bot=cloud.bot,p_owner=owner)
        except UserError:ok=False
        if not ok:
            job.cancel()
            return

async def process(config,cloud,tg,brain,source,update,store):
    svc=Service(config,store,tg,source,brain,cloud)
    try:await svc.handle(update)
    except UserError as e:
        m=update.get('message',{})
        await tg.say(m['chat']['id'],str(e),m.get('message_thread_id',0),m.get('message_id'))
    except Exception as e:
        log.error('Command failed: %s',type(e).__name__)
        m=update.get('message',{})
        await tg.say(m['chat']['id'],'Не удалось завершить действие. Сохранённые серии доступны; попробуйте ещё раз.',m.get('message_thread_id',0))

async def worker(app):
    cfg=app['config'];cloud=app['cloud'];owner=str(uuid.uuid4())
    source=Source(app['client'],cfg.hosts,cfg.data/'projects',cfg.max_assets)
    brain=Brain(app['client'],cfg.api_key,cfg.model)
    cache=cfg.data/'state-cache';cache.mkdir(parents=True,exist_ok=True)
    while True:
        try:
            item=await cloud.rpc('stories_claim',p_bot=cloud.bot,p_owner=owner)
            if not item:
                app['wake'].clear()
                try:await asyncio.wait_for(app['wake'].wait(),timeout=5)
                except TimeoutError:pass
                continue
            with tempfile.TemporaryDirectory(dir=cache) as tmp:
                dbpath=Path(tmp)/'working.sqlite3'
                restore_snapshot(item['snapshot'],dbpath)
                store=Store(dbpath)
                task=asyncio.create_task(process(cfg,cloud,app['tg'],brain,source,item['update'],store))
                guard=asyncio.create_task(lease_guard(cloud,owner,task))
                try:
                    # Bound jobs, so a stuck API cannot indefinitely hold the lease.
                    await asyncio.wait_for(task,timeout=600)
                    state=snapshot(store)
                    ok=await cloud.rpc('stories_complete',p_bot=cloud.bot,p_owner=owner,
                                       p_update_id=item['update']['update_id'],p_snapshot=state)
                    if not ok:raise UserError('Срок обработки команды истёк; она будет повторена.')
                except asyncio.CancelledError:
                    # Lost lease: don't acknowledge or publish this local checkpoint.
                    if asyncio.current_task().cancelling():raise
                    log.warning('Lease lost; update remains queued')
                finally:
                    task.cancel();guard.cancel()
                    await asyncio.gather(task,guard,return_exceptions=True)
                    store.db.close()
        except asyncio.CancelledError:raise
        except Exception as e:
            # No exception text: URLs may contain API tokens or database credentials.
            log.warning('Cloud worker retry: %s',type(e).__name__)
            await asyncio.sleep(10)

def create_app(config,cloud,secret,username='avangardstories_bot'):
    if len(secret)<32:raise UserError('WEBHOOK_SECRET должен содержать не менее 32 символов.')
    app=web.Application(client_max_size=256*1024)
    app['config']=config;app['cloud']=cloud;app['secret']=secret;app['username']=username
    app['wake']=asyncio.Event()
    app.router.add_post('/telegram',receive)
    async def health(request):return web.json_response({'status':'ok'})
    app.router.add_get('/health',health)
    return app

async def runtime(app):
    cfg=app['config']
    async with httpx.AsyncClient(headers={'User-Agent':'AvgStoriesBot/1.0'},limits=httpx.Limits(max_connections=12)) as client:
        app['client']=client;app['cloud'].client=client;app['tg']=Telegram(client,cfg.token)
        public=os.getenv('PUBLIC_URL') or ('https://'+os.environ['RENDER_EXTERNAL_HOSTNAME'] if os.getenv('RENDER_EXTERNAL_HOSTNAME') else '')
        if not public.startswith('https://'):raise UserError('Нужен PUBLIC_URL — HTTPS-адрес веб-сервиса Render.')
        # Setting AUTO_SET_WEBHOOK=1 explicitly authorizes connecting this deployment.
        if os.getenv('AUTO_SET_WEBHOOK','0')=='1':
            desired=public.rstrip('/')+'/telegram'
            current=await app['tg'].call('getWebhookInfo')
            if current.get('url') and current['url']!=desired:
                raise UserError('У бота уже другой webhook. Отключите старое размещение перед переносом.')
            await app['tg'].call('setWebhook',{'url':desired,'secret_token':app['secret'],
                                              'allowed_updates':['message'],'max_connections':1,'drop_pending_updates':False})
        task=asyncio.create_task(worker(app))
        try:yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):await task

def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.CRITICAL);logging.getLogger('httpcore').setLevel(logging.CRITICAL)
    cfg=Config.load()
    if not cfg.token:raise SystemExit('Нужен TELEGRAM_BOT_TOKEN в секретах сервиса.')
    cfg.data.mkdir(parents=True,exist_ok=True)
    for name in ('SUPABASE_URL','SUPABASE_SERVICE_ROLE_KEY','WEBHOOK_SECRET'):
        if not os.getenv(name):raise SystemExit('Нужно заполнить секрет сервиса: '+name)
    cloud=Cloud(None,os.environ['SUPABASE_URL'],os.environ['SUPABASE_SERVICE_ROLE_KEY'],
                os.getenv('BOT_INSTANCE_KEY','avangardstories'),os.getenv('SUPABASE_BUCKET','stories-originals'),cfg.data)
    app=create_app(cfg,cloud,os.environ['WEBHOOK_SECRET'],os.getenv('BOT_USERNAME','avangardstories_bot'))
    app.cleanup_ctx.append(runtime)
    # Avoid logging webhook bodies, secret headers or request query strings.
    web.run_app(app,host='0.0.0.0',port=int(os.getenv('PORT','10000')),access_log=None,shutdown_timeout=20)

if __name__=='__main__':main()
