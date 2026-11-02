import asyncio
import copy
import hashlib
import json
from io import BytesIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock
from PIL import Image
import httpx

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from demo import example
from stories_bot.brain import Brain
from stories_bot.config import Config,ROOT,UserError
from stories_bot.models import Draft,validate_draft
from stories_bot.render import Renderer
from stories_bot.service import Service,command
from stories_bot.source import extract_project,public_url,Source
from stories_bot.store import Store
from stories_bot.telegram import Telegram

class Commands(unittest.TestCase):
    def test_russian_tags_and_aliases(self):
        for text,expected in [('текст: Сканди 78','text'),('(текст) Сканди 78','text'),
                              ('Сканди 78 (текстом)','text'),('редакт: сократи','edit'),
                              ('правка: увеличь','fix'),('пришли картинками #12','images'),
                              ('/images@mybot #12','images'),('исходники #3','files')]:
            with self.subTest(text=text):self.assertEqual(command(text)[0],expected)
    def test_group_conversation_is_ignored(self):
        for text in ['Как вам этот текст?', 'Сканди 78', 'Привет', '']:
            self.assertIsNone(command(text)[0])

class SourceTests(unittest.TestCase):
    def fixture(self):
        return '''<div data-record-type="1" class="uc-projectname"><h1>Сканди 78</h1></div>
        <div data-record-type="1">Площадь 78 м² 3 комнаты
        <div data-field-imgs-value='[{"li_img":"https://static.tildacdn.com/a/house.jpg"}]'></div></div>
        <div data-record-type="1" class="uc-plans"><div data-img-zoom-url="https://static.tildacdn.com/a/plan.jpg"></div></div>
        <div data-record-type="1">«Сканди 78» — '''+'описание проекта '*15+'''</div>
        <div data-record-type="1"><div data-field-imgs-value='[{"li_img":"https://static.tildacdn.com/unrelated/other.jpg"}]'></div></div>'''
    def test_only_current_project_gallery_and_plan(self):
        p=extract_project(self.fixture(),'https://old.avgst.ru/catalog/test')
        self.assertEqual(len(p['assets']),2)
        self.assertEqual([a['kind'] for a in p['assets']],['photo','plan'])
        self.assertNotIn('other.jpg',json.dumps(p))
        self.assertIn('78',p['text'])
    def test_unknown_site_structure_fails_explicitly(self):
        with self.assertRaises(UserError):extract_project('<h1>Дом</h1><img src="x.jpg">','https://old.avgst.ru')
    def test_source_allowlist_blocks_private_and_foreign_urls(self):
        for url in ['http://old.avgst.ru','https://127.0.0.1/','https://evil.test/',
                    'https://old.avgst.ru.evil.test','https://user:pw@old.avgst.ru',
                    'https://old.avgst.ru:8443']:
            with self.subTest(url=url),self.assertRaises(UserError):public_url(url,{'old.avgst.ru'})
    def test_bad_asset_and_plan_crop_rejected(self):
        p,d,_=example()
        d.slides[0].image_ids=['foreign']
        with self.assertRaises(ValueError):validate_draft(d,p)
        p,d,_=example();d.slides[3].crop=[0,0,.8,.8]
        with self.assertRaises(ValueError):validate_draft(d,p)
        p,d,_=example();d.slides[0].crop=[.8,0,.8,1]
        with self.assertRaises(ValueError):validate_draft(d,p)

class Persistence(unittest.TestCase):
    def test_isolation_revisions_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'db.sqlite'
            st=Store(path);st.enable(-1,{'version':1})
            s=st.create(-1,2,42,{'title':'x'},{'version':1},{'slides':[]},100)
            self.assertIsNone(st.get(s['id'],-2,2));self.assertIsNone(st.get(s['id'],-1,3))
            st.revise(s,{'slides':['edited']},101)
            st.link(-1,55,s['id'],2)
            st.enqueue({'update_id':10,'message':{'text':'правка: x'}})
            st.db.close();st=Store(path)
            self.assertEqual(st.get(s['id'],-1,2)['revision'],2)
            self.assertEqual(st.operation(100,-1,2)['draft'],{'slides':[]})
            self.assertEqual(st.operation(101,-1,2)['revision'],2)
            self.assertEqual(st.linked(-1,55)['slide'],2)
            self.assertIsNone(st.linked(-2,55))
            self.assertEqual(len(st.pending()),1)
            st.finish(10);self.assertEqual(st.pending(),[])
            self.assertEqual(st.db.execute('SELECT payload FROM inbox WHERE id=10').fetchone()[0],'{}')
            st.db.close()
    def test_daily_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp)/'x.sqlite')
            self.assertTrue(s.use(1,1));self.assertFalse(s.use(1,1));self.assertTrue(s.use(2,1));s.db.close()

class RenderTests(unittest.TestCase):
    def test_six_exports_and_source_integrity(self):
        p,d,b=example();before={a['path']:hashlib.sha256(Path(a['path']).read_bytes()).hexdigest() for a in p['assets']}
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);paths=Renderer().render(d,p,b,out)
            self.assertEqual(len(paths),6)
            for f in paths:
                with Image.open(f) as im:self.assertEqual(im.size,(1080,1920))
            old={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in paths}
            d.slides[1].body='Более короткое описание.'
            changed=Renderer().render(d,p,b,out,{1})
            self.assertEqual([x.name for x in changed],['story-02.png'])
            self.assertNotEqual(old['story-02.png'],hashlib.sha256((out/'story-02.png').read_bytes()).hexdigest())
            for f in paths:
                if f.name!='story-02.png':self.assertEqual(old[f.name],hashlib.sha256(f.read_bytes()).hexdigest())
        for path,digest in before.items():self.assertEqual(digest,hashlib.sha256(Path(path).read_bytes()).hexdigest())
    def test_long_copy_fails_instead_of_silent_rewrite(self):
        p,d,b=example();d.slides[0].body='очень длинный текст '*30
        with tempfile.TemporaryDirectory() as tmp,self.assertRaises(UserError):Renderer().render(d,p,b,Path(tmp))

class BrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_slide_count_does_not_replace_project_number(self):
        async with httpx.AsyncClient() as client:
            b=Brain(client,'','gpt-5-mini')
            urls=['https://old.avgst.ru/catalog/product/scandi-78','https://old.avgst.ru/catalog/product/scandi-89']
            self.assertTrue((await b.select('10 сторис про Сканди 78',urls)).endswith('78'))
    async def test_responses_schema_and_no_generation_tools(self):
        p,d,b=example();captured=[]
        def respond(request):
            body=json.loads(request.content);captured.append(body)
            return httpx.Response(200,json={'status':'completed','output':[{'type':'message','content':[
                {'type':'output_text','text':json.dumps({'action':'apply','message':'','draft':d.model_dump()})}]}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result=await Brain(client,'fake','gpt-5-mini').draft('Сканди 78',p,b)
        self.assertEqual(len(result.slides),6)
        self.assertFalse(captured[0]['store']);self.assertEqual(captured[0]['text']['format']['type'],'json_schema')
        self.assertNotIn('tools',captured[0])
        self.assertTrue(any(c['type']=='input_image' for c in captured[0]['input'][0]['content']))
    async def test_single_slide_edit_cannot_mutate_other_slides(self):
        p,d,b=example();bad=d.model_copy(deep=True);bad.slides[0].body='unrequested change'
        def respond(request):
            return httpx.Response(200,json={'status':'completed','output':[{'type':'message','content':[
                {'type':'output_text','text':json.dumps({'action':'apply','message':'','draft':bad.model_dump()})}]}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with self.assertRaises(UserError):await Brain(client,'fake','model').draft('Исправь второй',p,b,d.model_dump(),1)
    async def test_api_error_does_not_echo_key(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(401,json={'error':'secret'}))) as client:
            with self.assertRaises(UserError) as c:await Brain(client,'super-secret','model').select('неясная тема',[f'https://x/{i}' for i in range(15)])
            self.assertNotIn('secret',str(c.exception))

class FakeTelegram:
    def __init__(self):self.counter=100;self.sent=[];self.albums=[]
    async def say(self,chat,text,topic=0,reply=None):
        self.counter+=1;self.sent.append((chat,text,self.counter));return {'message_id':self.counter}
    async def call(self,method,payload=None):return {'status':'member'}
    async def album(self,chat,paths,topic=0,caption=''):
        self.albums.append(paths);result=[]
        for p in paths:self.counter+=1;result.append({'message_id':self.counter})
        return result
    async def document(self,*a,**kw):self.counter+=1;return {'message_id':self.counter}

class TelegramTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limited_upload_rewinds_file(self):
        bodies=[]
        def respond(request):
            bodies.append(request.content)
            if len(bodies)==1:return httpx.Response(429,json={'ok':False,'error_code':429,'parameters':{'retry_after':0}})
            return httpx.Response(200,json={'ok':True,'result':{'message_id':1}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            tg=Telegram(client,'fake-token')
            await tg.call('sendPhoto',{'chat_id':1},{'photo':('image.png',BytesIO(b'original-image-content'),'image/png')})
        self.assertEqual(len(bodies),2)
        self.assertTrue(all(b'original-image-content' in b for b in bodies))

class Cycle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.project,self.draft,self.brand=example()
        self.cfg=Config('fake','fake','model',42,'https://old.avgst.ru',frozenset({'old.avgst.ru'}),self.root)
        self.st=Store(self.root/'db.sqlite');self.tg=FakeTelegram()
        self.source=type('Source',(),{})();self.source.index=AsyncMock(return_value=[self.project['url']]);self.source.collect=AsyncMock(return_value=self.project)
        self.brain=type('Brain',(),{})();self.brain.select=AsyncMock(return_value=self.project['url']);self.brain.draft=AsyncMock(return_value=self.draft)
        self.service=Service(self.cfg,self.st,self.tg,self.source,self.brain)
        self.st.enable(-100,self.service.default)
    async def asyncTearDown(self):self.st.db.close();self.tmp.cleanup()
    def update(self,uid,text,chat=-100,reply=None,user=42):
        m={'message_id':uid,'chat':{'id':chat,'type':'supergroup'},'from':{'id':user},'text':text}
        if reply:m['reply_to_message']={'message_id':reply}
        return {'update_id':uid,'message':m}
    async def test_full_create_edit_images_fix_and_restart_replay(self):
        create=self.update(1,'текст: Сканди 78');await self.service.handle(create)
        self.assertEqual(self.brain.draft.call_count,1)
        await self.service.handle(create)
        self.assertEqual(self.brain.draft.call_count,1)
        edited=self.draft.model_copy(deep=True);edited.slides[5].body='в директ — расскажем о комплектациях.'
        self.brain.draft.return_value=edited
        await self.service.handle(self.update(2,'редакт: сократи финал',reply=1))
        self.assertEqual(self.st.get(1,-100,0)['revision'],2)
        await self.service.handle(self.update(3,'пришли картинками',reply=1))
        self.assertEqual(len(self.tg.albums[-1]),6)
        slide_message=self.tg.counter-5
        fixed=edited.model_copy(deep=True);fixed.slides[0].heading_size=105;self.brain.draft.return_value=fixed
        await self.service.handle(self.update(4,'правка: увеличь заголовок',reply=slide_message))
        self.assertEqual(len(self.tg.albums[-1]),1)
        self.assertEqual(self.st.get(1,-100,0)['revision'],3)
        await self.service.handle(self.update(5,'исходники #1'))
        self.assertTrue((self.root/'exports/-100/1/3/stories.zip').exists())
    async def test_unauthorized_chat_never_calls_model(self):
        await self.service.handle(self.update(1,'текст: Сканди 78',chat=-200,user=43))
        self.brain.draft.assert_not_called();self.source.collect.assert_not_called()
    async def test_member_cannot_change_brand(self):
        with self.assertRaises(UserError):await self.service.handle(self.update(1,'настройки: новый логотип',user=43))
    async def test_multiple_series_require_explicit_choice(self):
        await self.service.handle(self.update(1,'текст: Сканди 78'))
        await self.service.handle(self.update(2,'текст: Сканди 78'))
        with self.assertRaises(UserError):self.service.resolve(self.update(3,'пришли картинками')['message'],'')
    async def test_other_chat_or_topic_cannot_resolve_series(self):
        await self.service.handle(self.update(1,'текст: Сканди 78'))
        msg=self.update(2,'пришли картинками #1',chat=-200)['message']
        with self.assertRaises(UserError):self.service.resolve(msg,'#1')
        msg=self.update(3,'пришли картинками #1')['message'];msg['message_thread_id']=5
        with self.assertRaises(UserError):self.service.resolve(msg,'#1')

if __name__=='__main__':unittest.main()
