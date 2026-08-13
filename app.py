import os
import io
import json
import re
import uuid
import time
import sqlite3
import secrets
import asyncio
from datetime import date

import requests
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Body
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pypdf import PdfReader
from docx import Document
from pptx import Presentation
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# КОНФИГ GIGACHAT
# ============================================================
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY", "")
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

# GigaChat использует сертификаты российского удостоверяющего центра.
# Для быстрого старта проверку SSL отключаем — см. README про продакшен-вариант.
VERIFY_SSL = False

# Максимальная длина текста лекции, которую отправляем в GigaChat за раз.
MAX_INPUT_CHARS = 15000

# Максимальный размер загружаемого файла (в байтах). 10 МБ с запасом
# хватает на любую текстовую лекцию — так пользователь не положит сервер,
# закинув видео или огромный архив по ошибке.
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 МБ


class QuotaExceeded(Exception):
    """Внутреннее исключение: у модели закончился бесплатный баланс токенов."""
    pass


# Порядок моделей для разных задач — если на первой в списке кончился
# баланс, автоматически пробуем следующую. Карточки — самая частая
# операция, поэтому впереди Lite (у него больше всего токенов). Для
# уточнений и карты лекции первым ставим Pro — там важнее качество ответа.
MODELS_FOR_CARDS = ["GigaChat", "GigaChat-Pro"]
MODELS_FOR_CLARIFY = ["GigaChat-Pro", "GigaChat"]
MODELS_FOR_MAP = ["GigaChat-Pro", "GigaChat"]

# Тариф GigaChat (Freemium) разрешает только 1 одновременный запрос ко
# всему API-ключу. Если два пользователя нажмут кнопку одновременно —
# второй запрос без этой защиты просто получит ошибку от GigaChat.
# Семафор ставит все запросы в очередь: они выполняются по одному,
# автоматически дожидаясь освобождения "потока", вместо падения с ошибкой.
gigachat_semaphore = asyncio.Semaphore(1)

# Простой счётчик использований в памяти (сбрасывается при перезапуске
# сервера). Для реальной статистики между перезапусками потребуется база
# данных, но для теста на группе этого достаточно.
_usage_stats = {"date": str(date.today()), "count": 0}

_token_cache = {"access_token": None, "expires_at": 0}

# ============================================================
# ХРАНИЛИЩЕ ДЛЯ РАСШАРИВАЕМЫХ НАБОРОВ КАРТОЧЕК (SQLite)
# ============================================================
# ВАЖНО: на бесплатном тарифе Render диск не постоянный — файл базы
# может обнулиться при перезапуске/передеплое сервиса. Для теста на
# группе этого достаточно; для долгосрочного хранения ссылок в будущем
# потребуется подключить постоянную БД (например, Render Postgres).
DB_PATH = os.path.join(os.path.dirname(__file__), "cards.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS card_sets (
            id TEXT PRIMARY KEY,
            cards_json TEXT NOT NULL,
            title TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


init_db()

# Временное хранилище текста последних лекций — нужно, чтобы кнопка
# "Показать карту лекции" могла запросить карту позже, не заставляя
# пользователя заново загружать файл. Живёт только в памяти процесса
# (как и счётчик использований) — этого достаточно для тестового этапа.
_recent_texts: dict[str, str] = {}


def get_gigachat_token() -> str:
    if not GIGACHAT_AUTH_KEY:
        raise HTTPException(
            status_code=500,
            detail="GIGACHAT_AUTH_KEY не задан. Добавь его в .env файл.",
        )

    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] > now + 5:
        return _token_cache["access_token"]

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "RqUID": str(uuid.uuid4()),
        "Authorization": f"Basic {GIGACHAT_AUTH_KEY}",
    }
    data = {"scope": GIGACHAT_SCOPE}

    try:
        resp = requests.post(OAUTH_URL, headers=headers, data=data, verify=VERIFY_SSL, timeout=30)
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="GigaChat не ответил за 30 секунд (получение токена). Попробуй ещё раз.")
    except requests.exceptions.ConnectionError:
        raise HTTPException(status_code=502, detail="Не удалось подключиться к серверу GigaChat. Проверь интернет-соединение.")

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Ошибка получения токена GigaChat: {resp.status_code} {resp.text}",
        )
    payload = resp.json()
    _token_cache["access_token"] = payload["access_token"]
    expires_at_raw = payload.get("expires_at", 0)
    _token_cache["expires_at"] = expires_at_raw / 1000 if expires_at_raw > 10**12 else now + 1700
    return _token_cache["access_token"]


def ask_gigachat(prompt: str, model: str = "GigaChat", max_tokens: int = 8000, temperature: float = 0.3):
    """Низкоуровневый запрос к конкретной модели GigaChat."""
    token = get_gigachat_token()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
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
        resp = requests.post(CHAT_URL, headers=headers, json=body, verify=VERIFY_SSL, timeout=60)
    except requests.exceptions.Timeout:
        raise HTTPException(
            status_code=504,
            detail="GigaChat не ответил за 60 секунд. Возможно, лекция слишком большая — попробуй сократить текст.",
        )
    except requests.exceptions.ConnectionError:
        raise HTTPException(
            status_code=502,
            detail="Не удалось подключиться к серверу GigaChat. Проверь интернет-соединение и повтори попытку.",
        )

    # 402/429 обычно значит "закончился баланс токенов на эту модель"
    if resp.status_code in (402, 429):
        raise QuotaExceeded(f"Лимит токенов исчерпан для модели {model}: {resp.status_code} {resp.text}")

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Ошибка запроса к GigaChat: {resp.status_code} {resp.text}",
        )
    data = resp.json()
    choice = data["choices"][0]
    content = choice["message"]["content"]
    finish_reason = choice.get("finish_reason", "unknown")
    return content, finish_reason


def ask_gigachat_with_fallback(prompt: str, models: list[str], max_tokens: int = 8000, temperature: float = 0.3):
    """Пробует модели по очереди из списка — если на текущей закончился
    баланс токенов (QuotaExceeded), автоматически переходит к следующей.
    Так лимит одной модели не останавливает работу приложения."""
    last_error = None
    for model in models:
        try:
            return ask_gigachat(prompt, model=model, max_tokens=max_tokens, temperature=temperature)
        except QuotaExceeded as e:
            last_error = e
            continue
    raise HTTPException(
        status_code=402,
        detail=f"Лимит токенов исчерпан на всех доступных моделях ({', '.join(models)}). {last_error}",
    )


def extract_json(raw_text: str, finish_reason: str = "unknown"):
    """GigaChat иногда оборачивает ответ в ```json ... ``` — чистим и парсим.
    Модель иногда путает формат и вместо массива [] возвращает объект {}
    с ключами "0", "1", "2"... — приводим оба варианта к единому списку.
    Если ответ обрезан по лимиту токенов, пытаемся восстановить хотя бы те
    карточки, что успели прийти целиком, вместо полного отказа."""
    cleaned = raw_text.strip()
    cleaned = re.sub(r"^```(json)?", "", cleaned.strip())
    cleaned = re.sub(r"```$", "", cleaned.strip())
    cleaned = cleaned.strip()

    match = re.search(r"[\[{].*", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)

    opening_char = cleaned[:1]
    closing_char = "]" if opening_char == "[" else "}"

    parsed = None
    try:
        parsed = json.loads(cleaned, strict=False)
    except json.JSONDecodeError:
        last_complete = cleaned.rfind("}")
        if last_complete == -1:
            raise HTTPException(
                status_code=502,
                detail=f"Модель вернула пустой или нечитаемый ответ. Сырой ответ: {raw_text[:500]}",
            )
        repaired = cleaned[: last_complete + 1] + closing_char
        try:
            parsed = json.loads(repaired, strict=False)
        except json.JSONDecodeError as e:
            if finish_reason == "length":
                reason_hint = "Не хватило max_tokens — ответ обрезан по лимиту длины."
            else:
                reason_hint = f"Модель завершила ответ некорректно (finish_reason: {finish_reason})."
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Ответ от модели повреждён и не удалось восстановить: {e}. {reason_hint} "
                    f"Сырой ответ: {raw_text[:500]}"
                ),
            )

    # Модель вернула объект вместо массива — берём значения по порядку ключей
    if isinstance(parsed, dict):
        parsed = list(parsed.values())

    return parsed


# ============================================================
# ИЗВЛЕЧЕНИЕ ТЕКСТА ИЗ ФАЙЛОВ
# ============================================================
def extract_text_from_upload(filename: str, content: bytes) -> str:
    ext = os.path.splitext(filename)[1].lower()
    text = ""
    stream = io.BytesIO(content)

    if ext in (".txt", ".md"):
        text = content.decode("utf-8", errors="ignore")
    elif ext == ".pdf":
        reader = PdfReader(stream)
        for page in reader.pages:
            extracted = page.extract_text()
            if extracted:
                text += extracted + "\n"
    elif ext == ".docx":
        doc = Document(stream)
        for para in doc.paragraphs:
            if para.text.strip():
                text += para.text + "\n"
    elif ext == ".pptx":
        prs = Presentation(stream)
        for slide in prs.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        p_text = "".join(run.text for run in para.runs)
                        if p_text.strip():
                            text += p_text + "\n"
    else:
        raise HTTPException(status_code=400, detail=f"Формат {ext} не поддерживается")

    return text


# ============================================================
# ПРОМПТ ДЛЯ ГЕНЕРАЦИИ КАРТОЧЕК
# ============================================================
def build_prompt(source_text: str, level: str = "detailed") -> str:
    trimmed = source_text[:MAX_INPUT_CHARS]

    if level == "brief":
        answer_instruction = 'краткий ответ (1 предложение, только суть)'
    else:
        answer_instruction = 'подробный ответ (2-4 предложения с пояснением)'

    return (
        "Преврати текст лекции ниже в набор обучающих карточек для повторения. "
        "Каждая карточка — это один ключевой факт, определение или концепция.\n\n"
        "Верни ТОЛЬКО JSON-массив объектов со следующими полями:\n"
        '- "title": короткий заголовок темы карточки (3-6 слов)\n'
        '- "question": вопрос для самопроверки\n'
        f'- "answer": {answer_instruction}\n'
        '- "tags": массив из 1-3 ключевых слов темы\n\n'
        "КРИТИЧЕСКИ ВАЖНО: верни строго валидный JSON-МАССИВ (начинается с "
        "символа [ и заканчивается символом ]), а НЕ объект с ключами-номерами "
        '(НЕ { "0": ..., "1": ... }). Каждый элемент массива — это объект в '
        "фигурных скобках {}, а не строка в кавычках.\n\n"
        "Сделай от 5 до 12 карточек в зависимости от объёма материала. "
        "Не добавляй ничего, кроме самого JSON-массива. Не форматируй JSON "
        "с отступами и переносами строк — верни компактную запись в одну строку, "
        "это экономит место в ответе.\n\n"
        f"Текст лекции:\n{trimmed}"
    )


# ============================================================
# FASTAPI ПРИЛОЖЕНИЕ
# ============================================================
app = FastAPI(title="Лекция -> Карточки")


def build_map_prompt(source_text: str) -> str:
    """Промпт для карты лекции — просим Markdown с заголовками и списками
    (не Mermaid!), это надёжно парсится библиотекой markmap на фронтенде
    и почти невозможно сломать по формату, в отличие от строгого
    графового синтаксиса."""
    trimmed = source_text[:MAX_INPUT_CHARS]
    return (
        "Построй структурную карту лекции ниже в виде Markdown-документа "
        "с заголовками и вложенными списками — она станет майндмэпом.\n\n"
        "СТРОГИЙ ФОРМАТ:\n"
        "# Название лекции\n"
        "## Тема 1\n"
        "- Ключевой факт 1\n"
        "- Ключевой факт 2\n"
        "## Тема 2\n"
        "- Ключевой факт\n"
        "### Подтема\n"
        "- Уточняющий факт\n\n"
        "Правила: 3-7 тем верхнего уровня (##), у каждой 2-5 фактов "
        "(пункты списка через дефис), при необходимости — подтемы (###). "
        "Не добавляй ничего, кроме самого Markdown — без пояснений до "
        "или после, без обёртки в блок кода ```.\n\n"
        f"Текст лекции:\n{trimmed}"
    )


def build_clarify_prompt(card_question: str, card_answer: str, user_question: str) -> str:
    """Промпт для уточняющего вопроса по конкретной карточке. Даём модели
    только контекст этой карточки (не всю лекцию) — так дешевле и быстрее,
    а модель отвечает по существу, не расплываясь."""
    return (
        "Студент изучает карточку для повторения со следующим содержимым:\n"
        f"Вопрос карточки: {card_question}\n"
        f"Ответ карточки: {card_answer}\n\n"
        f"Студент задал уточняющий вопрос: {user_question}\n\n"
        "Ответь кратко и по существу (2-4 предложения), не повторяя "
        "дословно то, что уже написано в ответе карточки — дай именно "
        "уточнение или объяснение сверх уже известного."
    )


@app.post("/api/generate-cards")
async def generate_cards(
    file: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
    level: str = Form(default="detailed"),
):
    if file is not None:
        content = await file.read()
        if len(content) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Файл слишком большой (максимум {MAX_FILE_SIZE_BYTES // (1024*1024)} МБ)",
            )
        source_text = extract_text_from_upload(file.filename, content)
    elif text and text.strip():
        source_text = text
    else:
        raise HTTPException(status_code=400, detail="Нужно прислать файл или текст")

    if not source_text.strip():
        raise HTTPException(status_code=400, detail="Не удалось извлечь текст из файла")

    was_truncated = len(source_text) > MAX_INPUT_CHARS

    prompt = build_prompt(source_text, level=level)
    async with gigachat_semaphore:
        raw_response, finish_reason = ask_gigachat_with_fallback(prompt, models=MODELS_FOR_CARDS)
    cards = extract_json(raw_response, finish_reason)

    # Сохраняем текст лекции в памяти — понадобится, если пользователь
    # позже нажмёт "Показать карту лекции", без повторной загрузки файла.
    map_session_id = secrets.token_urlsafe(8)
    _recent_texts[map_session_id] = source_text

    today = str(date.today())
    if _usage_stats["date"] != today:
        _usage_stats["date"] = today
        _usage_stats["count"] = 0
    _usage_stats["count"] += 1

    return JSONResponse({
        "cards": cards,
        "source_length": len(source_text),
        "max_input_chars": MAX_INPUT_CHARS,
        "was_truncated": was_truncated,
        "map_session_id": map_session_id,
    })


@app.get("/api/limits")
async def get_limits():
    return JSONResponse({
        "max_input_chars": MAX_INPUT_CHARS,
        "max_file_size_mb": MAX_FILE_SIZE_BYTES // (1024 * 1024),
    })


@app.post("/api/save-set")
async def save_set(payload: dict = Body(...)):
    """Сохраняет набор сгенерированных карточек и возвращает короткую
    ссылку, по которой их можно посмотреть без повторной генерации."""
    cards = payload.get("cards")
    title = payload.get("title", "Набор карточек")

    if not cards or not isinstance(cards, list):
        raise HTTPException(status_code=400, detail="Нет карточек для сохранения")

    set_id = secrets.token_urlsafe(5)  # короткий читаемый ID, например "aB3xQ9"

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO card_sets (id, cards_json, title, created_at) VALUES (?, ?, ?, ?)",
        (set_id, json.dumps(cards, ensure_ascii=False), title, str(date.today())),
    )
    conn.commit()
    conn.close()

    return JSONResponse({"id": set_id, "url": f"/s/{set_id}"})


@app.get("/api/set/{set_id}")
async def get_set(set_id: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT cards_json, title FROM card_sets WHERE id = ?", (set_id,)
    ).fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Набор карточек не найден (возможно, ссылка устарела)")

    cards_json, title = row
    return JSONResponse({"cards": json.loads(cards_json), "title": title})


@app.get("/s/{set_id}")
async def view_shared_set(set_id: str):
    return FileResponse(os.path.join("static", "shared.html"))


@app.get("/api/stats")
async def get_stats():
    """Счётчик генераций за сегодня — пригодится, когда будешь вводить тарифы."""
    today = str(date.today())
    if _usage_stats["date"] != today:
        return JSONResponse({"date": today, "count": 0})
    return JSONResponse(_usage_stats)


@app.post("/api/generate-map")
async def generate_map(payload: dict = Body(...)):
    """Строит карту лекции (Markdown-outline для рендера через markmap)
    по тексту, сохранённому при последней генерации карточек."""
    session_id = payload.get("map_session_id")
    source_text = _recent_texts.get(session_id)

    if not source_text:
        raise HTTPException(
            status_code=404,
            detail="Текст лекции не найден (сервер мог перезапуститься, или сессия устарела). Сгенерируй карточки заново.",
        )

    prompt = build_map_prompt(source_text)
    async with gigachat_semaphore:
        raw_markdown, finish_reason = ask_gigachat_with_fallback(
            prompt, models=MODELS_FOR_MAP, max_tokens=2000
        )

    # На всякий случай чистим обёртку в блок кода, если модель её добавила
    cleaned = raw_markdown.strip()
    cleaned = re.sub(r"^```(markdown|md)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()

    return JSONResponse({"markdown": cleaned})


@app.post("/api/clarify")
async def clarify_card(payload: dict = Body(...)):
    """Отвечает на уточняющий вопрос студента по конкретной карточке."""
    card_question = payload.get("card_question", "")
    card_answer = payload.get("card_answer", "")
    user_question = payload.get("user_question", "")

    if not user_question.strip():
        raise HTTPException(status_code=400, detail="Вопрос пустой")

    prompt = build_clarify_prompt(card_question, card_answer, user_question)
    async with gigachat_semaphore:
        answer_text, _ = ask_gigachat_with_fallback(
            prompt, models=MODELS_FOR_CLARIFY, max_tokens=500
        )

    return JSONResponse({"answer": answer_text.strip()})


@app.get("/robots.txt")
async def robots_txt():
    return FileResponse(os.path.join("static", "robots.txt"), media_type="text/plain")


@app.get("/sitemap.xml")
async def sitemap_xml():
    return FileResponse(os.path.join("static", "sitemap.xml"), media_type="application/xml")


@app.get("/")
async def root():
    return FileResponse(os.path.join("static", "index.html"))


app.mount("/static", StaticFiles(directory="static"), name="static")
