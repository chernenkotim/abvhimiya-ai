import os
import re
import html
import time
import asyncio
from datetime import datetime
from typing import List, Dict, Optional
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from google import genai
from google.genai import types
from dotenv import load_dotenv

from feed_loader import catalog

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "abv2026")

SALESDRIVE_DOMAIN = os.getenv("SALESDRIVE_DOMAIN", "").strip().rstrip("/")
SALESDRIVE_API_KEY = os.getenv("SALESDRIVE_API_KEY", "").strip()

PHONE_REGEX = re.compile(r"(?:\+?38\s*)?(?:\(0\d{2}\)|0\d{2})[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}")

# Ловить: замовлення, замовленням, замовленні, заявка, №, #, чек, статус + номер
ORDER_ID_REGEX = re.compile(r"(?:замовленн\w*|заявк\w*|номер\w*|статус\w*|№|#)\s*(\d{4,8})", re.IGNORECASE)
ANY_DIGITS_REGEX = re.compile(r"\b\d{4,7}\b")

SESSION_CACHE: Dict[str, dict] = {}
RATE_LIMIT_CACHE: Dict[str, list] = {}
MAX_REQUESTS_PER_MINUTE = 20
MAX_HISTORY_TURNS = 8
SESSION_TTL_SECONDS = 14400

RECENT_LOGS: List[dict] = []
MAX_SAVED_LOGS = 150

SALESDRIVE_STATUSES = {
    1: "Нова",
    2: "В обробці",
    3: "Комплектується",
    4: "Відправлено",
    5: "Отримано",
    6: "Відмова / Скасовано",
    7: "Оплачено"
}

async def send_telegram_alert(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            })
    except Exception as e:
        print(f"Помилка відправки в Telegram: {e}")

async def get_salesdrive_order_status(target: str) -> dict:
    """Запит до SalesDrive CRM за номером замовлення або номером телефону"""
    if not SALESDRIVE_DOMAIN or not SALESDRIVE_API_KEY:
        print("[SalesDrive] Помилка: Не задано SALESDRIVE_DOMAIN або SALESDRIVE_API_KEY")
        return {"found": False, "message": "SalesDrive API не налаштовано."}

    clean_digits = re.sub(r"\D", "", target)
    if not clean_digits:
        return {"found": False, "message": "Немає цифр для пошуку."}

    url = f"https://{SALESDRIVE_DOMAIN}/api/order/list/"
    headers = {
        "X-Api-Key": SALESDRIVE_API_KEY,
        "Content-Type": "application/json"
    }

    # Пошук за номером замовлення (3-8 цифр без телефонних префіксів 0 або 380)
    if len(clean_digits) <= 8 and not clean_digits.startswith("0") and not clean_digits.startswith("380"):
        params = {
            "idFrom": clean_digits,
            "idTo": clean_digits,
            "filter[id]": clean_digits
        }
    else:
        phone_10 = clean_digits[-10:]
        params = {
            "filter[phone]": phone_10,
            "phone": phone_10
        }

    try:
        async with httpx.AsyncClient(timeout=9.0) as client:
            res = await client.get(url, params=params, headers=headers)
            print(f"[SalesDrive] Код відповіді API: {res.status_code}")

            if res.status_code == 200:
                data = res.json()
                orders = data.get("data", []) or data.get("orders", [])

                matched_order = None
                if orders:
                    for ord_item in orders:
                        if str(ord_item.get("id")) == clean_digits:
                            matched_order = ord_item
                            break
                    if not matched_order:
                        matched_order = orders[0]

                if matched_order:
                    order_id = matched_order.get("id", "")

                    status_val = matched_order.get("status") or matched_order.get("statusText")
                    status_id = matched_order.get("statusId")

                    if isinstance(status_val, dict):
                        status_name = status_val.get("text", "В обробці")
                    elif isinstance(status_val, str) and status_val:
                        status_name = status_val
                    elif status_id in SALESDRIVE_STATUSES:
                        status_name = SALESDRIVE_STATUSES[status_id]
                    else:
                        status_name = f"В обробці (код {status_id})" if status_id else "В обробці"

                    ttn = (
                        matched_order.get("trackingNumber") or 
                        matched_order.get("ttn") or 
                        matched_order.get("ord_delivery_data", {}).get("trackingNumber") or 
                        ""
                    )

                    order_date = matched_order.get("orderTime") or matched_order.get("orderDate") or ""

                    print(f"[SalesDrive] Успішно знайдено замовлення №{order_id}, статус: {status_name}, ТТН: {ttn}")
                    return {
                        "found": True,
                        "order_id": order_id,
                        "status": status_name,
                        "ttn": ttn,
                        "date": order_date
                    }
                else:
                    print(f"[SalesDrive] Замовлення {clean_digits} не знайдено в масиві data")
            else:
                print(f"[SalesDrive] Помилка API: {res.text}")

            return {"found": False, "message": "Замовлення за вказаними даними не знайдено."}
    except Exception as e:
        print(f"[SalesDrive] Виняток при з'єднанні: {e}")
        return {"found": False, "message": "Помилка зв'язку з CRM."}

@asynccontextmanager
async def lifespan(app: FastAPI):
    await catalog.update_feed()

    async def periodic_refresh():
        interval = int(os.getenv("REFRESH_INTERVAL_MINUTES", "30")) * 60
        while True:
            await asyncio.sleep(interval)
            await catalog.update_feed()

            now = time.time()
            expired = [s_id for s_id, data in SESSION_CACHE.items() if now - data.get("last_active", 0) > SESSION_TTL_SECONDS]
            for s_id in expired:
                SESSION_CACHE.pop(s_id, None)

            for s_id in list(RATE_LIMIT_CACHE.keys()):
                RATE_LIMIT_CACHE[s_id] = [t for t in RATE_LIMIT_CACHE[s_id] if now - t < 60]
                if not RATE_LIMIT_CACHE[s_id]:
                    RATE_LIMIT_CACHE.pop(s_id, None)

    task = asyncio.create_task(periodic_refresh())
    yield
    task.cancel()

app = FastAPI(title="ABV Himiya AI Commercial", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://abvhimiya.com.ua",
        "https://www.abvhimiya.com.ua",
        "*"
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

SYSTEM_INSTRUCTION = """
Ти — провідний технолог-консультант та менеджер інтернет-магазину будівельної хімії та 3D-друку abvhimiya.com.ua.

ПРАВИЛА РЕКОМЕНДАЦІЇ ТОВАРІВ:
1. Завжди використовуй спеціальний тег для товару:
   [PRODUCT|Назва товару|Ціна|Наявність (В наявності або Під замовлення)|URL|Артикул]
2. Текстом коротко поясни технічні переваги суміші під завдання користувача.

ПРАВИЛО ДОПРОДАЖУ (CROSS-SELL):
3. Якщо клієнт запитує фарбу або емаль — коротко нагадай про необхідність ґрунтовки або знежирювача для надійного зчеплення.
4. Якщо клієнт обирає герметик чи клей у картриджі/тубі — ввічливо запитай, чи потрібен монтажний пістолет або очисник.

ВТРАЧ
