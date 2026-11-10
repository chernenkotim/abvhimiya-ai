"""Validated JSON/YML catalog, last-good snapshot, lexical indexing, and fast retrieval."""
import asyncio
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import html
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import socket
import time
from urllib.parse import urlsplit

from defusedxml import ElementTree
import httpx

log = logging.getLogger(__name__)

# Ліміт 64 МБ для безперешкодного завантаження великих вивантажень Хорошопу
MAX_BYTES = 64 * 1024 * 1024

STOP = frozenset([
    'потрібен', 'потрібна', 'потрібно', 'порадь', 'підкажи', 'будь', 'ласка', 'хочу', 
    'купити', 'товар', 'товари', 'для', 'на', 'в', 'у', 'і', 'та', 'є', 'який', 'яка', 
    'які', 'як', 'чи', 'мені', 'мене', 'це', 'цей', 'ця', 'маєте', 'знайти', 'шукаю', 
    'добро', 'дня', 'добрий', 'вечір'
])

# Автоматичне виправлення розкладки
ENG_TO_UKR = str.maketrans(
    "qwertyuiop[]asdfghjkl;'zxcvbnm,./`QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?~",
    "йцукенгшщзхїфівапролджєячсмитьбю.ʼЙЦУКЕНГШЩЗХЇФІВАПРОЛДЖЄЯЧСМИТЬБЮ,ʼ"
)

# Синоніми та стеммінг для ABV Хімія
ALIASES = {
    'эпоксид': 'епоксид', 'epoxy': 'епоксид', 'епоксидка': 'епоксид',
    'клея': 'клей', 'клею': 'клей', 'клеї': 'клей', 'клей-піна': 'клей', 'клейпіна': 'клей',
    'металла': 'метал', 'металу': 'метал', 'металі': 'метал', 'steel': 'метал',
    'сталь': 'метал', 'сталі': 'метал', 'стали': 'метал', 'залізо': 'метал',
    'ванны': 'ванн', 'ванни': 'ванн', 'ванну': 'ванн', 'ванною': 'ванн',
    'краска': 'фарб', 'фарба': 'фарб', 'фарби': 'фарб', 'фарбу': 'фарб', 'фарбою': 'фарб',
    'резиновая': 'гумов', 'резинові': 'гумов', 'гумова': 'гумов', 'гумової': 'гумов', 'гумові': 'гумов',
    'емалі': 'емал', 'емаль': 'емал', 'эмаль': 'емал', 'аерозоль': 'балончик', 'спрей': 'балончик',
    'мікрохвильовка': 'мікрохвильов', 'мікрохвильовки': 'мікрохвильов', 'мікрохвильовкі': 'мікрохвильов',
    'микроволновка': 'мікрохвильов', 'микроволновки': 'мікрохвильов',
    'печі': 'печ', 'печь': 'печ', 'піч': 'печ', 'духовка': 'термостійк', 'камін': 'термостійк',
    'филамент': 'філамент', 'нитка': 'філамент', 'пластик': 'філамент',
    'герметика': 'герметик', 'герметику': 'герметик', 'силикон': 'силікон',
    'піни': 'пін', 'піну': 'пін', 'пена': 'пін',
    'растворитель': 'розчинник', 'розчинника': 'розчинник', 'сольвенту': 'сольвент',
    'уайтспірит': 'уайт-спірит', 'спирит': 'уайт-спірит', 'калоша': 'нефрас'
}

COLORS = {'білий', 'чорний', 'прозорий', 'сірий', 'червоний', 'синій', 'зелений', 'жовтий', 'золото', 'срібло'}
STEMS = ('епоксид', 'метал', 'бетон', 'дерев', 'ванн', 'фарб', 'герметик',
         'силікон', 'філамент', 'розчинник', 'грунт', 'пін', 'емал', 'гумов',
         'термостійк', 'мікрохвильов', 'печ', 'керамік', 'акваріум')

def fix_layout_if_needed(text: str) -> str:
    raw_lower = text.strip().lower()
    if re.search(r'[a-z]{3,}', raw_lower) and not any(w in raw_lower for w in ('pla', 'petg', 'abs', 'tpu', 'belife', 'rino', 'hercul', 'sunlu', 'ml', 'кг', 'гр')):
        converted = text.translate(ENG_TO_UKR)
        if re.search(r'[а-яіїєґ]', converted.lower()):
            return converted
    return text

def clean(value, length=4000):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]*>', ' ', str(value or ''))))[:length].strip()

def normalize_code(val: str) -> str:
    s = str(val).strip().casefold().replace('-', '').replace(' ', '').replace('_', '')
    trans = str.maketrans('асрокемітх', 'acpokemitx')
    return s.translate(trans)

def tokens(text):
    text = fix_layout_if_needed(text)
    raw = re.findall(r'[\w-]+', text.casefold())
    result = []
    for word in raw:
        if word in STOP or len(word) < 2:
            continue
        word = ALIASES.get(word, word)
        for stem in STEMS:
            if word.startswith(stem):
                word = stem
                break
        result.append(word)
    return list(dict.fromkeys(result))

@dataclass
class Product:
    id: str
    name: str
    price: str | None
    available: bool
    url: str
    vendor_code: str = ''
    description: str = ''
    attributes: dict = field(default_factory=dict)

    def public(self):
        return {key: getattr(self, key) for key in ('id', 'name', 'price', 'available', 'url', 'vendor_code')}

class Catalog:
    def __init__(self, settings):
        self.settings = settings
        self.products = {}
        self.updated_at = 0.0
        self.last_error = None
        
        # Індекс для швидкого семантичного пошуку
        self._indexed_products = None
        self._index = []
        
        self.lock = asyncio.Lock()
        self.snapshot = Path(settings.db_path).parent / 'catalog-snapshot.json'
        self.source_id = hashlib.sha256(
            (str(settings.demo) + '|' + str(settings.catalog_url) + '|' + str(settings.catalog_file)).encode()
        ).hexdigest()

    @property
    def fresh(self):
        return bool(self.products) and time.time() - self.updated_at <= self.settings.catalog_max_age

    def parse_product(self, raw):
        pid, name = clean(raw.get('id'), 120), clean(raw.get('name'), 250)
        if not pid or not name: raise ValueError('Product needs id and name')

        curr = str(raw.get('currency', raw.get('currencyId', 'UAH'))).upper()
        if curr != 'UAH': raise ValueError(f'Only UAH is supported (got {curr})')

        try:
            raw_p = raw.get('price')
            price_val = Decimal(str(raw_p).replace(' ', '').replace(',', '.')) if raw_p is not None and str(raw_p).strip() != '' else Decimal(0)
            if not price_val.is_finite() or price_val < 0 or price_val > 10000000: raise ValueError('Invalid price')
            price = str(price_val.quantize(Decimal('0.01'))) if price_val > 0 else None
        except (KeyError, InvalidOperation):
            raise ValueError('Invalid product price') from None

        url = str(raw.get('url', '')).strip()
        p = urlsplit(url)
        if p.scheme != 'https' or p.hostname not in self.settings.product_hosts or p.username or p.password or p.port not in (None, 443):
            raise ValueError(f'Product URL must belong to the configured shop: {url}')

        available = raw.get('available', False)
        if available in (True, 'true', '1', 1, 'yes', 'в наявності'): available = True
        elif available in (False, 'false', '0', 0, None, '', 'немає в наявності'): available = False
        else: raise ValueError('Invalid available flag')

        attrs = raw.get('attributes', {})
        if not isinstance(attrs, dict): raise ValueError('attributes must be an object')

        return Product(
            pid, name, price, available and (price is not None), url,
            clean(raw.get('vendor_code', raw.get('vendorCode', '')), 120),
            clean(raw.get('description', '')),
            {clean(k, 80): clean(v, 500) for k, v in list(attrs.items())[:40]}
        )

    def decode(self, content):
        if len(content) > MAX_BYTES: raise ValueError('Catalog is too large')

        if content.lstrip().startswith(b'<'):
            root = ElementTree.fromstring(content)
            raws = []
            for o in root.findall('.//offer'):
                row = {k: o.findtext(k, '') for k in ('name', 'price', 'currencyId', 'url', 'vendorCode', 'description')}
                row.update(
                    id=o.get('id'),
                    available=o.get('available', 'false'),
                    attributes={p.get('name', ''): p.text or '' for p in o.findall('param')}
                )
                raws.append(row)
        else:
            data = json.loads(content)
            raws = data.get('products') if isinstance(data, dict) else data

        if not isinstance(raws, list) or not (0 < len(raws) <= 50000):
            raise ValueError('Expected 1–50000 products')

        products = {}
        for raw in raws:
            p = self.parse_product(raw)
            if p.id not in products:
                products[p.id] = p
        return products

    async def read_remote(self):
        parsed = urlsplit(self.settings.catalog_url)
        if parsed.scheme != 'https' or parsed.hostname not in self.settings.catalog_hosts or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError('Catalog source must be HTTPS on CATALOG_ALLOWED_HOSTS')

        addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
            raise ValueError('Private catalog destinations are prohibited')

        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ABV-Agent'}
        async with httpx.AsyncClient(timeout=35, follow_redirects=True) as client:
            async with client.stream('GET', self.settings.catalog_url, headers=headers) as r:
                r.raise_for_status()
                data = bytearray()
                async for chunk in r.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES: raise ValueError('Catalog is too large')
                return bytes(data)

    async def refresh(self):
        async with self.lock:
            try:
                source_time = time.time()
                if self.settings.catalog_url:
                    content = await self.read_remote()
                elif self.settings.catalog_file:
                    path = Path(self.settings.catalog_file)
                    if not path.is_file(): raise FileNotFoundError(f"Catalog file not found: {path}")
                    if path.stat().st_size > MAX_BYTES: raise ValueError('Catalog is too large')
                    source_time = path.stat().st_mtime
                    content = await asyncio.to_thread(path.read_bytes)
                else:
                    raise ValueError('Catalog source is not configured')

                products = await asyncio.to_thread(self.decode, content)
                updated_at = source_time
                self.snapshot.parent.mkdir(parents=True, exist_ok=True)
                data = {
                    'source_id': self.source_id,
                    'updated_at': updated_at,
                    'products': [vars(p) for p in products.values()]
                }
                temp = self.snapshot.with_suffix('.tmp')
                await asyncio.to_thread(temp.write_text, json.dumps(data, ensure_ascii=False), encoding='utf-8')
                os.replace(temp, self.snapshot)

                self.products, self.updated_at, self.last_error = products, updated_at, None
                log.info(f"Catalog successfully refreshed. Total valid products: {len(products)}")
                return True
            except Exception as e:
                self.last_error = type(e).__name__
                log.warning(f'Catalog refresh failed ({type(e).__name__}): {e}; retaining last snapshot')
                return False

    def restore(self):
        try:
            if not self.snapshot.is_file() or self.snapshot.stat().st_size > MAX_BYTES: return
            data = json.loads(self.snapshot.read_text(encoding='utf-8'))
            if data.get('source_id') != self.source_id: return
            self.products = self.decode(json.dumps(data['products']).encode())
            self.updated_at = float(data['updated_at'])
            log.info(f"Restored {len(self.products)} products from snapshot.")
        except Exception as e:
            log.warning(f"Snapshot restore bypassed: {e}")

    def search(self, query, limit=5):
        """Оптимізований пошук з бальною системою ранжування."""
        clean_q = fix_layout_if_needed(query.strip())
        norm_q = normalize_code(clean_q)
        if not norm_q: return []

        # 1. Швидкий пошук за точним артикулом / ID
        exact_matches = []
        for p in self.products.values():
            p_code = normalize_code(p.vendor_code) if p.vendor_code else ''
            p_id = normalize_code(p.id)
            if norm_q and (norm_q in (p_id, p_code) or (len(norm_q) >= 4 and (norm_q == p_code.lstrip('0') or norm_q in p_code))):
                exact_matches.append(p)
        if exact_matches:
            return sorted(exact_matches, key=lambda x: x.available, reverse=True)[:limit]

        # 2. Семантичний / Лексичний пошук
        words = tokens(clean_q)
        if not words: return []

        # Кешування індексу (Використання Set для швидких перетинів)
        if self._indexed_products is not self.products:
            self._index = []
            for p in self.products.values():
                name_set = set(tokens(p.name + ' ' + p.vendor_code))
                attr_set = set(tokens(' '.join(f'{k} {v}' for k, v in p.attributes.items())))
                desc_set = set(tokens(p.description))
                self._index.append((p, name_set, attr_set, desc_set))
            self._indexed_products = self.products

        ranked = []
        for p, name_set, attr_set, desc_set in self._index:
            score = 0
            matched_words = 0
            
            for w in words:
                # Гнучкий метчинг (дозволяє знайти "фарб" у "фарба")
                is_name = any(w in t or t in w for t in name_set)
                is_attr = any(w in t or t in w for t in attr_set)
                is_desc = any(w in t or t in w for t in desc_set)
                
                if is_name:
                    score += 15
                    matched_words += 1
                elif is_attr:
                    score += 8
                    matched_words += 1
                elif is_desc:
                    score += 2
                    matched_words += 1

            if matched_words == 0:
                continue

            # Сувора перевірка на тип філаменту (уникає показу PETG, коли шукають PLA)
            req_3d = [w for w in words if w in ('pla', 'petg', 'abs', 'tpu')]
            if req_3d and not all(any(w in t for t in name_set) for w in req_3d):
                continue

            # Штраф за низьке покриття запиту (показуємо лише якщо збіглося > 50% ключових слів)
            coverage = matched_words / len(words)
            if coverage < 0.4 and len(words) > 1:
                continue

            # Бонус за специфічні категорії (щоб фарба не перебивала мікрохвильовки)
            specific_keys = [w for w in words if w in ('мікрохвильов', 'ванн', 'печ', 'керамік', 'гумов') or w in COLORS]
            if specific_keys and any(sk in name_set for sk in specific_keys):
                score += 25

            # Бонус за наявність та сортування
            if p.available:
                score += 10

            ranked.append((score, p))

        # Повертаємо топ N результатів, відсортованих за балами (score)
        ranked.sort(key=lambda x: x[0], reverse=True)
        return [p for score, p in ranked[:limit]]
