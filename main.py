import os
import asyncio
from typing import List, Dict, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from google import genai
from google.genai import types
from dotenv import load_dotenv

from feed_loader import catalog

load_dotenv()

app = FastAPI(title="ABV Himiya Assistant")

# Дозволяємо запити з магазину
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Просте збереження історії останніх повідомлень для кожного користувача
SESSION_HISTORIES: Dict[str, List[Dict[str, str]]] = {}
MAX_HISTORY_TURNS = 6

SYSTEM_INSTRUCTION = """
Ти — провідний технолог-консультант інтернет-магазину будівельної хімії та 3D-друку abvhimiya.com.ua.
Твоє завдання: фахово підбирати матеріали, прораховувати витрати та консультувати клієнтів і менеджерів.

ПРАВИЛА РОБОТИ:
1. Спирайся на список наданих товарів каталогу. Вказуй точну назву, актуальну ціну, статус наявності та активне посилання: [Назва товару](URL).
2. Якщо клієнт запитує загально ("потрібен клей/герметик/грунт") — уточни тип матеріалів (бетон, метал, дерево, пластик) та умови (зовнішні чи внутрішні роботи, вологість, навантаження).
3. Якщо потрібного товару немає або властивість не описана — чесно зазнач це та запропонуй зв'язатися з технічним відділом за контактами магазину.
4. Завжди відповідай структуровано, ввічливо, українською мовою.
"""

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = "default_user"

class ChatResponse(BaseModel):
    reply: str

@app.on_event("startup")
async def startup_event():
    await catalog.update_feed()
    
    async def periodic_refresh():
        interval = int(os.getenv("REFRESH_INTERVAL_MINUTES", "60")) * 60
        while True:
            await asyncio.sleep(interval)
            await catalog.update_feed()
            
    asyncio.create_task(periodic_refresh())

@app.post("/api/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest):
    user_query = req.message.strip()
    session_id = req.session_id or "default_user"

    if not user_query:
        raise HTTPException(status_code=400, detail="Порожнє повідомлення")

    # 1. Пошук товарів у каталозі за запитом
    relevant_items = catalog.search_relevant_products(user_query, limit=5)
    
    catalog_context = "АКТУАЛЬНІ ТОВАРИ В КАТАЛОЗІ ХОРОШОП:\n"
    if relevant_items:
        for idx, item in enumerate(relevant_items, 1):
            avail_str = "В наявності" if item["available"] else "Під замовлення / Немає"
            catalog_context += (
                f"{idx}. {item['name']}\n"
                f"   Ціна: {item['price']} {item['currency']}\n"
                f"   Статус: {avail_str}\n"
                f"   Картка: {item['url']}\n"
                f"   Параметри: {item['params']}\n"
                f"   Опис: {item['description']}\n\n"
            )
    else:
        catalog_context += "Прямих збігів за ключовими словами не знайдено.\n"

    # 2. Отримання історії діалогу для збереження контексту
    history = SESSION_HISTORIES.get(session_id, [])
    history_text = ""
    if history:
        history_text = "ПОПЕРЕДНІЙ ДІАЛОГ:\n"
        for h in history[-MAX_HISTORY_TURNS:]:
            role = "Клієнт" if h["role"] == "user" else "Консультант"
            history_text += f"{role}: {h['text']}\n"
        history_text += "\n"

    full_prompt = f"{catalog_context}\n{history_text}Поточне запитання покупця: {user_query}"

    # 3. Виклик Gemini з чергою стабільних моделей (Fallback)
    candidate_models = ["gemini-2.0-flash", "gemini-1.5-flash"]
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
                )
            )
            if response and response.text:
                break
        except Exception as err:
            last_error = err
            continue

    if response and response.text:
        bot_reply = response.text
        # Оновлюємо історію розмови
        if session_id not in SESSION_HISTORIES:
            SESSION_HISTORIES[session_id] = []
        SESSION_HISTORIES[session_id].append({"role": "user", "text": user_query})
        SESSION_HISTORIES[session_id].append({"role": "bot", "text": bot_reply})
        return ChatResponse(reply=bot_reply)
    else:
        return ChatResponse(reply=f"Сервіс тимчасово перевантажений. Помилка: {str(last_error)}")

@app.get("/widget", response_class=HTMLResponse)
def get_widget_page():
    return """
    <!DOCTYPE html>
    <html lang="uk">
    <head>
        <meta charset="UTF-8">
        <title>Онлайн-консультант ABV Himiya</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 40px; background: #f1f5f9; }
            #ai-chat-launcher {
                position: fixed; bottom: 25px; right: 25px;
                background: #0284c7; color: #fff; border-radius: 50%;
                width: 60px; height: 60px; display: flex;
                align-items: center; justify-content: center;
                cursor: pointer; box-shadow: 0 6px 18px rgba(2, 132, 199, 0.35);
                z-index: 999999; font-size: 26px; user-select: none;
                transition: transform 0.2s ease;
            }
            #ai-chat-launcher:hover { transform: scale(1.06); }
            #ai-chat-window {
                position: fixed; bottom: 95px; right: 25px;
                width: 380px; height: 530px; background: #ffffff;
                border-radius: 16px; box-shadow: 0 10px 30px rgba(0,0,0,0.18);
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
            .msg { margin-bottom: 12px; max-width: 86%; padding: 10px 14px; border-radius: 12px; word-break: break-word; }
            .msg.user { background: #0284c7; color: #ffffff; margin-left: auto; border-bottom-right-radius: 2px; }
            .msg.bot { background: #ffffff; color: #1e293b; margin-right: auto; border-bottom-left-radius: 2px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); border: 1px solid #e2e8f0; }
            .msg a { color: #0284c7; text-decoration: underline; font-weight: 600; }
            #ai-chat-input-box { display: flex; border-top: 1px solid #e2e8f0; background: #ffffff; padding: 4px; }
            #ai-chat-input { flex: 1; border: none; padding: 12px; outline: none; font-size: 13.5px; }
            #ai-chat-send { background: #0284c7; color: #ffffff; border: none; padding: 0 18px; cursor: pointer; font-weight: 600; border-radius: 8px; margin: 4px; }
        </style>
    </head>
    <body>
        <h2>Тестова зона ABV Himiya</h2>
        <p>Натисніть на віджет праворуч знизу для перевірки консультацій.</p>

        <div id="ai-chat-launcher" onclick="toggleAIChat()">💬</div>

        <div id="ai-chat-window">
            <div id="ai-chat-header">
                <span>Технічний консультант</span>
                <span onclick="toggleAIChat()" style="cursor:pointer;font-size:18px;">✕</span>
            </div>
            <div id="ai-chat-messages">
                <div class="msg bot">Вітаю! Чим можу допомогти щодо вибору хімії, герметиків чи матеріалів?</div>
            </div>
            <div id="ai-chat-input-box">
                <input type="text" id="ai-chat-input" placeholder="Запитайте про клей, фарбу, фасування..." onkeydown="if(event.key==='Enter') sendAIMsg()">
                <button id="ai-chat-send" onclick="sendAIMsg()">➤</button>
            </div>
        </div>

        <script>
            const sessionId = "sess_" + Math.random().toString(36).substring(2, 9);

            function toggleAIChat() {
                const win = document.getElementById("ai-chat-window");
                win.style.display = (win.style.display === "flex") ? "none" : "flex";
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
                box.innerHTML += `<div class="msg bot" id="${botId}"><i>Шукаю в каталозі...</i></div>`;
                box.scrollTop = box.scrollHeight;

                try {
                    const res = await fetch("/api/chat", {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({ message: text, session_id: sessionId })
                    });
                    const data = await res.json();
                    
                    let formatted = escapeHtml(data.reply)
                        .replace(/\\[(.*?)\\]\\((https?:\\/\\/.*?)\\)/g, '<a href="$2" target="_blank">$1</a>')
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
