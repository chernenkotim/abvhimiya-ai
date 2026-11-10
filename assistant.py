"""Grounded consultation with contextual retrieval, SKU resolution, and SalesDrive order lookup."""
import asyncio
import json
import logging
import re
import httpx

from catalog import tokens
from privacy import redact

log = logging.getLogger(__name__)

SYSTEM = '''Ти провідний AI-консультант та технолог інтернет-магазину будівельної хімії та 3D-друку ABV Хімія (abvhimiya.com.ua).
Спілкуйся природною, професійною українською мовою, стисло (не більше 3-4 абзаців), доброзичливо й експертно.

КОМЕРЦІЙНІ ПРАВИЛА ТА ПОРІВНЯННЯ:
1. Загальні технічні та хімічні знання:
   Якщо клієнт запитує про різницю між матеріалами (нефрас/уайт-спірит, типи герметиків, силіконів, епоксидні/поліуретанові суміші, філаменти PLA/PETG) або сфери їх застосування:
   - Надай чітке, експертне пояснення їхніх фізико-хімічних відмінностей.
   - Поясни простими словами, для якого завдання що краще обрати, щоб допомогти клієнту прийняти рішення про покупку.
2. Робота з каталогом (facts):
   - Характеристики товарів, застосування та сумісність базуй ВИКЛЮЧНО на наданих facts.
   - Обов'язково запропонуй позиції з facts, вказавши їхні id у масиві product_ids.
   - НІКОЛИ не вигадуй товари, яких немає у facts. Не пиши ціни (вони підтягуються автоматично).
3. Передача менеджеру (Handoff):
   - Встановлюй "handoff": true, якщо клієнт запитує про гуртові ціни, знижки, точні терміни/вартість доставки, або потребує індивідуального розрахунку кількості матеріалу на об'єкт.
4. Безпека:
   - Заборонено давати поради щодо небезпечного змішування хімії. Нагадуй про ЗІЗ та провітрювання при роботі з розчинниками.

ФОРМАТ ВІДПОВІДІ (Тільки JSON):
{"text":"твоя експертна та комерційна відповідь", "product_ids":["id1", "id2"], "handoff":false}
product_ids — від 1 до 4 id товарів із facts, які найкраще підходять. Завжди порожній список [], якщо facts порожні.'''

SALESDRIVE_STATUSES = {
    1: "Нова заявка", 2: "В обробці", 3: "Комплектується",
    4: "Відправлено", 5: "Отримано", 6: "Скасовано", 7: "Оплачено"
}

def normalize_code(val: str) -> str:
    """Очищення артикулів і кодів."""
    s = str(val or '').strip().casefold().replace('-', '').replace(' ', '').replace('_', '')
    trans = str.maketrans('асрокемітх', 'acpokemitx')
    return s.translate(trans)

def safe_public_product(p) -> dict:
    """Безпечне приведення товару до публічного формату картки."""
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
    """Пошук замовлення в SalesDrive CRM."""
    if not domain or not api_key:
        return {"found": False, "message": "SalesDrive не налаштовано."}

    clean_digits = re.sub(r'\D', '', target)
    query_str = clean_digits if clean_digits else target.strip()
    if not query_str: return {"found": False}

    clean_domain = domain if domain.endswith('.salesdrive.me') else f"{domain}.salesdrive.me"
    base_url = f"https://{clean_domain}/api/order/list/"
    headers = {"X-Api-Key": api_key, "Accept": "application/json", "User-Agent": "ABV-AI-Assistant/3.1"}

    urls_to_try = []
    if len(clean_digits) <= 8 and not clean_digits.startswith(('0', '380')):
        urls_to_try.extend([f"{base_url}?filter[externalId]={query_str}", f"{base_url}?filter[search]={query_str}"])
    else:
        phone_10 = clean_digits[-10:] if len(clean_digits) >= 10 else clean_digits
        urls_to_try.extend([f"{base_url}?filter[phone]={phone_10}", f"{base_url}?filter[phone]=38{phone_10}"])

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            for req_url in urls_to_try:
                res = await client.get(req_url, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    orders = data.get("data", []) or data.get("orders", [])
                    if orders:
                        m = orders[0]
                        internal_id, ext_id = m.get("id"), m.get("externalId") or m.get("id") or query_str
                        full_card_data = {}
                        if internal_id:
                            try:
                                single_res = await client.get(f"https://{clean_domain}/api/order/{internal_id}/", headers=headers)
                                if single_res.status_code == 200:
                                    single_json = single_res.json()
                                    full_card_data = single_json.get("data", {}) or single_json
                            except Exception: pass

                        combined_dump = json.dumps([m, full_card_data], ensure_ascii=False)
                        ttn_match = re.search(r'\b(204\d{11}|59\d{12}|1\d{13}|205\d{11})\b', combined_dump)
                        ttn = ttn_match.group(1) if ttn_match else ""

                        st = m.get("status") or full_card_data.get("status")
                        if isinstance(st, dict): st_name = st.get("text", "В обробці")
                        elif isinstance(st, str) and st: st_name = st
                        elif m.get("statusId") and m.get("statusId") in SALESDRIVE_STATUSES:
                            st_name = SALESDRIVE_STATUSES[m["statusId"]]
                        else: st_name = "В обробці"

                        order_date = m.get("orderTime") or full_card_data.get("orderTime") or ""
                        if order_date and "T" in str(order_date): order_date = str(order_date).split("T")[0]

                        return {"found": True, "order_id": ext_id, "status": st_name, "ttn": ttn, "date": order_date}
    except Exception as e:
        log.warning(f"SalesDrive request error: {e}")
    return {"found": False}

def retrieval_query(message, history):
    q_lower = (message or '').casefold()
    extra = ""
    if any(k in q_lower for k in ("мікрохвильов", "микроволнов", "печ")): extra = " ceramic мікрохвильових"
    elif "ванн" in q_lower: extra = " ceramic ванн"
    elif any(k in q_lower for k in ("гумов", "резинов")): extra = " гумова"
    elif any(k in q_lower for k in ("термо", "герметик", "високотемператур")): extra = " герметик термостійкий"
    elif any(k in q_lower for k in ("нефрас", "уайт", "вайт", "розчинник", "растворител", "калош", "бр-2", "бр2")):
        extra = " нефрас уайт-спірит калоша розчинник"
    return f"{message} {extra}".strip()

def fallback(message, items):
    if items:
        q = (message or '').casefold()
        question = 'Для якої саме поверхні або завдання підбираєте матеріал?'
        if 'ванн' in q: question = 'З якого матеріалу ванна та чи є на ній старе покриття?'
        elif 'pla' in q: question = 'Який колір і фасування PLA вам потрібні?'
        elif 'філамент' in q or '3d' in q: question = 'Який тип пластику й колір потрібні?'
        elif re.search(r'мікрохвильов|микроволнов', q): question = 'Потрібно пофарбувати корпус ззовні чи внутрішню камеру?'
        elif re.search(r'гумов|резинов', q): question = 'Для якої поверхні потрібна гумова фарба (дах, фасад, цоколь чи бетон)?'
        elif re.search(r'термо|піч|камін|герметик', q): question = 'Яка максимальна температура очікується та де саме будете герметизувати?'
        elif re.search(r'нефрас|уайт|розчинник|знежир', q): question = 'Вам потрібен розчинник для розведення фарби чи для якісного знежирення деталей?'
        elif re.search(r'метал|дерев|бетон|пласт|скл|стін|підлог', q): question = 'Це роботи всередині приміщення чи на вулиці?'
        return f'Знайшов відповідні товари за вашим запитом. {question}'
    return 'Не знайшов точного збігу в каталозі за цим запитом. Уточніть назву або артикул товару чи залиште заявку менеджеру.'

class Assistant:
    def __init__(self, settings, store, catalog):
        self.settings, self.store, self.catalog = settings, store, catalog
        self.semaphore = asyncio.Semaphore(10) # Збільшено для кращого паралелізму
        # Перевикористання клієнта для швидких з'єднань
        limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)
        self.http_client = httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(connect=5.0, read=25.0, write=5.0, pool=5.0))

    async def aclose(self):
        """Закриття HTTP клієнта при вимиканні сервісу."""
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

            # 1. Захист від небезпечного змішування
            if any(w in q for w in ('зміша', 'смеша')) and any(w in q for w in ('хлор', 'відбілювач', 'аміак', 'кислот')):
                return {'reply': 'Не змішуйте ці засоби: можливе виділення небезпечних газів. Дотримуйтеся інструкцій виробника.', 'products': [], 'handoff': True}

            # 2. ПЕРЕВІРКА АРТИКУЛІВ ТОВАРІВ
            is_article_intent = bool(re.search(r'(?:артикул|арт|код|тов|модел|позиці)\b', q, re.IGNORECASE))
            has_letters_sku = bool(re.search(r'[a-zA-Zа-яА-ЯіїєґІЇЄҐ_-]+[0-9]+|[0-9]+[a-zA-Zа-яА-ЯіїєґІЇЄҐ_-]+', q))
            
            if is_article_intent or (has_letters_sku and not re.search(r'\b(замовлен|заявк|статус|ттн)\b', q)):
                art_match = re.search(r'(?:артикул|арт|код|тов)?[:\s-]*([a-zA-Z0-9а-яА-ЯіїєґІЇЄҐ_-]{3,20})', q)
                if art_match:
                    candidate_norm = normalize_code(art_match.group(1))
                    if candidate_norm not in ('товару', 'замовлення', 'заявки', 'статус'):
                        found_by_art = [p for p in self._get_all_products() if candidate_norm in normalize_code(getattr(p, 'id', '')) or candidate_norm in normalize_code(getattr(p, 'vendor_code', ''))]
                        if found_by_art:
                            return {'reply': f"Знайшов товар за артикулом: {getattr(found_by_art[0], 'name', 'Товар')}.", 'products': [safe_public_product(p) for p in found_by_art[:3]], 'handoff': False}

            # 3. ПЕРЕВІРКА СТАТУСУ ЗАМОВЛЕННЯ CRM
            phone_match = re.search(r'(?:\+?38)?0\d{9}', q)
            is_status_req = bool(re.search(r'статус|ттн|де (?:моє )?замовлення|де посилка', q))
            
            search_id = None
            if phone_match and is_status_req: search_id = phone_match.group(0)
            else:
                order_match = re.search(r'(?:замовленн\w*|заявк\w*|№|#)\s*([1-9]\d{3,7})\b', q, re.IGNORECASE)
                if order_match: search_id = order_match.group(1)

            if search_id:
                info = await fetch_salesdrive_status(self.settings.salesdrive_domain, self.settings.salesdrive_key, search_id)
                if info.get("found"):
                    ttn_part = f"ТТН: {info['ttn']}." if info.get("ttn") else "ТТН ще формується на складі."
                    return {'reply': f"Замовлення №{info['order_id']}:\n• Статус: {info['status']}\n• {ttn_part}", 'products': [], 'handoff': False}
                return {'reply': f"Замовлення '{search_id}' не знайдено. Зверніться до менеджера.", 'products': [], 'handoff': True}

            if is_status_req and not search_id:
                return {'reply': "Вкажіть номер замовлення (наприклад, 19139) або ваш телефон для перевірки статусу.", 'products': [], 'handoff': False}

            # 4. Ввічливі фрази
            if re.fullmatch(r'(?:дякую|дуже дякую|спасибі|ок|добре)[!.\s]*', q):
                return {'reply': 'Будь ласка! Звертайтеся, якщо потрібна допомога з вибором.', 'products': [], 'handoff': False}
            if re.search(r'менеджер|оператор|зателефон|зв[’\x27]яз', q):
                return {'reply': 'Залиште свій контакт у формі, і менеджер оперативно зв’яжеться з вами.', 'products': [], 'handoff': True}

            # 5. ПОШУК ТОВАРІВ
            items, query_text = [], retrieval_query(message, history)
            try:
                if hasattr(self.catalog, 'search') and callable(self.catalog.search):
                    items = self.catalog.search(query_text)
            except Exception as search_err:
                log.warning(f"Catalog search error: {search_err}")

            if not items:
                keywords = [w for w in re.findall(r'[a-zA-Zа-яА-ЯіїєґІЇЄҐ0-9]{4,}', q) if w not in ('потрібен', 'матеріал', 'різниця', 'чому')]
                if keywords:
                    for p in self._get_all_products():
                        text_to_search = (str(getattr(p, 'name', '')) + " " + str(getattr(p, 'description', ''))).casefold()
                        if any(k in text_to_search for k in keywords):
                            items.append(p)
                            if len(items) >= 6: break

            reply = fallback(message, items)
            handoff = not bool(items)

            # 6. AI-ГЕНЕРАЦІЯ
            if getattr(self.settings, 'gemini_key', None):
                try:
                    if self.store.allow([('ai:global', self.settings.daily_ai_calls, 86400)]):
                        result = await self.generate(message, history, items)
                        if result:
                            ai_reply, ids, ai_handoff = result
                            matched_items = [p for p in items if str(getattr(p, 'id', '')).strip() in ids]
                            items = matched_items if matched_items else items[:4]
                            reply, handoff = ai_reply, ai_handoff
                except Exception as ai_e:
                    log.warning(f"AI generation call failed: {ai_e}")

            return {'reply': reply, 'products': [safe_public_product(p) for p in items[:4]], 'handoff': handoff}

        except Exception as top_err:
            log.error(f"Critical error in assistant.answer: {top_err}")
            return {'reply': 'Підбираю матеріали. Перегляньте варіанти або залишіть заявку:', 'products': [], 'handoff': True}

    async def generate(self, message, history, items):
        facts = [{'id': str(getattr(p, 'id', '')).strip(), 'name': str(getattr(p, 'name', '')), 'description': str(getattr(p, 'description', '') or '')[:160]} for p in items[:4]]
        
        payload = {
            'facts': facts,
            'history': [{'role': str(h.get('role', 'user')), 'text': redact(str(h.get('text', '')))[:200]} for h in history[-2:]],
            'message': redact(str(message))
        }

        # Нормалізація моделі
        configured_model = (getattr(self.settings, 'gemini_model', '') or 'gemini-3.8-flash').strip().replace('models/', '')
        models_to_try = [configured_model] if configured_model else []
        for m in ['gemini-3.8-flash', 'gemini-2.5-flash']:
            if m not in models_to_try: models_to_try.append(m)

        body = {
            'systemInstruction': {'parts': [{'text': SYSTEM}]},
            'contents': [{'role': 'user', 'parts': [{'text': json.dumps(payload, ensure_ascii=False)}]}],
            'generationConfig': {'temperature': 0.1, 'maxOutputTokens': 400, 'responseMimeType': 'application/json'}
        }

        max_retries = 2 
        for model_name in models_to_try:
            for attempt in range(max_retries + 1):
                try:
                    async with self.semaphore:
                        response = await self.http_client.post(
                            f'https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent',
                            headers={'x-goog-api-key': self.settings.gemini_key},
                            json=body
                        )

                        if response.status_code == 404:
                            log.warning(f"Model {model_name} 404, switching...")
                            break 

                        if response.status_code in (503, 429, 500):
                            if attempt < max_retries:
                                await asyncio.sleep(1.5 * (attempt + 1))
                                continue
                            break

                        if response.status_code != 200: break

                        data = response.json()
                        raw_text = ''.join(p.get('text', '') for p in data.get('candidates', [{}])[0].get('content', {}).get('parts', [])).strip()
                        if not raw_text: break

                        raw_text = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw_text)
                        result = json.loads(raw_text, strict=False)
                        
                        ids = [str(x).strip() for x in result.get('product_ids', []) if x]
                        allowed = {str(getattr(p, 'id', '')).strip() for p in items}
                        return str(result.get('text', '')).strip(), [i for i in ids if i in allowed], bool(result.get('handoff', False))
                        
                except Exception as e:
                    if attempt < max_retries:
                        await asyncio.sleep(1.0)
                        continue
                    break
        return None
