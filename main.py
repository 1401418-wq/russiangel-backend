from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
import httpx
import os
import re
import json
import uuid
import base64
import secrets
import asyncio
import asyncpg

app = FastAPI()

import time as _time

ALLOWED_ORIGINS = [
    "https://russiangel.ru",
    "https://www.russiangel.ru",
    "https://pervyyii.ru",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
# Family-wide leads bot is hosted by pervyyii-backend. We POST lead notifications
# there and pervyyii fans them out to all subscribers via Telegram.
BROADCAST_URL = os.environ.get("BROADCAST_URL", "")
BROADCAST_SECRET = os.environ.get("BROADCAST_SECRET", "")

# ─────────────────── Лимиты и защита от абуза ───────────────────
_rate_buckets: dict[str, list[float]] = {}
MAX_MESSAGES = 40
MAX_MSG_CHARS = 8000
MAX_TOTAL_CHARS = 24000


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limited(key: str, limit: int = 10, window: int = 3600) -> bool:
    now = _time.time()
    bucket = [t for t in _rate_buckets.get(key, []) if now - t < window]
    if len(bucket) >= limit:
        _rate_buckets[key] = bucket
        return True
    bucket.append(now)
    _rate_buckets[key] = bucket
    if len(_rate_buckets) > 10000:
        for k in [k for k, v in _rate_buckets.items() if not [t for t in v if now - t < window]]:
            _rate_buckets.pop(k, None)
    return False


def _validate_chat_messages(messages) -> str | None:
    if not isinstance(messages, list) or not messages:
        return "messages is empty"
    if len(messages) > MAX_MESSAGES:
        return "too many messages"
    total = 0
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            return "invalid message role"
        content = m.get("content")
        if not isinstance(content, str):
            return "message content must be a string"
        if len(content) > MAX_MSG_CHARS:
            return "message too long"
        total += len(content)
    if total > MAX_TOTAL_CHARS:
        return "conversation too long"
    return None


# ─────────────────── Обезличивание ПД перед отправкой за рубеж (152-ФЗ) ───────────────────
# За границу (Anthropic) уходит только текст с плейсхолдерами вместо ПД.
# Реальные значения остаются на сервере, ответ обратно un-mask'ается для пользователя.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_TG_RE = re.compile(r"@[A-Za-z][A-Za-z0-9_]{3,}")
_PHONE_RE = re.compile(r"\+?[78]?[\s\-()]*\d(?:[\s\-()]*\d){9,10}")
_NAME_RE = re.compile(r"(меня зовут|мо[её] имя|зовут меня)\s+([А-ЯЁ][а-яё]+)", re.I)


def _mask_pii(text: str, mapping: dict) -> str:
    if not isinstance(text, str):
        return text

    def _ph(kind: str, val: str) -> str:
        for ph, v in mapping.items():
            if v == val:
                return ph
        ph = f"[{kind}_{len(mapping) + 1}]"
        mapping[ph] = val
        return ph

    text = _EMAIL_RE.sub(lambda m: _ph("EMAIL", m.group(0)), text)
    text = _TG_RE.sub(lambda m: _ph("TG", m.group(0)), text)
    text = _PHONE_RE.sub(lambda m: _ph("PHONE", m.group(0)), text)
    text = _NAME_RE.sub(lambda m: m.group(1) + " " + _ph("NAME", m.group(2)), text)
    return text


def _unmask(text: str, mapping: dict) -> str:
    for ph, val in mapping.items():
        text = text.replace(ph, val)
    return text


def _mask_messages(messages: list) -> tuple[list, dict]:
    """(обезличенные messages, mapping) — для зарубежного LLM."""
    mapping: dict = {}
    masked = [
        {"role": m["role"], "content": _mask_pii(str(m.get("content", "")), mapping)}
        for m in messages
    ]
    return masked, mapping


def _extract_contact_local(text: str) -> dict:
    """Извлекает контакт из текста ЛОКАЛЬНО (без LLM) — чтобы ПД не уходили за рубеж."""
    phone = _PHONE_RE.search(text)
    tg = _TG_RE.search(text)
    email = _EMAIL_RE.search(text)
    name_m = _NAME_RE.search(text)
    if phone:
        contact = phone.group(0).strip()
    elif tg:
        contact = tg.group(0).strip()
    elif email:
        contact = email.group(0).strip()
    else:
        contact = None
    name = name_m.group(2) if name_m else None
    return {"name": name, "contact": contact, "has_lead": bool(contact)}


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


# ─────────────────── Broadcast to family bot ───────────────────

async def broadcast_lead(payload: dict) -> None:
    """Send a lead notification to the family-wide bot hosted at BROADCAST_URL.
    Fire-and-forget; never raises."""
    if not (BROADCAST_URL and BROADCAST_SECRET):
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                BROADCAST_URL,
                json=payload,
                headers={"X-Broadcast-Secret": BROADCAST_SECRET},
            )
    except Exception as e:
        print(f"[broadcast] failed: {e}")


# ─────────────────── Metadata extraction ───────────────────

EXTRACTION_SYSTEM = """Ты обрабатываешь диалог посетителя сайта Ангелины ("Фея русского языка") с её AI-помощником. Ангелина — преподаватель русского языка, продаёт игры для уроков, курсы для учеников/учителей и репетиторство. Контактные данные уже вырезаны и заменены плейсхолдерами вида [NAME_1], [PHONE_2] — не пытайся их угадать.

Извлеки структурированные данные. Верни СТРОГО валидный JSON, без markdown, без комментариев, в одну строку или с переносами. Поля:

{
  "business_niche": кто посетитель — одна из строк ["учитель","ученик/родитель","репетитор","методист","другое","не определено"],
  "tariff_interest": что человек присматривает — одна из ["игры PowerPoint","игры Genially","пакет Сундучок","пакет Сокровище","все материалы","игра на заказ","курс для учеников","курс для учителей","репетиторство","несколько","не определено"],
  "intent_summary": строка 1-2 предложения, что человек спрашивал и чего хочет
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
        # Контакт извлекаем ЛОКАЛЬНО (в Claude ПД не уходят)
        local = _extract_contact_local(transcript)
        has_lead_new = local["has_lead"]
        # Нишу/тариф/интент — из Claude по ОБЕЗЛИЧЕННОМУ транскрипту
        emap: dict = {}
        masked_transcript = _mask_pii(transcript, emap)
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
                    "messages": [{"role": "user", "content": masked_transcript}],
                },
            )
            data = response.json()
        text = "".join(b.get("text", "") for b in (data.get("content") or []) if b.get("type") == "text").strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        extracted = json.loads(text)
        intent_summary = _unmask(extracted.get("intent_summary") or "", emap)
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE sessions SET
                    business_niche=$2, tariff_interest=$3, intent_summary=$4,
                    has_lead=$5, lead_name=$6, lead_contact=$7, last_extracted_at=NOW()
                   WHERE session_id=$1""",
                session_id,
                extracted.get("business_niche") or "не определено",
                extracted.get("tariff_interest") or "не определено",
                intent_summary,
                has_lead_new,
                local["name"],
                local["contact"],
            )
        if has_lead_new and not (sess and sess["lead_notified"]):
            await broadcast_lead({
                "source": "russiangel.ru",
                "name": local["name"],
                "contact": local["contact"],
                "niche": extracted.get("business_niche"),
                "tariff": extracted.get("tariff_interest"),
                "summary": intent_summary,
            })
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

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    messages = body.get("messages", [])
    err = _validate_chat_messages(messages)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    ip = _client_ip(request)
    if ip not in ("127.0.0.1", "::1", "localhost", "unknown") and _rate_limited(f"chat:{ip}", limit=60, window=3600):
        return JSONResponse({"error": "Слишком много запросов. Попробуйте позже."}, status_code=429)
    if _rate_limited("chat:_global", limit=300, window=60):
        return JSONResponse({"error": "Сервис перегружен, попробуйте через минуту."}, status_code=429)

    session_id = body.get("session_id") or str(uuid.uuid4())
    referrer = body.get("referrer") or ""
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

    # Обезличиваем перед отправкой за рубеж (Anthropic, США): ПД → плейсхолдеры
    masked_messages, pii_map = _mask_messages(messages)

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
                    "messages": masked_messages,
                },
            )
            data = response.json()
    except httpx.HTTPError as e:
        return JSONResponse({"error": f"upstream request failed: {e}"}, status_code=502)

    if "error" in data:
        print(f"[chat] upstream error: {data['error']}")
        return JSONResponse({"error": "upstream error"}, status_code=response.status_code or 500)

    content = data.get("content") or []
    text_parts = [block.get("text", "") for block in content if block.get("type") == "text"]
    reply = "".join(text_parts).strip()
    if not reply:
        print(f"[chat] empty reply, raw={data}")
        return JSONResponse({"error": "empty reply from model"}, status_code=502)

    # Возвращаем настоящие значения в ответ пользователю (Claude их не видел)
    reply = _unmask(reply, pii_map)

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
    }


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

@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"status": "ok", "service": "RussianAngel AI Agent"}
