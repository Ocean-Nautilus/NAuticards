import os
import io
import json
import re
import uuid
import time
from datetime import date

import requests
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
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

# Простой счётчик использований в памяти (сбрасывается при перезапуске
# сервера). Для реальной статистики между перезапусками потребуется база
# данных, но для теста на группе этого достаточно.
_usage_stats = {"date": str(date.today()), "count": 0}

_token_cache = {"access_token": None, "expires_at": 0}


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


def ask_gigachat(prompt: str) -> str:
    token = get_gigachat_token()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    body = {
        "model": "GigaChat",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты помощник, который превращает текст лекций в обучающие "
                    "карточки. Отвечай ТОЛЬКО валидным JSON-массивом, без markdown, "
                    "без пояснений до или после."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 8000,
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

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Ошибка запроса к GigaChat: {resp.status_code} {resp.text}",
        )
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def extract_json(raw_text: str):
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
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Ответ от модели обрезан и не удалось восстановить: {e}. "
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
        "Сделай от 5 до 15 карточек в зависимости от объёма материала. "
        "Не добавляй ничего, кроме самого JSON-массива.\n\n"
        f"Текст лекции:\n{trimmed}"
    )


# ============================================================
# FASTAPI ПРИЛОЖЕНИЕ
# ============================================================
app = FastAPI(title="Лекция -> Карточки")


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
    raw_response = ask_gigachat(prompt)
    cards = extract_json(raw_response)

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
    })


@app.get("/api/limits")
async def get_limits():
    return JSONResponse({
        "max_input_chars": MAX_INPUT_CHARS,
        "max_file_size_mb": MAX_FILE_SIZE_BYTES // (1024 * 1024),
    })


@app.get("/api/stats")
async def get_stats():
    """Счётчик генераций за сегодня — пригодится, когда будешь вводить тарифы."""
    today = str(date.today())
    if _usage_stats["date"] != today:
        return JSONResponse({"date": today, "count": 0})
    return JSONResponse(_usage_stats)


@app.get("/")
async def root():
    return FileResponse(os.path.join("static", "index.html"))


app.mount("/static", StaticFiles(directory="static"), name="static")
