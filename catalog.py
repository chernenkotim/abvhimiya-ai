"""Validated JSON/YML catalog, last-good snapshot and Ukrainian lexical search."""
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
MAX_BYTES = 16 * 1024 * 1024
STOP = set('потрібен потрібна потрібно порадь підкажи будь ласка хочу купити товар товари для на в у і та є який яка які як чи мені мене це цей ця маєте'.split())
ALIASES = {'эпоксид':'епоксид', 'epoxy':'епоксид', 'клея':'клей', 'клею':'клей', 'клеї':'клей', 'металла':'метал', 'металу':'метал', 'металі':'метал', 'steel':'метал', 'сталь':'метал', 'сталі':'метал', 'стали':'метал', 'ванны':'ванн', 'ванни':'ванн', 'краска':'фарб', 'фарба':'фарб', 'фарби':'фарб', 'фарбу':'фарб', 'фарбою':'фарб', 'филамент':'філамент'}

def clean(value, length=4000):
    return re.sub(r'\s+',' ',html.unescape(re.sub(r'<[^>]*>', ' ', str(value or ''))))[:length].strip()

def tokens(text):
    raw = re.findall(r'[\w-]+', text.casefold())
    result = []
    for word in raw:
        if word in STOP or len(word) < 2: continue
        word = ALIASES.get(word, word)
        for stem in ('епоксид', 'метал', 'бетон', 'дерев', 'ванн', 'фарб', 'герметик', 'силікон', 'філамент'):
            if word.startswith(stem): word = stem; break
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
        return {key: getattr(self, key) for key in ('id','name','price','available','url','vendor_code')}

class Catalog:
    def __init__(self, settings):
        self.settings = settings
        self.products = {}
        self.updated_at = 0.0
        self.last_error = None
        self._indexed_products = None
        self._index = []
        self.lock = asyncio.Lock()
        self.snapshot = Path(settings.db_path).parent / 'catalog-snapshot.json'
        self.source_id = hashlib.sha256((str(settings.demo)+'|'+settings.catalog_url+'|'+settings.catalog_file).encode()).hexdigest()

    @property
    def fresh(self):
        return bool(self.products) and time.time() - self.updated_at <= self.settings.catalog_max_age

    def parse_product(self, raw):
        pid, name = clean(raw.get('id'),120), clean(raw.get('name'),250)
        if not pid or not name: raise ValueError('Product needs id and name')
        if str(raw.get('currency', raw.get('currencyId','UAH'))).upper() != 'UAH':
            raise ValueError('Only UAH is supported; convert the feed currency explicitly')
        try:
            # In this shop's YML, zero means no confirmed price, not a free item.
            price = Decimal(str(raw['price']).replace(',', '.')) if raw['price'] is not None else Decimal(0)
            if not price.is_finite() or price < 0 or price > 10000000: raise ValueError('Invalid price')
            price = str(price.quantize(Decimal('0.01'))) if price else None
        except (KeyError, InvalidOperation): raise ValueError('Invalid product price') from None
        url = str(raw.get('url','')).strip()
        p = urlsplit(url)
        if p.scheme != 'https' or p.hostname not in self.settings.product_hosts or p.username or p.password or p.port not in (None,443):
            raise ValueError('Product URL must belong to the configured shop')
        available = raw.get('available', False)
        if available in (True, 'true', '1', 1): available = True
        elif available in (False, 'false', '0', 0, None, ''): available = False
        else: raise ValueError('Invalid available flag')
        attrs = raw.get('attributes', {})
        if not isinstance(attrs, dict): raise ValueError('attributes must be an object')
        return Product(pid, name, price, available and price is not None, url, clean(raw.get('vendor_code',raw.get('vendorCode','')),120),
                       clean(raw.get('description','')), {clean(k,80): clean(v,500) for k,v in list(attrs.items())[:40]})

    def decode(self, content):
        if len(content) > MAX_BYTES: raise ValueError('Catalog is too large')
        if content.lstrip().startswith(b'<'):
            root = ElementTree.fromstring(content)
            raws = []
            for o in root.findall('.//offer'):
                row = {k:o.findtext(k,'') for k in ('name','price','currencyId','url','vendorCode','description')}
                row.update(id=o.get('id'), available=o.get('available','false'), attributes={p.get('name',''):p.text or '' for p in o.findall('param')})
                raws.append(row)
        else:
            data = json.loads(content)
            raws = data.get('products') if isinstance(data,dict) else data
        if not isinstance(raws,list) or not 0 < len(raws) <= 25000: raise ValueError('Expected 1–25000 products')
        products = {}
        for raw in raws:
            p = self.parse_product(raw)
            if p.id in products: raise ValueError('Duplicate product id')
            products[p.id] = p
        return products

    async def read_remote(self):
        parsed = urlsplit(self.settings.catalog_url)
        if parsed.scheme != 'https' or parsed.hostname not in self.settings.catalog_hosts or parsed.username or parsed.password or parsed.port not in (None,443):
            raise ValueError('Catalog source must be HTTPS on CATALOG_ALLOWED_HOSTS')
        addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname,443,type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
            raise ValueError('Private catalog destinations are prohibited')
        async with httpx.AsyncClient(timeout=20,follow_redirects=False) as client:
            async with client.stream('GET',self.settings.catalog_url) as r:
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
                if self.settings.catalog_url: content = await self.read_remote()
                elif self.settings.catalog_file:
                    path = Path(self.settings.catalog_file)
                    if path.stat().st_size > MAX_BYTES: raise ValueError('Catalog is too large')
                    source_time = path.stat().st_mtime
                    content = await asyncio.to_thread(path.read_bytes)
                else: raise ValueError('Catalog source is not configured')
                products = await asyncio.to_thread(self.decode, content)
                updated_at = source_time
                self.snapshot.parent.mkdir(parents=True,exist_ok=True)
                data = {'source_id':self.source_id,'updated_at':updated_at, 'products':[vars(p) for p in products.values()]}
                temp = self.snapshot.with_suffix('.tmp')
                await asyncio.to_thread(temp.write_text,json.dumps(data,ensure_ascii=False),encoding='utf-8')
                os.replace(temp,self.snapshot)
                self.products, self.updated_at, self.last_error = products, updated_at, None
                return True
            except Exception as e:
                # A catalog URL can contain a private access token: do not log it.
                self.last_error = type(e).__name__
                log.warning('Catalog refresh failed (%s); retaining last valid snapshot',type(e).__name__)
                return False

    def restore(self):
        try:
            if self.snapshot.stat().st_size > MAX_BYTES: return
            data = json.loads(self.snapshot.read_text())
            if data.get('source_id') != self.source_id: return
            self.products = self.decode(json.dumps(data['products']).encode())
            self.updated_at = float(data['updated_at'])
        except (OSError,ValueError,KeyError,TypeError): pass

    def search(self, query, limit=4):
        words = tokens(query)
        if not words: return []
        ranked = []
        def matches(word, term):
            # PLA must not match PLASTIC, and PET must not match PETG.
            if len(word)<=3 or len(term)<=3: return word==term
            return term.startswith(word) or word.startswith(term)
        if self._indexed_products is not self.products:
            self._index = [(p,tokens(p.name+' '+p.vendor_code),tokens(p.description+' '+json.dumps(p.attributes,ensure_ascii=False))) for p in self.products.values()]
            self._indexed_products = self.products
        for p,name,body in self._index:
            exact = query.strip().casefold() in (p.id.casefold(),p.vendor_code.casefold())
            matched = [w for w in words if any(matches(w,t) for t in name+body)]
            required = [w for w in words if w in ('pla','petg','pet','abs','asa','tpu')]
            if required and not all(w in name for w in required) and not exact: continue
            categories = [w for w in words if w in ('клей','фарб','філамент','герметик','епоксид')]
            if categories and not all(w in name for w in categories) and not exact: continue
            if not matched and not exact: continue
            score = 100 if exact else sum(5 if any(matches(w,t) for t in name) else 1 for w in matched)
            ranked.append((score+len(matched)/len(words),p.available,p.name,p))
        ranked.sort(key=lambda x:(-x[0],-x[1],x[2]))
        if not ranked: return []
        cutoff = ranked[0][0]*0.65
        return [x[3] for x in ranked[:limit] if x[0]>=cutoff]
