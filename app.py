"""ABV chat consultant: catalog consultation, durable manager requests, and instant order tracking."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import logging
from pathlib import Path
import secrets
import time
from uuid import UUID
import weakref

from dotenv import load_dotenv
from fastapi import FastAPI, Depends, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel, Field, field_validator

from assistant import Assistant
from catalog import Catalog
from config import BASE, Settings
from privacy import normalize_phone, redact
from storage import Conflict, Storage

load_dotenv(BASE / '.env')
logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s %(message)s')
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
log = logging.getLogger('abv')

class SessionRequest(BaseModel):
    session_id: str = Field(min_length=40, max_length=160)

class ChatRequest(SessionRequest):
    message: str = Field(min_length=1, max_length=2000)

class LeadRequest(SessionRequest):
    request_id: UUID
    name: str = Field(default='', max_length=100)
    phone: str = Field(max_length=32)
    note: str = Field(default='', max_length=1000)
    product_id: str | None = Field(default=None, max_length=120)
    consent: bool = False

    @field_validator('phone')
    @classmethod
    def phone_valid(cls, value): 
        return normalize_phone(value)

class LeadState(BaseModel):
    state: str = Field(pattern=r'^(new|in_progress|done)$')

class BodyLimit:
    def __init__(self, app): 
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in ('POST', 'PATCH', 'PUT'):
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect': 
                return
            body.extend(message.get('body', b''))
            if len(body) > 32768:
                return await JSONResponse({'detail': 'Запит завеликий'}, status_code=413)(scope, receive, send)
            if not message.get('more_body', False): 
                break
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()
        await self.app(scope, replay, send)

def create_app(settings=None):
    settings = (settings or Settings.from_env()).validate()
    store, catalog = Storage(settings.db_path), Catalog(settings)
    assistant = Assistant(settings, store, catalog)
    turn_locks = weakref.WeakValueDictionary()

    async def catalog_loop():
        while True:
            await asyncio.sleep(settings.catalog_refresh)
            await catalog.refresh()

    async def delivery_loop():
        while True:
            try:
                if settings.telegram_token and settings.telegram_chat and not settings.demo:
                    for _ in range(10):
                        lead_item = store.claim_notification()
                        if not lead_item: 
                            break
                        ok, error = await send_telegram(settings, lead_item)
                        store.finish_notification(lead_item, ok, error)
            except asyncio.CancelledError: 
                raise
            except Exception: 
                log.error('Notification worker failed; requests remain in database')
            await asyncio.sleep(5)

    async def cleanup_loop():
        while True:
            store.cleanup(settings.history_days, settings.lead_days)
            await asyncio.sleep(3600)

    @asynccontextmanager
    async def lifespan(app):
        catalog.restore()
        await catalog.refresh()
        tasks = [asyncio.create_task(job()) for job in (catalog_loop, delivery_loop, cleanup_loop)]
        try: 
            yield
        finally:
            for task in tasks: 
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            store.close()

    app = FastAPI(
        title='ABV Хімія — чат-консультант',
        version='3.0.0',
        lifespan=lifespan,
        docs_url=None if settings.production else '/docs',
        redoc_url=None
    )
    app.state.settings, app.state.store, app.state.catalog, app.state.assistant = settings, store, catalog, assistant
    
    origins = list(dict.fromkeys((settings.public_origin, *settings.parent_origins)))
    app.add_middleware(BodyLimit)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=['GET', 'POST', 'PATCH'],
        allow_headers=['Content-Type', 'Authorization']
    )
    app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')

    @app.middleware('http')
    async def security(request: Request, call_next):
        origin = request.headers.get('origin', '')
        if request.method in ('POST', 'PATCH', 'PUT', 'DELETE') and origin and origin not in origins:
            return JSONResponse({'detail': 'Заборонене джерело запиту'}, status_code=403)
        response = await call_next(request)
        widget = request.url.path in ('/widget', '/')
        ancestors = ' '.join(("'self'", *settings.parent_origins)) if widget else "'none'"
        response.headers.update({
            'Content-Security-Policy': f"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' https: data:; connect-src 'self'; frame-ancestors {ancestors}; base-uri 'none'; object-src 'none'",
            'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer',
            'Permissions-Policy': 'camera=(), microphone=(), geolocation=()'
        })
        if not widget: 
            response.headers['X-Frame-Options'] = 'DENY'
        if request.url.path.startswith(('/api', '/admin')) or widget: 
            response.headers['Cache-Control'] = 'no-store'
        return response

    def ip_key(request):
        return request.client.host if request.client else 'unknown'

    def limit(request, kind, session_val=None):
        rules = [
            ('ip:' + ip_key(request), 90, 60),
            ('ip:' + kind + ':' + ip_key(request), {'session': 15, 'chat': 30, 'lead': 8, 'admin': 30}.get(kind, 30), 60)
        ]
        if session_val:
            rules.append(('session:' + kind + ':' + session_val, {'chat': 25, 'lead': 6}.get(kind, 30), 60))
        if not store.allow(rules): 
            raise HTTPException(429, 'Забагато запитів. Спробуйте за хвилину.', headers={'Retry-After': '60'})

    def mint_session():
        import hmac
        value = f'{secrets.token_hex(16)}.{int(time.time()) + 86400}'
        signature = hmac.new(settings.secret.encode(), value.encode(), 'sha256').hexdigest()
        return value + '.' + signature

    def validate(token):
        import hmac
        try:
            uid, expiry, signature = token.split('.')
            expected = hmac.new(settings.secret.encode(), f'{uid}.{expiry}'.encode(), 'sha256').hexdigest()
            if len(uid) != 32 or not hmac.compare_digest(signature, expected): 
                raise ValueError
            if int(expiry) < time.time(): 
                raise ValueError
            return uid
        except (ValueError, TypeError): 
            raise HTTPException(403, 'Сесія недійсна або завершилася', headers={'X-Session-Expired': '1'})

    async def admin_auth(request: Request, authorization: str = Header(default='')):
        import hmac
        limit(request, 'admin')
        if not settings.admin_token or not hmac.compare_digest(authorization, 'Bearer ' + settings.admin_token):
            raise HTTPException(401, 'Потрібна авторизація', headers={'WWW-Authenticate': 'Bearer'})

    @app.get('/')
    @app.get('/widget')
    async def widget_page(): 
        return FileResponse(BASE / 'static' / 'widget.html')

    @app.get('/admin')
    async def admin_view(): 
        return FileResponse(BASE / 'static' / 'admin.html')

    @app.get('/privacy')
    async def privacy_view(): 
        return FileResponse(BASE / 'static' / 'privacy.html')

    @app.get('/health')
    @app.get('/health/live')
    async def health_live(): 
        return {'status': 'ok', 'catalog_ready': catalog.fresh, 'catalog_count': len(catalog.products)}

    @app.get('/api/config')
    async def get_config():
        return {
            'demo': settings.demo,
            'catalog_ready': catalog.fresh,
            'privacy_url': settings.privacy_url or '/privacy',
            'consent_version': settings.consent_version,
            'ai_enabled': bool(settings.gemini_key),
            'product_hosts': settings.product_hosts,
            'parent_origins': settings.parent_origins
        }

    @app.post('/api/session')
    async def create_session(request: Request):
        limit(request, 'session')
        return {'session_id': mint_session()}

    @app.post('/api/history')
    async def read_history(body: SessionRequest, request: Request):
        uid = validate(body.session_id)
        limit(request, 'history', uid)
        messages = []
        for item in store.history(uid):
            prods = [catalog.products[pid].public() for pid in item.get('product_ids', []) if pid in catalog.products] if catalog.fresh else []
            messages.append(item | {'products': prods})
        return {'messages': messages}

    @app.post('/api/clear-history')
    async def clear_chat(body: SessionRequest, request: Request):
        uid = validate(body.session_id)
        limit(request, 'history', uid)
        lock = turn_locks.setdefault(uid, asyncio.Lock())
        async with lock: 
            store.delete_history(uid)
        return {'status': 'cleared'}

    @app.post('/api/chat')
    async def chat_endpoint(body: ChatRequest, request: Request):
        uid = validate(body.session_id)
        limit(request, 'chat', uid)
        message = body.message.strip()
        if not message: 
            raise HTTPException(422, 'Напишіть питання')
        
        lock = turn_locks.setdefault(uid, asyncio.Lock())
        async with lock:
            history = store.history(uid)
            clean_msg = redact(message)
            store.save_history(uid, history + [{'role': 'user', 'text': clean_msg}])
            
            # Виклик асистента
            answer = await assistant.answer(clean_msg, history)
            
            store.save_history(uid, history + [
                {'role': 'user', 'text': clean_msg},
                {
                    'role': 'assistant',
                    'text': redact(answer['reply']),
                    'product_ids': [p['id'] for p in answer['products']],
                    'handoff': answer['handoff']
                }
            ])
        return answer

    @app.post('/api/manager-request')
    async def create_lead(body: LeadRequest, request: Request):
        uid = validate(body.session_id)
        limit(request, 'lead', uid)
        if not body.consent: 
            raise HTTPException(422, 'Підтвердьте згоду на обробку контакту для заявки')
        
        submitted = {
            'name': body.name.strip(),
            'phone': body.phone,
            'note': body.note.strip(),
            'product_id': body.product_id
        }
        fingerprint = hashlib.sha256(json.dumps(submitted, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        request_id = str(body.request_id)
        
        try:
            if not store.existing(request_id, uid, fingerprint):
                product = None
                if body.product_id:
                    item = catalog.products.get(body.product_id)
                    if not item: 
                        raise HTTPException(404, 'Товар не знайдено. Залиште загальну заявку менеджеру.')
                    product = item.public() if catalog.fresh else {'id': item.id, 'name': item.name, 'price': None, 'available': None}
                
                payload = submitted | {
                    'product': product,
                    'context': store.history(uid)[-8:],
                    'consent_version': settings.consent_version,
                    'consent_at': time.time(),
                    'demo': settings.demo
                }
                store.create_lead(request_id, uid, fingerprint, payload)
        except Conflict: 
            raise HTTPException(409, 'Номер заявки вже використано для іншого звернення')

        return {
            'status': 'accepted',
            'request_id': request_id,
            'message': 'Заявку збережено! Менеджер зв’яжеться з вами найближчим часом.'
        }

    @app.get('/admin/overview', dependencies=[Depends(admin_auth)])
    async def get_overview():
        return {
            'catalog': {
                'fresh': catalog.fresh,
                'count': len(catalog.products),
                'updated_at': catalog.updated_at,
                'error': catalog.last_error
            },
            'notifications': store.stats(),
            'telegram_configured': bool(settings.telegram_token and settings.telegram_chat),
            'demo': settings.demo
        }

    @app.get('/admin/leads', dependencies=[Depends(admin_auth)])
    async def list_leads(limit: int = 50, offset: int = 0, state: str | None = None):
        if not 1 <= limit <= 100 or offset < 0 or state not in (None, 'new', 'in_progress', 'done'):
            raise HTTPException(422, 'Некоректний фільтр')
        return store.list_leads(state, limit, offset)

    @app.patch('/admin/leads/{request_id}', dependencies=[Depends(admin_auth)])
    async def update_lead_state(request_id: UUID, body: LeadState):
        if not store.set_state(str(request_id), body.state): 
            raise HTTPException(404, 'Заявку не знайдено')
        return {'status': 'updated'}

    @app.post('/admin/refresh-catalog', dependencies=[Depends(admin_auth)])
    async def refresh_catalog():
        if not await catalog.refresh(): 
            raise HTTPException(503, 'Не вдалося оновити каталог; збережено попередній знімок')
        return {'count': len(catalog.products)}

    return app

async def send_telegram(settings, lead_obj):
    data = lead_obj['payload']
    product = data.get('product')
    lines = [
        '⚡ <b>ЗАЯВКА З ЧАТУ ABV ХІМІЯ</b>',
        f"<b>ID:</b> <code>{lead_obj['id']}</code>",
        f"<b>Ім’я:</b> {data['name'] or 'Не вказано'}",
        f"<b>Телефон:</b> <code>{data['phone']}</code>"
    ]
    if product:
        lines.append(f"<b>Товар:</b> {product['name']} (Арт: {product.get('vendor_code', product['id'])})")
    if data['note']:
        lines.append(f"<b>Коментар:</b> {data['note']}")
    
    if data.get('context'):
        ctx_lines = [f"{h['role']}: {h['text']}" for h in data['context'][-4:]]
        lines.append('<b>Діалог:</b>\n<i>' + '\n'.join(ctx_lines) + '</i>')

    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
            r = await client.post(
                f'https://api.telegram.org/bot{settings.telegram_token}/sendMessage',
                json={
                    'chat_id': settings.telegram_chat,
                    'text': '\n'.join(lines)[:3900],
                    'parse_mode': 'HTML',
                    'disable_web_page_preview': True
                }
            )
            r.raise_for_status()
            return bool(r.json().get('ok')), None if r.json().get('ok') else 'Telegram rejected message'
    except Exception:
        return False, 'Telegram delivery not confirmed'

app = create_app()
