"""Durable state and immutable files for a single-worker, low-volume free deployment.

SQLite is a local working copy only. Supabase owns the inbox, lease and checkpoint.
An RPC commits the checkpoint and acknowledges an update in one transaction.
"""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import quote, urlsplit
import httpx
from .config import ROOT,UserError

class Cloud:
    def __init__(self,client,url,key,bot,bucket,root):
        self.client=client;self.url=url.rstrip('/');self.key=key;self.bot=bot
        self.bucket=bucket;self.root=Path(root).resolve()
        p=urlsplit(self.url)
        if p.scheme!='https' or not p.hostname or not p.hostname.endswith('.supabase.co'):
            raise UserError('Укажите HTTPS-адрес проекта Supabase вида https://…supabase.co.')

    @property
    def headers(self):return {'apikey':self.key,'Authorization':'Bearer '+self.key}

    async def rpc(self,name,**params):
        try:
            r=await self.client.post(self.url+'/rest/v1/rpc/'+name,
                headers=self.headers,json=params,timeout=30)
        except httpx.HTTPError:raise UserError('Хранилище временно недоступно. Команда останется в очереди.') from None
        if r.status_code>=300:
            raise UserError(f'Хранилище недоступно (код {r.status_code}). Проверьте Supabase и установку таблиц.')
        return r.json() if r.content else None

    def object_key(self,path):
        path=Path(path).resolve()
        try:relative=path.relative_to(self.root)
        except ValueError:raise UserError('Файл находится вне папки данных бота.') from None
        return quote(self.bot+'/'+relative.as_posix(),safe='/')

    async def upload(self,path):
        path=Path(path)
        url=self.url+'/storage/v1/object/'+quote(self.bucket,safe='')+'/'+self.object_key(path)
        try:
            r=await self.client.post(url,headers={**self.headers,'Content-Type':'application/octet-stream','x-upsert':'true'},
                                     content=path.read_bytes(),timeout=60)
        except httpx.HTTPError:raise UserError('Не удалось сохранить исходник в постоянное хранилище.') from None
        if r.status_code>=300:raise UserError(f'Не удалось сохранить исходник (код {r.status_code}). Проверьте место в Supabase.')

    async def ensure(self,path,digest=None):
        path=Path(path).resolve()
        if path.is_relative_to((ROOT/'assets').resolve()):
            if not path.is_file():raise UserError('В пакете отсутствует файл оформления.')
            return
        key=self.object_key(path)
        if path.is_file() and (not digest or hashlib.sha256(path.read_bytes()).hexdigest()==digest):return
        try:
            async with self.client.stream('GET',self.url+'/storage/v1/object/authenticated/'+quote(self.bucket,safe='')+'/'+key,
                                          headers=self.headers,timeout=60) as r:
                if r.status_code!=200:raise UserError('Исходник недоступен в постоянном хранилище. Проверьте Supabase.')
                data=bytearray()
                async for chunk in r.aiter_bytes():
                    data.extend(chunk)
                    if len(data)>12_000_000:raise UserError('Сохранённый исходник превышает допустимый размер.')
        except httpx.HTTPError:raise UserError('Не удалось восстановить сохранённый исходник.') from None
        if digest and hashlib.sha256(data).hexdigest()!=digest:raise UserError('Контрольная сумма сохранённого исходника не совпадает.')
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)

    async def save_project(self,project):
        for a in project['assets']:await self.upload(a['path'])

    async def restore(self,project=None,brand=None):
        if project:
            for a in project['assets']:await self.ensure(a['path'],a.get('sha256'))
        if brand:
            for field in ('logo','heading_font','body_font'):
                if brand.get(field):await self.ensure(brand[field])
            for field in ('references','negative_references'):
                for path in brand.get(field,[]):await self.ensure(path)

def snapshot(store):
    # backup() + DELETE journal mode makes a standalone DB, never a WAL-only copy.
    temp=store.db.execute('PRAGMA database_list').fetchone()[2]+'.checkpoint'
    dest=sqlite3.connect(temp)
    store.db.backup(dest);dest.execute('PRAGMA journal_mode=DELETE');dest.close()
    path=Path(temp)
    try:
        data=path.read_bytes()
        if len(data)>20_000_000:raise UserError('История бота превысила лимит бесплатного режима. Нужна архивация данных.')
        return base64.b64encode(data).decode()
    finally:path.unlink(missing_ok=True)

def restore_snapshot(encoded,path):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    if encoded:
        data=base64.b64decode(encoded,validate=True)
        if len(data)>20_000_000 or not data.startswith(b'SQLite format 3\x00'):
            raise UserError('Сохранённое состояние бота повреждено.')
        path.write_bytes(data)
