from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import httpx
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["*"],
)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

SYSTEM = """Ты — умный помощник Ангелины, преподавателя русского языка, известной как "Фея русского языка". 
Отвечай серьёзно, по делу, с правильной пунктуацией. Без лишних эмодзи — максимум 1-2 в сообщении если уместно. 
Отвечай только на русском языке.

ИНФОРМАЦИЯ ОБ АНГЕЛИНЕ:
- Преподаватель русского языка, магистр МГУ, эксперт ЕГЭ, 8 лет опыта
- Первая геймифицировала обучение русскому языку
- Автор курсов для учителей и учеников
- Более 8000 подписчиков в Instagram (@russiangel)
- Сайт: russiangel.ru
- Telegram для связи и покупок: @russiangel_me
- WhatsApp: +7 969 044-43-33

ПРОДУКТЫ:

1. ИГРЫ ДЛЯ УРОКОВ (для учителей):
- Игры в PowerPoint — работают без интернета, без подписок, редактируемые
- Игры в Genially — онлайн, для использования достаточен бесплатный аккаунт
- Темы игр: Гарри Поттер, Marvel, Уэнсдей, Эйфория, вампиры и другие
- Все игры рассчитаны на 8–11 классы
- Задания к каждой игре можно посмотреть в видеообзорах на сайте
- ВСЕ игры можно редактировать — передаются в редактируемом формате
- Ангелина создаёт игры на заказ с нуля — от 2500 руб.
- Также может отредактировать готовую игру под конкретный запрос
- Для выбора подходящей игры Ангелина помогает лично в TG

2. ЦЕНЫ НА ИГРЫ:
- Отдельные игры: от 500 руб. и от 1000 руб. за штуку
- Пакет "Сундучок" (6 игр на выбор): 3800 руб.
- Пакет "Сокровище": 9800 руб.
- Все материалы оптом (40+ штук): 24500 руб.
- Игра на заказ с нуля: от 2500 руб.
- Авторские расширения Genially: 2500 руб.

3. КУРСЫ ДЛЯ УЧЕНИКОВ:
- Курс ОГЭ по русскому: 3500 руб.
- Курс по сочинению ЕГЭ: 3500 руб.
- Аргументы к итоговому сочинению: 2500 руб.
- Рабочие тетради ОГЭ (в игровом формате): от 1300 руб.

4. КУРСЫ ДЛЯ УЧИТЕЛЕЙ:
- "Как создавать игры в PowerPoint": 3500 руб.
- "Курс по Genially": 3500 руб.
- "Курс ОГЭ: как готовить детей": 3500 руб.
- "Канал педагога": 3000 руб.
- Мастер-класс "ИИ для педагога": 2000 руб.

5. РЕПЕТИТОРСТВО:
- Индивидуальные занятия: 3000–5000 руб./час
- Онлайн и очно в Москве

6. КАК ОПЛАТИТЬ:
- Написать Ангелине в Telegram: @russiangel_me
- Она пришлёт реквизиты для оплаты

ПРАВИЛА:
- Вопрос про оплату → написать в TG @russiangel_me
- Вопрос про подписки → никаких подписок не нужно
- Вопрос какая игра подойдёт → все игры для 8-11 классов, посмотреть видеообзоры, написать Ангелине лично
- Всегда указывай @russiangel_me если вопрос требует личного обсуждения
- Не придумывай информацию которой нет выше"""


@app.post("/chat")
async def chat(request: Request):
    if not ANTHROPIC_API_KEY:
        return JSONResponse(
            {"error": "ANTHROPIC_API_KEY is not configured on the server"},
            status_code=500,
        )

    body = await request.json()
    messages = body.get("messages", [])
    if not messages:
        return JSONResponse({"error": "messages is empty"}, status_code=400)

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 2000,
                    "system": [
                        {
                            "type": "text",
                            "text": SYSTEM,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    "messages": messages,
                },
            )
            data = response.json()
    except httpx.HTTPError as e:
        return JSONResponse({"error": f"upstream request failed: {e}"}, status_code=502)

    if "error" in data:
        return JSONResponse({"error": data["error"]}, status_code=response.status_code or 500)

    content = data.get("content") or []
    text_parts = [block.get("text", "") for block in content if block.get("type") == "text"]
    reply = "".join(text_parts).strip()
    if not reply:
        return JSONResponse(
            {"error": "empty reply from model", "raw": data},
            status_code=502,
        )
    return JSONResponse({"reply": reply, "usage": data.get("usage")})


@app.get("/")
async def root():
    return {"status": "ok", "service": "RussianAngel AI Agent"}
