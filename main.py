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

SALESDRIVE_DOMAIN = os.getenv("SALESDRIVE_DOMAIN", "").strip().replace("https://", "").replace("http://", "").rstrip("/")
SALESDRIVE_API_KEY = os.getenv("SALESDRIVE_API_KEY", "").strip()

PHONE_REGEX = re.compile(r"(?:\+?38\s*)?(?:\(0\d{2}\)|0\d{2})[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}")
ORDER_ID_REGEX = re.compile(r"(?:замовленн\w*|заявк\w*|номер\w*|статус\w*|№|#|\b)\s*(\d{4,8})\b", re.IGNORECASE)

SESSION_CACHE: Dict[str, dict] = {}
RATE_LIMIT_CACHE: Dict[str, list] = {}
MAX_REQUESTS_PER_MINUTE = 20
MAX_HISTORY_TURNS = 8
SESSION_TTL_SECONDS = 14400

RECENT_LOGS: List[dict] = []
MAX_SAVED_LOGS = 150

# Базові статуси замовлень SalesDrive
SALESDRIVE_STATUSES = {
    1: "Нова заявка",
    2: "В обробці",
    3: "Комплектується",
    4: "Відправлено",
    5: "Отримано",
    6: "Скасовано",
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
    """Точний запит до SalesDrive CRM через офіційні параметри idFrom / idTo"""
    if not SALESDRIVE_DOMAIN or not SALESDRIVE_API_KEY:
        print("[SalesDrive] ПОМИЛКА: Не вказано SALESDRIVE_DOMAIN або SALESDRIVE_API_KEY в Environment!")
        return {"found": False, "message": "SalesDrive API не налаштовано."}

    clean_digits = re.sub(r"\D", "", target)
    if not clean_digits:
        return {"found": False, "message": "Немає цифр для пошуку."}

    url = f"https://{SALESDRIVE_DOMAIN}/api/order/list/"
    headers = {
        "X-Api-Key": SALESDRIVE_API_KEY,
        "Accept": "application/json"
    }

    # Для пошуку за номером заявки (4-8 цифр) SalesDrive вимагає діапазон idFrom / idTo
    if len(clean_digits) <= 8 and not clean_digits.startswith("0") and not clean_digits.startswith("380"):
        params = {
            "idFrom": int(clean_digits),
            "idTo": int(clean_digits)
        }
    else:
        # Пошук за телефоном
        phone_10 = clean_digits[-10:]
        params = {"phone": phone_10}

    print(f"[SalesDrive] Запит до: {url} з параметрами: {params}")

    try:
        async with httpx.AsyncClient(timeout=9.0, follow_redirects=True) as client:
            res = await client.get(url, params=params, headers=headers)
            print(f"[SalesDrive] Відповідь сервера: {res.status_code}")

            if res.status_code == 200:
                data = res.json()
                orders = data.get("data", []) or data.get("orders", [])

                matched = None
                if orders:
                    for ord_item in orders:
                        if str(ord_item.get("id")) == clean_digits:
                            matched = ord_item
                            break
                    if not matched:
                        matched = orders[0]

                if matched:
                    order_id = matched.get("id", clean_digits)

                    # Статус замовлення
                    status_name = "В обробці"
                    if matched.get("status"):
                        st = matched.get("status")
                        status_name = st.get("text") if isinstance(st, dict) else str(st)
                    elif matched.get("statusText"):
                        status_name = matched.get("statusText")
                    elif matched.get("statusId") and matched.get("statusId") in SALESDRIVE_STATUSES:
                        status_name = SALESDRIVE_STATUSES[matched["statusId"]]

                    # ТТН Нової Пошти / Укрпошти
                    ttn = (
                        matched.get("trackingNumber") or
                        matched.get("ttn") or
                        matched.get("ord_delivery_data", {}).get("trackingNumber") or
                        ""
                    )

                    # Дата оформлення
                    order_date = matched.get("orderTime") or matched.get("orderDate") or ""
                    if order_date and "T" in str(order_date):
                        order_date = str(order_date).split("T")[0]

                    print(f"[SalesDrive] ЗНАЙДЕНО замовлення №{order_id}: статус '{status_name}', ТТН: '{ttn}'")

                    return {
                        "found": True,
                        "order_id": order_id,
                        "status": status_name,
                        "ttn": ttn,
                        "date": order_date
                    }
                else:
                    print(f"[SalesDrive] Список data порожній для номера {clean_digits}")
            else:
                print(f"[SalesDrive] Помилка API {res.status_code}: {res.text}")

            return {"found": False, "message": "Замовлення за вказаними даними не знайдено."}
    except Exception as e:
        print(f"[SalesDrive] Виняток: {e}")
        return {"found": False, "message": f"Помилка зв'язку з CRM: {e}"}

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
Ти — провідний технолог-консультант інтернет-магазину будівельної хімії та 3D-друку abvhimiya.com.ua.

ПРАВИЛА НАДАННЯ СТАТУСУ ЗАМОВЛЕНЬ (SalesDrive):
1. Якщо в контексті є блок "ІНФОРМАЦІЯ ПРО ЗАМОВЛЕННЯ З SALESDRIVE CRM":
   - НЕ ПРОСИ НОМЕР ТЕЛЕФОНУ! Вся інформація вже перед тобою.
   - Одразу і чітко напиши клієнту:
     • Номер замовлення: №...
     • Актуальний статус: ...
     • Номер ТТН: (якщо є ТТН — обов'язково напиши номер накладної та нагадай про відстеження в додатку Нової Пошти; якщо ТТН ще немає — зазнач, що замовлення готується до відправки зі складу).
2. Якщо клієнт питає "як дізнатись статус замовлення" або "який статус замовлення", але не вказав номер:
   - Ввічливо напиши: "Будь ласка, вкажіть номер вашого замовлення (наприклад: 19138) або контактний номер телефону, і я миттєво перевірю інформацію в системі!"
3. Якщо замовлення не знайдено:
   - Напиши, що замовлення за цим номером не знайдено в базі, та запропонуй уточнити номер або зв'язатися з менеджером.

ПРАВИЛА РОБОТИ З ТОВАРАМИ:
4. Якщо рекомендуєш товар, використовуй тег:
   [PRODUCT|Назва товару|Ціна|Наявність (В наявності або Під замовлення)|URL|Артикул]
5. Завжди відповідай українською мовою.
"""

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = "default_user"

class ChatResponse(BaseModel):
    reply: str

class QuickOrderRequest(BaseModel):
    name: str
    phone: str
    product_name: str
    product_price: str
    product_code: str
    session_id: Optional[str] = "default_user"

@app.get("/")
def root():
    return RedirectResponse(url="/widget")

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)

@app.post("/api/refresh-catalog")
async def trigger_refresh():
    await catalog.update_feed()
    return {
        "status": "success",
        "message": "Каталог успішно оновлено!",
        "total_products": len(catalog.products),
        "updated_at": catalog.last_updated
    }

@app.post("/api/quick-order")
async def quick_order_endpoint(req: QuickOrderRequest):
    clean_phone = req.phone.strip()
    if not clean_phone or len(re.sub(r"\D", "", clean_phone)) < 9:
        raise HTTPException(status_code=400, detail="Некоректний номер телефону")

    safe_name = html.escape(req.name.strip() or "Клієнт")
    safe_phone = html.escape(clean_phone)
    safe_prod = html.escape(req.product_name)
    safe_price = html.escape(req.product_price)
    safe_code = html.escape(req.product_code)

    alert_text = (
        f"⚡ <b>ЗАМОВЛЕННЯ В 1 КЛІК!</b>\n\n"
        f"👤 <b>Ім'я:</b> {safe_name}\n"
        f"📞 <b>Телефон:</b> <code>{safe_phone}</code>\n"
        f"📦 <b>Товар:</b> {safe_prod}\n"
        f"🏷 <b>Артикул:</b> <code>{safe_code}</code>\n"
        f"💰 <b>Ціна:</b> {safe_price}\n\n"
        f"🚀 <i>Терміново зв'яжіться для відправки!</i>"
    )
    asyncio.create_task(send_telegram_alert(alert_text))

    RECENT_LOGS.insert(0, {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": req.session_id,
        "user": f"[ШВИДКЕ ЗАМОВЛЕННЯ] {safe_prod} ({safe_phone})",
        "bot": "Заявку прийнято та надіслано в Telegram.",
        "is_order": True
    })

    return {"status": "success", "message": "Замовлення прийнято!"}

@app.get("/admin/logs", response_class=HTMLResponse)
def view_logs(secret: str = Query(..., description="Ключ доступу")):
    if secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Доступ заборонено.")

    rows_html = ""
    for item in RECENT_LOGS:
        badge = "<span style='background:#ef4444;color:#fff;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:700;'>ЗАМОВЛЕННЯ</span>" if item["is_order"] else "<span style='background:#0284c7;color:#fff;padding:2px 8px;border-radius:12px;font-size:11px;'>ПИТАННЯ</span>"
        rows_html += f"""
        <div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:16px;margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,0.05);">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                <div>{badge} <b style="margin-left:6px;font-size:12px;color:#64748b;">Сесія: {html.escape(item['session_id'])}</b></div>
                <span style="font-size:12px;color:#94a3b8;">{item['time']}</span>
            </div>
            <div style="background:#f1f5f9;padding:10px 12px;border-radius:8px;margin-bottom:8px;font-size:13.5px;color:#0f172a;">
                <b>Клієнт:</b> {html.escape(item['user'])}
            </div>
            <div style="background:#f8fafc;border-left:3px solid #0284c7;padding:10px 12px;font-size:13.5px;color:#334155;white-space:pre-wrap;">
                <b>Асистент:</b> {html.escape(item['bot'])}
            </div>
        </div>
        """

    if not rows_html:
        rows_html = "<div style='color:#64748b;text-align:center;padding:40px;'>Діалогів поки що немає.</div>"

    return f"""
    <!DOCTYPE html>
    <html lang="uk">
    <head>
        <meta charset="UTF-8">
        <title>Журнал консультацій ABV Himiya</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background:#f8fafc; padding:24px; max-width:960px; margin:auto; }}
            .header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:20px; }}
            .btn {{ background:#0284c7; color:#fff; text-decoration:none; padding:8px 14px; border-radius:8px; font-size:13px; font-weight:600; }}
        </style>
    </head>
    <body>
        <div class="header">
            <div>
                <h2 style="margin:0;color:#0f172a;">Журнал діалогів AI-асистента</h2>
                <div style="font-size:13px;color:#64748b;">Збережено записів: {len(RECENT_LOGS)} / {MAX_SAVED_LOGS}</div>
            </div>
            <a href="javascript:location.reload()" class="btn">🔄 Оновити</a>
        </div>
        <div>{rows_html}</div>
    </body>
    </html>
    """

@app.post("/api/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest):
    user_query = req.message.strip()
    session_id = req.session_id or "default_user"

    if not user_query:
        raise HTTPException(status_code=400, detail="Порожнє повідомлення")

    now = time.time()
    user_requests = RATE_LIMIT_CACHE.get(session_id, [])
    user_requests = [t for t in user_requests if now - t < 60]
    if len(user_requests) >= MAX_REQUESTS_PER_MINUTE:
        raise HTTPException(status_code=429, detail="Забагато запитів. Зачекайте хвилину.")
    user_requests.append(now)
    RATE_LIMIT_CACHE[session_id] = user_requests

    session_data = SESSION_CACHE.get(session_id, {"last_active": time.time(), "history": []})
    history = session_data["history"]

    # 1. Пошук номера в поточному або попередньому повідомленні
    phone_match = PHONE_REGEX.search(user_query)
    order_id_match = ORDER_ID_REGEX.search(user_query)

    search_target = ""
    if order_id_match:
        search_target = order_id_match.group(1)
    elif phone_match:
        search_target = phone_match.group(0)
    elif any(k in user_query.lower() for k in ["статус", "де замовленн", "що з", "де посилк"]):
        # Перевіряємо історію, якщо номер назвали крок тому
        for prev in reversed(history[-2:]):
            prev_order = ORDER_ID_REGEX.search(prev["text"])
            if prev_order:
                search_target = prev_order.group(1)
                break

    crm_context = ""
    if search_target:
        order_data = await get_salesdrive_order_status(search_target)
        if order_data.get("found"):
            crm_context = (
                f"\nІНФОРМАЦІЯ ПРО ЗАМОВЛЕННЯ З SALESDRIVE CRM:\n"
                f"- Номер замовлення: №{order_data['order_id']}\n"
                f"- Поточний статус: {order_data['status']}\n"
                f"- Номер ТТН: {order_data['ttn'] if order_data['ttn'] else 'Ще не сформовано'}\n"
                f"- Дата замовлення: {order_data.get('date', 'Сьогодні')}\n"
            )
        else:
            crm_context = f"\nІНФОРМАЦІЯ ПРО ЗАМОВЛЕННЯ: За номером '{search_target}' замовлення в SalesDrive не знайдено.\n"

    # 2. Пошук товарів у каталозі Хорошоп (якщо це не статусний запит)
    relevant_items = []
    is_status_query = any(k in user_query.lower() for k in ["статус", "де замовленн", "що з", "відправк", "посилк", "де посилк", "ттн"]) or bool(search_target)

    if not is_status_query:
        relevant_items = catalog.search_relevant_products(user_query, limit=5)

    catalog_context = "АКТУАЛЬНІ ТОВАРИ В КАТАЛОЗІ:\n"
    if relevant_items:
        for idx, item in enumerate(relevant_items, 1):
            avail_str = "В наявності" if item["available"] else "Під замовлення / Немає"
            catalog_context += (
                f"{idx}. {item['name']}\n"
                f"   Артикул: {item.get('vendor_code', item.get('id', ''))}\n"
                f"   Ціна: {item['price']}\n"
                f"   Статус: {avail_str}\n"
                f"   Картка: {item['url']}\n"
                f"   Опис: {item.get('description', '')}\n\n"
            )
    else:
        catalog_context += "Прямих товарних запитів не виявлено.\n"

    history_text = ""
    if history:
        history_text = "ПОПЕРЕДНІЙ ДІАЛОГ:\n"
        for h in history[-MAX_HISTORY_TURNS:]:
            role = "Клієнт" if h["role"] == "user" else "Консультант"
            history_text += f"{role}: {h['text']}\n"
        history_text += "\n"

    full_prompt = f"{catalog_context}\n{crm_context}\n{history_text}Поточне запитання покупця: {user_query}"

    candidate_models = ["gemini-3.5-flash-lite", "gemini-3.8-flash"]
    response = None
    last_error = None

    for model_name in candidate_models:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=full_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=0.1,
                    max_output_tokens=650,
                )
            )
            if response and response.text:
                break
        except Exception as err:
            last_error = err
            continue

    if response and response.text:
        bot_reply = response.text

        history.append({"role": "user", "text": user_query})
        history.append({"role": "bot", "text": bot_reply})
        SESSION_CACHE[session_id] = {
            "last_active": time.time(),
            "history": history[-MAX_HISTORY_TURNS:]
        }

        is_order_intent = bool(phone_match and not is_status_query and not search_target)

        log_entry = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "session_id": session_id,
            "user": user_query,
            "bot": bot_reply,
            "is_order": is_order_intent
        }
        RECENT_LOGS.insert(0, log_entry)
        if len(RECENT_LOGS) > MAX_SAVED_LOGS:
            RECENT_LOGS.pop()

        safe_user = html.escape(user_query)
        safe_bot = html.escape(bot_reply[:350]) + ("..." if len(bot_reply) > 350 else "")

        if is_order_intent:
            phone_num = phone_match.group(0).strip()
            msg = (
                f"🚨 <b>НОВЕ ЗАМОВЛЕННЯ З ЧАТУ!</b>\n\n"
                f"📞 <b>Телефон:</b> <code>{html.escape(phone_num)}</code>\n"
                f"👤 <b>Клієнт:</b> {safe_user}\n"
                f"🤖 <b>Бот:</b> {safe_bot}"
            )
        else:
            msg = (
                f"💬 <b>Нове повідомлення в чаті</b>\n"
                f"👤 <b>Клієнт:</b> {safe_user}\n"
                f"🤖 <b>Бот:</b> {safe_bot}"
            )
        asyncio.create_task(send_telegram_alert(msg))

        return ChatResponse(reply=bot_reply)
    else:
        return ChatResponse(reply=f"Сервіс оновлює каталог. Помилка: {str(last_error)}")

@app.get("/widget", response_class=HTMLResponse)
def get_widget_page():
    return """
    <!DOCTYPE html>
    <html lang="uk">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Онлайн-консультант ABV Himiya</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 20px; background: #f8fafc; }
            
            #ai-proactive-bubble {
                position: fixed; bottom: 155px; right: 25px;
                background: #ffffff; color: #0f172a; padding: 10px 14px;
                border-radius: 12px; box-shadow: 0 8px 24px rgba(0,0,0,0.14);
                font-size: 13px; font-weight: 500; z-index: 999998;
                display: none; cursor: pointer; border: 1px solid #e2e8f0;
                animation: popIn 0.3s ease;
            }
            #ai-proactive-bubble::after {
                content: ''; position: absolute; bottom: -8px; right: 25px;
                width: 0; height: 0; border-left: 8px solid transparent;
                border-right: 8px solid transparent; border-top: 8px solid #ffffff;
            }
            @keyframes popIn {
                from { opacity: 0; transform: translateY(10px); }
                to { opacity: 1; transform: translateY(0); }
            }

            #ai-chat-launcher {
                position: fixed; bottom: 85px; right: 25px;
                background: #0284c7; color: #fff; border-radius: 50%;
                width: 60px; height: 60px; display: flex;
                align-items: center; justify-content: center;
                cursor: pointer; box-shadow: 0 6px 20px rgba(2, 132, 199, 0.4);
                z-index: 999999; font-size: 26px; user-select: none;
                transition: transform 0.2s ease;
            }
            #ai-chat-launcher:hover { transform: scale(1.08); }
            #ai-chat-window {
                position: fixed; bottom: 155px; right: 25px;
                width: 400px; max-width: calc(100vw - 30px); height: 580px; max-height: calc(100vh - 180px);
                background: #ffffff; border-radius: 16px; box-shadow: 0 12px 35px rgba(0,0,0,0.18);
                display: none; flex-direction: column; overflow: hidden;
                font-family: inherit; z-index: 999999; border: 1px solid #e2e8f0;
            }
            #ai-chat-header {
                background: #0284c7; color: #ffffff; padding: 14px 18px;
                font-weight: 600; display: flex; justify-content: space-between; align-items: center;
            }
            #ai-chat-messages {
                flex: 1; padding: 14px; overflow-y: auto; background: #f8fafc;
                font-size: 13.5px; line-height: 1.5; color: #1e293b;
            }
            .msg { margin-bottom: 12px; max-width: 90%; padding: 10px 14px; border-radius: 12px; word-break: break-word; }
            .msg.user { background: #0284c7; color: #ffffff; margin-left: auto; border-bottom-right-radius: 2px; }
            .msg.bot { background: #ffffff; color: #1e293b; margin-right: auto; border-bottom-left-radius: 2px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); border: 1px solid #e2e8f0; }

            .quick-chips-box {
                display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0 12px 0;
            }
            .quick-chip {
                background: #f0f9ff; color: #0284c7; border: 1px solid #bae6fd;
                padding: 5px 10px; border-radius: 14px; font-size: 11.5px;
                cursor: pointer; font-weight: 600; transition: background 0.15s;
            }
            .quick-chip:hover { background: #e0f2fe; }

            .prod-card {
                background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px;
                padding: 12px; margin: 10px 0; box-shadow: 0 2px 6px rgba(0,0,0,0.04);
            }
            .prod-title { font-weight: 700; color: #0f172a; margin-bottom: 4px; font-size: 14px; }
            .prod-meta { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; font-size: 12.5px; }
            .prod-price { font-size: 15px; font-weight: 700; color: #0284c7; }
            .badge-instock { background: #dcfce7; color: #15803d; padding: 2px 6px; border-radius: 6px; font-weight: 600; font-size: 11px; }
            .badge-outstock { background: #fef9c3; color: #854d0e; padding: 2px 6px; border-radius: 6px; font-weight: 600; font-size: 11px; }
            .prod-actions { display: flex; gap: 6px; margin-top: 8px; }
            .btn-view {
                flex: 1; text-align: center; background: #f1f5f9; color: #334155; text-decoration: none;
                padding: 6px 8px; border-radius: 6px; font-size: 12px; font-weight: 600; border: 1px solid #cbd5e1;
            }
            .btn-order-fast {
                flex: 1.2; background: #0284c7; color: #ffffff; border: none; border-radius: 6px;
                padding: 6px 8px; font-size: 12px; font-weight: 600; cursor: pointer; text-align: center;
            }
            #ai-chat-input-box { display: flex; border-top: 1px solid #e2e8f0; background: #ffffff; padding: 6px; }
            #ai-chat-input { flex: 1; border: none; padding: 10px 12px; outline: none; font-size: 13.5px; }
            #ai-chat-send { background: #0284c7; color: #ffffff; border: none; padding: 0 16px; cursor: pointer; font-weight: 600; border-radius: 8px; }

            #fast-order-modal {
                display: none; position: absolute; inset: 0; background: rgba(15, 23, 42, 0.6);
                z-index: 1000000; align-items: center; justify-content: center; padding: 16px;
            }
            .modal-content { background: #ffffff; border-radius: 12px; padding: 18px; width: 100%; max-width: 310px; }
            .modal-title { font-weight: 700; margin-bottom: 8px; font-size: 15px; color: #0f172a; }
            .modal-input { width: 100%; box-sizing: border-box; padding: 9px 12px; border: 1px solid #cbd5e1; border-radius: 6px; margin-bottom: 8px; font-size: 13px; outline: none; }
            .modal-btns { display: flex; gap: 8px; margin-top: 10px; }
            .btn-submit { flex: 1; background: #0284c7; color: #fff; border: none; padding: 8px; border-radius: 6px; font-weight: 600; cursor: pointer; }
            .btn-cancel { background: #f1f5f9; color: #64748b; border: none; padding: 8px 12px; border-radius: 6px; cursor: pointer; }
        </style>
    </head>
    <body>
        <h2>Онлайн-консультант ABV Himiya</h2>

        <div id="ai-proactive-bubble" onclick="openChatFromBubble()">Підказати сумісність клею чи витрату на площу? 👋</div>
        <div id="ai-chat-launcher" onclick="toggleAIChat()">💬</div>

        <div id="ai-chat-window">
            <div id="ai-chat-header">
                <div>
                    <div>Технічний консультант</div>
                    <div style="font-size: 11px; font-weight: 400; opacity: 0.9;">abvhimiya.com.ua</div>
                </div>
                <span onclick="toggleAIChat()" style="cursor:pointer;font-size:18px;">✕</span>
            </div>
            
            <div id="ai-chat-messages">
                <div class="msg bot">Вітаю! Чим можу допомогти? Підкажу щодо вибору будівельної хімії чи перевірю статус вашого замовлення!</div>
                <div class="quick-chips-box">
                    <span class="quick-chip" onclick="handleChipClick('Де моє замовлення?')">📦 Де моє замовлення?</span>
                    <span class="quick-chip" onclick="handleChipClick('Фарба для ванни є?')">🛁 Фарба для ванни</span>
                    <span class="quick-chip" onclick="handleChipClick('Порадь епоксидний клей для металу')">🧪 Епоксидний клей</span>
                    <span class="quick-chip" onclick="handleChipClick('Який філамент PLA є в наявності?')">🧵 Пластик для 3D</span>
                    <span class="quick-chip" onclick="handleChipClick('Як звʼязатися з менеджером?')">📞 Менеджер</span>
                </div>
            </div>

            <div id="ai-chat-input-box">
                <input type="text" id="ai-chat-input" placeholder="Запитайте про товар або введіть номер замовлення..." onkeydown="if(event.key==='Enter') sendAIMsg()">
                <button id="ai-chat-send" onclick="sendAIMsg()">➤</button>
            </div>

            <div id="fast-order-modal">
                <div class="modal-content">
                    <div class="modal-title">⚡ Швидке замовлення</div>
                    <div id="modal-product-name" style="font-size: 12.5px; color: #475569; margin-bottom: 10px;"></div>
                    <input type="text" id="fast-name" class="modal-input" placeholder="Ваше ім'я">
                    <input type="tel" id="fast-phone" class="modal-input" placeholder="Номер телефону (+380...)">
                    <div class="modal-btns">
                        <button class="btn-cancel" onclick="closeOrderModal()">Скасувати</button>
                        <button class="btn-submit" onclick="submitQuickOrder()">Замовити</button>
                    </div>
                </div>
            </div>
        </div>

        <script>
            let sessionId = localStorage.getItem("abv_chat_session");
            if (!sessionId) {
                sessionId = "sess_" + Math.random().toString(36).substring(2, 10);
                localStorage.setItem("abv_chat_session", sessionId);
            }

            let currentOrderData = null;

            setTimeout(() => {
                const win = document.getElementById("ai-chat-window");
                if (win.style.display !== "flex") {
                    document.getElementById("ai-proactive-bubble").style.display = "block";
                }
            }, 12000);

            function openChatFromBubble() {
                document.getElementById("ai-proactive-bubble").style.display = "none";
                toggleAIChat();
            }

            function toggleAIChat() {
                const win = document.getElementById("ai-chat-window");
                const bubble = document.getElementById("ai-proactive-bubble");
                bubble.style.display = "none";
                win.style.display = (win.style.display === "flex") ? "none" : "flex";
                if (win.style.display === "flex") {
                    document.getElementById("ai-chat-input").focus();
                }
            }

            function handleChipClick(text) {
                document.getElementById("ai-chat-input").value = text;
                sendAIMsg();
            }

            function openOrderModal(name, price, code) {
                currentOrderData = { name, price, code };
                document.getElementById("modal-product-name").innerText = `${name} — ${price}`;
                document.getElementById("fast-order-modal").style.display = "flex";
            }

            function closeOrderModal() {
                document.getElementById("fast-order-modal").style.display = "none";
            }

            async function submitQuickOrder() {
                const name = document.getElementById("fast-name").value.trim();
                const phone = document.getElementById("fast-phone").value.trim();
                if (!phone) {
                    alert("Вкажіть номер телефону!");
                    return;
                }

                try {
                    await fetch("/api/quick-order", {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({
                            name: name,
                            phone: phone,
                            product_name: currentOrderData.name,
                            product_price: currentOrderData.price,
                            product_code: currentOrderData.code,
                            session_id: sessionId
                        })
                    });
                    closeOrderModal();
                    const box = document.getElementById("ai-chat-messages");
                    box.innerHTML += `<div class="msg bot" style="border-left: 3px solid #16a34a;"><b>Дякуємо!</b> Заявку на ${escapeHtml(currentOrderData.name)} прийнято в обробку. Менеджер зателефонує вам!</div>`;
                    box.scrollTop = box.scrollHeight;
                } catch(e) {
                    alert("Помилка відправки.");
                }
            }

            async function sendAIMsg() {
                const inp = document.getElementById("ai-chat-input");
                const text = inp.value.trim();
                if (!text) return;

                const box = document.getElementById("ai-chat-messages");
                box.innerHTML += `<div class="msg user">${escapeHtml(text)}</div>`;
                inp.value = "";
                box.scrollTop = box.scrollHeight;

                const botId = "ai-bot-" + Date.now();
                box.innerHTML += `<div class="msg bot" id="${botId}"><i>Секунду, уточнюю інформацію...</i></div>`;
                box.scrollTop = box.scrollHeight;

                try {
                    const res = await fetch("/api/chat", {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({ message: text, session_id: sessionId })
                    });
                    const data = await res.json();
                    let raw = data.reply;

                    raw = raw.replace(/\\[PRODUCT\\|(.*?)\\|(.*?)\\|(.*?)\\|(.*?)\\|(.*?)\\]/g, function(match, pName, pPrice, pAvail, pUrl, pCode) {
                        const isAvail = pAvail.includes("наявності");
                        const badgeClass = isAvail ? "badge-instock" : "badge-outstock";
                        const badgeText = isAvail ? "🟢 В наявності" : "🟡 Під замовлення";

                        return `
                        <div class="prod-card">
                            <div class="prod-title">${escapeHtml(pName)}</div>
                            <div class="prod-meta">
                                <span class="prod-price">${escapeHtml(pPrice)}</span>
                                <span class="${badgeClass}">${badgeText}</span>
                            </div>
                            <div class="prod-actions">
                                <a href="${escapeHtml(pUrl)}" target="_blank" class="btn-view">Картка ↗</a>
                                <button class="btn-order-fast" onclick="openOrderModal('${escapeHtml(pName).replace(/'/g, "\\\\'")}', '${escapeHtml(pPrice)}', '${escapeHtml(pCode)}')">⚡ В 1 клік</button>
                            </div>
                        </div>`;
                    });

                    let formatted = raw
                        .replace(/\\[(.*?)\\]\\((https?:\\/\\/.*?)\\)/g, '<a href="$2" target="_blank">$1 ↗</a>')
                        .replace(/\\*\\*(.*?)\\*\\*/g, '<b>$1</b>')
                        .replace(/\\n/g, "<br>");

                    document.getElementById(botId).innerHTML = formatted;
                } catch (e) {
                    document.getElementById(botId).innerText = "Помилка зв'язку з сервером.";
                }
                box.scrollTop = box.scrollHeight;
            }

            function escapeHtml(s) {
                return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
            }
        </script>
    </body>
    </html>
    """
