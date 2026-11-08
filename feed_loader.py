"""Read-only catalog adapter. Supply a Horoshop/YML export or local JSON.
Never uses LLM as authoritative price/stock source.
"""
import asyncio
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse
import httpx
from defusedxml import ElementTree as SafeET

log = logging.getLogger(__name__)

def _text(node, name, default=''):
    x = node.find(name)
    return (x.text or '').strip() if x is not None else default

def _available(v):
    return str(v).strip().lower() in ('true', '1', 'yes', 'available', 'в наявності', 'в наличии')

def _normalize(raw):
    if not isinstance(raw, dict):
        return None

    product_id = str(raw.get('id') or raw.get('product_id') or raw.get('vendor_code') or '').strip()
    name = str(raw.get('name') or raw.get('title') or '').strip()
    url = str(raw.get('url') or raw.get('link') or '').strip()

    if not product_id or not name:
        return None

    # Дозволяємо посилання як https, так і http (або підставляємо базовий URL магазину за потреби)
    if url and not url.startswith(('http://', 'https://')):
        url = 'https://' + url.lstrip('/')
    elif not url:
        url = 'https://abvhimiya.com.ua'

    try:
        price = float(str(raw.get('price', '')).replace(' ', '').replace(',', '.'))
        if not (0 < price < 1e8):
            return None
    except (ValueError, TypeError):
        return None

    avail = raw.get('available', False)
    if not isinstance(avail, bool):
        avail = _available(avail)

    desc = re.sub(r'<[^>]+>', ' ', str(raw.get('description') or ''))
    desc = re.sub(r'\s+', ' ', desc).strip()[:1500]

    return {
        'id': product_id,
        'name': name[:250],
        'vendor_code': str(raw.get('vendor_code') or raw.get('sku') or product_id)[:80],
        'price': round(price, 2),
        'available': avail,
        'url': url,
        'description': desc
    }

class Catalog:
    def __init__(self):
        self.products = []
        self.by_id = {}
        self.last_updated = None
        self._lock = asyncio.Lock()
        self.loaded = False

    async def update_feed(self, url='', file=''):
        async with self._lock:
            # 1. Якщо аргументи не передано, беремо з Environment
            target_url = url or os.getenv('CATALOG_URL') or os.getenv('HOROSHOP_FEED_URL') or ''
            target_file = file or os.getenv('CATALOG_FILE') or ''

            target_url = target_url.strip()
            target_file = target_file.strip()

            if not target_url and not target_file:
                log.warning('Catalog source is not configured (set CATALOG_URL or CATALOG_FILE)')
                return False

            try:
                content = b""
                if target_url:
                    parsed = urlparse(target_url)
                    if parsed.scheme not in ('http', 'https'):
                        raise ValueError('Catalog URL must be http or https')

                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                    }
                    # follow_redirects=True обов'язковий для Хорошопу
                    async with httpx.AsyncClient(timeout=35, follow_redirects=True) as client:
                        res = await client.get(target_url, headers=headers)
                        res.raise_for_status()
                        content = res.content

                    if len(content) > 100_000_000:
                        raise ValueError('Catalog file is too large (>100MB)')
                else:
                    path = Path(target_file)
                    if not path.is_file():
                        raise FileNotFoundError(f"Catalog file not found: {target_file}")
                    content = path.read_bytes()

                # 2. Обробка JSON або YML/XML
                if content.lstrip().startswith((b'{', b'[')):
                    data = json.loads(content)
                    if isinstance(data, dict):
                        data = data.get('products', [])
                else:
                    root = SafeET.fromstring(content)
                    data = []
                    # Пошук за всіма offer в YML/XML Хорошопу
                    for offer in root.findall('.//offer'):
                        data.append({
                            'id': offer.get('id'),
                            'name': _text(offer, 'name'),
                            'vendor_code': _text(offer, 'vendorCode') or _text(offer, 'vendor_code'),
                            'price': _text(offer, 'price'),
                            'available': offer.get('available', 'false'),
                            'url': _text(offer, 'url'),
                            'description': _text(offer, 'description')
                        })

                if not isinstance(data, list):
                    raise ValueError('Invalid products feed structure')

                clean = [x for item in data if (x := _normalize(item))]
                if not clean:
                    raise ValueError('No valid products found in feed')

                self.products = clean
                self.by_id = {x['id']: x for x in clean}

                from datetime import datetime, timezone
                self.last_updated = datetime.now(timezone.utc).isoformat()
                self.loaded = True

                log.info(f"Catalog successfully updated! Total products: {len(self.products)}")
                return True

            except Exception as e:
                log.exception(f'Catalog refresh failed: {e}')
                return False

    def get(self, product_id):
        return self.by_id.get(str(product_id))

    def search_relevant_products(self, query, limit=5):
        tokens = re.findall(r'[\wа-яіїєґ]+', query.casefold(), flags=re.I)
        tokens = [t for t in tokens if len(t) > 2][:20]
        if not tokens:
            return []

        ranked = []
        for p in self.products:
            name = p['name'].casefold()
            sku = p['vendor_code'].casefold()
            desc = p['description'].casefold()

            score = sum(7 if t in sku else 5 if t in name else 1 if t in desc else 0 for t in tokens)
            if score:
                ranked.append((score, bool(p['available']), p))

        ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
        return [r[2] for r in ranked[:limit]]

catalog = Catalog()
