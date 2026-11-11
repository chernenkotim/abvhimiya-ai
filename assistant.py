"""Grounded consultation with contextual retrieval, SKU resolution, and SalesDrive order lookup."""
import asyncio
import json
import logging
import re
import time
import httpx

from catalog import tokens
from privacy import redact

log = logging.getLogger(__name__)

SYSTEM = '''Ти провідний AI-консультант інтернет-магазину ABV Хімія (abvhimiya.com.ua).
Відповідай українською мовою, дуже стисло (1-2 речення), доброзичливо й експертно.
Базуй характеристики на facts. Вказуй id товарів у product_ids. Не пиши ціни.'''

SALESDRIVE_STATUSES = {
    1: "Нова заявка", 2: "В обробці", 3: "Комплектується",
    4: "Відправлено", 5: "Отримано", 6: "Скасовано", 7: "Оплачено"
}

def normalize_code(val: str) -> str:
    s = str(val or '').strip().casefold().replace('-', '').replace(' ', '').replace('_', '')
    trans = str.maketrans('асрокемітх', 'acpokemitx')
    return s.translate(trans)

def safe_public_product(p) -> dict:
    if isinstance(p, dict):
        return {
            'id': str(p.get('id', '')), 'name': str(p.get('name', 'Товар')),
            'price': p.get('price'), 'available': p.get('available', True),
            'url': p.get('url'), 'image': p.get('image'), 'vendor_code': str(p.get('vendor_code', ''))
        }
    if hasattr(p, 'public') and callable(p.public):
        try: return p.public()
        except Exception: pass
    return {
        'id': str(getattr(p, 'id', '')), 'name': str(getattr(p, 'name', 'Товар')),
        'price': getattr(p, 'price', None), 'available': getattr(p, 'available', True),
        'url': getattr(p, 'url', None), 'image': getattr(p, 'image', None),
        'vendor_code': str(getattr(p, 'vendor_code', ''))
    }

async def fetch_salesdrive_status(domain: str, api_key: str, target: str) -> dict:
    if not domain or not api_key:
        return {"found": False}
    clean_digits = re.sub(r'\D', '', target)
    query_str = clean_digits if clean_digits else target.strip()
    if not query_str: return {"found": False}

    clean_domain = domain if domain.endswith('.salesdrive.me') else f"{domain}.salesdrive.me"
    base_url = f"https://{clean_domain}/api/order/list/"
    headers = {"X-Api-Key": api_key, "Accept": "application/json", "User-Agent": "ABV-AI-Assistant/3.2"}

    urls_to_try = []
    if len(clean_digits) <= 8 and not clean_digits.startswith(('0', '380')):
        urls_to_try.extend([f"{base_url}?filter[externalId]={query_str}", f"{base_url}?filter[search]={query_str}"])
    else:
        phone_10 = clean_digits[-10:] if len(clean_digits) >= 10 else clean_digits
        urls_to_try.extend([f"{base_url}?filter[phone]={phone_10}", f"{base_url}?filter[phone]=38{phone_10}"])

    try:
        async with httpx.AsyncClient(timeout=6, follow_redirects=True) as client:
            for req_url in urls_to_try:
                res = await client.get(req_url, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    orders = data.get("data", []) or data.get("orders", [])
                    if orders:
                        m = orders[0]
                        internal_id, ext_id = m.get("id"), m.get("externalId") or m.get("id") or query_str
                        st = m.get("status") or "В обробці"
                        if isinstance(st, dict): st_name = st.get("text", "В обробці")
                        elif isinstance(st, str) and st: st_name = st
                        else: st_name = "В обробці"
                        return {"found": True, "order_id": ext_id, "status": st_name, "ttn": "", "date": ""}
    except Exception:
        pass
    return {"found": False}

class Assistant:
    def __init__(self, settings, store, catalog):
        self.settings, self.store, self.catalog = settings, store, catalog
        self.semaphore = asyncio.Semaphore(15)
        # Швидкий кеш для частих питань в пам'яті
        self.cache = {}
        
        limits = httpx.Limits(max_keepalive_connections=20, max_connections=40)
        self.http_client = httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(connect=3.0, read=12.0, write=3.0, pool=3.0))

    async def aclose(self):
        await self.http_client.aclose()

    def _get_all_products(self) -> list:
        prods = getattr(self.catalog, 'products', None)
        if prods is None: return []
        if isinstance(prods, dict): return list(prods.values())
        if isinstance(prods, list): return prods
        return []

    async def answer(self, message, history):
        try:
            q = str(message or '').casefold().strip()

            # 0. ПЕРЕВІРКА ЛОКАЛЬНОГО КЕШУ (Миттєво < 0.01с)
            if q in self.cache:
                cached_reply, cached_ids = self.cache[q]
                cached_items = [self.catalog.products.get(pid) for pid in cached_ids if pid in self.catalog.products]
                return {'reply': cached_reply, 'products': [safe_public_product(p) for p in cached_items], 'handoff': False}

            # 1. Захист від небезпечного змішування
            if any(w in q for w in ('зміша', 'смеша')) and any(w in q for w in ('хлор', 'відбілювач', 'аміак', 'кислот')):
                return {'reply': 'Не змішуйте ці засоби: можливе виділення небезпечних газів!', 'products': [], 'handoff': True}

            # 2. ПЕРЕВІРКА АРТИКУЛІВ (Швидкий шлях без ШІ)
            if re.search(r'(?:артикул|арт|код|тов|модел)\b', q, re.IGNORECASE) or re.search(r'[a-zA-Zа-яА-Яіїєґ_-]+\d+', q):
                art_match = re.search(r'([a-zA-Z0-9а-яА-Яіїєґ_-]{3,20})', q)
                if art_match:
                    cand = normalize_code(art_match.group(1))
                    found = [p for p in self._get_all_products() if cand in normalize_code(getattr(p, 'id', '')) or cand in normalize_code(getattr(p, 'vendor_code', ''))]
                    if found:
                        return {'reply': f"Знайшов за артикулом:", 'products': [safe_public_product(p) for p in found[:3]], 'handoff': False}

            # 3. СТАТУС ЗАМОВЛЕННЯ SALESDRIVE
            if re.search(r'статус|ттн|де (?:моє )?замовлення|де посилка', q):
                phone_match = re.search(r'(?:\+?38)?0\d{9}', q)
                order_match = re.search(r'\b([1-9]\d{3,7})\b', q)
                search_id = phone_match.group(0) if phone_match else (order_match.group(1) if order_match else None)
                
                if search_id:
                    info = await fetch_salesdrive_status(self.settings.salesdrive_domain, self.settings.salesdrive_key, search_id)
                    if info.get("found"):
                        return {'reply': f"Замовлення №{info['order_id']} — Статус: {info['status']}", 'products': [], 'handoff': False}
                    return {'reply': f"Замовлення за номером '{search_id}' не знайдено.", 'products': [], 'handoff': True}
                return {'reply': "Вкажіть номер замовлення або телефон для перевірки статусу.", 'products': [], 'handoff': False}

            # 4. Ввічливі фрази
            if re.fullmatch(r'(?:дякую|спасибі|ок|добре)[!.\s]*', q):
                return {'reply': 'Будь ласка! Звертайтеся.', 'products': [], 'handoff': False}

            # 5. ПОШУК ТОВАРІВ У КАТАЛОЗІ
            items = []
            try:
                if hasattr(self.catalog, 'search') and callable(self.catalog.search):
                    items = self.catalog.search(q)
            except Exception:
                pass

            # FAST-PATH: Якщо запит короткий і є точні товари, віддаємо одразу БЕЗ виклику Gemini (Швидкість ~0.05с!)
            if items and len(q.split()) <= 2 and not any(k in q for k in ('різниця', 'порівн', 'як', 'чому', 'що краще')):
                return {
                    'reply': 'Ось що знайшов у каталозі за вашим запитом:',
                    'products': [safe_public_product(p) for p in items[:4]],
                    'handoff': False
                }

            # 6. AI-ГЕНЕРАЦІЯ (Тільки для складних технічних запитань або порівнянь)
            if getattr(self.settings, 'gemini_key', None):
                try:
                    if self.store.allow([('ai:global', self.settings.daily_ai_calls, 86400)]):
                        result = await self.generate(message, history, items)
                        if result:
                            ai_reply, ids, ai_handoff = result
                            matched = [p for p in items if str(getattr(p, 'id', '')).strip() in ids]
                            items = matched if matched else items[:4]
                            
                            # Зберігаємо в кеш
                            if not ai_handoff and len(ids) > 0:
                                self.cache[q] = (ai_reply, ids)
                                
                            return {'reply': ai_reply, 'products': [safe_public_product(p) for p in items[:4]], 'handoff': ai_handoff}
                except Exception as ai_e:
                    log.warning(f"AI generation failed: {ai_e}")

            return {
                'reply': 'Знайшов відповідні товари у нашому каталозі. Перегляньте картки нижче:',
                'products': [safe_public_product(p) for p in items[:4]],
                'handoff': not bool(items)
            }

        except Exception as top_err:
            log.error(f"Critical error: {top_err}")
            return {'reply': 'Підбираю матеріали з каталогу ABV Хімія:', 'products': [], 'handoff': True}

    async def generate(self, message, history, items):
        facts = [{'id': str(getattr(p, 'id', '')).strip(), 'name': str(getattr(p, 'name', '')), 'description': str(getattr(p, 'description', '') or '')[:100]} for p in items[:3]]
        
        payload = {
            'facts': facts,
            'message': redact(str(message))
        }

        configured_model = (getattr(self.settings, 'gemini_model', '') or 'gemini-3.8-flash').strip().replace('models/', '')
        models_to_try = [configured_model, 'gemini-3.8-flash']

        body = {
            'systemInstruction': {'parts': [{'text': SYSTEM}]},
            'contents': [{'role': 'user', 'parts': [{'text': json.dumps(payload, ensure_ascii=False)}]}],
            'generationConfig': {'temperature': 0.1, 'maxOutputTokens': 200, 'responseMimeType': 'application/json'}
        }

        # Робимо лише 1 спробу без довгих очікувань, щоб не тримати клієнта
        for model_name in models_to_try:
            try:
                async with self.semaphore:
                    response = await self.http_client.post(
                        f'https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent',
                        headers={'x-goog-api-key': self.settings.gemini_key},
                        json=body
                    )
                    if response.status_code != 200:
                        continue

                    data = response.json()
                    raw_text = ''.join(p.get('text', '') for p in data.get('candidates', [{}])[0].get('content', {}).get('parts', [])).strip()
                    if not raw_text: continue

                    raw_text = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw_text)
                    result = json.loads(raw_text, strict=False)
                    
                    ids = [str(x).strip() for x in result.get('product_ids', []) if x]
                    allowed = {str(getattr(p, 'id', '')).strip() for p in items}
                    return str(result.get('text', '')).strip(), [i for i in ids if i in allowed], bool(result.get('hando_off', False))
            except Exception:
                continue
        return None
