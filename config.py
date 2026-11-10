"""Configuration is read once; credentials never enter API responses."""
from dataclasses import dataclass, field
from pathlib import Path
import os
import secrets
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parent

def flag(name, default=False):
    return os.getenv(name, str(default)).lower() in ('true', '1', 'yes')

@dataclass
class Settings:
    production: bool = False
    demo: bool = False
    secret: str = field(default_factory=lambda: secrets.token_urlsafe(48))
    
    # Авторизація до Адмін-панелі
    admin_user: str = 'admin'
    admin_password: str = ''
    admin_token: str = ''
    
    public_origin: str = 'http://127.0.0.1:8000'
    parent_origins: tuple = ('https://abvhimiya.com.ua', 'https://www.abvhimiya.com.ua')
    db_path: str = ''
    
    catalog_file: str = ''
    catalog_url: str = ''
    catalog_hosts: tuple = ('abvhimiya.com.ua', 'www.abvhimiya.com.ua')
    product_hosts: tuple = ('abvhimiya.com.ua', 'www.abvhimiya.com.ua', 'sunlu.kyiv.ua', 'www.sunlu.kyiv.ua')
    catalog_max_age: int = 3600
    catalog_refresh: int = 900
    
    # Актуальна Google Gemini модель
    gemini_key: str = ''
    gemini_model: str = 'gemini-3.8-flash'
    daily_ai_calls: int = 1500
    
    telegram_token: str = ''
    telegram_chat: str = ''
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
            if len(self.secret) < 32:
                raise ValueError('APP_SECRET must contain at least 32 characters')
            if not self.public_origin.startswith('https://'):
                raise ValueError('PUBLIC_ORIGIN must use HTTPS in production')
            if not self.catalog_url and not self.catalog_file:
                raise ValueError('Configure CATALOG_URL or CATALOG_FILE')

        # Забезпечуємо існування директорії для бази даних
        if self.db_path:
            db_dir = Path(self.db_path).parent
            db_dir.mkdir(parents=True, exist_ok=True)
            
        return self

    @classmethod
    def from_env(cls):
        production = os.getenv('APP_ENV', 'development') == 'production'
        
        def csv(key, default): 
            return tuple(x.strip().rstrip('/') for x in os.getenv(key, default).split(',') if x.strip())
        
        cat_file = os.getenv('CATALOG_FILE', '')
        if cat_file and not Path(cat_file).is_absolute():
            cat_file = str(BASE / cat_file)

        # Визначаємо шлях до БД і створюємо папку (важливо для Render Persistent Disks)
        raw_db_path = os.getenv('DATABASE_PATH')
        if not raw_db_path:
            raw_db_path = str((BASE / 'data' / 'agent.sqlite3').resolve())
        elif not Path(raw_db_path).is_absolute():
            raw_db_path = str((BASE / raw_db_path).resolve())

        return cls(
            production=production,
            demo=flag('DEMO_MODE', False),
            secret=os.getenv('APP_SECRET', '') if production else (os.getenv('APP_SECRET') or secrets.token_urlsafe(48)),
            
            admin_user=os.getenv('ADMIN_USER', 'admin'),
            admin_password=os.getenv('ADMIN_PASSWORD', 'abvhimiya2026'), # Пароль для Basic Auth
            admin_token=os.getenv('ADMIN_TOKEN', ''),
            
            public_origin=os.getenv('PUBLIC_ORIGIN', 'https://abvhimiya-ai.onrender.com').rstrip('/'),
            parent_origins=csv('PARENT_ORIGINS', 'https://abvhimiya.com.ua,https://www.abvhimiya.com.ua'),
            db_path=raw_db_path,
            
            catalog_file=cat_file,
            catalog_url=os.getenv('CATALOG_URL', '') or os.getenv('HOROSHOP_FEED_URL', ''),
            catalog_hosts=csv('CATALOG_ALLOWED_HOSTS', 'abvhimiya.com.ua,www.abvhimiya.com.ua'),
            product_hosts=csv('PRODUCT_ALLOWED_HOSTS', 'abvhimiya.com.ua,www.abvhimiya.com.ua,sunlu.kyiv.ua,www.sunlu.kyiv.ua'),
            catalog_max_age=int(os.getenv('CATALOG_MAX_AGE_SECONDS', '3600')),
            catalog_refresh=int(os.getenv('CATALOG_REFRESH_SECONDS', '900')),
            
            gemini_key=os.getenv('GEMINI_API_KEY', ''),
            gemini_model=os.getenv('GEMINI_MODEL', 'gemini-3.8-flash'), # Змінено на актуальну модель
            daily_ai_calls=int(os.getenv('DAILY_AI_CALLS', '1500')),
            
            telegram_token=os.getenv('TELEGRAM_BOT_TOKEN', ''),
            telegram_chat=os.getenv('TELEGRAM_CHAT_ID', ''),
            privacy_url=os.getenv('PRIVACY_URL', '/privacy'),
            consent_version=os.getenv('CONSENT_VERSION', '2026-11-09'),
            history_days=int(os.getenv('HISTORY_RETENTION_DAYS', '30')),
            lead_days=int(os.getenv('LEAD_RETENTION_DAYS', '180')),
            
            salesdrive_domain=os.getenv('SALESDRIVE_DOMAIN', '').replace('https://', '').replace('http://', '').strip().rstrip('/'),
            salesdrive_key=os.getenv('SALESDRIVE_API_KEY', '').strip()
        ).validate()
