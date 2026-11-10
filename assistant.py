"""Grounded consultation with contextual retrieval, SKU resolution, and SalesDrive order lookup."""
import asyncio
import json
import logging
import re
import httpx

from catalog import tokens
from privacy import redact

log = logging.getLogger(__name__)

SYSTEM = '''Ти AI-консультант інтернет-магазину будівельної хімії та 3D-друку ABV Хімія (abvhimiya.com.ua).
Спілкуйся природною українською, стисло й по суті.

ПРАВИЛА РОБОТИ З ТОВАРАМИ:
Твоє завдання: допомогти вибрати товар і відповісти на запитання клієнта.
Єдине джерело тверджень про конкретні товари — надані facts. Не вигадуй характеристик, сумісності,
пропорцій змішування чи витрати, якщо їх немає в facts.
Ціни, посилання і наявність показуються окремими серверними картками: не дублюй ціни вручну в тексті.
Якщо клієнт шукає специфічні покриття (ванни, радіатори, мікрохвильові печі, гумова фарба, термостійкі матеріали), пропонуй лише ті варіанти з facts, у яких це зазначено в назві чи описі.
Якщо в facts є відповідні товари — обов'язково коротко опиши їх застосування і поверни їхні id у product_ids.
Не давай порад зі змішування побутової хімії чи іншого небезпечного застосування.

ФОРМАТ ВІДПОВІДІ:
Поверни строго валідний JSON: {"text":"відповідь", "product_ids":["id"], "handoff":false}.
product_ids — лише id товарів із facts, які дійсно підходять (від 1 до 4 id).
handoff=true встановлюй тоді, коли потрібне пряме втручання менеджера або точних характеристик немає.'''

SALESDRIVE_STATUSES = {
    1: "Нова заявка",
    2: "В обробці",
    3: "Комплектується",
    4: "Відправлено",
    5: "Отримано",
    6: "Скасовано",
    7: "Оплачено"
}

def normalize_code(val: str) -> str:
    """Очищення артикулів і кодів (регістр, дефіси, схожі кириличні літери)."""
    s = str(val).strip().casefold().replace('-', '').replace(' ', '').replace('_', '')
    trans = str.maketrans('асрокемітх', 'acpokemitx')
    return s.translate(trans)

def safe_public_product(p) -> dict:
    """Безпечне перетворення об'єкта товару на словник для клієнта."""
    try:
        if hasattr(p, 'public') and callable(p.public):
            return p.public()
    except Exception:
        pass
    return {
        'id': str(getattr(p, 'id', '')),
        'name': str(getattr(p, 'name', 'Товар')),
        'price': getattr(p, 'price', None),
        'available': getattr(p, 'available', True),
        'url': getattr(p, 'url', None),
        'image': getattr(p, 'image', None),
        'vendor_code': getattr(p, 'vendor_code', '')
    }

async def fetch_salesdrive_status(domain: str, api_key: str, target: str) -> dict:
    """Пошук замовлення в SalesDrive CRM суворо за Зовнішнім номером заявки з сайту або телефоном."""
    if not domain or not api_key:
        log.warning("SalesDrive domain or api_key is missing in settings")
        return {"found": False, "message": "SalesDrive не налаштовано."}

    clean_digits = re.sub(r'\D', '', target)
    query_str = clean_digits if clean_digits else target.strip()
    if not query_str:
        return {"found": False, "message": "Немає даних для пошуку."}

    clean_domain = domain if domain.endswith('.salesdrive.me') else f"{domain}.salesdrive.me"
    base_url = f"https://{clean_domain}/api/order/list/"
    headers = {
        "X-Api-Key": api_key,
        "Accept": "application/json",
        "User-Agent": "ABV-AI-Assistant/3.0"
    }

    urls_to_try = []
    if len(clean_digits) <= 8 and not clean_digits.startswith(('0', '380')):
        urls_to_try.append(f"{base_url}?filter[externalId]={query_str}")
        urls_to_try.append(f"{base_url}?filter[externalId][]={query_str}")
        urls_to_try.append(f"{base_url}?filter[search]={query_str}")
        urls_to_try.append(f"{base_url}?idFrom={query_str}&idTo={query_str}")
    else:
        phone_10 = clean_digits[-10:] if len(clean_digits) >= 10 else clean_digits
        urls_to_try.append(f"{base_url}?filter[phone]={phone_10}")
        urls_to_try.append(f"{base_url}?filter[phone]=38{phone_10}")
        urls_to_try.append(f"{base_url}?filter[search]={phone_10}")

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            for req_url in urls_to_try:
                res = await client.get(req_url, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    orders = data.get("data", []) or data.get("orders", [])
                    if orders:
                        m = orders[0]
                        internal_id = m.get("id")
                        ext_id = m.get("externalId") or internal_id or query_str

                        full_card_data = {}
                        if internal_id:
                            try:
                                single_res = await client.get(f"https://{clean_domain}/api/order/{internal_id}/", headers=headers)
                                if single_res.status_code == 200:
                                    single_json = single_res.json()
                                    full_card_data = single_json.get("data", {}) or single_json
                            except Exception as sub_e:
                                log.warning(f"Error fetching full order details: {sub_e}")

                        combined_dump = json.dumps([m, full_card_data], ensure_ascii=False)

                        ttn = ""
                        ttn_match = re.search(r'\b(204\d{11}|59\d{12}|1\d{13}|205\d{11})\b', combined_dump)
                        if ttn_match:
                            ttn = ttn_match.group(1)

                        st = m.get("status") or full_card_data.get("status")
                        if isinstance(st, dict):
                            st_name = st.get("text", "В обробці")
                        elif isinstance(st, str) and st:
                            st_name = st
                        elif m.get("statusId") and m.get("statusId") in SALESDRIVE_STATUSES:
                            st_name = SALESDRIVE_STATUSES[m["statusId"]]
                        else:
                            st_name = "В обробці"

                        order_date = m.get("orderTime") or m.get("orderDate") or full_card_data.get("orderTime") or ""
                        if order_date and "T" in str(order_date):
                            order_date = str(order_date).split("T")[0]

                        log.info(f"SalesDrive знайдено: ext_id={ext_id}, status={st_name}, ttn={ttn}")
                        return {
                            "found": True,
                            "order_id": ext_id,
                            "status": st_name,
                            "ttn": ttn,
                            "date": order_date
                        }
    except Exception as e:
        log.warning(f"SalesDrive request error: {e}")

    return {"found": False}

def retrieval_query(message, history):
    q_lower = message.casefold()
    extra = ""
    if any(k in q_lower for k in ("мікрохвильов", "микроволнов", "печ")):
        extra = " ceramic мікрохвильових"
    elif "ванн" in q_lower:
        extra = " ceramic ванн"
    elif any(k in q_lower for k in ("гумов", "резинов")):
        extra = " гумова"
    elif any(k in q_lower for k in ("термо", "герметик", "високотемператур")):
        extra = " термостійкий герметик"

    # Не роздуваємо запит десятками токенів, залишаємо чистий фокус
    return f"{message} {extra}".strip()

def fallback(message, items):
    if items:
        q = message.casefold()
        question = 'Для якої саме поверхні або завдання підбираєте матеріал?'
        if 'ванн' in q: question = 'З якого матеріалу ванна та чи є на ній старе покриття?'
        elif 'pla' in q: question = 'Який колір і фасування PLA вам потрібні?'
        elif 'філамент' in q or '3d' in q: question = 'Який тип пластику й колір потрібні?'
        elif re.search(r'мікрохвильов|микроволнов', q): question = 'Потрібно пофарбувати корпус ззовні чи внутрішню камеру?'
        elif re.search(r'гумов|резинов', q): question = 'Для якої поверхні потрібна гумова фарба (дах, фасад, цоколь чи бетон)?'
        elif re.search(r'термо|піч|камін|герметик', q): question = 'Яка максимальна температура очікується та де саме будете герметизувати?'
        elif re.search(r'метал|дерев|бетон|пласт|скл|стін|підлог', q): question = 'Це роботи всередині приміщення чи на вулиці?'
        return 'Знайшов відповідні товари за вашим запитом — перегляньте картки нижче. ' + question
    return 'Не знайшов точного збігу в каталозі за цим запитом. Уточніть назву або артикул товару чи залиште заявку менеджеру.'

class Assistant:
    def __init__(self, settings, store, catalog):
        self.settings, self.store, self.catalog = settings, store, catalog
        self.semaphore = asyncio.Semaphore(4)

    async def answer(self, message, history):
        try:
            q = str(message or '').casefold().strip()

            # 1. Захист від небезпечного змішування побутової хімії
            if any(w in q for w in ('зміша', 'смеша')) and any(w in q for w in ('хлор', 'відбілювач', 'аміак', 'кислот', 'bleach')):
                return {
                    'reply': 'Не змішуйте ці засоби: можливе виділення небезпечних газів. Дотримуйтеся інструкцій виробника. За різкого запаху провітріть приміщення, за погіршення самопочуття телефонуйте 103 або 112.',
                    'products': [],
                    'handoff': True
                }

            # 2. ПЕРЕВІРКА АРТИКУЛІВ ТОВАРІВ
            is_article_intent = bool(re.search(r'(?:артикул|арт|код|тов|модел|позиці)\b', q, re.IGNORECASE))
            has_letters_sku = bool(re.search(r'[a-zA-Zа-яА-ЯіїєґІЇЄҐ_-]+[0-9]+|[0-9]+[a-zA-Zа-яА-ЯіїєґІЇЄҐ_-]+', q))
            
            if is_article_intent or (has_letters_sku and not re.search(r'\b(замовлен|заявк|статус|ттн)\b', q)):
                art_match = re.search(r'(?:артикул|арт|код|тов)?[:\s-]*([a-zA-Z0-9а-яА-ЯіїєґІЇЄҐ_-]{3,20})', q)
                if art_match:
                    candidate_raw = art_match.group(1).strip()
                    candidate_norm = normalize_code(candidate_raw)
                    if candidate_raw.casefold() not in ('товару', 'товара', 'замовлення', 'заявки', 'статус'):
                        found_by_art = []
                        for p in self.catalog.products.values():
                            p_code_norm = normalize_code(getattr(p, 'vendor_code', '')) if getattr(p, 'vendor_code', None) else ''
                            p_id_norm = normalize_code(getattr(p, 'id', ''))
                            if candidate_norm in (p_id_norm, p_code_norm) or (len(candidate_norm) >= 4 and candidate_norm in p_code_norm):
                                found_by_art.append(p)
                        if found_by_art:
                            return {
                                'reply': f"Знайшов товар за артикулом/кодом: {found_by_art[0].name}. Перегляньте картку нижче:",
                                'products': [safe_public_product(p) for p in found_by_art[:3]],
                                'handoff': False
                            }

            # 3. ПЕРЕВІРКА СТАТУСУ ЗАМОВЛЕННЯ ТА ТТН У SALESDRIVE
            phone_match = re.search(r'(?:\+?38)?0\d{9}', q)
            is_status_req = bool(re.search(r'статус|ттн|де (?:моє )?замовлення|де посилка|відстеж|що з замовленн', q))
            
            last_bot_asked_order = False
            if history and history[-1].get('role') == 'assistant':
                last_text = history[-1].get('text', '').casefold()
                if any(phrase in last_text for phrase in ('номер вашого замовлення', 'номер заявки', 'перевірю статус')):
                    last_bot_asked_order = True

            order_match = None
            if not is_article_intent:
                order_match = re.search(r'(?:замовленн\w*|заявк\w*|номер\w*|статус\w*|№|#)\s*([1-9]\d{3,7})\b', q, re.IGNORECASE)
                if not order_match and (is_status_req or last_bot_asked_order or re.fullmatch(r'\d{4,8}', q)):
                    m = re.search(r'\b([1-9]\d{3,7})\b', q)
                    if m:
                        order_match = m

            search_id = None
            if order_match:
                search_id = order_match.group(1)
            elif phone_match and (is_status_req or last_bot_asked_order or re.fullmatch(r'(?:\+?38)?0\d{9}', q)):
                search_id = phone_match.group(0)
            elif is_status_req:
                for prev in reversed(history[-2:]):
                    prev_digits = re.search(r'\b([1-9]\d{3,7})\b', prev.get('text', ''))
                    if prev_digits:
                        search_id = prev_digits.group(1)
                        break

            if search_id:
                info = await fetch_salesdrive_status(
                    self.settings.salesdrive_domain,
                    self.settings.salesdrive_key,
                    search_id
                )
                if info.get("found"):
                    ttn_part = (
                        f"Номер ТТН Нової Пошти: {info['ttn']}. Відстежити посилку можна у додатку Нової Пошти."
                        if info.get("ttn")
                        else "Номер ТТН ще формується на складі та надійде вам у SMS після відправки."
                    )
                    msg = (
                        f"Інформація щодо замовлення №{info['order_id']}:\n"
                        f"• Поточний статус: {info['status']}\n"
                        f"• {ttn_part}"
                    )
                    return {'reply': msg, 'products': [], 'handoff': False}
                else:
                    return {
                        'reply': f"Замовлення за номером '{search_id}' не знайдено в системі SalesDrive. Перевірте, будь ласка, Зовнішній номер заявки з сайту або зверніться до менеджера.",
                        'products': [],
                        'handoff': True
                    }

            if is_status_req and not search_id:
                return {
                    'reply': "Вкажіть, будь ласка, номер вашого замовлення з сайту (наприклад, 19139) або контактний телефон, і я одразу перевірю статус та номер ТТН!",
                    'products': [],
                    'handoff': False
                }

            # 4. Ввічливі фрази та звернення до менеджера
            if re.fullmatch(r'(?:дякую|дуже дякую|спасибі|ок|добре|зрозуміло)[!.\s]*', q):
                return {'reply': 'Будь ласка! Якщо потрібні інші матеріали чи консультація — звертайтеся.', 'products': [], 'handoff': False}

            if re.search(r'менеджер|оператор|людин[аоу]|зателефон|передзвон|зв[’\x27]яз', q):
                return {'reply': 'Залиште свій контакт у формі — менеджер отримає заявку та оперативно зв’яжеться з вами.', 'products': [], 'handoff': True}

            if re.search(r'^(привіт|вітаю|добрий (день|вечір|ранок)|доброго (дня|вечора|ранку))[!?.\s]*$', q):
                return {'reply': 'Вітаю! Допоможу підібрати будівельну хімію або пластик для 3D-друку з каталогу ABV Хімія. Що саме ви шукаєте?', 'products': [], 'handoff': False}

            if not self.catalog.fresh:
                return {'reply': 'Каталог зараз оновлюється. Залиште заявку у формі, і менеджер оперативно підкаже наявність та ціну.', 'products': [], 'handoff': True}

            # 5. Пошук по каталогу товарів
            query_text = retrieval_query(message, history)
            items = []
            try:
                items = self.catalog.search(query_text)
            except Exception as search_err:
                log.warning(f"Catalog search error: {search_err}")

            # Резервний пошук за входженням слів у назву (якщо основний індекс дав 0 товарів)
            if not items and hasattr(self.catalog, 'products') and self.catalog.products:
                keywords = [w for w in re.findall(r'[a-zA-Zа-яА-ЯіїєґІЇЄҐ0-9]{4,}', q) if w not in ('потрібен', 'треба', 'порадьте', 'шукаю')]
                if keywords:
                    for p in self.catalog.products.values():
                        p_name_lower = str(getattr(p, 'name', '')).casefold()
                        if any(k in p_name_lower for k in keywords):
                            items.append(p)
                        if len(items) >= 4:
                            break

            technical = bool(re.search(r'сумісн|витрат|пропорц|тверд|температур|скільки|підійде|підходить|можна', q))
            reply, handoff = fallback(message, items), not bool(items)

            if technical and items:
                reply = 'Щоб дати точну технічну рекомендацію, орієнтуйтеся на інструкцію виробника конкретного матеріалу. Нижче наведено відповідні позиції з каталогу; якщо потрібне додаткове узгодження, допоможе менеджер.'
                handoff = True

            # Безпечний виклик генерації AI
            if items and self.settings.gemini_key:
                try:
                    if self.store.allow([('ai:global', self.settings.daily_ai_calls, 86400)]):
                        result = await self.generate(message, history, items)
                        if result:
                            ai_reply, ids, ai_handoff = result
                            matched_items = [p for p in items if getattr(p, 'id', None) in ids]
                            items = matched_items if matched_items else items[:3]
                            reply = ai_reply
                            handoff = ai_handoff
                except Exception as e:
                    log.warning(f"AI response generation failed, falling back to catalog template: {e}")

            return {'reply': reply, 'products': [safe_public_product(p) for p in items], 'handoff': handoff}

        except Exception as top_err:
            log.error(f"Top-level assistant error: {top_err}", exc_info=True)
            return {
                'reply': 'Не вдалося завантажити картки за цим запитом. Спробуйте уточнити назву або зверніться до менеджера через форму.',
                'products': [],
                'handoff': True
            }

    async def generate(self, message, history, items):
        facts = []
        for p in items[:4]:
            attrs = getattr(p, 'attributes', {})
            attrs_dict = attrs if isinstance(attrs, dict) else {}
            facts.append({
                'id': str(getattr(p, 'id', '')),
                'name': str(getattr(p, 'name', '')),
                'description': str(getattr(p, 'description', '') or '')[:1400],
                'attributes': {str(k)[:60]: str(v)[:250] for k, v in list(attrs_dict.items())[:25]}
            })

        payload = {
            'facts': facts,
            'history': [{'role': str(h.get('role', 'user')), 'text': redact(str(h.get('text', '')))[:600]} for h in history[-6:]],
            'message': redact(str(message))
        }

        model_name = (self.settings.gemini_model or 'gemini-2.5-flash').strip()
        if not re.fullmatch(r'[A-Za-z0-9._-]+', model_name):
            model_name = 'gemini-2.5-flash'

        body = {
            'systemInstruction': {'parts': [{'text': SYSTEM}]},
            'contents': [{'role': 'user', 'parts': [{'text': json.dumps(payload, ensure_ascii=False)}]}],
            'generationConfig': {
                'temperature': 0.1,
                'maxOutputTokens': 800,
                'responseMimeType': 'application/json'
            }
        }

        try:
            async with asyncio.timeout(12):
                async with self.semaphore, httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                    response = await client.post(
                        f'https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent',
                        headers={'x-goog-api-key': self.settings.gemini_key},
                        json=body
                    )
                    if response.status_code != 200:
                        log.warning(f"Gemini API returned status {response.status_code}: {response.text}")
                        return None

                    data = response.json()
                    candidates = data.get('candidates', [])
                    if not candidates:
                        return None

                    parts = candidates[0].get('content', {}).get('parts', [])
                    raw_text = ''.join(p.get('text', '') for p in parts).strip()
                    if not raw_text:
                        return None

                    if raw_text.startswith('```'):
                        raw_text = re.sub(r'^```(?:json)?\s*', '', raw_text)
                        raw_text = re.sub(r'\s*```$', '', raw_text)

                    result = json.loads(raw_text, strict=False)
                    text = str(result.get('text', '')).strip()
                    ids = result.get('product_ids', [])
                    allowed = {getattr(p, 'id', None) for p in items}

                    if not text or not isinstance(ids, list):
                        return None

                    return text, [i for i in ids if i in allowed], bool(result.get('handoff', False))
        except Exception as e:
            log.warning(f'AI model response unavailable, using catalog fallback: {e}')
            return None
