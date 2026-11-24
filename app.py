import asyncio
import io
import json
import hashlib
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import date
from urllib.parse import quote

import requests
import urllib3
from dotenv import load_dotenv
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
VERIFY_DIR = os.path.join(BASE_DIR, "verify")
DB_PATH = os.path.join(BASE_DIR, "cards.db")
FONT_PATH = os.path.join(BASE_DIR, "fonts", "DejaVuSans.ttf")

# ============================================================
# КОНФИГ
# ============================================================
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY", "")
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

# GigaChat использует сертификаты российского УЦ. По умолчанию проверка
# отключена (как и раньше); для продакшена: GIGACHAT_VERIFY_SSL=1 и
# установленный корневой сертификат Минцифры.
VERIFY_SSL = os.getenv("GIGACHAT_VERIFY_SSL", "0") == "1"
if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MAX_INPUT_CHARS = 15000
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024
MAX_EXPORT_CARDS = 100

DAILY_LIMIT = 5          # генераций в сутки на один IP
CLARIFY_LIMIT = 1        # уточнений к одной карточке на один IP

# Если на первой модели кончился баланс токенов — пробуем следующую.
MODELS_FOR_CARDS = ["GigaChat", "GigaChat-Pro"]
MODELS_FOR_CLARIFY = ["GigaChat-Pro", "GigaChat"]


class QuotaExceeded(Exception):
    """У модели закончился бесплатный баланс токенов."""


# ============================================================
# БАЗА ДАННЫХ (SQLite)
# На бесплатном Render диск не постоянный: файл может обнулиться
# при передеплое.
# ============================================================
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    try:
        with conn:  # commit / rollback
            yield conn
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS card_sets (
                id TEXT PRIMARY KEY,
                cards_json TEXT NOT NULL,
                title TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ip_usage (
                ip TEXT NOT NULL,
                date TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (ip, date)
            );
            CREATE TABLE IF NOT EXISTS clarify_usage (
                key TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0
            );
            """
        )


init_db()


def client_ip(request: Request) -> str:
    """За прокси (Render) реальный IP лежит в X-Forwarded-For."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def remaining_quota(ip: str) -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT count FROM ip_usage WHERE ip = ? AND date = ?",
            (ip, date.today().isoformat()),
        ).fetchone()
    return max(0, DAILY_LIMIT - (row[0] if row else 0))


def use_quota(ip: str) -> int:
    """Атомарно списывает одну генерацию. Возвращает остаток или кидает 429."""
    today = date.today().isoformat()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO ip_usage (ip, date, count) VALUES (?, ?, 1) "
            "ON CONFLICT(ip, date) DO UPDATE SET count = count + 1 WHERE count < ?",
            (ip, today, DAILY_LIMIT),
        )
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=429,
                detail=f"Дневной лимит ({DAILY_LIMIT} генераций) исчерпан. Он обновится завтра.",
            )
        used = conn.execute(
            "SELECT count FROM ip_usage WHERE ip = ? AND date = ?", (ip, today)
        ).fetchone()[0]
    return DAILY_LIMIT - used


def refund_quota(ip: str):
    """Возвращает генерацию, если запрос к модели не удался."""
    with db() as conn:
        conn.execute(
            "UPDATE ip_usage SET count = MAX(count - 1, 0) WHERE ip = ? AND date = ?",
            (ip, date.today().isoformat()),
        )


def use_clarify(ip: str, card_question: str):
    key = ip + "|" + hashlib.sha256(card_question.encode("utf-8")).hexdigest()[:16]
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO clarify_usage (key, count) VALUES (?, 1) "
            "ON CONFLICT(key) DO UPDATE SET count = count + 1 WHERE count < ?",
            (key, CLARIFY_LIMIT),
        )
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=429,
                detail="К этой карточке уточнение уже использовано.",
            )


# ============================================================
# GIGACHAT
# ============================================================
_session = requests.Session()
_session.verify = VERIFY_SSL
_token = {"value": None, "expires_at": 0.0}

# Тариф GigaChat допускает 1 одновременный запрос. Блокирующие вызовы
# уходят в поток (event loop не замирает), а Lock выстраивает их в очередь.
_gigachat_lock = asyncio.Lock()


def get_token() -> str:
    if not GIGACHAT_AUTH_KEY:
        raise HTTPException(status_code=500, detail="GIGACHAT_AUTH_KEY не задан. Добавь его в .env файл.")

    now = time.time()
    if _token["value"] and _token["expires_at"] > now + 5:
        return _token["value"]

    try:
        resp = _session.post(
            OAUTH_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "RqUID": str(uuid.uuid4()),
                "Authorization": f"Basic {GIGACHAT_AUTH_KEY}",
            },
            data={"scope": GIGACHAT_SCOPE},
            timeout=30,
        )
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="GigaChat не ответил за 30 секунд (получение токена). Попробуй ещё раз.")
    except requests.exceptions.ConnectionError:
        raise HTTPException(status_code=502, detail="Не удалось подключиться к серверу GigaChat.")

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Ошибка получения токена GigaChat: {resp.status_code}")

    payload = resp.json()
    expires_raw = payload.get("expires_at", 0)
    _token["value"] = payload["access_token"]
    _token["expires_at"] = expires_raw / 1000 if expires_raw > 10**12 else now + 1700
    return _token["value"]


def ask_gigachat(prompt: str, model: str, max_tokens: int, temperature: float = 0.3):
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты помощник, который помогает студентам разбираться в лекциях. "
                    "Следуй формату, который просит пользователь, точно и без отступлений."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        resp = _session.post(
            CHAT_URL,
            headers={"Accept": "application/json", "Authorization": f"Bearer {get_token()}"},
            json=body,
            timeout=60,
        )
    except requests.exceptions.Timeout:
        raise HTTPException(
            status_code=504,
            detail="GigaChat не ответил за 60 секунд. Возможно, лекция слишком большая — попробуй сократить текст.",
        )
    except requests.exceptions.ConnectionError:
        raise HTTPException(status_code=502, detail="Не удалось подключиться к серверу GigaChat. Повтори попытку.")

    if resp.status_code in (402, 429):  # закончился баланс токенов модели
        raise QuotaExceeded(model)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Ошибка запроса к GigaChat: {resp.status_code}")

    choice = resp.json()["choices"][0]
    return choice["message"]["content"], choice.get("finish_reason", "unknown")


def ask_with_fallback(prompt: str, models: list[str], max_tokens: int):
    for model in models:
        try:
            return ask_gigachat(prompt, model, max_tokens)
        except QuotaExceeded:
            continue
    raise HTTPException(
        status_code=402,
        detail=f"Лимит токенов исчерпан на всех доступных моделях ({', '.join(models)}).",
    )


async def gigachat(prompt: str, models: list[str], max_tokens: int = 8000):
    async with _gigachat_lock:
        return await asyncio.to_thread(ask_with_fallback, prompt, models, max_tokens)


# ============================================================
# РАЗБОР ОТВЕТА МОДЕЛИ
# ============================================================
def extract_json(raw: str, finish_reason: str = "unknown") -> list:
    """Достаёт JSON из ответа: срезает ```-обёртку, чинит обрезанный ответ
    (оставляя целые карточки) и приводит объект вида {"0":..., "1":...} к списку."""
    text = re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip()
    match = re.search(r"[\[{].*", text, re.DOTALL)
    if not match:
        raise HTTPException(status_code=502, detail="Модель вернула пустой или нечитаемый ответ.")
    text = match.group(0)

    try:
        parsed = json.loads(text, strict=False)
    except json.JSONDecodeError:
        last = text.rfind("}")
        closing = "]" if text[0] == "[" else "}"
        try:
            if last == -1:
                raise json.JSONDecodeError("no object", text, 0)
            parsed = json.loads(text[: last + 1] + closing, strict=False)
        except json.JSONDecodeError:
            hint = (
                "ответ обрезан по лимиту длины"
                if finish_reason == "length"
                else f"модель завершила ответ некорректно ({finish_reason})"
            )
            raise HTTPException(status_code=502, detail=f"Не удалось разобрать ответ модели: {hint}. Попробуй ещё раз.")

    return list(parsed.values()) if isinstance(parsed, dict) else parsed


def clean_cards(items) -> list[dict]:
    """Приводит карточки к единому виду: только словари, поля — строки."""
    cards = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        tags = item.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        cards.append({
            "title": str(item.get("title") or "").strip(),
            "question": str(item.get("question") or "").strip(),
            "answer": str(item.get("answer") or "").strip(),
            "tags": [str(t).strip() for t in tags if str(t).strip()][:3],
        })
    return cards


# ============================================================
# ТЕКСТ ИЗ ФАЙЛОВ (тяжёлые библиотеки грузим только когда нужны)
# ============================================================
def extract_text(filename: str, content: bytes) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    stream = io.BytesIO(content)

    if ext in (".txt", ".md"):
        return content.decode("utf-8", errors="ignore")

    if ext == ".pdf":
        from pypdf import PdfReader
        return "\n".join(filter(None, (p.extract_text() for p in PdfReader(stream).pages)))

    if ext == ".docx":
        from docx import Document
        return "\n".join(p.text for p in Document(stream).paragraphs if p.text.strip())

    if ext == ".pptx":
        from pptx import Presentation
        lines = []
        for slide in Presentation(stream).slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        line = "".join(run.text for run in para.runs)
                        if line.strip():
                            lines.append(line)
        return "\n".join(lines)

    raise HTTPException(status_code=400, detail=f"Формат {ext or 'без расширения'} не поддерживается")


# ============================================================
# ПРОМПТЫ
# ============================================================
def build_prompt(source_text: str, level: str) -> str:
    answer = (
        "краткий ответ (1 предложение, только суть)"
        if level == "brief"
        else "подробный ответ (2-4 предложения с пояснением)"
    )
    return (
        "Преврати текст лекции ниже в набор обучающих карточек для повторения. "
        "Каждая карточка — это один ключевой факт, определение или концепция.\n\n"
        "Верни ТОЛЬКО JSON-массив объектов со следующими полями:\n"
        '- "title": короткий заголовок темы карточки (3-6 слов)\n'
        '- "question": вопрос для самопроверки\n'
        f'- "answer": {answer}\n'
        '- "tags": массив из 1-3 ключевых слов темы\n\n'
        "КРИТИЧЕСКИ ВАЖНО: верни строго валидный JSON-МАССИВ (начинается с "
        "символа [ и заканчивается символом ]), а НЕ объект с ключами-номерами "
        '(НЕ { "0": ..., "1": ... }). Каждый элемент массива — это объект в '
        "фигурных скобках {}, а не строка в кавычках.\n\n"
        "ВАЖНО ПРО КАВЫЧКИ: если в тексте лекции встречаются названия или "
        "цитаты в кавычках (например, название проекта \"Pixel Peak\"), "
        "НЕ используй двойные кавычки внутри значений полей — заменяй их "
        "на одинарные кавычки ' или просто убирай, иначе сломаешь формат JSON.\n\n"
        "Сделай от 5 до 12 карточек в зависимости от объёма материала. "
        "Не добавляй ничего, кроме самого JSON-массива. Не форматируй JSON "
        "с отступами и переносами строк — верни компактную запись в одну строку, "
        "это экономит место в ответе.\n\n"
        f"Текст лекции:\n{source_text[:MAX_INPUT_CHARS]}"
    )


def build_clarify_prompt(card_question: str, card_answer: str, user_question: str) -> str:
    return (
        "Студент изучает карточку для повторения со следующим содержимым:\n"
        f"Вопрос карточки: {card_question}\n"
        f"Ответ карточки: {card_answer}\n\n"
        f"Студент задал уточняющий вопрос: {user_question}\n\n"
        "Ответь кратко и по существу (2-4 предложения), не повторяя "
        "дословно то, что уже написано в ответе карточки — дай именно "
        "уточнение или объяснение сверх уже известного."
    )


# ============================================================
# ЭКСПОРТ
# ============================================================
def build_docx(cards: list[dict]) -> io.BytesIO:
    from docx import Document

    doc = Document()
    doc.add_heading("Карточки для повторения", level=1)
    for card in cards:
        doc.add_heading(card["title"], level=2)
        if card["tags"]:
            doc.add_paragraph().add_run(" · ".join(card["tags"])).italic = True
        doc.add_paragraph().add_run("Вопрос: " + card["question"]).bold = True
        doc.add_paragraph("Ответ: " + card["answer"])
        doc.add_paragraph("")

    buffer = io.BytesIO()
    doc.save(buffer)
    buffer.seek(0)
    return buffer


def build_pdf(cards: list[dict]) -> io.BytesIO:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.add_font("DejaVu", "", FONT_PATH)  # стандартные шрифты не знают кириллицу

    def line(text, size, height):
        pdf.set_font("DejaVu", size=size)
        pdf.multi_cell(0, height, text, new_x="LMARGIN", new_y="NEXT")

    line("Карточки для повторения", 16, 12)
    pdf.ln(4)
    for card in cards:
        line(card["title"], 13, 8)
        if card["tags"]:
            line(" · ".join(card["tags"]), 10, 6)
        line("Вопрос: " + card["question"], 11, 7)
        line("Ответ: " + card["answer"], 11, 7)
        pdf.ln(6)

    return io.BytesIO(pdf.output())


EXPORTERS = {
    "docx": (build_docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "pdf": (build_pdf, "application/pdf"),
}


# ============================================================
# ПРИЛОЖЕНИЕ
# ============================================================
app = FastAPI(title="Лекция -> Карточки")


@app.get("/api/config")
async def get_config(request: Request):
    """Лимиты и остаток дневной квоты — одним запросом при загрузке страницы."""
    return {
        "max_input_chars": MAX_INPUT_CHARS,
        "max_file_size_mb": MAX_FILE_SIZE_BYTES // (1024 * 1024),
        "quota_remaining": remaining_quota(client_ip(request)),
        "quota_limit": DAILY_LIMIT,
    }


@app.post("/api/generate-cards")
async def generate_cards(
    request: Request,
    file: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
    level: str = Form(default="detailed"),
):
    # Сначала проверяем вход — чтобы кривой файл не тратил лимит.
    if file is not None:
        content = await file.read()
        if len(content) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Файл слишком большой (максимум {MAX_FILE_SIZE_BYTES // (1024 * 1024)} МБ)",
            )
        source_text = await asyncio.to_thread(extract_text, file.filename, content)
    elif text and text.strip():
        source_text = text
    else:
        raise HTTPException(status_code=400, detail="Нужно прислать файл или текст")

    if not source_text.strip():
        raise HTTPException(status_code=400, detail="Не удалось извлечь текст из файла")

    ip = client_ip(request)
    remaining = use_quota(ip)

    try:
        raw, finish_reason = await gigachat(build_prompt(source_text, level), MODELS_FOR_CARDS)
        cards = clean_cards(extract_json(raw, finish_reason))
        if not cards:
            raise HTTPException(status_code=502, detail="Модель не вернула ни одной карточки. Попробуй ещё раз.")
    except HTTPException:
        refund_quota(ip)  # неудачная генерация не должна сжигать лимит
        raise

    set_id = secrets.token_urlsafe(5)
    title = cards[0]["title"] or "Карточки"
    with db() as conn:
        conn.execute(
            "INSERT INTO card_sets (id, cards_json, title, created_at) VALUES (?, ?, ?, ?)",
            (set_id, json.dumps(cards, ensure_ascii=False), title, date.today().isoformat()),
        )

    return {
        "cards": cards,
        "id": set_id,
        "url": f"/s/{set_id}",
        "title": title,
        "was_truncated": len(source_text) > MAX_INPUT_CHARS,
        "quota_remaining": remaining,
        "quota_limit": DAILY_LIMIT,
    }


@app.post("/api/clarify")
async def clarify_card(request: Request, payload: dict = Body(...)):
    card_question = str(payload.get("card_question", ""))
    card_answer = str(payload.get("card_answer", ""))
    user_question = str(payload.get("user_question", "")).strip()

    if not user_question:
        raise HTTPException(status_code=400, detail="Вопрос пустой")
    if len(user_question) > 500:
        raise HTTPException(status_code=400, detail="Вопрос слишком длинный (максимум 500 символов)")

    use_clarify(client_ip(request), card_question)
    answer, _ = await gigachat(
        build_clarify_prompt(card_question, card_answer, user_question),
        MODELS_FOR_CLARIFY,
        max_tokens=500,
    )
    return {"answer": answer.strip()}


@app.get("/api/set/{set_id}")
async def get_set(set_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT cards_json, title FROM card_sets WHERE id = ?", (set_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Набор карточек не найден (возможно, ссылка устарела)")
    return {"cards": json.loads(row[0]), "title": row[1]}


@app.post("/api/export/{fmt}")
async def export_cards(fmt: str, payload: dict = Body(...)):
    if fmt not in EXPORTERS:
        raise HTTPException(status_code=400, detail="Неизвестный формат (доступны: docx, pdf)")

    cards = clean_cards(payload.get("cards"))[:MAX_EXPORT_CARDS]
    if not cards:
        raise HTTPException(status_code=400, detail="Нет карточек для экспорта")

    build, media_type = EXPORTERS[fmt]
    buffer = await asyncio.to_thread(build, cards)

    # В HTTP-заголовке кириллицы быть не может: ASCII-имя + filename*=UTF-8''...
    disposition = f'attachment; filename="cards.{fmt}"; filename*=UTF-8\'\'{quote("карточки." + fmt)}'
    return StreamingResponse(buffer, media_type=media_type, headers={"Content-Disposition": disposition})


# ---------------- страницы и статика ----------------
@app.get("/")
async def index_page():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/s/{set_id}")
async def shared_page(set_id: str):
    return FileResponse(os.path.join(STATIC_DIR, "shared.html"))


@app.get("/robots.txt")
async def robots_txt():
    return FileResponse(os.path.join(BASE_DIR, "robots.txt"), media_type="text/plain")


@app.get("/sitemap.xml")
async def sitemap_xml():
    return FileResponse(os.path.join(BASE_DIR, "sitemap.xml"), media_type="application/xml")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Файлы подтверждения владения сайтом (Яндекс.Вебмастер, Google Search Console)
# должны лежать в корне сайта. Кладёшь файл в verify/ — он доступен как /имя_файла.
app.mount("/", StaticFiles(directory=VERIFY_DIR, check_dir=False), name="verify")
