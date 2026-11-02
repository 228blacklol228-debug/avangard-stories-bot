from dataclasses import dataclass
from pathlib import Path
import os
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

@dataclass(frozen=True)
class Config:
    token: str
    api_key: str
    model: str
    owner: int
    source: str
    hosts: frozenset[str]
    data: Path
    daily_limit: int = 30
    max_assets: int = 16

    @classmethod
    def load(cls):
        load_dotenv(ROOT / '.env')
        source = os.getenv('SOURCE_URL', 'https://old.avgst.ru').rstrip('/')
        return cls(os.getenv('TELEGRAM_BOT_TOKEN', ''), os.getenv('OPENAI_API_KEY', ''),
                   os.getenv('OPENAI_MODEL', 'gpt-5-mini'), int(os.getenv('OWNER_TELEGRAM_ID', '0')),
                   source, frozenset(x.strip().lower() for x in os.getenv(
                       'ALLOWED_SOURCE_HOSTS', 'old.avgst.ru,avgst.ru').split(',') if x.strip()),
                   Path(os.getenv('DATA_DIR', str(ROOT / 'data'))).resolve(),
                   int(os.getenv('MAX_DAILY_JOBS_PER_CHAT', '30')),
                   int(os.getenv('MAX_ASSETS_PER_PROJECT', '16')))

class UserError(Exception):
    """A short, safe message that can be shown in Telegram."""
