"""Read-only catalog adapter. Supply a Horoshop/YML export or local JSON.
Never uses LLM as authoritative price/stock source.
"""
import asyncio
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET
import httpx
from defusedxml import ElementTree as SafeET

log = logging.getLogger(__name__)

def _text(node, name, default=''):
    x = node.find(name)
    return (x.text or '').strip() if x is not None else default

def _available(v):
    return str(v).lower() in ('true','1','yes','available','в наявності')

def _normalize(raw):
    if not isinstance(raw, dict): return None
    product_id = str(raw.get('id') or raw.get('product_id') or raw.get('vendor_code') or '').strip()
    name = str(raw.get('name') or raw.get('title') or '').strip()
    url = str(raw.get('url') or raw.get('link') or '').strip()
    if not product_id or not name or not url.startswith('https://'): return None
    host = (urlparse(url).hostname or '').lower()
    if host not in {'abvhimiya.com.ua', 'www.abvhimiya.com.ua','sunlu.kyiv.ua','www.sunlu.kyiv.ua'}: return None
    try:
        price = float(str(raw.get('price', '')).replace(' ', '').replace(',', '.'))
        if not (0 < price < 1e8): return None
    except (ValueError,TypeError): return None
    return {'id':product_id, 'name':name[:250], 'vendor_code':str(raw.get('vendor_code') or raw.get('sku') or product_id)[:80],
            'price':round(price,2), 'available':_available(raw.get('available',False)) if not isinstance(raw.get('available'),bool) else raw['available'],
            'url':url,'description':re.sub('<[^>]+>',' ',str(raw.get('description') or ''))[:1500]}

class Catalog:
    def __init__(self):
        self.products = []
        self.by_id = {}
        self.last_updated = None
        self._lock = asyncio.Lock()
        self.loaded = False

    async def update_feed(self, url='', file=''):
        async with self._lock:
            if not url and not file:
                log.warning('Catalog source is not configured')
                return False
            try:
                if url:
                    parsed=urlparse(url)
                    if parsed.scheme!='https': raise ValueError('Catalog URL must be https')
                    async with httpx.AsyncClient(timeout=20,follow_redirects=False) as client:
                        res=await client.get(url); res.raise_for_status(); content=res.content
                    if len(content)>25_000_000: raise ValueError('Catalog too large')
                else:
                    content=Path(file).read_bytes()
                    if len(content)>25_000_000: raise ValueError('Catalog too large')
                if content.lstrip().startswith((b'{',b'[')):
                    data=json.loads(content)
                    if isinstance(data,dict): data=data.get('products',[])
                else:
                    root=SafeET.fromstring(content)
                    data=[]
                    for offer in root.findall('.//offer'):
                        data.append({'id':offer.get('id'), 'name':_text(offer,'name'), 'vendor_code':_text(offer,'vendorCode') or _text(offer,'vendor_code'),
                         'price':_text(offer,'price'), 'available':offer.get('available','false'), 'url':_text(offer,'url'),
                         'description':_text(offer,'description')})
                if not isinstance(data,list): raise ValueError('Invalid products feed')
                clean=[x for item in data if (x:=_normalize(item))]
                if not clean: raise ValueError('No valid products in feed (keeping old catalog)')
                self.products=clean
                self.by_id={x['id']:x for x in clean}
                from datetime import datetime,timezone
                self.last_updated=datetime.now(timezone.utc).isoformat()
                self.loaded=True
                return True
            except Exception:
                log.exception('Catalog refresh failed, keeping previous snapshot')
                return False

    def get(self,product_id): return self.by_id.get(str(product_id))
    def search_relevant_products(self,query,limit=5):
        tokens=re.findall(r'[\wа-яіїєґ]+',query.casefold(),flags=re.I)
        tokens=[t for t in tokens if len(t)>2][:20]
        if not tokens: return []
        ranked=[]
        for p in self.products:
            name=p['name'].casefold(); sku=p['vendor_code'].casefold(); desc=p['description'].casefold()
            score=sum(7 if t in sku else 5 if t in name else 1 if t in desc else 0 for t in tokens)
            if score: ranked.append((score, bool(p['available']),p))
        ranked.sort(key=lambda r:(r[0],r[1]),reverse=True)
        return [r[2] for r in ranked[:limit]]

catalog=Catalog()
