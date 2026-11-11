"""Grounded consultation with contextual retrieval, SKU resolution, and SalesDrive order lookup."""
import asyncio
import json
import logging
import re
import httpx

from catalog import tokens
from privacy import redact

log = logging.getLogger(__name__)

SYSTEM = '''Ти — провідний експерт-технолог та консультант інтернет-магазину будівельної хімії та 3D-друку ABV Хімія (abvhimiya.com.ua).
Спілкуйся природною, професійною українською мовою, стисло, чітко й по суті.

ТВОЇ ЗАВДАННЯ:
1. Технічні консультації та порівняння:
   Якщо клієнт запитує про відмінності матеріалів (наприклад, нефрас чи уайт-спірит, типи герметиків, розчинників, епоксидні/поліуретанові клеї, пластики PLA/PETG) або що вибрати:
   - ОБОВ'ЯЗКОВО дай пряму, розгорнуту та зрозумілу відповідь із власних технічних знань (основа, леткість, запах, призначення, швидкість випаровування, для чого краще підходить).
   - Поясни, що вибрати для конкретної задачі клієнта.
2. Пропозиція товарів:
   - Якщо у списку facts є релевантні позиції з нашого каталогу, обов'язково зазнач їхні id у масиві product_ids (до 4 шт).
   - Якщо точних товарів немає, все одно дай повну технічну відповідь, а product_ids залиш порожнім [].
   - Ціни в тексті не пиши (вони підтягуються у веб-картках).
3. Безпека:
   - Нагадуй про провітрювання та роботу в рукавичках при роботі з розчинниками. Не радити змішувати небезпечні речовини.

ФОРМАТ ВІДПОВІДІ (ТІЛЬКИ ВАЛІДНИЙ JSON):
{"text": "Твоє технічне пояснення і порада покупцеві", "product_ids": ["id1", "id2"], "handoff": false}'''

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
    headers = {"X-Api-Key": api_key, "Accept": "application/json", "User-Agent": "ABV-AI-Assistant/3.3"}

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
                        ext_id = m.get("externalId") or m.get("id") or query_str
                        st = m.get("status") or "В обробці"
                        st_name = st.get("text", "В обробці") if isinstance(st, dict) else str(st)
                        return {"found": True, "order_id": ext_id, "status": st_name}
    except Exception:
        pass
    return {"found": False}

class Assistant:
    def __init__(self, settings, store, catalog):
        self.settings, self.store, self.catalog = settings, store, catalog
        self.semaphore = asyncio.Semaphore(15)
        self.cache = {}
        limits = httpx.Limits(max_keepalive_connections=20, max_connections=40)
        self.http_client = httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(connect=4.0, read=16.0, write=4.0, pool=4.0))

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

            # 0. Перевірка кешу для миттєвих відповідей
            if q in self.cache:
                c_reply, c_ids = self.cache[q]
                c_items = [self.catalog.products[pid] for pid in c_ids if pid in getattr(self.catalog, 'products', {})]
                return {'reply': c_reply, 'products': [safe_public_product(p) for p in c_items], 'handoff': False}

            # 1. Захист від небезпеки
            if any(w in q for w in ('зміша', 'смеша')) and any(w in q for w in ('хлор', 'відбілювач', 'аміак', 'кислот')):
                return {'reply': 'Не змішуйте ці засоби: можливе виділення небезпечних токсичних газів!', 'products': [], 'handoff': True}

            # 2. Артикули
            if re.search(r'(?:артикул|арт|код|тов|модел)\b', q, re.IGNORECASE) or re.search(r'[a-zA-Zа-яА-Яіїєґ_-]+\d+', q):
                art_match = re.search(r'([a-zA-Z0-9а-яА-Яіїєґ_-]{3,20})', q)
                if art_match:
                    cand = normalize_code(art_match.group(1))
                    found = [p for p in self._get_all_products() if cand in normalize_code(getattr(p, 'id', '')) or cand in normalize_code(getattr(p, 'vendor_code', ''))]
                    if found:
                        return {'reply': f"Знайшов позицію за артикулом:", 'products': [safe_public_product(p) for p in found[:3]], 'handoff': False}

            # 3. Статус замовлення в SalesDrive
            if re.search(r'статус|ттн|де (?:моє )?замовлення|де посилка', q):
                phone_match = re.search(r'(?:\+?38)?0\d{9}', q)
                order_match = re.search(r'\b([1-9]\d{3,7})\b', q)
                search_id = phone_match.group(0) if phone_match else (order_match.group(1) if order_match else None)
                if search_id:
                    info = await fetch_salesdrive_status(self.settings.salesdrive_domain, self.settings.salesdrive_key, search_id)
                    if info.get("found"):
                        return {'reply': f"Замовлення №{info['order_id']} — статус: {info['status']}", 'products': [], 'handoff': False}
                    return {'reply': f"Замовлення за номером '{search_id}' не знайдено.", 'products': [], 'handoff': True}
                return {'reply': "Вкажіть номер замовлення або телефон для перевірки статусу.", 'products': [], 'handoff': False}

            # 4. Ввічливі фрази
            if re.fullmatch(r'(?:дякую|спасибі|ок|добре)[!.\s]*', q):
                return {'reply': 'Будь ласка! Якщо виникнуть інші питання по хімії чи 3D-друку — звертайтеся.', 'products': [], 'handoff': False}

            # 5. Пошук супутніх товарів у каталозі
            items = []
            try:
                if hasattr(self.catalog, 'search') and callable(self.catalog.search):
                    items = self.catalog.search(q)
            except Exception:
                pass

            # ПЕРЕВІРКА: чи це запитання-порівняння (У чому різниця, як вибрати, що краще)?
            is_technical_question = any(k in q for k in ('різниц', 'порівн', 'як', 'чому', 'що краще', 'чим', 'навіщо', 'відмінн'))

            # Швидкий шлях БЕЗ ШІ спрацьовує ТІЛЬКИ якщо це короткий пошук конкретного товару, а НЕ технічне питання
            if items and len(q.split()) <= 2 and not is_technical_question:
                return {
                    'reply': 'Ось товари за вашим запитом з каталогу:',
                    'products': [safe_public_product(p) for p in items[:4]],
                    'handoff': False
                }

            # 6. ВИКЛИК ШІ (Обов'язковий для консультацій та питань про властивості)
            if getattr(self.settings, 'gemini_key', None):
                try:
                    result = await self.generate(message, history, items)
                    if result:
                        ai_reply, ids, ai_handoff = result
                        matched = [p for p in items if str(getattr(p, 'id', '')).strip() in ids]
                        final_products = matched if matched else items[:4]
                        
                        # Кешуємо для наступних миттєвих відповідей
                        if not ai_handoff:
                            self.cache[q] = (ai_reply, [str(getattr(p, 'id', '')) for p in final_products])

                        return {
                            'reply': ai_reply,
                            'products': [safe_public_product(p) for p in final_products],
                            'handoff': ai_handoff
                        }
                except Exception as ai_e:
                    log.error(f"AI generation call failed: {ai_e}")

            # Якщо ключ відсутній або виклик ШІ дав збій — резервна відповідь
            return {
                'reply': 'Підібрав відповідні матеріали з каталогу ABV Хімія. Перегляньте варіанти нижче або запитайте в нашого консультанта:',
                'products': [safe_public_product(p) for p in items[:4]],
                'handoff': not bool(items)
            }

        except Exception as top_err:
            log.error(f"Critical error in assistant: {top_err}")
            return {'reply': 'Підбираю необхідні матеріали з каталогу:', 'products': [], 'handoff': True}

    async def generate(self, message, history, items):
        facts = [
            {
                'id': str(getattr(p, 'id', '')).strip(),
                'name': str(getattr(p, 'name', '')),
                'description': str(getattr(p, 'description', '') or '')[:120]
            }
            for p in items[:4]
        ]

        payload = {
            'facts': facts,
            'message': redact(str(message))
        }

        configured_model = (getattr(self.settings, 'gemini_model', '') or 'gemini-3.8-flash').strip().replace('models/', '')
        models_to_try = [configured_model]
        if 'gemini-3.8-flash' not in models_to_try:
            models_to_try.append('gemini-3.8-flash')

        body = {
            'systemInstruction': {'parts': [{'text': SYSTEM}]},
            'contents': [{'role': 'user', 'parts': [{'text': json.dumps(payload, ensure_ascii=False)}]}],
            'generationConfig': {
                'temperature': 0.2,
                'maxOutputTokens': 350,
                'responseMimeType': 'application/json'
            }
        }

        for model_name in models_to_try:
            try:
                async with self.semaphore:
                    response = await self.http_client.post(
                        f'https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent',
                        headers={'x-goog-api-key': self.settings.gemini_key},
                        json=body
                    )
                    if response.status_code != 200:
                        log.warning(f"Gemini {model_name} HTTP {response.status_code}: {response.text[:200]}")
                        continue

                    data = response.json()
                    candidates = data.get('candidates', [])
                    if not candidates:
                        continue

                    raw_text = ''.join(p.get('text', '') for p in candidates[0].get('content', {}).get('parts', [])).strip()
                    if not raw_text:
                        continue

                    raw_text = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw_text)
                    result = json.loads(raw_text, strict=False)

                    text_reply = str(result.get('text', '')).strip()
                    ids = [str(x).strip() for x in result.get('product_ids', []) if x]
                    allowed = {str(getattr(p, 'id', '')).strip() for p in items}

                    if text_reply:
                        return text_reply, [i for i in ids if i in allowed], bool(result.get('handoff', False))
            except Exception as e:
                log.warning(f"Error calling {model_name}: {e}")
                continue

        return None
