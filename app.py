"""ABV AI Commercial: safe-by-default FastAPI assistant.
Read README.md before production deployment.
"""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import html
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from collections import defaultdict, deque

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from feed_loader import catalog
from storage import Storage

load_dotenv()
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))
log = logging.getLogger('abv_ai')
BASE = Path(__file__).resolve().parent
DEV = os.getenv('APP_ENV', 'development') != 'production'
APP_SECRET = os.getenv('APP_SECRET', '')
ADMIN_TOKEN = os.getenv('ADMIN_TOKEN', '')
ALLOWED_ORIGINS = [x.strip().rstrip('/') for x in os.getenv('ALLOWED_ORIGINS', 'https://abvhimiya.com.ua,https://www.abvhimiya.com.ua,https://abvhimiya-ai.onrender.com').split(',') if x.strip()]
if DEV: 
    ALLOWED_ORIGINS.extend(['http://localhost:8000', 'http://127.0.0.1:8000'])

if not DEV and (len(APP_SECRET) < 32 or len(ADMIN_TOKEN) < 32):
    raise RuntimeError('Set strong APP_SECRET and ADMIN_TOKEN in production (min 32 characters)')

CATALOG_URL = os.getenv('CATALOG_URL', '')
CATALOG_FILE = os.getenv('CATALOG_FILE', '')
DB_PATH = os.getenv('DATABASE_PATH', str(BASE / 'data' / 'abv.sqlite3'))
if not Path(DB_PATH).is_absolute(): 
    DB_PATH = str(BASE / DB_PATH)

store = Storage(DB_PATH)

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
MODEL = os.getenv('GEMINI_MODEL', 'gemini-3.5-flash-lite')
FALLBACK = os.getenv('GEMINI_FALLBACK_MODEL', 'gemini-3.8-flash')

SALES_DOMAIN = os.getenv('SALESDRIVE_DOMAIN', '').replace('https://', '').replace('http://', '').strip().rstrip('/')
SALES_KEY = os.getenv('SALESDRIVE_API_KEY', '')
CRM_ENABLED = os.getenv('SALESDRIVE_CREATE_ENABLED', 'false').lower() == 'true'

PHONE_RE = re.compile(r'(?<!\d)(?:\+?38)?0\d{9}(?!\d)')
ORDER_ID_RE = re.compile(r'(?:замовленн\w*|заявк\w*|номер\w*|статус\w*|№|#|\b)\s*(\d{4,8})\b', re.IGNORECASE)
STATUS_WORDS = ('статус', 'де моє замовлення', 'де замовлення', 'де посилка', 'відстежити', 'що з замовленням', 'що з посилкою', 'ттн')

HISTORY_LIMIT = 12
RATE = defaultdict(deque)
RATE_LOCK = asyncio.Lock()

SALESDRIVE_STATUSES = {
    1: "Нова заявка",
    2: "В обробці",
    3: "Комплектується",
    4: "Відправлено",
    5: "Отримано",
    6: "Скасовано",
    7: "Оплачено"
}

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(min_length=16, max_length=128)

class ChatResponse(BaseModel):
    reply: str
    products: list[dict] = Field(default_factory=list)
    handoff: bool = False

class QuickOrderRequest(BaseModel):
    name: str = Field(default='Клієнт', max_length=100)
    phone: str = Field(max_length=32)
    product_id: str = Field(min_length=1, max_length=120)
    quantity: int = Field(default=1, ge=1, le=100)
    session_id: str = Field(min_length=16, max_length=128)
    request_id: str = Field(min_length=16, max_length=100)
    consent: bool = False

    @field_validator('phone')
    @classmethod
    def validate_phone(cls, v):
        digits = re.sub(r'\D', '', v)
        if digits.startswith('380') and len(digits) == 12: 
            digits = '0' + digits[3:]
        if len(digits) != 10 or not digits.startswith('0'):
            raise ValueError('Очікується український номер телефону')
        return '+38' + digits

class ManagerRequest(BaseModel):
    name: str = Field(default='Клієнт', max_length=100)
    phone: str = Field(max_length=32)
    note: str = Field(default='', max_length=1000)
    session_id: str = Field(min_length=16, max_length=128)
    request_id: str = Field(min_length=16, max_length=100)
    consent: bool = False

    @field_validator('phone')
    @classmethod
    def check_phone(cls, v): 
        return QuickOrderRequest.validate_phone(v)

async def enforce_limits(request: Request, key: str, max_calls=20):
    ip = request.client.host if request.client else 'unknown'
    now = time.monotonic()
    async with RATE_LOCK:
        for k, limit in ((f'ip:{ip}', 60), (f'sess:{key}', max_calls)):
            q = RATE[k]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= limit:
                raise HTTPException(429, 'Забагато запитів. Спробуйте трохи пізніше.')
            q.append(now)
        if len(RATE) > 20000:
            for k in list(RATE)[:10000]:
                if not RATE[k] or now - RATE[k][-1] > 120:
                    RATE.pop(k, None)

SESSION_SECRET = (APP_SECRET or secrets.token_urlsafe(48)).encode()

def _session_secret():
    return SESSION_SECRET

def validate_session(sid: str):
    try:
        unique, sig = sid.rsplit('.', 1)
        correct = hmac.new(_session_secret(), unique.encode(), 'sha256').hexdigest()[:32]
        if not hmac.compare_digest(correct, sig):
            raise ValueError
        if not re.fullmatch(r'[0-9a-f]{32}', unique):
            raise ValueError
    except (ValueError, AttributeError):
        raise HTTPException(403, 'Сесія недійсна. Оновіть сторінку.')

def create_session():
    unique = secrets.token_hex(16)
    sig = hmac.new(SESSION_SECRET, unique.encode(), 'sha256').hexdigest()[:32]
    return unique + '.' + sig

def redact(s: str):
    return PHONE_RE.sub('[телефон приховано]', s)

async def notify_telegram(message: str) -> bool:
    token = os.getenv('TELEGRAM_BOT_TOKEN', '')
    chat = os.getenv('TELEGRAM_CHAT_ID', '')
    if not (token and chat):
        return False
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(
                f'https://api.telegram.org/bot{token}/sendMessage',
                json={'chat_id': chat, 'text': message[:3500], 'parse_mode': 'HTML', 'disable_web_page_preview': True}
            )
            r.raise_for_status()
            return bool(r.json().get('ok'))
    except Exception:
        log.exception('Telegram delivery failed')
        return False

async def get_salesdrive_order_status(target: str) -> dict:
    """Універсальний пошук статусу замовлення в SalesDrive CRM через filter[search]"""
    if not SALES_DOMAIN or not SALES_KEY:
        log.warning('SalesDrive credentials not set')
        return {"found": False, "message": "SalesDrive не налаштовано."}

    clean_digits = re.sub(r'\D', '', target)
    query_str = clean_digits if clean_digits else target.strip()
    if not query_str:
        return {"found": False, "message": "Немає даних для пошуку."}

    domain = SALES_DOMAIN if SALES_DOMAIN.endswith('.salesdrive.me') else f"{SALES_DOMAIN}.salesdrive.me"
    url = f"https://{domain}/api/order/list/"
    headers = {
        "X-Api-Key": SALES_KEY,
        "Accept": "application/json"
    }

    # Пошукові шаблони (включаючи зовнішній номер Хорошоп та телефон)
    queries = [
        {"filter[search]": query_str},
        {"filter[search]": query_str[-10:] if len(clean_digits) >= 10 else query_str}
    ]
    if len(clean_digits) <= 8 and not clean_digits.startswith(('0', '380')):
        queries.append({"filter[externalId]": clean_digits})
        queries.append({"idFrom": int(clean_digits), "idTo": int(clean_digits)})

    try:
        async with httpx.AsyncClient(timeout=9, follow_redirects=True) as client:
            for params in queries:
                res = await client.get(url, params=params, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    orders = data.get("data", []) or data.get("orders", [])
                    if orders:
                        m = orders[0]
                        order_id = m.get("externalId") or m.get("id") or query_str
                        
                        st = m.get("status")
                        if isinstance(st, dict):
                            status_name = st.get("text", "В обробці")
                        elif isinstance(st, str) and st:
                            status_name = st
                        elif m.get("statusText"):
                            status_name = m.get("statusText")
                        elif m.get("statusId") and m.get("statusId") in SALESDRIVE_STATUSES:
                            status_name = SALESDRIVE_STATUSES[m["statusId"]]
                        else:
                            status_name = "В обробці"

                        ttn = (
                            m.get("trackingNumber") or 
                            m.get("ttn") or 
                            m.get("ord_delivery_data", {}).get("trackingNumber") or 
                            ""
                        )

                        order_date = m.get("orderTime") or m.get("orderDate") or ""
                        if order_date and "T" in str(order_date):
                            order_date = str(order_date).split("T")[0]

                        return {
                            "found": True,
                            "order_id": order_id,
                            "status": status_name,
                            "ttn": ttn,
                            "date": order_date
                        }
            return {"found": False, "message": "Замовлення не знайдено."}
    except Exception as e:
        log.exception(f"Помилка зв'язку з SalesDrive: {e}")
        return {"found": False, "message": f"Помилка зв'язку: {e}"}

async def send_lead(lead):
    if not lead['telegram_sent']:
        title = '⚡ ЗАЯВКА З AI-ЧАТУ' if lead['product_id'] else '📞 ЗВЕРНЕННЯ ДО МЕНЕДЖЕРА'
        msg = f"<b>{title}</b>\nID: <code>{html.escape(lead['id'])}</code>\nІм'я: {html.escape(lead['name'])}\nТелефон: {html.escape(lead['phone'])}\n"
        if lead['product_id']:
            msg += f"Товар: {html.escape(lead['product_name'])}\nID: {html.escape(lead['product_id'])}\nЦіна: {lead['price']} грн\nКількість: {lead['quantity']}\n"
        if lead['note']:
            msg += f"Примітка: {html.escape(lead['note'][:500])}\n"
        if await notify_telegram(msg): 
            store.update_lead(lead['id'], telegram_sent=1)

SYSTEM_INSTRUCTION = '''Ти AI-консультант інтернет-магазину будівельної хімії та 3D-друку abvhimiya.com.ua. 
Відповідай українською, ввічливо та стисло.

ПРАВИЛА РОБОТИ З КАТАЛОГОМ:
- Використовуй ТІЛЬКИ дані каталогу для конкретних цін, SKU та наявності.
- Не вигадуй сумісності матеріалів чи неіснуючих характеристик. За відсутності інформації порадь звернутися до менеджера.

ПРАВИЛА НАДАННЯ СТАТУСУ ЗАМОВЛЕНЬ (SalesDrive):
- Якщо в запиті надано блок [ДАНІ ЗАМОВЛЕННЯ З CRM]:
  • НІКОЛИ не виводь технічний заголовок "[ДАНІ ЗАМОВЛЕННЯ З CRM]" або "SalesDrive".
  • Чітко озвуч клієнту номер замовлення (№...), актуальний статус та номер ТТН (якщо сформовано).
  • Якщо є ТТН — нагадай, що відстежити рух можна у мобільному додатку Нової Пошти.
  • Якщо ТТН немає — підкажи, що замовлення готується на складі і номер ТТН надійде в SMS після відправки.
  • НЕ запитуй додатково номер телефону, якщо замовлення вже знайдено!
- Якщо клієнт запитує статус, але не назвав номер або замовлення не знайдено — ввічливо попроси назвати номер замовлення з сайту (наприклад, 19138) або контактний телефон.
'''

def is_status_query(q: str):
    return any(s in q.casefold() for s in STATUS_WORDS)

def fallback_reply(q, items):
    if items: 
        return 'Знайшов відповідні товари в каталозі. Для уточнення деталей ви можете звернутися до менеджера.'
    return 'Зараз не можу надати точну інформацію. Напишіть назву товару або зверніться до менеджера.'

async def generate_reply(q, history, items, crm_context=""):
    if not GEMINI_API_KEY:
        return fallback_reply(q, items)
    context = '\n'.join(f"- {x['name']} | ID {x['id']} | {x['price']} грн | {'наявний' if x['available'] else 'недоступний'} | {x['description'][:700]}" for x in items)
    prior = '\n'.join(f"{h['role']}: {h['text'][:500]}" for h in history[-8:])
    
    prompt = f"<catalog_data>\n{context or 'Немає знайдених товарів'}\n</catalog_data>\n"
    if crm_context:
        prompt += f"<crm_data>\n{crm_context}\n</crm_data>\n"
    prompt += f"<previous_conversation>\n{prior}\n</previous_conversation>\n<customer_message>\n{q}\n</customer_message>"

    async with httpx.AsyncClient(timeout=18, follow_redirects=False) as client:
        for model in dict.fromkeys((MODEL, FALLBACK)):
            if not re.fullmatch(r'[a-zA-Z0-9._-]+', model):
                continue
            url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
            body = {
                'systemInstruction': {'parts': [{'text': SYSTEM_INSTRUCTION}]},
                'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
                'generationConfig': {'temperature': 0.15, 'maxOutputTokens': 500}
            }
            try:
                r = await client.post(url, headers={'x-goog-api-key': GEMINI_API_KEY}, json=body)
                r.raise_for_status()
                data = r.json()
                parts = (data.get('candidates') or [{}])[0].get('content', {}).get('parts', [])
                answer = ''.join(p.get('text', '') for p in parts if isinstance(p, dict)).strip()
                if answer:
                    return answer[:3500]
            except Exception:
                log.warning('Gemini model call failed: %s', model, exc_info=True)
    return fallback_reply(q, items)

async def maintenance():
    interval = max(5, int(os.getenv('CATALOG_REFRESH_MINUTES', '30'))) * 60
    while True:
        try:
            await asyncio.sleep(interval)
            await catalog.update_feed(CATALOG_URL, CATALOG_FILE)
            store.clean()
            for lead in store.pending(25):
                await send_lead(lead)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Maintenance failed')

@asynccontextmanager
async def lifespan(app: FastAPI):
    await catalog.update_feed(CATALOG_URL, CATALOG_FILE)
    task = asyncio.create_task(maintenance())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    store.close()

app = FastAPI(title='ABV Himiya AI Commercial', version='2.0.0', lifespan=lifespan, docs_url='/docs' if DEV else None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=['GET', 'POST'], allow_headers=['Content-Type', 'Authorization'], allow_credentials=False)
app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')

@app.middleware('http')
async def security_headers(request: Request, call_next):
    origin = request.headers.get('origin', '').rstrip('/')
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and origin and origin not in ALLOWED_ORIGINS:
        return JSONResponse(status_code=403, content={'detail': 'Заборонене джерело запиту'})
    resp = await call_next(request)
    resp.headers.update({
        'X-Content-Type-Options': 'nosniff',
        'Referrer-Policy': 'no-referrer',
        'X-Frame-Options': 'SAMEORIGIN',
        'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' https: data:; connect-src 'self'; frame-ancestors 'self' https://abvhimiya.com.ua https://www.abvhimiya.com.ua; base-uri 'none'; object-src 'none'"
    })
    return resp

@app.get('/')
def root():
    return RedirectResponse('/widget')

@app.get('/health')
def health():
    return {'status': 'ok', 'catalog_ready': bool(catalog.products), 'catalog_count': len(catalog.products)}

@app.get('/widget')
def widget():
    return FileResponse(BASE / 'static' / 'widget.html', media_type='text/html')

@app.get('/admin')
def admin_page():
    return FileResponse(BASE / 'static' / 'admin.html', media_type='text/html')

@app.post('/api/session')
async def new_session(request: Request):
    await enforce_limits(request, 'new-session:' + str(request.client.host if request.client else 'unknown'), 10)
    return {'session_id': create_session()}

@app.post('/api/chat', response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request):
    validate_session(req.session_id)
    await enforce_limits(request, req.session_id)
    q = req.message.strip()
    if not q:
        raise HTTPException(400, 'Порожнє повідомлення')
    
    history = store.history(req.session_id)
    
    # 1. Витягуємо номер замовлення або телефон із поточного повідомлення чи контексту
    phone_m = PHONE_RE.search(q)
    order_m = ORDER_ID_RE.search(q)
    search_target = ""

    if order_m:
        search_target = order_m.group(1)
    elif phone_m:
        search_target = phone_m.group(0)
    elif is_status_query(q):
        for prev in reversed(history[-2:]):
            prev_order = ORDER_ID_RE.search(prev.get("text", ""))
            if prev_order:
                search_target = prev_order.group(1)
                break

    crm_context = ""
    handoff = False
    items = []

    # 2. Якщо є номер або запит статусу — перевіряємо в SalesDrive
    if search_target:
        st_data = await get_salesdrive_order_status(search_target)
        if st_data.get("found"):
            crm_context = (
                f"[ДАНІ ЗАМОВЛЕННЯ З CRM]:\n"
                f"- Номер замовлення: №{st_data['order_id']}\n"
                f"- Статус: {st_data['status']}\n"
                f"- Номер ТТН: {st_data['ttn'] if st_data['ttn'] else 'Ще не сформовано (комплектується на складі)'}\n"
                f"- Дата замовлення: {st_data.get('date', 'Нещодавно')}\n"
            )
        else:
            crm_context = f"[ДАНІ ЗАМОВЛЕННЯ]: Замовлення за номером '{search_target}' не знайдено."
            handoff = True
        reply = await generate_reply(q, history, [], crm_context)
    elif is_status_query(q):
        reply = "Вкажіть, будь ласка, номер вашого замовлення з сайту (наприклад, 19138) або контактний номер телефону, і я одразу перевірю статус та ТТН!"
        handoff = False
    else:
        items = catalog.search_relevant_products(q, 5)
        reply = await generate_reply(q, history, items)
        handoff = False

    history.extend([{'role': 'user', 'text': redact(q)}, {'role': 'assistant', 'text': redact(reply)}])
    store.save_history(req.session_id, history[-HISTORY_LIMIT:])
    store.log(req.session_id, 'chat', redact(q), redact(reply))

    return ChatResponse(
        reply=reply,
        products=[{k: v for k, v in p.items() if k in ('id', 'name', 'price', 'available', 'url', 'vendor_code')} for p in items],
        handoff=handoff
    )

@app.post('/api/quick-order')
async def quick_order(req: QuickOrderRequest, request: Request):
    validate_session(req.session_id)
    await enforce_limits(request, req.session_id, 8)
    if not req.consent:
        raise HTTPException(422, 'Потрібна згода на обробку даних для заявки')
    
    item = catalog.get(req.product_id)
    if not item:
        raise HTTPException(404, 'Товар не знайдено. Оновіть каталог або зверніться до менеджера.')
    if not item['available']:
        raise HTTPException(409, 'Зараз товар недоступний для швидкого замовлення.')
    
    lid = req.request_id
    lead = {
        'id': lid, 'name': req.name.strip() or 'Клієнт', 'phone': req.phone,
        'product_id': item['id'], 'product_name': item['name'],
        'price': item['price'], 'quantity': req.quantity
    }
    
    created = store.create_lead(lead)
    if not created:
        old = store.lead(lid)
        if old and (old['phone'] != lead['phone'] or old['product_id'] != lead['product_id']):
            raise HTTPException(409, 'Ідентифікатор заявки вже використано')
    else:
        store.log(req.session_id, 'lead', 'Нова заявка на ' + item['name'], 'Збережено у базі')
    
    try:
        await asyncio.wait_for(send_lead(store.lead(lid)), timeout=15)
    except Exception:
        log.exception('Lead notification attempt failed')
        
    return {'status': 'accepted', 'request_id': lid, 'message': 'Заявку збережено. Менеджер зв’яжеться з вами для підтвердження!'}

@app.post('/api/manager-request')
async def manager_request(req: ManagerRequest, request: Request):
    validate_session(req.session_id)
    await enforce_limits(request, req.session_id, 8)
    if not req.consent:
        raise HTTPException(422, 'Потрібна згода на обробку даних для звернення')
    
    lead = {
        'id': req.request_id, 'name': req.name.strip() or 'Клієнт', 'phone': req.phone,
        'product_id': None, 'product_name': None, 'price': None, 'quantity': 1, 'note': req.note
    }
    created = store.create_lead(lead)
    if not created:
        old = store.lead(req.request_id)
        if old and old['phone'] != req.phone:
            raise HTTPException(409, 'Ідентифікатор звернення вже використано')
            
    try:
        await asyncio.wait_for(send_lead(store.lead(req.request_id)), timeout=15)
    except Exception:
        log.exception('Manager notification attempt failed')
        
    return {'status': 'accepted', 'request_id': req.request_id, 'message': 'Звернення збережено, менеджер зв’яжеться з вами.'}

async def admin_auth(authorization: str = Header(default='')):
    if not ADMIN_TOKEN or not authorization.startswith('Bearer ') or not hmac.compare_digest(authorization[7:], ADMIN_TOKEN):
        raise HTTPException(401, 'Потрібна авторизація', headers={'WWW-Authenticate': 'Bearer'})

@app.get('/admin/logs', dependencies=[Depends(admin_auth)])
async def admin_logs():
    return {'logs': store.logs(), 'note': 'Контактні телефони в чат-логах замасковано. Заявки — окремо.'}

@app.get('/admin/leads', dependencies=[Depends(admin_auth)])
async def admin_leads():
    return {'leads': store.pending(100)}

@app.post('/api/refresh-catalog', dependencies=[Depends(admin_auth)])
async def admin_refresh():
    ok = await catalog.update_feed(CATALOG_URL, CATALOG_FILE)
    if not ok:
        raise HTTPException(503, 'Не вдалося оновити каталог, попередній знімок збережено')
    return {'status': 'success', 'total_products': len(catalog.products), 'updated_at': catalog.last_updated}
