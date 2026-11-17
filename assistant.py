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

SYSTEM = '''Ти найкращий продавець-консультант інтернет-магазину ABV Хімія (abvhimiya.com.ua).
Спілкуйся українською мовою, дуже доброзичливо, як жива людина. Тексти пиши короткі (1-2 абзаци).

ТВОЇ ГОЛОВНІ ЗАВДАННЯ (ПРОДАЖІ ТА КОНСУЛЬТАЦІЯ):
1. ЗАДАВАЙ ПИТАННЯ: Якщо клієнт пише загально (наприклад "мені потрібна синя фарба" або "шукаю клей"), не видавай суху відповідь. Обов'язково постав 1 уточнююче питання в кінці: "Вам потрібна аерозольна фарба, емаль чи гумова? Для якої поверхні (метал, дерево, пластик)?" або "Де будуть проводитись роботи — на вулиці чи в приміщенні?".
2. РОБИ ДОПРОДАЖІ (Крос-сейл): Якщо клієнт вибрав фарбу чи емаль — завжди м'яко пропонуй додати розчинник (уайт-спірит/нефрас) для знежирення або розведення. Якщо бере епоксидку — нагадай про засоби захисту.
3. ОПЛАТА ТА РЕКВІЗИТИ: Якщо клієнт запитує реквізити, як оплатити, або питає номер картки, видай цей текст: "Ось реквізити для оплати:\nРахунок (IBAN): UA593052990000026004020124857\nОтримувач: ФОП Черненко Ольга Сергіївна (ЄДРПОУ 3449906025)\nБанк: АТ КБ «ПРИВАТБАНК»\nПризначення платежу: Оплата інтернет-замовлення № [вкажіть номер вашого замовлення].\nПісля оплати, будь ласка, напишіть нам, щоб ми якнайшвидше відправили товар!"
4. Базуй характеристики товарів ВИКЛЮЧНО на масиві facts. Вказуй id товарів у product_ids. Не пиши ціни в тексті.

АБСОЛЮТНО ВАЖЛИВО: Твоя відповідь має бути ТІЛЬКИ валідним JSON без зайвого тексту навколо:
{"text": "тут твій текст для клієнта", "product_ids": ["id1", "id2"], "handoff": false}'''

EXPERT_KNOWLEDGE = {
    "нефрас_уайт": (
        "Головна відмінність — у швидкості випаровування, залишку та призначенні:\n\n"
        "• **Нефрас (бензин-«калоша»)** — легкий розчинник. Випаровується майже миттєво, не залишає жирних слідів і плівки. "
        "Ідеальний для знежирення поверхонь перед склеюванням/фарбуванням, промивання електроніки та інструментів.\n\n"
        "• **Уайт-спірит** — важча фракція (гасова основа). Випаровується значно повільніше, має маслянисту основу й характерний запах. "
        "Його основне призначення — розведення алкідних емалей, масляних фарб, лаків та очищення пензлів.\n\n"
        "Для знежирення деталей обирайте **нефрас**, а для розведення фарби — **уайт-спірит**."
    ),
    "pla_petg": (
        "Різниця між пластиками для 3D-друку:\n\n"
        "• **PLA** — найпростіший у друці, екологічний, не дає усадки, твердий, але крихкий і плавиться вже при 55-60°C. Чудовий для декоративних моделей.\n\n"
        "• **PETG** — міцніший, ударостійкий, стійкий до ультрафіолету, вологи та температури до 75-80°C. Оптимальний для технічних деталей та речей на вулиці."
    ),
    "знежирення_ванни": (
        "Для знежирення поверхні ванни перед реставрацією або фарбуванням найкраще підходить **нефрас (бензин-«калоша»)** або **ізопропіловий спирт** — вони швидко випаровуються і не залишають жирної плівки.\n\n"
        "• Для **чавунної або сталевої емальованої ванни** також можна використати **ацетон** або **розчинник 646**.\n"
        "• Для **акрилової ванни** агресивні розчинники (ацетон, 646) використовувати не можна — підійде тільки м'яке знежирення ізопропіловим спиртом або мийним засобом.\n\n"
        "Підкажіть, у вас чавунна, сталева чи акрилова ванна, і якою фарбою/емаллю плануєте покривати?"
    )
}

SALESDRIVE_STATUSES = {
    1: "Нова заявка", 2: "В обробці", 3: "Комплектується",
    4: "Відправлено", 5: "Отримано", 6: "Скасовано", 7: "Оплачено"
}

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
    if not domain or not api_key: 
        return {"found": False}
    clean_digits = re.sub(r'\D', '', target)
    query_str = clean_digits if clean_digits else target.strip()
    if not query_str: 
        return {"found": False}

    clean_domain = domain if domain.endswith('.salesdrive.me') else f"{domain}.salesdrive.me"
    base_url = f"https://{clean_domain}/api/order/list/"
    headers = {"X-Api-Key": api_key, "Accept": "application/json", "User-Agent": "ABV-AI-Assistant/4.0"}

    is_order_search = len(clean_digits) <= 8 and not clean_digits.startswith(('0', '380'))
    
    if is_order_search:
        urls_to_try = [
            f"{base_url}?filter[externalId]={query_str}",
            f"{base_url}?filter[id]={query_str}",
            f"{base_url}?filter[search]={query_str}"
        ]
    else:
        phone_10 = clean_digits[-10:] if len(clean_digits) >= 10 else clean_digits
        urls_to_try = [
            f"{base_url}?filter[phone]={phone_10}",
            f"{base_url}?filter[phone]=38{phone_10}"
        ]

    try:
        async with httpx.AsyncClient(timeout=6, follow_redirects=True) as client:
            for req_url in urls_to_try:
                res = await client.get(req_url, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    orders = data.get("data", []) or data.get("orders", [])
                    if not orders:
                        continue

                    # 1. Будуємо словник статусів з meta-даних CRM (щоб знати точні назви ваших статусів)
                    status_map = {}
                    meta = data.get("meta") or {}
                    fields_meta = meta.get("fields") or {}
                    status_options = (fields_meta.get("statusId") or {}).get("options") or meta.get("statusId") or []
                    if isinstance(status_options, list):
                        for opt in status_options:
                            if isinstance(opt, dict) and "value" in opt:
                                status_map[str(opt["value"])] = str(opt.get("text") or opt.get("name") or "")
                    elif isinstance(status_options, dict):
                        for k, v in status_options.items():
                            status_map[str(k)] = v.get("text", str(v)) if isinstance(v, dict) else str(v)

                    m = None
                    if is_order_search:
                        for o in orders:
                            if query_str in (str(o.get("externalId", "")), str(o.get("id", ""))):
                                m = o
                                break
                        if not m:
                            continue
                    else:
                        m = orders[0]

                    ext_id = m.get("externalId") or m.get("id") or query_str

                    # 2. Витягуємо основний статус замовлення (через statusId або status)
                    st_name = ""
                    raw_st_id = m.get("statusId")
                    if raw_st_id is not None:
                        st_name = status_map.get(str(raw_st_id)) or SALESDRIVE_STATUSES.get(int(raw_st_id), "") if str(raw_st_id).isdigit() else str(raw_st_id)
                    
                    if not st_name:
                        st = m.get("status")
                        if isinstance(st, dict):
                            st_name = st.get("text") or st.get("name") or ""
                        elif isinstance(st, int):
                            st_name = status_map.get(str(st)) or SALESDRIVE_STATUSES.get(st, "")
                        elif isinstance(st, str) and st.strip():
                            st_name = status_map.get(st.strip()) or st.strip()

                    # 3. Шукаємо статус Нової Пошти ("В дорозі", "Прибув у відділення" тощо) у вкладених блоках доставки
                    np_status = ""
                    np_blocks = m.get("ord_novaposhta") or m.get("ord_delivery_data") or []
                    if isinstance(np_blocks, dict):
                        np_blocks = [np_blocks]
                    if isinstance(np_blocks, list) and np_blocks:
                        first_np = np_blocks[0] if isinstance(np_blocks[0], dict) else {}
                        np_status = (
                            first_np.get("statusText")
                            or first_np.get("status")
                            or first_np.get("trackingStatus")
                            or first_np.get("stateName")
                            or ""
                        )
                        if isinstance(np_status, dict):
                            np_status = np_status.get("text", "")

                    if not np_status:
                        delivery_st = m.get("deliveryStatus") or m.get("tracking_status") or ""
                        np_status = delivery_st.get("text", "") if isinstance(delivery_st, dict) else str(delivery_st)

                    # Якщо статусу замовлення немає, але є ТТН — ставимо "Відправлено" замість "В обробці"
                    dump = json.dumps(m, ensure_ascii=False)
                    ttn_match = re.search(r'\b(204\d{11}|59\d{12}|1\d{13}|205\d{11})\b', dump)
                    ttn = ttn_match.group(1) if ttn_match else ""

                    if not st_name:
                        st_name = "Відправлено" if ttn else "В обробці"

                    if np_status and str(np_status).strip() and str(np_status).strip().lower() not in st_name.lower():
                        st_name = f"{st_name} — НП: {str(np_status).strip()}"

                    return {
                        "found": True, 
                        "order_id": ext_id, 
                        "status": st_name, 
                        "ttn": ttn
                    }
    except Exception as e:
        log.warning(f"SalesDrive lookup error: {e}")

    return {"found": False}

class Assistant:
    def __init__(self, settings, store, catalog):
        self.settings, self.store, self.catalog = settings, store, catalog
        self.semaphore = asyncio.Semaphore(15)
        self.cache = {}
        self.http_client = httpx.AsyncClient(
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=40),
            timeout=httpx.Timeout(connect=3.0, read=12.0, write=3.0, pool=3.0)
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

            if q in self.cache:
                c_reply, c_ids = self.cache[q]
                c_items = [self.catalog.products[pid] for pid in c_ids if pid in getattr(self.catalog, 'products', {})]
                return {'reply': c_reply, 'products': [safe_public_product(p) for p in c_items], 'handoff': False}

            if any(w in q for w in ('зміша', 'смеша')) and any(w in q for w in ('хлор', 'відбілювач', 'аміак', 'кислот')):
                return {'reply': 'Не змішуйте ці засоби: можливе виділення небезпечних токсичних газів!', 'products': [], 'handoff': True}

            # СТАТУС ЗАМОВЛЕННЯ В CRM (SalesDrive)
            order_patterns = [
                r'(?:номер\s+)?(?:замовленн\w*|заявк\w*|посилк\w*|чек\w*|№|#)\s*[:№#\s]*([1-9]\d{3,7})\b',
                r'\b([1-9]\d{3,7})\b'
            ]
            
            search_id = None
            is_order_intent = bool(re.search(r'замовленн|заявк|статус|ттн|де посилка|де моє', q))
            
            phone_match = re.search(r'(?:\+?38)?0\d{9}', q)
            if phone_match and is_order_intent:
                search_id = phone_match.group(0)
            else:
                for pattern in order_patterns:
                    m = re.search(pattern, q, re.IGNORECASE)
                    if m:
                        if pattern == r'\b([1-9]\d{3,7})\b' and not is_order_intent and len(q.split()) > 2:
                            continue
                        search_id = m.group(1)
                        break

            if search_id:
                info = await fetch_salesdrive_status(
                    self.settings.salesdrive_domain, 
                    self.settings.salesdrive_key, 
                    search_id
                )
                if info.get("found"):
                    ttn_text = f"\n📦 ТТН Нової Пошти: {info['ttn']}" if info.get('ttn') else ""
                    return {
                        'reply': f"Замовлення №{info['order_id']}:\n• Статус: {info['status']}{ttn_text}",
                        'products': [],
                        'handoff': False
                    }
                return {
                    'reply': f"Замовлення за номером '{search_id}' не знайдено в базі. Будь ласка, перевірте правильність номера або залиште заявку менеджеру.",
                    'products': [],
                    'handoff': True
                }

            if is_order_intent and not search_id:
                return {
                    'reply': "Вкажіть номер замовлення (наприклад, 19178) або ваш номер телефону для перевірки статусу.",
                    'products': [],
                    'handoff': False
                }

            # МИТТЄВА БАЗА ЗНАНЬ
            items = []
            try:
                if hasattr(self.catalog, 'search') and callable(self.catalog.search):
                    items = self.catalog.search(q)
            except Exception as e:
                log.warning(f"Catalog search error: {e}")

            if 'нефрас' in q and ('уайт' in q or 'вайт' in q):
                return {'reply': EXPERT_KNOWLEDGE["нефрас_уайт"], 'products': [safe_public_product(p) for p in items[:4]], 'handoff': False}
            
            if 'pla' in q and 'petg' in q:
                return {'reply': EXPERT_KNOWLEDGE["pla_petg"], 'products': [safe_public_product(p) for p in items[:4]], 'handoff': False}

            if ('обезжир' in q or 'знежир' in q) and 'ванн' in q:
                return {'reply': EXPERT_KNOWLEDGE["знежирення_ванни"], 'products': [safe_public_product(p) for p in items[:4]], 'handoff': False}

            # ПЕРЕВІРКА АРТИКУЛІВ
            if re.search(r'(?:артикул|арт|код|тов|модел)\b', q, re.IGNORECASE) or re.search(r'[a-zA-Zа-яА-Яіїєґ_-]+\d+', q):
                art_match = re.search(r'([a-zA-Z0-9а-яА-Яіїєґ_-]{3,20})', q)
                if art_match:
                    cand = normalize_code(art_match.group(1))
                    found = [p for p in self._get_all_products() if cand in normalize_code(getattr(p, 'id', '')) or cand in normalize_code(getattr(p, 'vendor_code', ''))]
                    if found:
                        return {'reply': "Знайшов позицію за артикулом:", 'products': [safe_public_product(p) for p in found[:3]], 'handoff': False}

            if re.fullmatch(r'(?:дякую|спасибі|ок|добре)[!.\s]*', q):
                return {'reply': 'Будь ласка! Якщо потрібна допомога з вибором матеріалів — звертайтеся.', 'products': [], 'handoff': False}

            # FAST-PATH ВИКЛЮЧЕННЯ
            broad_terms = ('різниця', 'порівн', 'як', 'чому', 'що краще', 'фарба', 'фарбу', 'клей', 'емаль', 'лак', 'обезжир', 'знежир')
            if items and len(q.split()) <= 2 and not any(k in q for k in broad_terms):
                return {
                    'reply': 'Ось що знайшов у каталозі за вашим запитом:',
                    'products': [safe_public_product(p) for p in items[:4]],
                    'handoff': False
                }

            # ГЕНЕРАЦІЯ ЧЕРЕЗ ШІ (Gemini)
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
                'description': str(getattr(p, 'description', '') or '')[:150]
            }
            for p in items[:3]
        ]

        payload = {
            'facts': facts,
            'message': redact(str(message))
        }

        model_name = (getattr(self.settings, 'gemini_model', '') or 'gemini-3.8-flash').strip().replace('models/', '')

        # ПРИБРАНО responseMimeType: 'application/json' — саме цей параметр викликав зависання запитів на стороні Google
        body = {
            'systemInstruction': {'parts': [{'text': SYSTEM}]},
            'contents': [{'role': 'user', 'parts': [{'text': json.dumps(payload, ensure_ascii=False)}]}],
            'generationConfig': {
                'temperature': 0.2,
                'maxOutputTokens': 350,
                'thinkingConfig': {'thinkingBudget': 0}
            },
            'safetySettings': SAFETY_SETTINGS
        }

        deadline = time.monotonic() + 22.0

        for attempt in range(2):
            if time.monotonic() > deadline:
                break

            try:
                async with self.semaphore:
                    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent'
                    response = await self.http_client.post(
                        url,
                        headers={'x-goog-api-key': self.settings.gemini_key},
                        json=body
                    )
                    
                    if response.status_code in (503, 429):
                        log.warning(f"Google API overload ({response.status_code}) on {model_name} (attempt {attempt + 1}/2).")
                        if attempt == 0 and time.monotonic() + 1.5 < deadline:
                            await asyncio.sleep(1.0)
                            continue
                        break
                            
                    if response.status_code != 200:
                        log.warning(f"Gemini API [{model_name}] HTTP {response.status_code}: {response.text[:150]}")
                        break 

                    data = response.json()
                    candidates = data.get('candidates', [])
                    if not candidates:
                        break

                    candidate = candidates[0]
                    parts = candidate.get('content', {}).get('parts', [])
                    raw_text = ''.join(p.get('text', '') for p in parts).strip()
                    if not raw_text:
                        break

                    raw_text = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw_text)
                    
                    try:
                        result = json.loads(raw_text, strict=False)
                        text_reply = str(result.get('text', '')).strip()
                        ids = [str(x).strip() for x in result.get('product_ids', []) if x]
                        handoff = bool(result.get('handoff', False))
                    except json.JSONDecodeError:
                        text_reply = raw_text
                        ids = []
                        handoff = False

                    allowed = {str(getattr(p, 'id', '')).strip() for p in items}

                    if text_reply:
                        return text_reply, [i for i in ids if i in allowed], handoff

            except httpx.TimeoutException:
                log.warning(f"Gemini timeout on {model_name} (attempt {attempt + 1}/2), retrying...")
                continue
            except Exception as e:
                log.warning(f"Error during Gemini call [{model_name}]: {e}")
                break

        return None
