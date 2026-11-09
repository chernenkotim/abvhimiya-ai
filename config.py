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
    demo: bool = True
    secret: str = field(default_factory=lambda: secrets.token_urlsafe(48))
    admin_token: str = ''
    public_origin: str = 'http://127.0.0.1:8000'
    parent_origins: tuple = ('https://abvhimiya.com.ua', 'https://www.abvhimiya.com.ua')
    db_path: str = str(BASE / 'data' / 'agent.sqlite3')
    catalog_file: str = str(BASE / 'demo_catalog.json')
    catalog_url: str = ''
    catalog_hosts: tuple = ('abvhimiya.com.ua', 'www.abvhimiya.com.ua')
    product_hosts: tuple = ('abvhimiya.com.ua', 'www.abvhimiya.com.ua')
    catalog_max_age: int = 3600
    catalog_refresh: int = 900
    gemini_key: str = ''
    gemini_model: str = ''
    telegram_token: str = ''
    telegram_chat: str = ''
    daily_ai_calls: int = 1000
    privacy_url: str = ''
    consent_version: str = '2026-11-09'
    history_days: int = 30
    lead_days: int = 180

    def validate(self):
        for value in (self.public_origin, *self.parent_origins):
            p = urlsplit(value)
            if p.scheme not in ('http', 'https') or not p.netloc or p.path or p.query or p.fragment or p.username:
                raise ValueError('Origins must contain only scheme and hostname (no trailing slash)')
        if self.production:
            if len(self.secret) < 32 or len(self.admin_token) < 32:
                raise ValueError('APP_SECRET and ADMIN_TOKEN must contain at least 32 characters')
            if not self.public_origin.startswith('https://'):
                raise ValueError('PUBLIC_ORIGIN must use HTTPS in production')
            if self.demo:
                raise ValueError('Disable DEMO_MODE before production')
            if not self.catalog_url and Path(self.catalog_file).name == 'demo_catalog.json':
                raise ValueError('Replace the demonstration catalog before production')
            if not self.privacy_url.startswith('https://'):
                raise ValueError('Set PRIVACY_URL to your actual privacy policy')
        if self.gemini_key and not self.gemini_model:
            raise ValueError('Set GEMINI_MODEL to a model available in your Google account')
        if self.catalog_max_age < 60 or self.catalog_refresh < 10:
            raise ValueError('Catalog intervals are too short')
        return self

    @classmethod
    def from_env(cls):
        production = os.getenv('APP_ENV', 'development') == 'production'
        csv = lambda key, default: tuple(x.strip().rstrip('/') for x in os.getenv(key, default).split(',') if x.strip())
        path = lambda key, default: str((BASE / os.getenv(key, default)).resolve())
        return cls(
            production=production, demo=flag('DEMO_MODE', True),
            secret=os.getenv('APP_SECRET', '') if production else (os.getenv('APP_SECRET') or secrets.token_urlsafe(48)),
            admin_token=os.getenv('ADMIN_TOKEN', ''),
            public_origin=os.getenv('PUBLIC_ORIGIN', 'http://127.0.0.1:8000').rstrip('/'),
            parent_origins=csv('PARENT_ORIGINS', 'https://abvhimiya.com.ua,https://www.abvhimiya.com.ua'),
            db_path=path('DATABASE_PATH', 'data/agent.sqlite3'),
            catalog_file=path('CATALOG_FILE', 'demo_catalog.json') if os.getenv('CATALOG_FILE', 'demo_catalog.json') else '',
            catalog_url=os.getenv('CATALOG_URL', ''),
            catalog_hosts=csv('CATALOG_ALLOWED_HOSTS', 'abvhimiya.com.ua,www.abvhimiya.com.ua'),
            product_hosts=csv('PRODUCT_ALLOWED_HOSTS', 'abvhimiya.com.ua,www.abvhimiya.com.ua'),
            catalog_max_age=int(os.getenv('CATALOG_MAX_AGE_SECONDS', '3600')),
            catalog_refresh=int(os.getenv('CATALOG_REFRESH_SECONDS', '900')),
            gemini_key=os.getenv('GEMINI_API_KEY', ''), gemini_model=os.getenv('GEMINI_MODEL', ''),
            telegram_token=os.getenv('TELEGRAM_BOT_TOKEN', ''), telegram_chat=os.getenv('TELEGRAM_CHAT_ID', ''),
            daily_ai_calls=int(os.getenv('DAILY_AI_CALLS', '1000')),
            privacy_url=os.getenv('PRIVACY_URL', ''), consent_version=os.getenv('CONSENT_VERSION', '2026-11-09'),
            history_days=int(os.getenv('HISTORY_RETENTION_DAYS', '30')), lead_days=int(os.getenv('LEAD_RETENTION_DAYS', '180')),
        ).validate()
