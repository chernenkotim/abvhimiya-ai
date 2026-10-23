import os
import re
import time
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

HOROSHOP_FEED_URL = os.getenv("HOROSHOP_FEED_URL", "")

# Словник транскодування випадкової англійської розкладки в кирилицю
EN_TO_UA_KEYBOARD = str.maketrans(
    "qwertyuiop[]asdfghjkl;'zxcvbnm,./`QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?~",
    "йцукенгшщзхїфівапролджєячсмитьбю.ʼЙЦУКЕНГШЩЗХЇФІВАПРОЛДЖЄЯЧСМИТЬБЮ,ʼ"
)

# Словник професійних синонімів будівельної хімії та 3D-друку
SYNONYMS_MAP = {
    "клей": ["glue", "adhesive", "kleber", "bond", "епоксидний", "epoxy"],
    "клею": ["glue", "adhesive", "клей"],
    "епоксидний": ["epoxy", "епоксид", "епоксидна", "епоксидка"],
    "епоксидка": ["epoxy", "епоксидний", "епоксидна"],
    "герметик": ["sealant", "silicone", "силікон", "акрил", "силіконовий"],
    "силікон": ["silicone", "герметик", "sealant"],
    "грунтовка": ["primer", "грунт", "ґрунтовка", "ґрунт"],
    "ґрунтовка": ["primer", "грунт", "грунтовка"],
    "фарба": ["paint", "емаль", "color", "краска"],
    "піна": ["foam", "монтажна піна"],
    "філамент": ["filament", "пластик", "pla", "petg", "abs"],
    "пластик": ["filament", "pla", "petg", "abs", "філамент"],
    "гідроізоляція": ["waterproof", "waterproofing", "гідробар'єр"],
    "очисник": ["cleaner", "змивка", "розчинник", "solvent"],
    "розчинник": ["solvent", "thinner", "розріджувач"]
}

def fix_keyboard_layout(text: str) -> str:
    """Виправляє текст, набраний помилково латинськими літерами замість кирилиці."""
    converted = text.translate(EN_TO_UA_KEYBOARD)
    # Якщо перетворений текст містить типові кириличні літери
    if re.search(r"[а-яіїєґ]", converted, re.IGNORECASE):
        return converted
    return text

def normalize_stem(word: str) -> str:
    """Відрізає поширені українські закінчення для м'якого збігу слів."""
    w = word.lower().strip()
    endings = ["ного", "ному", "ним", "ний", "на", "не", "ні", "ою", "ям", "ем", "ом", "ий", "их", "им", "ів", "а", "е", "у", "і", "и"]
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

        print(f"Завантаження фіда з Хорошоп: {self.feed_url}")
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            async with httpx.AsyncClient(timeout=90.0, follow_redirects=True, headers=headers) as client:
                response = await client.get(self.feed_url)
                response.raise_for_status()
                xml_content = response.content

            soup = BeautifulSoup(xml_content, "xml")
            parsed_items = []

            # 1. Розбір формату Rozetka / Prom / YML (<offer>)
            offers = soup.find_all("offer")
            if offers:
                print(f"Виявлено формат Rozetka/YML. Знайдено товарів: {len(offers)}")
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
                    desc_text = ""
                    if desc_elem:
                        desc_soup = BeautifulSoup(desc_elem.text, "html.parser")
                        desc_text = desc_soup.get_text(separator=" ").strip()

                    params = []
                    for p in offer.find_all("param"):
                        p_name = (p.get("name") or "").strip()
                        p_val = p.text.strip()
                        if p_name and p_val:
                            params.append(f"{p_name}: {p_val}")

                    codes = f"{offer_id} {vendor_code}".strip()
                    parsed_items.append({
                        "id": offer_id,
                        "vendor_code": vendor_code,
                        "name": name,
                        "price": f"{price} {currency}".strip(),
                        "available": available,
                        "url": url,
                        "params": "; ".join(params),
                        "description": desc_text[:450],
                        "search_text": f"{codes} {name} {desc_text} {' '.join(params)}".lower()
                    })

            # 2. Розбір формату Google Merchant (<item>)
            else:
                items = soup.find_all("item")
                print(f"Виявлено формат Google Feed. Знайдено товарів: {len(items)}")
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
                    desc_text = ""
                    if desc_elem:
                        desc_soup = BeautifulSoup(desc_elem.text, "html.parser")
                        desc_text = desc_soup.get_text(separator=" ").strip()

                    avail_elem = item.find(["g:availability", "availability"])
                    avail_str = avail_elem.text.strip().lower() if avail_elem else ""
                    available = ("in_stock" in avail_str) or ("in stock" in avail_str) or (avail_str == "true")

                    mpn_elem = item.find(["g:mpn", "mpn"])
                    vendor_code = mpn_elem.text.strip() if mpn_elem else offer_id

                    codes = f"{offer_id} {vendor_code}".strip()
                    parsed_items.append({
                        "id": offer_id,
                        "vendor_code": vendor_code,
                        "name": name,
                        "price": price,
                        "available": available,
                        "url": url,
                        "params": "",
                        "description": desc_text[:450],
                        "search_text": f"{codes} {name} {desc_text}".lower()
                    })

            self.products = parsed_items
            self.last_updated = time.time()
            print(f"Каталог успішно оновлено! Завантажено товарів: {len(self.products)}")
        except Exception as e:
            print(f"Помилка при завантаженні фіда: {e}")

    def search_relevant_products(self, query: str, limit: int = 10) -> list:
        if not self.products:
            return []

        # Виправлення можливої розкладки клавіатури
        clean_query = query.strip().lower()
        fixed_query = fix_keyboard_layout(clean_query)
        
        # Об'єднуємо обидва варіанти для надійності
        search_sources = set(clean_query.split() + fixed_query.split())
        query_tokens = [w.strip() for w in search_sources if len(w.strip()) >= 2]
        if not query_tokens:
            return self.products[:limit]

        # 1. Прямий пошук за артикулом або ID
        exact_code_matches = []
        for p in self.products:
            p_id = p["id"].lower()
            p_code = p["vendor_code"].lower()
            for token in query_tokens:
                if (p_id and token == p_id) or (p_code and token == p_code):
                    exact_code_matches.append(p)
                    break
        if exact_code_matches:
            return exact_code_matches[:limit]

        # 2. Розширення пошукових токенів через синоніми та основи слів
        expanded_search_terms = set()
        for token in query_tokens:
            expanded_search_terms.add(token)
            expanded_search_terms.add(normalize_stem(token))
            
            # Додаємо синоніми (наприклад: клей -> epoxy, glue, adhesive)
            for key, synonyms in SYNONYMS_MAP.items():
                if key in token or token in key:
                    expanded_search_terms.update(synonyms)

        # 3. Скоринг релевантності товарів
        scored_products = []
        for p in self.products:
            score = 0
            p_name_lower = p["name"].lower()
            search_blob = p["search_text"]

            for term in expanded_search_terms:
                if len(term) < 2:
                    continue
                # Збіг у назві має найвищу вагу
                if term in p_name_lower:
                    score += 8
                # Збіг у характеристиках / описі
                elif term in search_blob:
                    score += 2

            # Додатковий бонус товарам у наявності
            if score > 0:
                if p["available"]:
                    score += 1
                scored_products.append((score, p))

        scored_products.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored_products[:limit]]

catalog = CatalogManager(HOROSHOP_FEED_URL)
