import asyncio
import json
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import quote
import httpx
from .config import UserError

class Telegram:
    def __init__(self,client,token):self.client=client;self.token=token

    async def call(self,method,payload=None,files=None):
        # Never log HTTP request URLs: Telegram tokens are part of them.
        for attempt in range(3):
            try:
                if files:
                    for entry in files.values():entry[1].seek(0)
                kw={'data':{k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list,bool)) else str(v)
                            for k,v in (payload or {}).items()},'files':files} if files else {'json':payload or {}}
                r=await self.client.post(f'https://api.telegram.org/bot{self.token}/{method}',timeout=65,**kw)
                data=r.json()
            except (httpx.HTTPError,ValueError):
                raise UserError('Не удалось связаться с Telegram. Попробуйте ещё раз.') from None
            if data.get('ok'):return data['result']
            if data.get('error_code')==429 and attempt<2:
                await asyncio.sleep(min(data.get('parameters',{}).get('retry_after',2),45));continue
            raise UserError(f'Telegram не выполнил действие (код {data.get("error_code",r.status_code)}). Проверьте права бота в чате.')
        raise UserError('Telegram временно ограничил отправку. Попробуйте позже.')

    async def say(self,chat,text,topic=0,reply=None):
        payload={'chat_id':chat,'text':text[:4000],'link_preview_options':{'is_disabled':True}}
        if topic:payload['message_thread_id']=topic
        if reply:payload['reply_parameters']={'message_id':reply,'allow_sending_without_reply':True}
        return await self.call('sendMessage',payload)

    async def album(self,chat,paths,topic=0,caption=''):
        result=[]
        # One image cannot be sent as a media group.
        for offset in range(0,len(paths),10):
            chunk=paths[offset:offset+10]
            with ExitStack() as stack:
                files={f'p{i}':(p.name,stack.enter_context(p.open('rb')),'image/png') for i,p in enumerate(chunk)}
                payload={'chat_id':chat}
                if topic:payload['message_thread_id']=topic
                if len(chunk)==1:
                    payload['caption']=caption if offset==0 else ''
                    result.append(await self.call('sendPhoto',payload,{'photo':files['p0']}))
                else:
                    payload['media']=[{'type':'photo','media':f'attach://p{i}',
                                       **({'caption':caption} if i==0 and offset==0 else {})} for i in range(len(chunk))]
                    result.extend(await self.call('sendMediaGroup',payload,files))
        return result

    async def document(self,chat,path,topic=0,caption=''):
        payload={'chat_id':chat,'caption':caption}
        if topic:payload['message_thread_id']=topic
        with path.open('rb') as f:
            return await self.call('sendDocument',payload,{'document':(path.name,f,'application/zip')})

    async def download(self,file_id,dest:Path,max_bytes=12_000_000):
        info=await self.call('getFile',{'file_id':file_id})
        if info.get('file_size',0)>max_bytes:raise UserError('Файл слишком большой. Максимум 12 МБ.')
        path=info['file_path']
        if path.startswith('/') or '..' in path.split('/'):raise UserError('Недопустимый файл.')
        try:
            async with self.client.stream('GET',f'https://api.telegram.org/file/bot{self.token}/'+quote(path,safe='/'),timeout=40) as r:
                if r.status_code!=200:raise UserError('Telegram не отдал файл.')
                data=bytearray()
                async for part in r.aiter_bytes():
                    data.extend(part)
                    if len(data)>max_bytes:raise UserError('Файл слишком большой. Максимум 12 МБ.')
                dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data)
        except httpx.HTTPError:raise UserError('Не удалось скачать вложение из Telegram.') from None

