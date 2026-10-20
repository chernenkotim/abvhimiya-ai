import os
import asyncio
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

# Дозволяє Хорошопу надсилати запити на ваш бекенд
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

SYSTEM_INSTRUCTION = """
Ти — технічний експерт та онлайн-консультант інтернет-магазину будівельної хімії та 3D-друку abvhimiya.com.ua.
Твоє завдання: допомагати клієнтам обирати потрібні матеріали, розраховувати витрати та надавати точну інформацію з каталогу.

ПРАВИЛА ВІДПОВІДЕЙ:
1. Використовуй ТІЛЬКИ ті товари, які передані у списку знайдених товарів.
2. Якщо товар є — повідомляй його назву, актуальну ціну, наявність і давай пряме посилання у форматі Markdown: [Назва товару](URL).
3. Якщо клієнт просить підібрати клей, герметик чи фарбу — уточни тип поверхні чи умови (вулиця/приміщення), якщо це впливає на вибір.
4. Якщо потрібного товару немає в переліку — чесно скажи про це і запропонуй уточнити у менеджера.
5. Заборонено вигадувати ціни чи товари.
6. Мова відповідей — українська.
"""

class ChatRequest(BaseModel):
    message: str

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
    if not user_query:
        raise HTTPException(status_code=400, detail="Порожнє повідомлення")

    relevant_items = catalog.search_relevant_products(user_query, limit=5)
    
    catalog_context = "ТОВАРИ З КАТАЛОГУ ДЛЯ ВІДПОВІДІ:\n"
    if relevant_items:
        for idx, item in enumerate(relevant_items, 1):
            avail_str = "В наявності" if item["available"] else "Немає в наявності"
            catalog_context += (
                f"{idx}. {item['name']}\n"
                f"   Ціна: {item['price']} {item['currency']}\n"
                f"   Статус: {avail_str}\n"
                f"   Посилання: {item['url']}\n"
                f"   Параметри: {item['params']}\n"
                f"   Опис: {item['description']}\n\n"
            )
    else:
        catalog_context += "Точних збігів товарів за ключовими словами не знайдено.\n"

    prompt = f"{catalog_context}\nЗапитання покупця: {user_query}"

    try:
        # Список стабільних моделей у порядку пріоритету
candidate_models = ["gemini-2.0-flash", "gemini-1.5-flash"]
response = None
last_error = None

for model_name in candidate_models:
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
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
    return ChatResponse(reply=response.text)
else:
    return ChatResponse(reply=f"Виникла помилка: {str(last_error)}")
        return ChatResponse(reply=response.text)
    except Exception as e:
        return ChatResponse(reply=f"Виникла помилка: {str(e)}")

@app.get("/widget", response_class=HTMLResponse)
def get_widget_page():
    return """
    <!DOCTYPE html>
    <html lang="uk">
    <head>
        <meta charset="UTF-8">
        <title>Тест віджета ABV Himiya</title>
        <style>
            body { font-family: Arial, sans-serif; padding: 40px; background: #f8fafc; }
            #ai-chat-launcher {
                position: fixed; bottom: 25px; right: 25px;
                background: #0066cc; color: #fff; border-radius: 50%;
                width: 60px; height: 60px; display: flex;
                align-items: center; justify-content: center;
                cursor: pointer; box-shadow: 0 4px 14px rgba(0,0,0,0.3);
                z-index: 999999; font-size: 26px; user-select: none;
            }
            #ai-chat-window {
                position: fixed; bottom: 95px; right: 25px;
                width: 370px; height: 520px; background: #fff;
                border-radius: 14px; box-shadow: 0 8px 30px rgba(0,0,0,0.25);
                display: none; flex-direction: column; overflow: hidden;
                font-family: Arial, sans-serif; z-index: 999999; border: 1px solid #e2e8f0;
            }
            #ai-chat-header {
                background: #0066cc; color: #fff; padding: 14px 16px;
                font-weight: bold; display: flex; justify-content: space-between; align-items: center;
            }
            #ai-chat-messages {
                flex: 1; padding: 14px; overflow-y: auto; background: #f8fafc;
                font-size: 13.5px; line-height: 1.45;
            }
            .msg { margin-bottom: 10px; max-width: 85%; padding: 8px 12px; border-radius: 10px; word-break: break-word; }
            .msg.user { background: #0066cc; color: #fff; margin-left: auto; border-bottom-right-radius: 2px; }
            .msg.bot { background: #e2e8f0; color: #1e293b; margin-right: auto; border-bottom-left-radius: 2px; }
            .msg a { color: #0066cc; text-decoration: underline; font-weight: bold; }
            #ai-chat-input-box { display: flex; border-top: 1px solid #e2e8f0; background: #fff; }
            #ai-chat-input { flex: 1; border: none; padding: 12px; outline: none; font-size: 13.5px; }
            #ai-chat-send { background: #0066cc; color: #fff; border: none; padding: 0 16px; cursor: pointer; font-weight: bold; }
        </style>
    </head>
    <body>
        <h2>Тестова сторінка магазину ABV Himiya</h2>
        <p>Клікніть на синю круглу кнопку з іконкою 💬 внизу праворуч, щоб перевірити роботу асистента.</p>

        <div id="ai-chat-launcher" onclick="toggleAIChat()">💬</div>

        <div id="ai-chat-window">
            <div id="ai-chat-header">
                <span>Технічний консультант</span>
                <span onclick="toggleAIChat()" style="cursor:pointer;font-size:18px;">✕</span>
            </div>
            <div id="ai-chat-messages">
                <div class="msg bot">Вітаю! Чим можу допомогти щодо асортименту будівельної хімії чи 3D-друку?</div>
            </div>
            <div id="ai-chat-input-box">
                <input type="text" id="ai-chat-input" placeholder="Запитайте про товар чи ціну..." onkeydown="if(event.key==='Enter') sendAIMsg()">
                <button id="ai-chat-send" onclick="sendAIMsg()">➤</button>
            </div>
        </div>

        <script>
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
                        body: JSON.stringify({ message: text })
                    });
                    const data = await res.json();
                    
                    let formatted = escapeHtml(data.reply)
                        .replace(/\\[(.*?)\\]\\((https?:\\/\\/.*?)\\)/g, '<a href="$2" target="_blank">$1</a>')
                        .replace(/\\n/g, "<br>");

                    document.getElementById(botId).innerHTML = formatted;
                } catch (e) {
                    document.getElementById(botId).innerText = "Помилка підключення до сервера.";
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
