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
            print("ПОМИЛКА: Не вказано посилання HOROSHOP_FEED_URL в файлі .env")
            return

        print("Завантаження фіда товарів з Хорошопу...")
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.get(self.feed_url)
                response.raise_for_status()
                xml_content = response.content

            soup = BeautifulSoup(xml_content, "xml")
            offers = soup.find_all("offer")
            
            parsed_items = []
            for offer in offers:
                offer_id = offer.get("id", "")
                available = offer.get("available", "false").lower() == "true"
                
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
                    p_name = p.get("name", "").strip()
                    p_val = p.text.strip()
                    if p_name and p_val:
                        params.append(f"{p_name}: {p_val}")
                
                parsed_items.append({
                    "id": offer_id,
                    "name": name,
                    "price": price,
                    "currency": currency,
                    "available": available,
                    "url": url,
                    "params": "; ".join(params),
                    "description": desc_text[:400],
                    "search_text": f"{name} {desc_text} {' '.join(params)}".lower()
                })
            
            self.products = parsed_items
            self.last_updated = time.time()
            print(f"Готово! Завантажено товарів: {len(self.products)}")
        except Exception as e:
            print(f"Помилка при зчитуванні фіда: {e}")

    def search_relevant_products(self, query: str, limit: int = 6) -> list:
        if not self.products:
            return []
            
        words = [w.strip().lower() for w in query.split() if len(w.strip()) > 2]
        if not words:
            return self.products[:limit]

        scored_products = []
        for p in self.products:
            score = 0
            p_name_lower = p["name"].lower()
            for word in words:
                if word in p_name_lower:
                    score += 5
                elif word in p["search_text"]:
                    score += 1
            if score > 0:
                scored_products.append((score, p))

        scored_products.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored_products[:limit]]

catalog = CatalogManager(HOROSHOP_FEED_URL)