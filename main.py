import os
import re
import html
import time
import asyncio
from typing import List, Dict, Optional
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
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

# Точний шаблон українського мобільного номера
PHONE_REGEX = re.compile(r"(?:\+?38\s*)?(?:\(0\d{2}\)|0\d{2})[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}")

SESSION_CACHE: Dict[str, dict] = {}
MAX_HISTORY_TURNS = 8
SESSION_TTL_SECONDS = 14400  # Збільшено до 4 годин завдяки ресурсам Pro

async def send_telegram_alert(text: str):
    """Безпечна відправка сповіщення про замовлення у Telegram-чат"""
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
        print(f"Помилка надсилання в Telegram: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Первинна індексація каталогу при старті
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

    task = asyncio.create_task(periodic_refresh())
    yield
    task.cancel()

app = FastAPI(title="ABV Himiya AI Pro", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

SYSTEM_INSTRUCTION = """
Ти — провідний технолог-консультант інтернет-магазину будівельної хімії та 3D-друку abvhimiya.com.ua.
Твоє завдання — технічно грамотно підбирати матеріали (клеї, герметики, ґрунтовки, фарби, філаменти), прораховувати витрати та допомагати клієнтам швидко оформити замовлення.

ПРАВИЛА РОБОТИ:
1. Спирайся ТІЛЬКИ на наданий список товарів каталогу Хорошоп.
2. Для кожного рекомендованого матеріалу обов'язково надавай:
   - Повну точну назву;
   - Актуальну ціну та статус ("В наявності" чи "Під замовлення");
   - Чому саме цей склад підходить під завдання;
   - Активне посилання: [Переглянути товар на сайті](URL).
3. Якщо клієнт висловлює намір купити ("хочу замовити", "купую", "оформіть", "як забрати"):
   - Запропонуй швидке замовлення: попроси вказати номер телефону та ім'я для підтвердження менеджером.
4. Якщо клієнт уже написав номер телефону:
   - Підтверди, що замовлення прийнято в обробку, і зазнач, що менеджер зателефонує найближчим часом для уточнення оплати й доставки.
5. Якщо товару немає в наявності:
   - Ввічливо запропонуй залишити номер телефону або зв'язатися з технічним відділом для спецзамовлення чи підбору заводського аналога.
6. Відповідай чітко, професійно та структуровано українською мовою.
"""

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = "default_user"

class ChatResponse(BaseModel):
    reply: str

@app.get("/")
def root():
    return RedirectResponse(url="/widget")

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)

@app.post("/api/refresh-catalog")
async def trigger_refresh():
    """Ендпоінт миттєвого позачергового оновлення каталогу з фіда"""
    await catalog.update_feed()
    return {
        "status": "success",
        "message": "Каталог успішно оновлено!",
        "total_products": len(catalog.products),
        "updated_at": catalog.last_updated
    }

@app.post("/api/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest):
    user_query = req.message.strip()
    session_id = req.session_id or "default_user"

    if not user_query:
        raise HTTPException(status_code=400, detail="Порожнє повідомлення")

    phone_match = PHONE_REGEX.search(user_query)

    session_data = SESSION_CACHE.get(session_id, {"last_active": time.time(), "history": []})
    history = session_data["history"]

    search_query = user_query
    if len(user_query.split()) <= 3 and history:
        last_user = next((h["text"] for h in reversed(history) if h["role"] == "user"), "")
        search_query = f"{last_user} {user_query}"

    # Збільшено вибірку до 7 карток завдяки ресурсам плану Pro
    relevant_items = catalog.search_relevant_products(search_query, limit=7)

    catalog_context = "АКТУАЛЬНІ ТОВАРИ В КАТАЛОЗІ ХОРОШОП:\n"
    if relevant_items:
        for idx, item in enumerate(relevant_items, 1):
            avail_str = "В наявності" if item["available"] else "Під замовлення / Немає"
            catalog_context += (
                f"{idx}. {item['name']}\n"
                f"   Артикул: {item.get('vendor_code', item.get('id', ''))}\n"
                f"   Ціна: {item['price']}\n"
                f"   Статус: {avail_str}\n"
                f"   Картка: {item['url']}\n"
                f"   Параметри: {item.get('params', '')}\n"
                f"   Опис: {item.get('description', '')}\n\n"
            )
    else:
        catalog_context += "Прямих збігів за ключовими словами не знайдено.\n"

    history_text = ""
    if history:
        history_text = "ПОПЕРЕДНІЙ ДІАЛОГ:\n"
        for h in history[-MAX_HISTORY_TURNS:]:
            role = "Клієнт" if h["role"] == "user" else "Консультант"
            history_text += f"{role}: {h['text']}\n"
        history_text += "\n"

    full_prompt = f"{catalog_context}\n{history_text}Поточне запитання покупця: {user_query}"

    # Найшвидша модель першою в черзі
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
                    temperature=0.2,
                    max_output_tokens=700,  # Розширено ліміт токенів для детальних інструкцій
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

        # Відправка ліда в Telegram з екрануванням HTML
        if phone_match:
            phone_number = phone_match.group(0).strip()
            safe_query = html.escape(user_query)
            
            recent_dialog = []
            for h in history[-4:]:
                safe_speaker = html.escape(h['role'])
                safe_text = html.escape(h['text'][:140])
                recent_dialog.append(f"• <b>{safe_speaker}</b>: {safe_text}")
            
            summary_dialog = "\n".join(recent_dialog)
            
            alert_text = (
                f"🚨 <b>НОВЕ ЗАМОВЛЕННЯ З САЙТУ!</b>\n\n"
                f"📞 <b>Телефон клієнта:</b> <code>{html.escape(phone_number)}</code>\n"
                f"💬 <b>Запит:</b> {safe_query}\n\n"
                f"📋 <b>Останні репліки:</b>\n{summary_dialog}\n\n"
                f"⚡ <i>Передзвоніть клієнту для підтвердження!</i>"
            )
            asyncio.create_task(send_telegram_alert(alert_text))

        return ChatResponse(reply=bot_reply)
    else:
        return ChatResponse(reply=f"Сервіс оновлює дані каталогу. Помилка: {str(last_error)}")

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
            #ai-chat-launcher {
                position: fixed; bottom: 25px; right: 25px;
                background: #0284c7; color: #fff; border-radius: 50%;
                width: 60px; height: 60px; display: flex;
                align-items: center; justify-content: center;
                cursor: pointer; box-shadow: 0 6px 20px rgba(2, 132, 199, 0.4);
                z-index: 999999; font-size: 26px; user-select: none;
                transition: transform 0.2s ease;
            }
            #ai-chat-launcher:hover { transform: scale(1.08); }
            #ai-chat-window {
                position: fixed; bottom: 95px; right: 25px;
                width: 380px; max-width: calc(100vw - 40px); height: 550px; max-height: calc(100vh - 120px);
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
            .msg { margin-bottom: 12px; max-width: 88%; padding: 10px 14px; border-radius: 12px; word-break: break-word; }
            .msg.user { background: #0284c7; color: #ffffff; margin-left: auto; border-bottom-right-radius: 2px; }
            .msg.bot { background: #ffffff; color: #1e293b; margin-right: auto; border-bottom-left-radius: 2px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); border: 1px solid #e2e8f0; }
            .msg a {
                display: inline-block; background: #0284c7; color: #ffffff !important;
                padding: 6px 12px; border-radius: 6px; font-weight: 600;
                text-decoration: none; margin-top: 6px; font-size: 12.5px;
            }
            .msg a:hover { background: #0369a1; }
            #ai-chat-input-box { display: flex; border-top: 1px solid #e2e8f0; background: #ffffff; padding: 6px; }
            #ai-chat-input { flex: 1; border: none; padding: 10px 12px; outline: none; font-size: 13.5px; }
            #ai-chat-send { background: #0284c7; color: #ffffff; border: none; padding: 0 16px; cursor: pointer; font-weight: 600; border-radius: 8px; }
        </style>
    </head>
    <body>
        <h2>Тестова зона ABV Himiya</h2>
        <p>Клікніть на значок 💬 праворуч знизу для початку консультації.</p>

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
                <div class="msg bot">Вітаю! Чим можу допомогти щодо вибору клеїв, фарб, герметиків чи пластику для 3D-друку?</div>
            </div>
            <div id="ai-chat-input-box">
                <input type="text" id="ai-chat-input" placeholder="Запитайте про товар чи введіть телефон..." onkeydown="if(event.key==='Enter') sendAIMsg()">
                <button id="ai-chat-send" onclick="sendAIMsg()">➤</button>
            </div>
        </div>

        <script>
            let sessionId = localStorage.getItem("abv_chat_session");
            if (!sessionId) {
                sessionId = "sess_" + Math.random().toString(36).substring(2, 10);
                localStorage.setItem("abv_chat_session", sessionId);
            }

            function toggleAIChat() {
                const win = document.getElementById("ai-chat-window");
                win.style.display = (win.style.display === "flex") ? "none" : "flex";
                if (win.style.display === "flex") {
                    document.getElementById("ai-chat-input").focus();
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
                box.innerHTML += `<div class="msg bot" id="${botId}"><i>Секунду, перевіряю...</i></div>`;
                box.scrollTop = box.scrollHeight;

                try {
                    const res = await fetch("/api/chat", {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({ message: text, session_id: sessionId })
                    });
                    const data = await res.json();

                    let formatted = escapeHtml(data.reply)
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
