import base64
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock
import httpx
from aiohttp.test_utils import TestClient,TestServer

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from stories_bot.cloud import Cloud,snapshot,restore_snapshot
from stories_bot.config import Config,UserError
from stories_bot.store import Store
from stories_bot.webhook import actionable,create_app,webhook_secret,configure_webhook
from stories_bot.telegram import Telegram
import re

class SnapshotTests(unittest.TestCase):
    def test_generated_secret_is_telegram_compatible_and_stable(self):
        raw='a'*32+'+/= '+'я'
        result=webhook_secret(raw)
        self.assertRegex(result,r'^[A-Za-z0-9_-]{32,256}$')
        self.assertEqual(result,webhook_secret(raw))
        self.assertNotEqual(result,webhook_secret(raw+'different'))
        self.assertEqual(webhook_secret('safe-secret_'*4),'safe-secret_'*4)
    def test_checkpoint_survives_total_loss_of_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);s=Store(root/'before.sqlite')
            s.enable(-100,{'rules':['сохранить']})
            series=s.create(-100,0,42,{'title':'Сканди 78'},{'version':1},{'slides':['original']},11)
            s.revise(series,{'slides':['updated']},12)
            s.link(-100,5,series['id'],0)
            data=snapshot(s);s.db.close()
            restore_snapshot(data,root/'other-machine'/'new.sqlite')
            restored=Store(root/'other-machine'/'new.sqlite')
            self.assertTrue(restored.enabled(-100))
            self.assertEqual(restored.get(1,-100,0)['draft'],{'slides':['updated']})
            self.assertEqual(restored.linked(-100,5)['slide'],0)
            self.assertEqual(restored.operation(11,-100,0)['revision'],1)
            restored.db.close()
    def test_corrupt_checkpoint_rejected(self):
        with tempfile.TemporaryDirectory() as tmp,self.assertRaises(UserError):
            restore_snapshot(base64.b64encode(b'not a database').decode(),Path(tmp)/'x.sqlite')

class FilesTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_restored_and_hash_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);seen=[];data=b'immutable original'
            def respond(request):
                seen.append(request)
                return httpx.Response(200,content=data)
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                c=Cloud(client,'https://example.supabase.co','private-key','my-bot','stories-originals',root)
                f=root/'projects/hash/photo.jpg'
                await c.ensure(f,hashlib.sha256(data).hexdigest())
                self.assertEqual(f.read_bytes(),data)
                self.assertIn('/authenticated/stories-originals/my-bot/projects/hash/photo.jpg',str(seen[0].url))
                await c.ensure(f,hashlib.sha256(data).hexdigest());self.assertEqual(len(seen),1)
    async def test_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=Cloud(None,'https://example.supabase.co','private-key','my-bot','bucket',Path(tmp))
            with self.assertRaises(UserError):c.object_key(Path(tmp).parent/'outside-secret')
    async def test_storage_failure_does_not_leak_credentials(self):
        def respond(r):return httpx.Response(403,json={'message':'private-key'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            c=Cloud(client,'https://example.supabase.co','private-key','bot','bucket',Path(tempfile.gettempdir()))
            with self.assertRaises(UserError) as e:await c.rpc('stories_claim',p_bot='bot',p_owner='x')
            self.assertNotIn('private-key',str(e.exception))
    async def test_hash_mismatch_not_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=b'wrong'))) as client:
                c=Cloud(client,'https://example.supabase.co','key','bot','bucket',Path(tmp))
                path=Path(tmp)/'original.jpg'
                with self.assertRaises(UserError):await c.ensure(path,'00'*32)
                self.assertFalse(path.exists())

class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_accepts_hosting_secret_without_openai_key(self):
        self.cfg=Config('token','','model',42,'https://old.avgst.ru',frozenset({'old.avgst.ru'}),Path(self.tmp.name))
        raw='x'*32+'+/='
        app=create_app(self.cfg,self.cloud,raw)
        def respond(request):
            payload=json.loads(request.content)
            if request.url.path.endswith('/getWebhookInfo'):
                return httpx.Response(200,json={'ok':True,'result':{'url':''}})
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',payload['secret_token']):
                return httpx.Response(400,json={'ok':False,'error_code':400,'description':'Bad Request: secret token contains invalid characters'})
            self.assertEqual(payload['secret_token'],app['secret'])
            self.assertFalse(payload['drop_pending_updates'])
            return httpx.Response(200,json={'ok':True,'result':True})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await configure_webhook(Telegram(client,'token'),'https://example.onrender.com',app['secret'])
    async def test_receiver_uses_same_derived_secret_as_registration(self):
        raw='x'*32+'+/='
        app=create_app(self.cfg,self.cloud,raw)
        client=TestClient(TestServer(app));await client.start_server()
        try:
            r=await client.post('/telegram',json=self.payload(),headers={'X-Telegram-Bot-Api-Secret-Token':webhook_secret(raw)})
            self.assertEqual(r.status,200)
            r=await client.post('/telegram',json=self.payload(),headers={'X-Telegram-Bot-Api-Secret-Token':raw})
            self.assertEqual(r.status,403)
        finally:await client.close()
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.secret='s'*40
        self.cfg=Config('token','key','model',42,'https://old.avgst.ru',frozenset({'old.avgst.ru'}),Path(self.tmp.name))
        self.cloud=type('FakeCloud',(),{})();self.cloud.bot='bot';self.cloud.rpc=AsyncMock(return_value=True)
        self.app=create_app(self.cfg,self.cloud,self.secret)
        self.client=TestClient(TestServer(self.app));await self.client.start_server()
    async def asyncTearDown(self):await self.client.close();self.tmp.cleanup()
    def payload(self,text='текст: Сканди 78'):
        return {'update_id':1,'message':{'message_id':1,'chat':{'id':-100},'from':{'id':42},'text':text}}
    async def post(self,payload,secret=None):
        return await self.client.post('/telegram',json=payload,headers={'X-Telegram-Bot-Api-Secret-Token':secret or self.secret})
    async def test_unauthenticated_updates_never_enter_queue(self):
        r=await self.post(self.payload(),'wrong');self.assertEqual(r.status,403);self.cloud.rpc.assert_not_called()
    async def test_acknowledges_only_after_durable_enqueue(self):
        r=await self.post(self.payload());self.assertEqual(r.status,200)
        self.cloud.rpc.assert_awaited_once_with('stories_enqueue',p_bot='bot',p_update=self.payload())
        self.assertTrue(self.app['wake'].is_set())
    async def test_storage_failure_returns_retryable_503(self):
        self.cloud.rpc.side_effect=UserError('offline')
        r=await self.post(self.payload());self.assertEqual(r.status,503)
    async def test_ordinary_chat_not_persisted(self):
        r=await self.post(self.payload('Всем привет'));self.assertEqual(r.status,200);self.cloud.rpc.assert_not_called()
    async def test_commands_for_other_bot_ignored(self):
        r=await self.post(self.payload('/text@other_bot Сканди 78'));self.assertEqual(r.status,200);self.cloud.rpc.assert_not_called()
    async def test_malformed_update_rejected(self):
        r=await self.post({'update_id':'not-an-int'});self.assertEqual(r.status,400)

if __name__=='__main__':unittest.main()
