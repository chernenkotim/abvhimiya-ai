"""Grounded consultation with contextual retrieval, SKU resolution, and SalesDrive order lookup."""
import asyncio
import json
import logging
import re
import httpx

from catalog import tokens
from privacy import redact

log = logging.getLogger(__name__)

SYSTEM = '''Ти — провідний експерт-технолог та консультант інтернет-магазину ABV Хімія (abvhimiya.com.ua).
Спілкуйся природною українською мовою, стисло, чітко й професійно.

ЗАВДАННЯ:
1. Якщо запитують про різницю матеріалів (нефрас/уайт-спірит, типи розчинників, герметиків, клеїв, філаментів):
   - Обов'язково поясни різницю: основа, швидкість випаровування, запах, сумісність і де що застосовувати.
2. У полі product_ids вкажи id відповідних товарів з facts (якщо є). Не пиши ціни в тексті.
3. Повертай ТІЛЬКИ JSON формату:
{"text": "Пояснення різниці та порада", "product_ids": ["id1"], "handoff": false}'''

# Миттєва база знань для найчастіших хімічних запитань (відповідь за 0.05 сек без виклику хмари)
EXPERT_KNOWLEDGE = {
    "нефрас_уайт": (
        "Головна відмінність — у швидкості випаровування, залишку та призначенні:\n\n"
        "• **Нефрас (бензин-«калоша»)** — легкий розчинник. Випаровується майже миттєво, не залишає жирних слідів і плівки. "
        "Ідеальний для знежирення поверхонь перед склеюванням/фарбуванням, промивання електроніки, підшипників та інструментів.\n\n"
        "• **Уайт-спірит** — важча фракція (гасова основа). Випаровується значно повільніше, має маслянисту основу й характерний запах. "
        "Його основне призначення — розведення алкідних емалей, масляних фарб, лаків та очищення пензлів після малярних робіт.\n\n"
        "Для знежирення деталей обирайте **нефрас**, а для розведення фарби — **уайт-спірит**."
    ),
    "pla_petg": (
        "Різниця між пластиками для 3D-друку:\n\n"
        "• **PLA** — найпростіший у друці, екологічний (на основі кукурудзи), не дає усадки, твердий, але крихкий і плавиться вже при 55-60°C. Чудовий для декоративних моделей, сувенірів та прототипів.\n\n"
        "• **PETG** — міцніший, ударостійкий, стійкий до ультрафіолету, вологи та температури до 75-80°C. Оптимальний для технічних деталей, кронштейнів та речей, що використовуються на вулиці чи під навантаженням."
    )
}

SALESDRIVE_STATUSES = {
    1: "Нова заявка", 2: "В обробці", 3: "Комплектується",
    4: "Відправлено", 5: "Отримано", 6: "Скасовано", 7: "Оплачено"
}

# Вимикання блокувань безпеки для хімічної продукції
SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"}
]

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
    if not domain or not api_key: return {"found": False}
    clean_digits = re.sub(r'\D', '', target)
    query_str = clean_digits if clean_digits else target.strip()
    if not query_str: return {"found": False}

    clean_domain = domain if domain.endswith('.salesdrive.me') else f"{domain}.salesdrive.me"
    base_url = f"https://{clean_domain}/api/order/list/"
    headers = {"X-Api-Key": api_key, "Accept": "application/json", "User-Agent": "ABV-AI-Assistant/3.3"}

    urls_to_try = [
        f"{base_url}?filter[externalId]={query_str}",
        f"{base_url}?filter[search]={query_str}"
    ] if len(clean_digits) <= 8 else [
        f"{base_url}?filter[phone]={clean_digits[-10:]}",
        f"{base_url}?filter[phone]=38{clean_digits[-10:]}"
    ]

    try:
        async with httpx.AsyncClient(timeout=4, follow_redirects=True) as client:
            for req_url in urls_to_try:
                res = await client.get(req_url, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    orders = data.get("data", []) or data.get("orders", [])
                    if orders:
                        m = orders[0]
                        st = m.get("status") or "В обробці"
                        st_name = st.get("text", "В обробці") if isinstance(st, dict) else str(st)
                        return {"found": True, "order_id": m.get("externalId") or m.get("id"), "status": st_name}
    except Exception:
        pass
    return {"found": False}

class Assistant:
    def __init__(self, settings, store, catalog):
        self.settings, self.store, self.catalog = settings, store, catalog
        self.semaphore = asyncio.Semaphore(15)
        self.cache = {}
        self.http_client = httpx.AsyncClient(
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=40),
            timeout=httpx.Timeout(connect=3.0, read=8.0, write=3.0, pool=3.0)
        )

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

            # 0. Перевірка кешу сесії
            if q in self.cache:
                c_reply, c_ids = self.cache[q]
                c_items = [self.catalog.products[pid] for pid in c_ids if pid in getattr(self.catalog, 'products', {})]
                return {'reply': c_reply, 'products': [safe_public_product(p) for p in c_items], 'handoff': False}

            # 1. Захист від небезпеки
            if any(w in q for w in ('зміша', 'смеша')) and any(w in q for w in ('хлор', 'відбілювач', 'аміак', 'кислот')):
                return {'reply': 'Не змішуйте ці засоби: можливе виділення небезпечних токсичних газів!', 'products': [], 'handoff': True}

            # 2. Пошук супутніх товарів у каталозі
            items = []
            try:
                if hasattr(self.catalog, 'search') and callable(self.catalog.search):
                    items = self.catalog.search(q)
            except Exception as e:
                log.warning(f"Catalog search error: {e}")

            # 3. DIRECT EXPERT FAQ: Миттєва відповідь на поширені порівняння (0.05 сек)
            if 'нефрас' in q and ('уайт' in q or 'вайт' in q):
                reply_text = EXPERT_KNOWLEDGE["нефрас_уайт"]
                return {'reply': reply_text, 'products': [safe_public_product(p) for p in items[:4]], 'handoff': False}
            
            if 'pla' in q and 'petg' in q:
                reply_text = EXPERT_KNOWLEDGE["pla_petg"]
                return {'reply': reply_text, 'products': [safe_public_product(p) for p in items[:4]], 'handoff': False}

            # 4. Перевірка артикулів
            if re.search(r'(?:артикул|арт|код|тов|модел)\b', q, re.IGNORECASE) or re.search(r'[a-zA-Zа-яА-Яіїєґ_-]+\d+', q):
                art_match = re.search(r'([a-zA-Z0-9а-яА-Яіїєґ_-]{3,20})', q)
                if art_match:
                    cand = normalize_code(art_match.group(1))
                    found = [p for p in self._get_all_products() if cand in normalize_code(getattr(p, 'id', '')) or cand in normalize_code(getattr(p, 'vendor_code', ''))]
                    if found:
                        return {'reply': "Знайшов позицію за артикулом:", 'products': [safe_public_product(p) for p in found[:3]], 'handoff': False}

            # 5. Статус замовлення в CRM
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

            # 6. Ввічливі фрази
            if re.fullmatch(r'(?:дякую|спасибі|ок|добре)[!.\s]*', q):
                return {'reply': 'Будь ласка! Якщо потрібна допомога з вибором матеріалів — звертайтеся.', 'products': [], 'handoff': False}

            # 7. Генерація відповіді через Gemini AI
            if getattr(self.settings, 'gemini_key', None):
                result = await self.generate(message, history, items)
                if result:
                    ai_reply, ids, ai_handoff = result
                    matched = [p for p in items if str(getattr(p, 'id', '')).strip() in ids]
                    final_products = matched if matched else items[:4]
                    
                    if not ai_handoff:
                        self.cache[q] = (ai_reply, [str(getattr(p, 'id', '')) for p in final_products])

                    return {
                        'reply': ai_reply,
                        'products': [safe_public_product(p) for p in final_products],
                        'handoff': ai_handoff
                    }

            # 8. Резервна відповідь (якщо ШІ не відповів)
            return {
                'reply': 'Підібрав відповідні матеріали з каталогу ABV Хімія. Перегляньте варіанти нижче або запитайте в нашого консультанта:',
                'products': [safe_public_product(p) for p in items[:4]],
                'handoff': not bool(items)
            }

        except Exception as top_err:
            log.error(f"Critical error in assistant: {top_err}", exc_info=True)
            return {'reply': 'Підбираю необхідні матеріали з каталогу:', 'products': [], 'handoff': True}

    async def generate(self, message, history, items):
        facts = [
            {
                'id': str(getattr(p, 'id', '')).strip(),
                'name': str(getattr(p, 'name', '')),
                'description': str(getattr(p, 'description', '') or '')[:120]
            }
            for p in items[:3]
        ]

        payload = {
            'facts': facts,
            'message': redact(str(message))
        }

        # Моделі для обробки
        cfg_model = (getattr(self.settings, 'gemini_model', '') or '').strip().replace('models/', '')
        models_to_try = [cfg_model] if cfg_model and cfg_model not in ('gemini-3.8-flash',) else []
        for m in ['gemini-2.5-flash', 'gemini-2.5-flash-lite']:
            if m not in models_to_try:
                models_to_try.append(m)

        body = {
            'systemInstruction': {'parts': [{'text': SYSTEM}]},
            'contents': [{'role': 'user', 'parts': [{'text': json.dumps(payload, ensure_ascii=False)}]}],
            'generationConfig': {
                'temperature': 0.2,
                'maxOutputTokens': 350,
                'responseMimeType': 'application/json'
            },
            'safetySettings': SAFETY_SETTINGS
        }

        for model_name in models_to_try:
            try:
                async with self.semaphore:
                    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent'
                    response = await self.http_client.post(
                        url,
                        headers={'x-goog-api-key': self.settings.gemini_key},
                        json=body
                    )
                    
                    if response.status_code != 200:
                        log.warning(f"Gemini API [{model_name}] HTTP {response.status_code}: {response.text[:150]}")
                        continue

                    data = response.json()
                    candidates = data.get('candidates', [])
                    if not candidates:
                        log.warning(f"Gemini [{model_name}] returned empty candidates (promptFeedback: {data.get('promptFeedback')})")
                        continue

                    candidate = candidates[0]
                    parts = candidate.get('content', {}).get('parts', [])
                    raw_text = ''.join(p.get('text', '') for p in parts).strip()
                    if not raw_text:
                        log.warning(f"Gemini [{model_name}] returned empty text (finishReason: {candidate.get('finishReason')})")
                        continue

                    raw_text = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw_text)
                    
                    try:
                        result = json.loads(raw_text, strict=False)
                        text_reply = str(result.get('text', '')).strip()
                        ids = [str(x).strip() for x in result.get('product_ids', []) if x]
                        handoff = bool(result.get('handoff', False))
                    except json.JSONDecodeError:
                        log.warning(f"Model [{model_name}] output non-JSON text. Falling back to raw response.")
                        text_reply = raw_text
                        ids = []
                        handoff = False

                    allowed = {str(getattr(p, 'id', '')).strip() for p in items}

                    if text_reply:
                        return text_reply, [i for i in ids if i in allowed], handoff

            except httpx.TimeoutException:
                log.warning(f"Gemini timeout on model {model_name}")
                continue
            except Exception as e:
                log.warning(f"Error during Gemini call [{model_name}]: {e}")
                continue

        return None
