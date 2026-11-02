import asyncio
from collections import defaultdict
import json
import logging
import os
from pathlib import Path
import time
import httpx
from .brain import Brain
from .config import Config,UserError
from .service import Service,command
from .source import Source
from .store import Store
from .telegram import Telegram

log=logging.getLogger('stories_bot')

class ProcessLock:
    def __init__(self,path):self.path=path;self.file=None
    def __enter__(self):
        self.file=self.path.open('a+b');self.file.seek(0);self.file.write(b'0');self.file.flush();self.file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close();raise UserError('Другой экземпляр бота уже работает с этой папкой данных.') from None
        return self
    def __exit__(self,*args):self.file.close()

async def run(config):
    if not config.token:raise UserError('Добавьте TELEGRAM_BOT_TOKEN в файл .env. Инструкция: README.md.')
    config.data.mkdir(parents=True,exist_ok=True)
    with ProcessLock(config.data/'bot.lock'):
        store=Store(config.data/'bot.sqlite3')
        # HTTP libraries must not log URLs because Telegram authenticates in the URL path.
        logging.getLogger('httpx').setLevel(logging.CRITICAL)
        logging.getLogger('httpcore').setLevel(logging.CRITICAL)
        async with httpx.AsyncClient(headers={'User-Agent':'AvgStoriesBot/1.0'},limits=httpx.Limits(max_connections=16)) as client:
            tg=Telegram(client,config.token)
            me=await tg.call('getMe')
            webhook=await tg.call('getWebhookInfo')
            if webhook.get('url'):raise UserError('У этого бота уже настроен webhook. Нужен отдельный бот или отключение старого подключения.')
            await tg.call('setMyCommands',{'commands':[
                {'command':'help','description':'Как создать сторис'},
                {'command':'series','description':'Последние серии'},
                {'command':'settings','description':'Настройки бренда'},
                {'command':'id','description':'Мой номер для подключения'}]})
            source=Source(client,config.hosts,config.data/'projects',config.max_assets)
            service=Service(config,store,tg,source,Brain(client,config.api_key,config.model))
            locks=defaultdict(asyncio.Lock);budget=asyncio.Semaphore(3)
            tasks=set();inflight=set()
            async def job(update):
                m=update.get('message',{});chat=m.get('chat',{}).get('id',0)
                async with locks[chat]:
                    async with budget:
                        completed=False
                        try:
                            await service.handle(update);completed=True
                        except UserError as e:
                            completed=True
                            if chat:
                                try:await tg.say(chat,str(e),m.get('message_thread_id',0),m.get('message_id'))
                                except UserError:log.warning('Could not deliver a user-facing error')
                        except Exception as e:
                            completed=True
                            # Stack traces may include request URLs or private message contents.
                            log.error('Job failed: %s',type(e).__name__)
                            if chat:
                                try:await tg.say(chat,'Не удалось завершить действие. Серии сохранены. Попробуйте ещё раз.',m.get('message_thread_id',0))
                                except UserError:pass
                        finally:
                            if completed:store.finish(update['update_id'])
                            inflight.discard(update['update_id'])
            def schedule(update):
                uid=update['update_id']
                if uid not in inflight:
                    inflight.add(uid);task=asyncio.create_task(job(update));tasks.add(task);task.add_done_callback(tasks.discard)
            for update in store.pending():schedule(update)
            log.info('Bot connected: @%s; owner configured: %s',me.get('username',''),bool(config.owner))
            if not config.api_key:log.warning('Text model key is missing; model-backed actions are unavailable')
            offset=store.kv('offset',0)
            try:
                while True:
                    try:
                        updates=await tg.call('getUpdates',{'offset':offset,'timeout':30,'limit':20,'allowed_updates':['message']})
                        (config.data/'heartbeat').write_text(str(time.time()),encoding='ascii')
                        for update in updates:
                            m=update.get('message',{})
                            text=m.get('text') or m.get('caption','')
                            # Ignore ordinary group conversation; retain only actionable messages.
                            op,_=command(text)
                            caption=text.lower().strip()
                            upload=caption in ('логотип','шрифт','шрифт заголовка','шрифт текста','пример','не надо')
                            addressed=text.split(maxsplit=1)[0] if text.strip() else ''
                            wrong_bot=addressed.startswith('/') and '@' in addressed and addressed.split('@',1)[1].lower()!=me.get('username','').lower()
                            if (op or upload) and not wrong_bot:
                                store.enqueue(update);schedule(update)
                            offset=max(offset,update['update_id']+1)
                            store.setkv('offset',offset)
                        # Bound the in-memory backlog while keeping unacknowledged updates in Telegram.
                        if len(tasks)>=30:await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                    except UserError:
                        log.warning('Telegram connection unavailable; retrying')
                        await asyncio.sleep(5)
            finally:
                # Allow a short graceful shutdown; pending inbox items are replayed after restart.
                if tasks:
                    done,pending=await asyncio.wait(tasks,timeout=20)
                    for task in pending:task.cancel()

def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    try:asyncio.run(run(Config.load()))
    except KeyboardInterrupt:pass
    except UserError as e:log.error('%s',e);raise SystemExit(1)

if __name__=='__main__':main()
