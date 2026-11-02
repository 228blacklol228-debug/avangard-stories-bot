"""Read-only live check of the website adapter; does not call Telegram or the model."""
import asyncio
import httpx
from stories_bot.config import Config
from stories_bot.source import Source

async def main():
    cfg=Config.load()
    async with httpx.AsyncClient(headers={'User-Agent':'AvgStoriesBot/1.0'}) as client:
        source=Source(client,cfg.hosts,cfg.data/'source-check',cfg.max_assets)
        urls=await source.index(cfg.source)
        target=next(u for u in urls if u.endswith('/scandi-78'))
        project=await source.collect(target)
        print('Project pages:',len(urls))
        print('Original photos:',sum(a['kind']=='photo' for a in project['assets']))
        print('Original plans:',sum(a['kind']=='plan' for a in project['assets']))
        print('Source check passed')

if __name__=='__main__':asyncio.run(main())
