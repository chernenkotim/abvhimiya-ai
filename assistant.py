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
Якщо потрібної фарби/клею саме з таким вузьким маркуванням немає, але є універсальні емалі, термостійкі фарби або відповідні герметики з facts — запропонуй їх із зазначенням їхнього призначення з опису.
Якщо технічних даних бракує — скажи про це та запропонуй допомогу менеджера.
Не давай порад зі змішування побутової хімії чи іншого небезпечного застосування.

ФОРМАТ ВІДПОВІДІ:
Поверни строго валідний JSON: {"text":"відповідь", "product_ids":["id"], "handoff":false}.
product_ids — лише id товарів із facts, які дійсно підходять.
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

async def fetch_salesdrive_status(domain: str, api_key: str, target: str) -> dict:
    """Пошук замовлення в SalesDrive CRM за зовнішнім номером заявки (Хорошоп) або телефоном."""
    if not domain or not api_key:
        return {"found": False, "message": "SalesDrive не налаштовано."}

    clean_digits = re.sub(r'\D', '', target)
    query_str = clean_digits if clean_digits else target.strip()
    if not query_str:
        return {"found": False, "message": "Немає даних для пошуку."}

    clean_domain = domain if domain.endswith('.salesdrive.me') else f"{domain}.salesdrive.me"
    url = f"https://{clean_domain}/api/order/list/"
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}

    # Пріоритет: повнотекстовий пошук за Зовнішнім номером заявки з сайту та телефоном
    queries = [
        {"filter[search]": query_str},
        {"filter[search]": query_str[-10:] if len(clean_digits) >= 10 else query_str}
    ]
    if len(clean_digits) <= 8 and not clean_digits.startswith(('0', '380')):
        queries.append({"filter[externalId]": clean_digits})
        queries.append({"idFrom": int(clean_digits), "idTo": int(clean_digits)})
    else:
        queries.append({"filter[phone]": clean_digits[-10:]})

    try:
        async with httpx.AsyncClient(timeout=9, follow_redirects=True) as client:
            for params in queries:
                res = await client.get(url, params=params, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    orders = data.get("data", []) or data.get("orders", [])
                    if orders:
                        m = orders[0]
                        ext_id = m.get("externalId") or m.get("id") or query_str
                        
                        st = m.get("status")
                        if isinstance(st, dict):
                            st_name = st.get("text", "В обробці")
                        elif isinstance(st, str) and st:
                            st_name = st
                        elif m.get("statusId") and m.get("statusId") in SALESDRIVE_STATUSES:
                            st_name = SALESDRIVE_STATUSES[m["statusId"]]
                        else:
                            st_name = "В обробці"

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
                            "order_id": ext_id,
                            "status": st_name,
                            "ttn": ttn,
                            "date": order_date
                        }
    except Exception as e:
        log.warning(f"SalesDrive request error: {e}")
    return {"found": False}

def retrieval_query(message, history):
    # Якщо питають про побутову техніку/мікрохвильовку, підмішуємо емаль і термостійку фарбу для кращого пошуку
    q_lower = message.casefold()
    extra = ""
    if any(k in q_lower for k in ("мікрохвильов", "микроволнов", "піч", "печ")):
        extra = " емаль термостійка фарба метал"

    current = tokens(message + extra)
    context = [h['text'] for h in history[-8:] if h['role'] == 'user'][-3:]
    if len(current) <= 5 and context:
        return ' '.join(context + [message + extra])[-1200:]
    return (message + extra)

def fallback(message, items):
    if items:
        q = message.casefold()
        question = 'Для якої саме поверхні або завдання підбираєте матеріал?'
        if 'ванн' in q: question = 'З якого матеріалу ванна та чи є на ній старе покриття?'
        elif 'pla' in q: question = 'Який колір і фасування PLA вам потрібні?'
        elif 'філамент' in q or '3d' in q: question = 'Який тип пластику й колір потрібні?'
        elif re.search(r'мікрохвильов|микроволнов', q): question = 'Потрібно пофарбувати корпус ззовні чи внутрішню камеру?'
        elif re.search(r'метал|дерев|бетон|пласт|скл|стін|підлог', q): question = 'Це роботи всередині приміщення чи на вулиці?'
        return 'Знайшов відповідні товари за вашим запитом — перегляньте картки нижче. ' + question
    return 'Не знайшов точного збігу в каталозі за цим запитом. Уточніть назву або артикул товару чи залиште заявку менеджеру.'

class Assistant:
    def __init__(self, settings, store, catalog):
        self.settings, self.store, self.catalog = settings, store, catalog
        self.semaphore = asyncio.Semaphore(4)

    async def answer(self, message, history):
        q = message.casefold().strip()

        # 1. Захист від небезпечного змішування побутової хімії
        if any(w in q for w in ('зміша', 'смеша')) and any(w in q for w in ('хлор', 'відбілювач', 'аміак', 'кислот', 'bleach')):
            return {
                'reply': 'Не змішуйте ці засоби: можливе виділення небезпечних газів. Дотримуйтеся інструкцій виробника. За різкого запаху провітріть приміщення, за погіршення самопочуття телефонуйте 103 або 112.',
                'products': [],
                'handoff': True
            }

        # 2. ПЕРЕВІРКА АРТИКУЛІВ ТОВАРІВ (Пріоритет перед замовленнями)
        is_article_intent = bool(re.search(r'(?:артикул|арт|код|тов|модел|позиці)\b', q, re.IGNORECASE))
        if is_article_intent:
            art_match = re.search(r'(?:артикул|арт|код|тов)?[:\s-]*([a-zA-Z0-9а-яА-ЯіїєґІЇЄҐ_-]{3,20})', q)
            if art_match:
                candidate_code = art_match.group(1).strip().casefold()
                if candidate_code not in ('товару', 'товара', 'замовлення', 'заявки'):
                    found_by_art = [
                        p for p in self.catalog.products.values()
                        if candidate_code in p.vendor_code.casefold() or candidate_code in p.id.casefold()
                    ]
                    if found_by_art:
                        return {
                            'reply': f"Знайшов товар за артикулом/кодом: {found_by_art[0].name}. Перегляньте картку нижче:",
                            'products': [p.public() for p in found_by_art[:3]],
                            'handoff': False
                        }

        # 3. ПЕРЕВІРКА СТАТУСУ ЗАМОВЛЕННЯ ТА ТТН У SALESDRIVE
        phone_match = re.search(r'(?:\+?38)?0\d{9}', q)
        is_status_req = bool(re.search(r'статус|ттн|де (?:моє )?замовлення|де посилка|відстеж|що з замовленн', q))
        
        order_match = None
        if not is_article_intent:
            # Шукаємо явну вказівку номера замовлення (від 1000 до 99999999, не нулі 0000)
            order_match = re.search(r'(?:замовленн\w*|заявк\w*|номер\w*|статус\w*|№|#)\s*([1-9]\d{3,7})\b', q, re.IGNORECASE)
            if not order_match and is_status_req:
                m = re.search(r'\b([1-9]\d{3,7})\b', q)
                if m:
                    order_match = m

        search_id = None
        if order_match:
            search_id = order_match.group(1)
        elif phone_match and is_status_req:
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
                    f"Номер ТТН Нової Пошти: {info['ttn']}. Відстежити посилку можна у мобільному додатку Нової Пошти."
                    if info.get("ttn")
                    else "Номер ТТН ще формується на складі та надійде вам у SMS після відправлення."
                )
                msg = (
                    f"Інформація щодо замовлення №{info['order_id']}:\n"
                    f"• Поточний статус: {info['status']}\n"
                    f"• {ttn_part}"
                )
                return {'reply': msg, 'products': [], 'handoff': False}
            else:
                return {
                    'reply': f"Замовлення за номером '{search_id}' не знайдено в системі SalesDrive. Перевірте номер замовлення або зверніться до менеджера.",
                    'products': [],
                    'handoff': True
                }

        if is_status_req and not search_id:
            return {
                'reply': "Вкажіть, будь ласка, номер вашого замовлення з сайту (наприклад, 19138) або контактний телефон, і я одразу перевірю статус та номер ТТН!",
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
        items = self.catalog.search(query_text)

        technical = bool(re.search(r'сумісн|витрат|пропорц|тверд|температур|скільки|підійде|підходить|можна', q))
        reply, handoff = fallback(message, items), not bool(items)

        if technical:
            reply = 'Щоб дати точну технічну рекомендацію, орієнтуйтеся на інструкцію виробника конкретного матеріалу. Нижче наведено відповідні позиції з каталогу; якщо потрібне додаткове узгодження, допоможе менеджер.'
            handoff = True

        used_ai = False
        if items and self.settings.gemini_key and self.store.allow([('ai:global', self.settings.daily_ai_calls, 86400)]):
            result = await self.generate(message, history, items)
            if result:
                reply, ids, handoff = result
                # Залишаємо відібрані моделлю товари або перші відповідні з каталогу
                matched_items = [p for p in items if p.id in ids]
                items = matched_items if matched_items else items[:3]
                used_ai = True

        return {'reply': reply, 'products': [p.public() for p in items], 'handoff': handoff}

    async def generate(self, message, history, items):
        if not re.fullmatch(r'[A-Za-z0-9._-]+', self.settings.gemini_model):
            return None
        facts = [{'id': p.id, 'name': p.name, 'description': p.description[:1600], 'attributes': p.attributes} for p in items]
        payload = {
            'facts': facts,
            'history': [{'role': h['role'], 'text': redact(h['text'])[:700]} for h in history[-8:]],
            'message': redact(message)
        }
        body = {
            'systemInstruction': {'parts': [{'text': SYSTEM}]},
            'contents': [{'role': 'user', 'parts': [{'text': json.dumps(payload, ensure_ascii=False)}]}],
            'generationConfig': {'temperature': 0.1, 'maxOutputTokens': 800, 'responseMimeType': 'application/json'}
        }
        try:
            async with asyncio.timeout(12):
                async with self.semaphore, httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                    response = await client.post(
                        f'https://generativelanguage.googleapis.com/v1beta/models/{self.settings.gemini_model}:generateContent',
                        headers={'x-goog-api-key': self.settings.gemini_key},
                        json=body
                    )
                    response.raise_for_status()
                    data = response.json()
                    parts = data['candidates'][0]['content']['parts']
                    result = json.loads(''.join(p.get('text', '') for p in parts))
                    text = result['text'].strip()
                    ids = result['product_ids']
                    allowed = {p.id for p in items}
                    if not text or len(text) > 3000 or not isinstance(ids, list):
                        return None
                    return text, [i for i in ids if i in allowed], bool(result.get('handoff', False))
        except Exception as e:
            log.warning(f'AI model response unavailable, using catalog fallback: {e}')
            return None
