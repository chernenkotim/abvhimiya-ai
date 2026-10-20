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
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0.2,
            )
        )
        return ChatResponse(reply=response.text)
    except Exception as e:
        return ChatResponse(reply=f"Виникла помилка: {str(e)}")

@app.get("/widget", response_class=HTMLResponse)
def get_widget_page():
    return """
    <!DOCTYPE html>
    <html lang="uk">
    <head><meta charset="UTF-8"><title>Тест віджета</title></head>
    <body style="font-family: Arial, sans-serif; padding: 40px; background: #f0f2f5;">
        <h2>Тестова сторінка магазину</h2>
        <p>Клікніть на синю кнопку з іконкою повідомлення в правому нижньому кутку, щоб перевірити роботу асистента.</p>
    </body>
    </html>
    """