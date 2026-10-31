import os
import re
import time
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

HOROSHOP_FEED_URL = os.getenv("HOROSHOP_FEED_URL", "")

EN_TO_UA_KEYBOARD = str.maketrans(
    "qwertyuiop[]asdfghjkl;'zxcvbnm,./`QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?~",
    "йцукенгшщзхїфівапролджєячсмитьбю.ʼЙЦУКЕНГШЩЗХЇФІВАПРОЛДЖЄЯЧСМИТЬБЮ,ʼ"
)

# Двосторонній індекс термінів будівельної хімії та 3D-друку
BASE_SYNONYMS = [
    {"клей", "glue", "adhesive", "kleber", "епоксид", "епоксидний", "epoxy"},
    {"герметик", "sealant", "silicone", "силікон", "акрил", "силіконовий"},
    {"грунтовка", "грунт", "ґрунтовка", "ґрунт", "primer"},
    {"фарба", "paint", "емаль", "колор", "краска"},
    {"піна", "foam", "монтажна піна"},
    {"філамент", "filament", "пластик", "pla", "petg", "abs"},
    {"гідроізоляція", "waterproof", "waterproofing", "гідробар'єр"},
    {"очисник", "cleaner", "змивка", "розчинник", "solvent", "thinner"}
]

SYNONYMS_INDEX = {}
for group in BASE_SYNONYMS:
    for word in group:
        SYNONYMS_INDEX[word] = group

HTML_TAG_CLEANER = re.compile(r"<[^>]+>")

def clean_html(raw_html: str) -> str:
    """Швидке очищення опису від HTML-тегів без виклику важкого парсера"""
    if not raw_html:
        return ""
    text = HTML_TAG_CLEANER.sub(" ", raw_html)
    return " ".join(text.split())

def fix_keyboard_layout(text: str) -> str:
    """Виправляє запити, помилково набрані в англійській розкладці"""
    if re.fullmatch(r"[a-zA-Z0-9\s\.,'ʼ\-\/]+", text):
        converted = text.translate(EN_TO_UA_KEYBOARD)
        if re.search(r"[а-яіїєґ]{3,}", converted, re.IGNORECASE):
            return converted
    return text

def normalize_stem(word: str) -> str:
    w = word.lower().strip()
    endings = ["ного", "ному", "ним", "ний", "ою", "ям", "ем", "ом", "ий", "их", "им", "ів", "на", "не", "ні", "а", "е", "у", "і", "и"]
    for end in sorted(endings, key=len, reverse=True):
        if len(w) > len(end) + 3 and w.endswith(end):
            return w[:-len(end)]
    return w

class CatalogManager:
    def __init__(self, feed_url: str):
        self.feed_url = feed_url
        self.products = []
        self.last_updated = 0

    async def update_feed(self):
        if not self.feed_url:
            print("ПОМИЛКА: Не вказано HOROSHOP_FEED_URL")
            return

        # Додаємо мітку часу для обходу статичного кешування провайдерами
        separator = "&" if "?" in self.feed_url else "?"
        fresh_url = f"{self.feed_url}{separator}_t={int(time.time())}"

        print(f"Завантаження та швидка індексація каталогу: {self.feed_url}")
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            async with httpx.AsyncClient(timeout=90.0, follow_redirects=True, headers=headers) as client:
                response = await client.get(fresh_url)
                response.raise_for_status()
                xml_content = response.content

            soup = BeautifulSoup(xml_content, "xml")
            parsed_items = []

            # 1. Формат Rozetka / Prom / YML (<offer>)
            offers = soup.find_all("offer")
            if offers:
                print(f"Формат Rozetka/YML. Знайдено карток: {len(offers)}")
                for offer in offers:
                    offer_id = (offer.get("id") or "").strip()
                    available = offer.get("available", "false").lower() == "true"

                    vc_tag = offer.find("vendorCode") or offer.find("article") or offer.find("barcode")
                    vendor_code = vc_tag.text.strip() if vc_tag and vc_tag.text else ""

                    name_elem = offer.find("name")
                    name = name_elem.text.strip() if name_elem else ""

                    price_elem = offer.find("price")
                    price = price_elem.text.strip() if price_elem else "0"

                    currency_elem = offer.find("currencyId")
                    currency = currency_elem.text.strip() if currency_elem else "UAH"

                    url_elem = offer.find("url")
                    url = url_elem.text.strip() if url_elem else ""

                    desc_elem = offer.find("description")
                    desc_text = clean_html(desc_elem.text) if desc_elem else ""

                    category_elem = offer.find("categoryId")
                    category = category_elem.text.strip() if category_elem else ""

                    params = []
                    for p in offer.find_all("param"):
                        p_name = (p.get("name") or "").strip()
                        p_val = p.text.strip()
                        if p_name and p_val:
                            params.append(f"{p_name}: {p_val}")

                    codes = f"{offer_id} {vendor_code}".strip()
                    full_text = f"{codes} {name} {category} {desc_text} {' '.join(params)}".lower()

                    parsed_items.append({
                        "id": offer_id,
                        "vendor_code": vendor_code,
                        "name": name,
                        "price": f"{price} {currency}".strip(),
                        "currency": currency,
                        "available": available,
                        "url": url,
                        "params": "; ".join(params[:5]),  # Тільки перші 5 найважливіших параметрів
                        "description": desc_text[:180],     # Оптимальна довжина опису для AI
                        "name_lower": name.lower(),
                        "tokens": set(re.findall(r"[\w\d]+", full_text))
                    })

            # 2. Формат Google Feed (<item>)
            else:
                items = soup.find_all("item")
                print(f"Формат Google Feed. Знайдено карток: {len(items)}")
                for item in items:
                    g_id = item.find(["g:id", "id"])
                    offer_id = g_id.text.strip() if g_id else ""

                    title = item.find(["g:title", "title"])
                    name = title.text.strip() if title else ""

                    price_elem = item.find(["g:price", "price"])
                    price = price_elem.text.strip() if price_elem else "0"

                    link_elem = item.find(["g:link", "link"])
                    url = link_elem.text.strip() if link_elem else ""

                    desc_elem = item.find(["g:description", "description"])
                    desc_text = clean_html(desc_elem.text) if desc_elem else ""

                    avail_elem = item.find(["g:availability", "availability"])
                    avail_str = avail_elem.text.strip().lower() if avail_elem else ""
                    available = ("in_stock" in avail_str) or ("in stock" in avail_str) or (avail_str == "true")

                    mpn_elem = item.find(["g:mpn", "mpn"])
                    vendor_code = mpn_elem.text.strip() if mpn_elem else offer_id

                    codes = f"{offer_id} {vendor_code}".strip()
                    full_text = f"{codes} {name} {desc_text}".lower()

                    parsed_items.append({
                        "id": offer_id,
                        "vendor_code": vendor_code,
                        "name": name,
                        "price": price,
                        "currency": "UAH",
                        "available": available,
                        "url": url,
                        "params": "",
                        "description": desc_text[:180],
                        "name_lower": name.lower(),
                        "tokens": set(re.findall(r"[\w\d]+", full_text))
                    })

            self.products = parsed_items
            self.last_updated = time.time()
            print(f"Індексацію завершено. Готово до продажу товарів: {len(self.products)}")
        except Exception as e:
            print(f"Помилка при завантаженні фіда: {e}")

    def search_relevant_products(self, query: str, limit: int = 5) -> list:
        if not self.products:
            return []

        raw_query = query.strip().lower()
        fixed_layout = fix_keyboard_layout(raw_query)

        tokens = set(re.findall(r"[\w\d]+", raw_query))
        if fixed_layout != raw_query:
            tokens.update(re.findall(r"[\w\d]+", fixed_layout))

        query_tokens = [t for t in tokens if len(t) >= 2]
        if not query_tokens:
            return self.products[:limit]

        # 1. Пріоритет: точний збіг за артикулом або ID
        exact_matches = []
        for p in self.products:
            p_id = p["id"].lower()
            p_code = p["vendor_code"].lower()
            for token in query_tokens:
                if (p_id and token == p_id) or (p_code and token == p_code):
                    exact_matches.append(p)
                    break
        if exact_matches:
            return exact_matches[:limit]

        # 2. Розширення токенів синонімами та основами слів
        expanded_terms = set()
        for t in query_tokens:
            expanded_terms.add(t)
            expanded_terms.add(normalize_stem(t))
            if t in SYNONYMS_INDEX:
                expanded_terms.update(SYNONYMS_INDEX[t])

        # 3. Швидкий скоринг через множини токенів
        scored = []
        for p in self.products:
            score = 0
            p_name = p["name_lower"]
            p_tokens = p["tokens"]

            for term in expanded_terms:
                if len(term) < 2:
                    continue
                if term in p_name:
                    score += 10
                elif term in p_tokens:
                    score += 3

            if score > 0:
                if p["available"]:
                    score += 5
                else:
                    score -= 2
                scored.append((score, p))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored[:limit]]

catalog = CatalogManager(HOROSHOP_FEED_URL)
