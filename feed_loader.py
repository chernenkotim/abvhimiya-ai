import os
import time
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

HOROSHOP_FEED_URL = os.getenv("HOROSHOP_FEED_URL", "")

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

            # 1. Спроба обробити формат Google Merchant (<item>)
            google_items = soup.find_all("item")
            if google_items:
                print(f"Виявлено формат Google Feed (знайдено елементів: {len(google_items)})")
                for item in google_items:
                    g_id = item.find(["g:id", "id"])
                    offer_id = g_id.text.strip() if g_id else ""

                    title = item.find(["g:title", "title"])
                    name = title.text.strip() if title else ""

                    price_elem = item.find(["g:price", "price"])
                    price_raw = price_elem.text.strip() if price_elem else "0"
                    
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
                    mpn = mpn_elem.text.strip() if mpn_elem else ""

                    parsed_items.append({
                        "id": offer_id,
                        "vendor_code": mpn or offer_id,
                        "name": name,
                        "price": price_raw,
                        "currency": "",
                        "available": available,
                        "url": url,
                        "params": "",
                        "description": desc_text[:400],
                        "search_text": f"{offer_id} {mpn} {name} {desc_text}".lower()
                    })

            # 2. Спроба обробити стандартний YML / Rozetka (<offer>)
            else:
                offers = soup.find_all("offer")
                print(f"Виявлено формат Rozetka/YML (знайдено елементів: {len(offers)})")
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

                    codes_combined = f"{offer_id} {vendor_code}".strip()

                    parsed_items.append({
                        "id": offer_id,
                        "vendor_code": vendor_code,
                        "name": name,
                        "price": f"{price} {currency}".strip(),
                        "currency": currency,
                        "available": available,
                        "url": url,
                        "params": "; ".join(params),
                        "description": desc_text[:400],
                        "search_text": f"{codes_combined} {name} {desc_text} {' '.join(params)}".lower()
                    })

            self.products = parsed_items
            self.last_updated = time.time()
            print(f"Каталог успішно оновлено! Завантажено товарів: {len(self.products)}")
        except Exception as e:
            print(f"Помилка при завантаженні фіда: {e}")

    def search_relevant_products(self, query: str, limit: int = 6) -> list:
        if not self.products:
            return []
            
        clean_query = query.strip().lower()
        words = [w.strip() for w in clean_query.split() if len(w.strip()) >= 2]
        if not words:
            return self.products[:limit]

        # 1. Прямий пошук за ID / артикулом
        exact_code_matches = []
        for p in self.products:
            p_id = p["id"].lower()
            p_code = p["vendor_code"].lower()
            for w in words:
                if (p_id and w == p_id) or (p_code and w == p_code):
                    exact_code_matches.append(p)
                    break
        
        if exact_code_matches:
            return exact_code_matches[:limit]

        # 2. Пошук за назвою та характеристиками
        scored_products = []
        for p in self.products:
            score = 0
            p_name_lower = p["name"].lower()
            search_blob = p["search_text"]

            for word in words:
                if word in p["id"].lower() or (p["vendor_code"] and word in p["vendor_code"].lower()):
                    score += 15
                elif word in p_name_lower:
                    score += 5
                elif word in search_blob:
                    score += 1

            if score > 0:
                scored_products.append((score, p))

        scored_products.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored_products[:limit]]

catalog = CatalogManager(HOROSHOP_FEED_URL)
