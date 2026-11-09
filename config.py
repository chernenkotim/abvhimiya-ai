"""Configuration is read once; credentials never enter API responses."""
from dataclasses import dataclass, field
from pathlib import Path
import os
import secrets
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parent

def flag(name, default=False):
    return os.getenv(name, str(default)).lower() == 'true'

@dataclass
class Settings:
    production: bool = False
    demo: bool = False
    secret: str = field(default_factory=lambda: secrets.token_urlsafe(48))
    admin_token: str = ''
    public_origin: str = 'http://127.0.0.1:8000'
    parent_origins: tuple = ('https://abvhimiya.com.ua', 'https://www.abvhimiya.com.ua')
    db_path: str = str(BASE / 'data' / 'agent.sqlite3')
    catalog_file: str = ''
    catalog_url: str = ''
    catalog_hosts: tuple = ('abvhimiya.com.ua', 'www.abvhimiya.com.ua')
    product_hosts: tuple = ('abvhimiya.com.ua', 'www.abvhimiya.com.ua')
    catalog_max_age: int = 3600
    catalog_refresh: int = 900
    gemini_key: str = ''
    gemini_model: str = 'gemini-3.5-flash-lite'
    telegram_token: str = ''
    telegram_chat: str = ''
    daily_ai_calls: int = 1000
    privacy_url: str = ''
    consent_version: str = '2026-11-09'
    history_days: int = 30
    lead_days: int = 180
    salesdrive_domain: str = ''
    salesdrive_key: str = ''

    def validate(self):
        for value in (self.public_origin, *self.parent_origins):
            p = urlsplit(value)
            if p.scheme not in ('http', 'https') or not p.netloc or p.path or p.query or p.fragment or p.username:
                raise ValueError(f'Invalid origin format: {value}')
        if self.production:
            if len(self.secret) < 32 or len(self.admin_token) < 32:
                raise ValueError('APP_SECRET and ADMIN_TOKEN must contain at least 32 characters')
            if not self.public_origin.startswith('https://'):
                raise ValueError('PUBLIC_ORIGIN must use HTTPS in production')
            if not self.catalog_url and not self.catalog_file:
                raise ValueError('Configure CATALOG_URL or CATALOG_FILE')
        return self

    @classmethod
    def from_env(cls):
        production = os.getenv('APP_ENV', 'development') == 'production'
        csv = lambda key, default: tuple(x.strip().rstrip('/') for x in os.getenv(key, default).split(',') if x.strip())
        path = lambda key, default: str((BASE / os.getenv(key, default)).resolve()) if os.getenv(key) else ''
        
        cat_file = os.getenv('CATALOG_FILE', '')
        if cat_file and not Path(cat_file).is_absolute():
            cat_file = str(BASE / cat_file)

        return cls(
            production=production,
            demo=flag('DEMO_MODE', False),
            secret=os.getenv('APP_SECRET', '') if production else (os.getenv('APP_SECRET') or secrets.token_urlsafe(48)),
            admin_token=os.getenv('ADMIN_TOKEN', ''),
            public_origin=os.getenv('PUBLIC_ORIGIN', 'https://abvhimiya-ai.onrender.com').rstrip('/'),
            parent_origins=csv('PARENT_ORIGINS', 'https://abvhimiya.com.ua,https://www.abvhimiya.com.ua'),
            db_path=str((BASE / 'data' / 'agent.sqlite3').resolve()) if not os.getenv('DATABASE_PATH') else os.getenv('DATABASE_PATH'),
            catalog_file=cat_file,
            catalog_url=os.getenv('CATALOG_URL', '') or os.getenv('HOROSHOP_FEED_URL', ''),
            catalog_hosts=csv('CATALOG_ALLOWED_HOSTS', 'abvhimiya.com.ua,www.abvhimiya.com.ua'),
            product_hosts=csv('PRODUCT_ALLOWED_HOSTS', 'abvhimiya.com.ua,www.abvhimiya.com.ua'),
            catalog_max_age=int(os.getenv('CATALOG_MAX_AGE_SECONDS', '3600')),
            catalog_refresh=int(os.getenv('CATALOG_REFRESH_SECONDS', '900')),
            gemini_key=os.getenv('GEMINI_API_KEY', ''),
            gemini_model=os.getenv('GEMINI_MODEL', 'gemini-3.5-flash-lite'),
            telegram_token=os.getenv('TELEGRAM_BOT_TOKEN', ''),
            telegram_chat=os.getenv('TELEGRAM_CHAT_ID', ''),
            daily_ai_calls=int(os.getenv('DAILY_AI_CALLS', '1000')),
            privacy_url=os.getenv('PRIVACY_URL', '/privacy'),
            consent_version=os.getenv('CONSENT_VERSION', '2026-11-09'),
            history_days=int(os.getenv('HISTORY_RETENTION_DAYS', '30')),
            lead_days=int(os.getenv('LEAD_RETENTION_DAYS', '180')),
            salesdrive_domain=os.getenv('SALESDRIVE_DOMAIN', '').replace('https://', '').replace('http://', '').strip().rstrip('/'),
            salesdrive_key=os.getenv('SALESDRIVE_API_KEY', '').strip()
        ).validate()
