import asyncio
import json
import re
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from PIL import Image,ImageFont
from .brain import Brain
from .config import ROOT, UserError
from .models import Draft
from .render import Renderer
from .source import public_url

HELP='''Я готовлю сторис из оригинальных материалов сайта.

текст: 6 сторис про Сканди 78
редакт: на втором слайде сократи описание
пришли картинками
правка: на третьем слайде увеличь заголовок

Отвечайте на сценарий или картинку нужной серии. Если серий несколько — укажите #номер.
исходники — получить PNG без сжатия
/series — последние серии
/settings — настройки бренда
/id — ваш номер для подключения

Администраторы могут писать «настройки: …», «источник: https://…» и отправлять файлы с подписями «логотип», «шрифт заголовка», «шрифт текста», «пример», «не надо».
Фотографии не генерирую и не изменяю детали домов.'''

def command(text):
    text=text.strip()
    aliases={'/text':'text','/edit':'edit','/fix':'fix','/images':'images','/files':'files',
             '/settings':'settings','/series':'series','/start':'help','/help':'help','/id':'id','/enable':'enable'}
    first=text.split(maxsplit=1)[0] if text else ''
    if first.split('@')[0].lower() in aliases:
        return aliases[first.split('@')[0].lower()],text[len(first):].strip()
    labels={'текст':'text','текстом':'text','редакт':'edit','правка':'fix','настройки':'brand','источник':'source'}
    for label,op in labels.items():
        for pattern in (rf'^\(?{label}\)?\s*[:—-]?\s+',rf'\s+\({label}\)\s*$'):
            m=re.search(pattern,text,re.I)
            if m:return op,(text[:m.start()]+text[m.end():]).strip()
    if re.fullmatch(r'пришли\s+картинками(?:\s+#\d+)?',text,re.I):return 'images',text
    if re.fullmatch(r'исходники(?:\s+#\d+)?',text,re.I):return 'files',text
    return None,text

class Service:
    def __init__(self,config,store,tg,source,brain:Brain,files=None):
        self.cfg=config;self.store=store;self.tg=tg;self.source=source;self.brain=brain;self.renderer=Renderer()
        self.files=files
        self.default=json.loads((ROOT/'assets/brand.json').read_text(encoding='utf-8'))
        self.default['source']=config.source
        self.default['logo']=str(ROOT/'assets/logo.png')
        self.default['heading_font']=str(ROOT/'assets/fonts/Montserrat-Black.ttf')
        self.default['body_font']=str(ROOT/'assets/fonts/Montserrat-Regular.ttf')
        self.default['references']=[str(ROOT/'assets/reference.png')]

    async def admin(self,message):
        uid=message.get('from',{}).get('id');chat=message['chat']['id']
        if uid==self.cfg.owner:return True
        if message['chat']['type']=='private':return False
        if not uid:return False
        member=await self.tg.call('getChatMember',{'chat_id':chat,'user_id':uid})
        return member['status'] in ('creator','administrator')

    def resolve(self,message,text):
        chat=message['chat']['id'];topic=message.get('message_thread_id',0)
        target=None;sid=None
        reply=message.get('reply_to_message',{})
        linked=self.store.linked(chat,reply.get('message_id',0))
        if linked:sid=linked['series'];target=linked['slide']
        explicit=re.search(r'#(\d+)',text)
        if explicit:
            if sid and sid!=int(explicit[1]):raise UserError('Номер серии не совпадает с сообщением, на которое вы ответили.')
            sid=int(explicit[1])
        if not sid:
            recent=self.store.recent(chat,topic)
            if len(recent)==1:sid=recent[0]['id']
            else:raise UserError('Ответьте на сценарий или картинку нужной серии либо укажите #номер. Список: /series')
        series=self.store.get(sid,chat,topic)
        if not series:raise UserError('В этом чате и теме нет такой серии.')
        return series,target

    async def say(self,m,text):
        return await self.tg.say(m['chat']['id'],text,m.get('message_thread_id',0),m['message_id'])

    async def show(self,m,series):
        draft=Draft.model_validate(series['draft'])
        lines=[f"Серия #{series['id']} · {series['project']['title']} · версия {series['revision']}"]
        for i,s in enumerate(draft.slides,1):lines.extend([f'\n{i}. '+ ' '.join(s.title_lines),s.body])
        if draft.notice:lines.append('\n'+draft.notice)
        lines.extend(['\nИсточник: '+series['project']['url'],
                      '\nДля правок ответьте на это сообщение. Для оформления: «пришли картинками».'])
        # A long scenario is split only between slides, with every message linked to its series.
        chunks=[];buf=''
        for line in lines:
            if len(buf)+len(line)>3700:chunks.append(buf);buf=''
            buf+=line+'\n'
        if buf:chunks.append(buf)
        for chunk in chunks:
            result=await self.say(m,chunk)
            self.store.link(m['chat']['id'],result['message_id'],series['id'])

    async def upload_brand(self,m,brand):
        caption=m.get('caption','').lower().strip()
        fields={'логотип':'logo','шрифт заголовка':'heading_font','шрифт текста':'body_font','шрифт':'body_font',
                'пример':'references','не надо':'negative_references'}
        field=fields.get(caption)
        if not field:return False
        if not await self.admin(m):raise UserError('Менять настройки бренда может администратор чата.')
        doc=m.get('document');photo=m.get('photo')
        if not doc and not photo:return False
        if doc and doc.get('file_size',0)>12_000_000:raise UserError('Максимальный размер файла — 12 МБ.')
        f=doc or photo[-1]
        suffix='.ttf' if 'font' in field else '.png'
        path=self.cfg.data/'brands'/str(m['chat']['id'])/(uuid.uuid4().hex+suffix)
        await self.tg.download(f['file_id'],path)
        try:
            if 'font' in field:ImageFont.truetype(str(path),40)
            else:
                with Image.open(path) as im:
                    im.verify()
                with Image.open(path) as im:
                    im.convert('RGBA').save(path,format='PNG')
        except (ValueError,OSError):
            path.unlink(missing_ok=True)
            raise UserError('Нужен исправный TTF/OTF-шрифт или изображение PNG/JPEG/WebP.') from None
        if self.files:await self.files.upload(path)
        if field in ('references','negative_references'):brand[field]=(brand.get(field,[])+[str(path)])[-8:]
        else:brand[field]=str(path)
        brand['version']=brand.get('version',1)+1
        self.store.setbrand(m['chat']['id'],brand)
        await self.say(m,'Сохранил настройку бренда. Она будет действовать для новых серий. /settings — посмотреть настройки.')
        return True

    async def export(self,m,series,only=None):
        sid=series['id'];rev=series['revision'];chat=m['chat']['id'];topic=m.get('message_thread_id',0)
        folder=self.cfg.data/'exports'/str(chat)/str(sid)/str(rev)
        draft=Draft.model_validate(series['draft'])
        if self.files:await self.files.restore(series['project'],series['brand'])
        paths=await asyncio.to_thread(self.renderer.render,draft,series['project'],series['brand'],folder,only)
        if not paths:raise UserError('В этой версии нет новых изменений.')
        result=await self.tg.album(chat,paths,topic,f'Серия #{sid} · версия {rev}')
        indices=sorted(only) if only is not None else list(range(len(draft.slides)))
        for message,i in zip(result,indices):self.store.link(chat,message['message_id'],sid,i)
        await self.say(m,f'Готово: серия #{sid}. Для правок ответьте на картинку. «Исходники #{sid}» — PNG без сжатия.')

    async def handle(self,update):
        # Edited messages intentionally do not automatically change approved series.
        m=update.get('message')
        if not m or m.get('from',{}).get('is_bot'):return
        chat=m['chat']['id'];topic=m.get('message_thread_id',0);uid=m.get('from',{}).get('id',0)
        text=m.get('text') or m.get('caption','')
        op,arg=command(text)
        if op=='id':
            await self.say(m,f'Ваш Telegram ID: {uid}\nID чата: {chat}');return
        if op=='enable':
            if not self.cfg.owner or uid!=self.cfg.owner:raise UserError('Подключить чат может только владелец бота.')
            self.store.enable(chat,self.default)
            await self.say(m,'Чат подключён.\n\n'+HELP);return
        if not self.cfg.owner:
            if op:await self.say(m,'Владелец ещё не настроен. Отправьте /id и добавьте свой номер в OWNER_TELEGRAM_ID на сервере.')
            return
        if m['chat']['type']=='private' and uid==self.cfg.owner and not self.store.enabled(chat):self.store.enable(chat,self.default)
        if not self.store.enabled(chat):
            if op:await self.say(m,'Этот чат ещё не подключён. Владельцу бота нужно отправить /enable.')
            return
        brand=self.store.brand(chat,self.default)
        if (m.get('document') or m.get('photo')) and await self.upload_brand(m,brand):return
        if op=='help':await self.say(m,HELP);return
        if op=='settings':
            rules='\n'.join('• '+r for r in brand.get('rules',[]))
            await self.say(m,f"Настройки бренда · версия {brand.get('version',1)}\nИсточник: {brand['source']}\n"
                           f"Логотип: сохранён\nПримеров: {len(brand.get('references',[]))}\n"
                           f"Антипримеров: {len(brand.get('negative_references',[]))}\n\n{rules}\n\n"
                           'Существующие серии сохраняют настройки, с которыми были созданы.');return
        if op in ('brand','source'):
            if not await self.admin(m):raise UserError('Менять настройки бренда может администратор чата.')
            if not arg:raise UserError('Добавьте настройки после двоеточия.')
            if op=='source':
                public_url(arg,self.cfg.hosts)
                brand['source']=arg.rstrip('/')
            else:brand['rules']=(brand.get('rules',[])+[arg])[-40:]
            brand['version']=brand.get('version',1)+1;self.store.setbrand(chat,brand)
            await self.say(m,'Настройки сохранены для новых серий. /settings — посмотреть.');return
        if op=='series':
            ids=self.store.recent(chat,topic)
            await self.say(m,'Последние серии:\n'+'\n'.join('#'+str(r['id']) for r in ids) if ids else 'Серий пока нет. Начните с «текст: Сканди 78».');return
        if op not in ('text','edit','fix','images','files'):return
        replay=self.store.operation(update['update_id'],chat,topic)
        if replay:
            if op=='fix':await self.export(m,replay)
            else:await self.show(m,replay)
            return
        if op in ('text','edit','fix') and not self.store.use(chat,self.cfg.daily_limit):
            raise UserError('На сегодня достигнут лимит подготовки текста и правок для этого чата.')
        if op=='text':
            if not arg:raise UserError('Напишите тему после «текст:». Например: Сканди 78.')
            await self.say(m,'Ищу проект и оригинальные материалы на сайте…')
            urls=re.findall(r'https://[^\s<>]+',arg)
            if urls:
                url=urls[0].rstrip(').,');public_url(url,self.cfg.hosts)
                if urlsplit(url).hostname!=urlsplit(brand['source']).hostname:
                    raise UserError('Ссылка должна вести на выбранный источник бренда.')
            else:url=await self.brain.select(arg,await self.source.index(brand['source']))
            project=await self.source.collect(url)
            if self.files:
                await self.files.save_project(project)
                await self.files.restore(brand=brand)
            await self.say(m,'Материалы найдены. Готовлю текст и подбираю оригиналы для слайдов…')
            draft=await self.brain.draft(arg,project,brand)
            # Persist before sending so revisions remain reviewable after a Telegram failure.
            series=self.store.create(chat,topic,uid,project,brand,draft.model_dump(),update['update_id'])
            self.store.link(chat,m['message_id'],series['id'])
            await self.show(m,series);return
        series,target=self.resolve(m,arg)
        if op in ('edit','fix'):
            request=re.sub(r'#\d+','',arg).strip()
            if not request:raise UserError('Напишите, что изменить.')
            if target is None:
                named=re.search(r'(?:слайд(?:е|а)?|слайд\s*№)\s*(\d+)',request,re.I)
                if named:target=int(named[1])-1
            if target is not None and not 0<=target<len(series['draft']['slides']):raise UserError('В серии нет такого слайда.')
            await self.say(m,'Применяю правки…')
            if self.files:await self.files.restore(series['project'],series['brand'])
            draft=await self.brain.draft(request,series['project'],series['brand'],series['draft'],target)
            old=series['draft']['slides']
            changed={i for i,s in enumerate(draft.slides) if i>=len(old) or s.model_dump()!=old[i]}
            if not changed and len(old)==len(draft.slides):
                await self.say(m,'В этой версии текст и оформление остались без изменений. Уточните правку.');return
            self.store.revise(series,draft.model_dump(),update['update_id'])
            series=self.store.get(series['id'],chat,topic)
            self.store.link(chat,m['message_id'],series['id'],target)
            if op=='edit':await self.show(m,series)
            else:
                # Do not render unchanged slides or overwrite their old files.
                await self.export(m,series,changed)
            return
        if op=='images':await self.export(m,series);return
        if op=='files':
            if self.files:await self.files.restore(series['project'],series['brand'])
            folder=self.cfg.data/'exports'/str(chat)/str(series['id'])/str(series['revision'])
            # Regenerate all from the persisted approved version to include unchanged slides.
            paths=await asyncio.to_thread(self.renderer.render,Draft.model_validate(series['draft']),series['project'],series['brand'],folder)
            archive=folder/'stories.zip'
            with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
                for p in paths:z.write(p,p.name)
            result=await self.tg.document(chat,archive,topic,f"Серия #{series['id']} · PNG без сжатия")
            self.store.link(chat,result['message_id'],series['id'])
