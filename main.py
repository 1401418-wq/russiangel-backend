from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
import httpx
import os
import json
import uuid
import base64
import secrets
import asyncio
import asyncpg

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
TELEGRAM_SUBSCRIBE_CODE = os.environ.get("TELEGRAM_SUBSCRIBE_CODE", "")

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


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_activity_at TIMESTAMPTZ DEFAULT NOW(),
    user_agent TEXT,
    referrer TEXT,
    ip TEXT,
    business_niche TEXT,
    tariff_interest TEXT,
    intent_summary TEXT,
    lead_name TEXT,
    lead_contact TEXT,
    has_lead BOOLEAN DEFAULT FALSE,
    lead_notified BOOLEAN DEFAULT FALSE,
    msg_count INTEGER DEFAULT 0,
    last_extracted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read INTEGER
);

CREATE TABLE IF NOT EXISTS telegram_subscribers (
    chat_id BIGINT PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    subscribed_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_lead ON sessions(has_lead) WHERE has_lead = TRUE;
"""


pool: asyncpg.Pool | None = None


@app.on_event("startup")
async def startup() -> None:
    global pool
    if not DATABASE_URL:
        print("[startup] DATABASE_URL not set — analytics disabled")
        return
    try:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        async with pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
        print("[startup] DB pool ready, schema applied")
    except Exception as e:
        print(f"[startup] DB init failed: {e}")
        pool = None


@app.on_event("shutdown")
async def shutdown() -> None:
    if pool:
        await pool.close()


# ─────────────────── Telegram ───────────────────

async def tg_send(text: str) -> None:
    if not (TELEGRAM_BOT_TOKEN and pool):
        return
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT chat_id FROM telegram_subscribers")
        if not rows:
            return
        async with httpx.AsyncClient(timeout=10) as client:
            for r in rows:
                try:
                    await client.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        json={"chat_id": r["chat_id"], "text": text, "parse_mode": "HTML"},
                    )
                except Exception as e:
                    print(f"[tg] send to {r['chat_id']} failed: {e}")
    except Exception as e:
        print(f"[tg] tg_send failed: {e}")


async def _tg_send_to(chat_id: int, text: str) -> None:
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
    except Exception as e:
        print(f"[tg] direct send failed: {e}")


@app.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if not TELEGRAM_WEBHOOK_SECRET or not secrets.compare_digest(secret, TELEGRAM_WEBHOOK_SECRET):
        raise HTTPException(404)
    if not pool:
        return {"ok": True}
    update = await request.json()
    msg = update.get("message") or update.get("edited_message") or {}
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return {"ok": True}
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        code = parts[1].strip() if len(parts) > 1 else ""
        if not TELEGRAM_SUBSCRIBE_CODE or not secrets.compare_digest(code, TELEGRAM_SUBSCRIBE_CODE):
            return {"ok": True}
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO telegram_subscribers (chat_id, username, first_name)
                       VALUES ($1, $2, $3)
                       ON CONFLICT (chat_id) DO UPDATE
                       SET username=EXCLUDED.username, first_name=EXCLUDED.first_name""",
                    chat_id, chat.get("username"), chat.get("first_name"),
                )
            await _tg_send_to(chat_id, "Подписаны на уведомления о новых заявках на russiangel.ru ✓")
        except Exception as e:
            print(f"[tg] /start failed: {e}")
    elif text.startswith("/stop"):
        try:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM telegram_subscribers WHERE chat_id=$1", chat_id)
            await _tg_send_to(chat_id, "Отписаны от уведомлений.")
        except Exception as e:
            print(f"[tg] /stop failed: {e}")
    return {"ok": True}


# ─────────────────── Metadata extraction ───────────────────

EXTRACTION_SYSTEM = """Ты обрабатываешь диалог посетителя сайта Ангелины ("Фея русского языка") с её AI-помощником. Ангелина — преподаватель русского языка, продаёт игры для уроков, курсы для учеников/учителей и репетиторство.

Извлеки структурированные данные. Верни СТРОГО валидный JSON, без markdown, без комментариев, в одну строку или с переносами. Поля:

{
  "business_niche": кто посетитель — одна из строк ["учитель","ученик/родитель","репетитор","методист","другое","не определено"],
  "tariff_interest": что человек присматривает — одна из ["игры PowerPoint","игры Genially","пакет Сундучок","пакет Сокровище","все материалы","игра на заказ","курс для учеников","курс для учителей","репетиторство","несколько","не определено"],
  "intent_summary": строка 1-2 предложения, что человек спрашивал и чего хочет,
  "has_lead": true ТОЛЬКО если человек явно оставил имя И контакт (телефон/telegram/email/whatsapp). Если оставил только имя или только сферу — false.,
  "lead_name": имя или null,
  "lead_contact": контакт или null
}"""


async def extract_metadata(session_id: str) -> None:
    if not (pool and ANTHROPIC_API_KEY):
        return
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, content FROM messages WHERE session_id=$1 ORDER BY created_at LIMIT 40",
                session_id,
            )
            sess = await conn.fetchrow(
                "SELECT has_lead, lead_notified FROM sessions WHERE session_id=$1",
                session_id,
            )
        if not rows:
            return
        transcript = "\n\n".join(f"[{r['role']}] {r['content']}" for r in rows)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 400,
                    "system": EXTRACTION_SYSTEM,
                    "messages": [{"role": "user", "content": transcript}],
                },
            )
            data = response.json()
        text = "".join(b.get("text", "") for b in (data.get("content") or []) if b.get("type") == "text").strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        extracted = json.loads(text)
        has_lead_new = bool(extracted.get("has_lead"))
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE sessions SET
                    business_niche=$2, tariff_interest=$3, intent_summary=$4,
                    has_lead=$5, lead_name=$6, lead_contact=$7, last_extracted_at=NOW()
                   WHERE session_id=$1""",
                session_id,
                extracted.get("business_niche") or "не определено",
                extracted.get("tariff_interest") or "не определено",
                extracted.get("intent_summary"),
                has_lead_new,
                extracted.get("lead_name"),
                extracted.get("lead_contact"),
            )
        if has_lead_new and not (sess and sess["lead_notified"]):
            who = extracted.get("business_niche") or "—"
            name = extracted.get("lead_name") or "—"
            contact = extracted.get("lead_contact") or "—"
            product = extracted.get("tariff_interest") or "—"
            summary = extracted.get("intent_summary") or ""
            await tg_send(
                f"🎯 <b>Новая заявка с russiangel.ru</b>\n\n"
                f"<b>Имя:</b> {name}\n"
                f"<b>Контакт:</b> {contact}\n"
                f"<b>Кто:</b> {who}\n"
                f"<b>Интерес:</b> {product}\n\n"
                f"<i>{summary}</i>"
            )
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE sessions SET lead_notified=TRUE WHERE session_id=$1", session_id
                )
    except Exception as e:
        print(f"[extract] failed for {session_id}: {e}")


# ─────────────────── Chat ───────────────────

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

    session_id = body.get("session_id") or str(uuid.uuid4())
    referrer = body.get("referrer") or ""
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "").split(",")[0].strip()
    user_agent = request.headers.get("user-agent", "")[:500]

    last_user = messages[-1] if messages else None
    if pool and last_user and last_user.get("role") == "user":
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO sessions (session_id, user_agent, referrer, ip)
                       VALUES ($1, $2, $3, $4)
                       ON CONFLICT (session_id) DO UPDATE SET last_activity_at=NOW()""",
                    session_id, user_agent, referrer[:500], ip[:64],
                )
                await conn.execute(
                    "INSERT INTO messages (session_id, role, content) VALUES ($1, 'user', $2)",
                    session_id, str(last_user.get("content", ""))[:8000],
                )
        except Exception as e:
            print(f"[chat] db log user msg failed: {e}")

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
        return JSONResponse({"error": "empty reply from model", "raw": data}, status_code=502)

    usage = data.get("usage") or {}

    if pool:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO messages
                        (session_id, role, content, input_tokens, output_tokens, cache_read)
                       VALUES ($1, 'assistant', $2, $3, $4, $5)""",
                    session_id, reply[:8000],
                    usage.get("input_tokens"), usage.get("output_tokens"),
                    usage.get("cache_read_input_tokens"),
                )
                await conn.execute(
                    """UPDATE sessions SET msg_count = msg_count + 2, last_activity_at = NOW()
                       WHERE session_id=$1""",
                    session_id,
                )
            asyncio.create_task(extract_metadata(session_id))
        except Exception as e:
            print(f"[chat] db log assistant msg failed: {e}")

    return JSONResponse({"reply": reply, "usage": usage, "session_id": session_id})


# ─────────────────── Admin ───────────────────

def require_admin(request: Request) -> None:
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "Admin not configured")
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="admin"'})
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8", errors="ignore")
        _, _, pwd = decoded.partition(":")
    except Exception:
        raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="admin"'})
    if not secrets.compare_digest(pwd, ADMIN_PASSWORD):
        raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="admin"'})


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    require_admin(request)
    try:
        return FileResponse("admin.html")
    except Exception:
        return HTMLResponse("<h1>admin.html not found</h1>", status_code=500)


@app.get("/admin/data")
async def admin_data(request: Request):
    require_admin(request)
    if not pool:
        return JSONResponse({"error": "database not configured"}, status_code=503)
    async with pool.acquire() as conn:
        sessions = await conn.fetch(
            """SELECT session_id, created_at, last_activity_at, msg_count,
                      business_niche, tariff_interest, intent_summary,
                      has_lead, lead_name, lead_contact, referrer, ip, user_agent
               FROM sessions
               ORDER BY created_at DESC
               LIMIT 1000"""
        )
        stats = await conn.fetchrow(
            """SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE has_lead) AS leads,
                COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '1 day') AS today,
                COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '7 days') AS week
               FROM sessions"""
        )
        subs = await conn.fetch(
            "SELECT chat_id, username, first_name, subscribed_at FROM telegram_subscribers ORDER BY subscribed_at DESC"
        )
    return {
        "stats": dict(stats) if stats else {},
        "sessions": [
            {
                **dict(s),
                "created_at": s["created_at"].isoformat() if s["created_at"] else None,
                "last_activity_at": s["last_activity_at"].isoformat() if s["last_activity_at"] else None,
            }
            for s in sessions
        ],
        "telegram_subscribers": len(subs),
        "subscribers": [
            {
                "chat_id": str(s["chat_id"]),
                "username": s["username"],
                "first_name": s["first_name"],
                "subscribed_at": s["subscribed_at"].isoformat() if s["subscribed_at"] else None,
            }
            for s in subs
        ],
    }


@app.delete("/admin/subscriber/{chat_id}")
async def admin_unsubscribe(chat_id: int, request: Request):
    require_admin(request)
    if not pool:
        return JSONResponse({"error": "database not configured"}, status_code=503)
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM telegram_subscribers WHERE chat_id=$1", chat_id)
    return {"ok": True, "deleted": result.split()[-1] if result else "0"}


@app.get("/admin/session/{session_id}")
async def admin_session_detail(session_id: str, request: Request):
    require_admin(request)
    if not pool:
        return JSONResponse({"error": "database not configured"}, status_code=503)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT role, content, created_at FROM messages
               WHERE session_id=$1 ORDER BY created_at""",
            session_id,
        )
    return [
        {"role": r["role"], "content": r["content"], "created_at": r["created_at"].isoformat()}
        for r in rows
    ]


# ─────────────────── Public ───────────────────

@app.get("/")
async def root():
    return {"status": "ok", "service": "RussianAngel AI Agent"}
