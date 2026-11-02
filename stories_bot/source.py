"""Read only explicitly scoped project sections; never scrape shared recommendation galleries."""
import asyncio
import hashlib
import ipaddress
import json
import re
import socket
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
import httpx
from bs4 import BeautifulSoup
from PIL import Image
from .config import UserError

CDNS = frozenset({'static.tildacdn.com', 'static.tildacdn.net', 'static.tildacdn.pub', 'thb.tildacdn.com'})
Image.MAX_IMAGE_PIXELS = 30_000_000

def public_url(url, hosts):
    p = urlsplit(url)
    if p.scheme != 'https' or p.hostname not in hosts or p.username or p.password or p.port not in (None,443):
        raise UserError('Адрес не входит в разрешённые источники. Укажите страницу проекта на сайте компании.')
    return url

async def check_address(url, hosts):
    public_url(url, hosts)
    host = urlsplit(url).hostname
    try:
        addresses = await asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise UserError('Не удалось подключиться к сайту. Попробуйте позже.') from None
    if any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise UserError('Этот адрес источника недоступен.')

async def fetch(client, url, hosts, max_bytes=5_000_000):
    for _ in range(5):
        await check_address(url, hosts)
        try:
            async with client.stream('GET',url,timeout=30,follow_redirects=False) as r:
                if r.is_redirect:
                    url=urljoin(url,r.headers['location']);continue
                if r.status_code != 200:
                    raise UserError(f'Сайт не отдал материал (код {r.status_code}).')
                data=bytearray()
                async for part in r.aiter_bytes():
                    data.extend(part)
                    if len(data)>max_bytes:raise UserError('Материал слишком большой для загрузки.')
                return bytes(data),url
        except httpx.HTTPError:
            raise UserError('Не удалось загрузить материал с сайта. Попробуйте позже.') from None
    raise UserError('Сайт перенаправляет запрос слишком много раз.')

def normal(text): return re.sub(r'\s+', ' ', text).strip()

def extract_project(html: str, url: str):
    soup=BeautifulSoup(html,'html.parser')
    heading=soup.select_one('.uc-projectname h1')
    if heading is None:
        raise UserError('Структура этой страницы пока не поддерживается. Нужна карточка проекта старого сайта; новый сайт подключается отдельным адаптером.')
    title=normal(heading.get_text(' ',strip=True))
    heading_rec=heading.find_parent(attrs={'data-record-type':True})
    photos=[]; stats=[]
    for rec in heading_rec.find_next_siblings(attrs={'data-record-type':True}):
        if 'uc-plans' in rec.get('class',[]):break
        stats.append(normal(rec.get_text(' ',strip=True)))
        for e in rec.select('[data-field-imgs-value]'):
            try: photos.extend(x['li_img'] for x in json.loads(e['data-field-imgs-value']) if x.get('li_img'))
            except (ValueError,TypeError,KeyError):pass
    plans=[]
    for area in soup.select('.uc-plans'):
        for e in area.select('[data-img-zoom-url], [data-original]'):
            candidate=e.get('data-img-zoom-url') or e.get('data-original')
            if candidate and not candidate.endswith('.svg'):plans.append(candidate)
    descriptions=[]
    for rec in soup.select('[data-record-type]'):
        t=normal(rec.get_text(' ',strip=True))
        if t.startswith(('«'+title+'»', title+' —')) and len(t)>100:
            descriptions.append(t)
    description=max(descriptions,key=len,default='')
    if not photos:raise UserError('В карточке не найдены оригинальные изображения проекта.')
    assets=[]
    for kind,urls in [('photo',photos),('plan',plans)]:
        for i,u in enumerate(dict.fromkeys(urls),1):
            assets.append({'id':f'{kind}-{i:02}', 'kind':kind,'url':urljoin(url,u)})
    return {'title':title,'url':url,'text':normal(' '.join(stats)+' '+description)[:18000],
            'assets':assets,'retrieved_at':datetime.now(timezone.utc).isoformat()}

class Source:
    def __init__(self,client: httpx.AsyncClient,hosts,root:Path,max_assets=16):
        self.client=client;self.hosts=hosts;self.root=root;self.max_assets=max_assets
        self._indexes={}

    async def index(self,base):
        base=base.rstrip('/')
        if base in self._indexes:return self._indexes[base]
        raw,_=await fetch(self.client,base+'/sitemap.xml',self.hosts)
        def urls_from(data):
            tree=ET.fromstring(data)
            return [e.text for e in tree.iter() if e.tag.rsplit('}',1)[-1]=='loc' and e.text]
        try: urls=urls_from(raw)
        except ET.ParseError:raise UserError('Не удалось прочитать каталог сайта.') from None
        for child in list(urls):
            if child.endswith('.xml') and len(urls)<10000:
                data,_=await fetch(self.client,child,self.hosts)
                try:urls.extend(urls_from(data))
                except ET.ParseError:pass
        projects=sorted(set(u for u in urls if '/catalog/' in u and '/product/' in u))
        if not projects:raise UserError('Каталог изменился: нужно обновить подключение сайта.')
        self._indexes[base]=projects
        return projects

    async def collect(self,url):
        raw,effective=await fetch(self.client,url,self.hosts)
        project=extract_project(raw.decode('utf-8'),effective)
        photos=[a for a in project['assets'] if a['kind']=='photo'][:self.max_assets]
        plans=[a for a in project['assets'] if a['kind']=='plan'][:4]
        project['assets']=photos+plans
        key=hashlib.sha256(effective.encode()).hexdigest()[:16]
        folder=self.root/key;folder.mkdir(parents=True,exist_ok=True)
        (folder/'source.html').write_bytes(raw)
        sem=asyncio.Semaphore(4)
        async def asset(a):
            async with sem:
                allowed=self.hosts|CDNS
                candidates=[a['url']]
                if urlsplit(a['url']).hostname=='static.tildacdn.com':
                    p=urlsplit(a['url']);candidates.insert(0,urlunsplit(p._replace(netloc='static.tildacdn.net')))
                last=None
                for candidate in candidates:
                    try:
                        data,download_url=await fetch(self.client,candidate,allowed,12_000_000)
                        with Image.open(BytesIO(data)) as im:
                            im.verify()
                        with Image.open(BytesIO(data)) as im:
                            if im.width<300 or im.height<200:raise ValueError('Too small')
                            a['width'],a['height']=im.size
                            ext={'JPEG':'.jpg','PNG':'.png','WEBP':'.webp'}.get(im.format)
                            if not ext:raise ValueError('Unsupported image')
                        # Content-addressed originals keep older series immutable when the site changes.
                        digest=hashlib.sha256(data).hexdigest()
                        path=folder/(a['id']+'-'+digest[:12]+ext)
                        path.write_bytes(data)
                        a.update(path=str(path.resolve()),sha256=digest,download_url=download_url)
                        return a
                    except (UserError,ValueError,OSError) as e:last=e
                raise UserError('Не удалось получить оригинальные изображения. Ничего не заменяю изображениями другого дома.') from last
        project['assets']=await asyncio.gather(*(asset(a) for a in project['assets']))
        (folder/'project.json').write_text(json.dumps(project,ensure_ascii=False,indent=2),encoding='utf-8')
        return project
