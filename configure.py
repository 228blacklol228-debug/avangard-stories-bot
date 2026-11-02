"""Interactive local/server setup. Secrets are never printed or sent to a chat."""
from getpass import getpass
from pathlib import Path
import os
import re

path=Path(__file__).parent/'.env'
if path.exists():
    raise SystemExit('Файл .env уже есть. Отредактируйте его вручную; существующие ключи не заменены.')
token=getpass('Токен Telegram от BotFather (ввод скрыт): ').strip()
if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}',token):raise SystemExit('Формат Telegram-токена не распознан.')
key=getpass('Ключ OpenAI API (можно оставить пустым и добавить позже): ').strip()
owner=input('Ваш числовой Telegram ID (или 0, чтобы узнать его командой /id): ').strip() or '0'
if not owner.isdigit():raise SystemExit('Telegram ID должен быть числом.')
if any(c in key for c in '\r\n'):raise SystemExit('Недопустимый формат ключа.')
text=(f'TELEGRAM_BOT_TOKEN={token}\nOPENAI_API_KEY={key}\nOPENAI_MODEL=gpt-5-mini\n'
      f'OWNER_TELEGRAM_ID={owner}\nSOURCE_URL=https://old.avgst.ru\n'
      'ALLOWED_SOURCE_HOSTS=old.avgst.ru,avgst.ru\nMAX_DAILY_JOBS_PER_CHAT=30\n')
fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
with os.fdopen(fd,'w',encoding='utf-8') as f:f.write(text)
print('Настройки сохранены в .env. Значения ключей не выводятся.')
