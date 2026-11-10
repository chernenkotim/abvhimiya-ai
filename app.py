"""ABV chat consultant: catalog consultation, durable manager requests, and instant order tracking."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import html
import json
import logging
import secrets
import time
from uuid import UUID
import weakref

from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
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
            if len(body) > 65536:
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

security = HTTPBasic()

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
            except Exception as e: 
                log.error(f'Notification worker failed: {e}; requests remain in database')
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
            # Закриваємо пули з'єднань асистента
            if hasattr(assistant, 'aclose'):
                await assistant.aclose()
            store.close()

    app = FastAPI(
        title='ABV Хімія — чат-консультант',
        version='3.0.1',
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
    async def headers_middleware(request: Request, call_next):
        origin = request.headers.get('origin', '')
        if request.method in ('POST', 'PATCH', 'PUT', 'DELETE') and origin and origin not in origins:
            return JSONResponse({'detail': 'Заборонене джерело запиту'}, status_code=403)
        response = await call_next(request)
        widget = request.url.path in ('/widget', '/')
        ancestors = ' '.join(("'self'", *settings.parent_origins)) if widget else "'none'"
        response.headers.update({
            'Content-Security-Policy': f"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' https: data:; connect-src 'self'; frame-ancestors {ancestors}; base-uri 'none'; object-src 'none'",
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
            ('ip:' + ip_key(request), 120, 60),
            ('ip:' + kind + ':' + ip_key(request), {'session': 25, 'chat': 50, 'lead': 15, 'admin': 60}.get(kind, 40), 60)
        ]
        if session_val:
            rules.append(('session:' + kind + ':' + session_val, {'chat': 40, 'lead': 10}.get(kind, 35), 60))
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

    # Базова авторизація для адмінки
    async def admin_auth(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
        limit(request, 'admin')
        
        correct_username = secrets.compare_digest(credentials.username, getattr(settings, 'admin_user', 'admin'))
        correct_password = secrets.compare_digest(credentials.password, getattr(settings, 'admin_password', 'abvhimiya2026'))
        
        if not (correct_username and correct_password):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Невірний логін або пароль",
                headers={"WWW-Authenticate": "Basic"},
            )
        return True

    @app.get('/')
    @app.get('/widget')
    async def widget_page(): 
        return FileResponse(BASE / 'static' / 'widget.html')

    @app.get('/admin')
    async def admin_view(auth_passed: bool = Depends(admin_auth)): 
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
        # Спочатку блокуємо і читаємо історію
        async with lock:
            history = store.history(uid)
        
        clean_msg = redact(message)
        
        # ВИКЛИКАЄМО ШІ ПОЗА ЛОКОМ (щоб не блокувати наступні запити, поки чекаємо Gemini)
        try:
            # Захист від таймауту на рівні FastAPI (28 сек, бо на клієнті 30 сек)
            async with asyncio.timeout(28.0):
                answer = await assistant.answer(clean_msg, history)
        except asyncio.TimeoutError:
            log.warning(f"FastAPI chat endpoint timeout for user {uid}")
            answer = {'reply': 'Запит надто складний, і я не встиг його обробити. Уточніть питання або зверніться до менеджера.', 'products': [], 'handoff': True}
        
        # Зберігаємо результат, знову беремо лок
        async with lock:
            current_history = store.history(uid)
            store.save_history(uid, current_history + [
                {'role': 'user', 'text': clean_msg},
                {
                    'role': 'assistant',
                    'text': redact(answer['reply']),
                    'product_ids': [p['id'] for p in answer.get('products', [])],
                    'handoff': answer.get('handoff', False)
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

    # --- АДМІН-ПАНЕЛЬ ТА ПЕРЕГЛЯД ДІАЛОГІВ ---

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

    @app.get('/admin/dialogs-data', dependencies=[Depends(admin_auth)])
    async def get_dialogs_data(limit: int = 50, offset: int = 0):
        return store.list_dialogs(limit=min(limit, 100), offset=offset)

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
    
    lead_id_safe = html.escape(str(lead_obj['id']))
    name_safe = html.escape(data.get('name') or 'Не вказано')
    phone_safe = html.escape(str(data.get('phone', '')))
    
    lines = [
        '⚡ <b>ЗАЯВКА З ЧАТУ ABV ХІМІЯ</b>',
        f"<b>ID:</b> <code>{lead_id_safe}</code>",
        f"<b>Ім’я:</b> {name_safe}",
        f"<b>Телефон:</b> <code>{phone_safe}</code>"
    ]
    if product:
        p_name = html.escape(str(product.get('name', '')))
        p_sku = html.escape(str(product.get('vendor_code', product.get('id', ''))))
        lines.append(f"<b>Товар:</b> {p_name} (Арт: {p_sku})")
    if data.get('note'):
        lines.append(f"<b>Коментар:</b> {html.escape(str(data['note']))}")
    
    if data.get('context'):
        ctx_lines = []
        for h in data['context'][-4:]:
            role_name = 'Клієнт' if h.get('role') == 'user' else 'Бот'
            txt = html.escape(str(h.get('text', '')))
            ctx_lines.append(f"<b>{role_name}:</b> {txt}")
        lines.append('\n<b>Останні репліки діалогу:</b>\n' + '\n'.join(ctx_lines))

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
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
            res_json = r.json()
            return bool(res_json.get('ok')), None if res_json.get('ok') else 'Telegram rejected message'
    except Exception as e:
        log.warning(f"Telegram notification send error: {e}")
        return False, f'Telegram error: {e}'

app = create_app()
