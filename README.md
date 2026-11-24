# Conspector

Превращает лекцию (txt, pdf, docx, pptx или вставленный текст) в карточки «вопрос — ответ» через GigaChat.

## Запуск

```bash
python -m venv venv            # окружение держи ВНЕ папки static/
venv\Scripts\activate          # Linux/macOS: source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env           # впиши GIGACHAT_AUTH_KEY
uvicorn app:app --reload
```

Лимиты: 5 генераций в сутки на IP, 1 уточнение к карточке. Меняются константами `DAILY_LIMIT` и `CLARIFY_LIMIT` в `app.py`.
Файлы подтверждения Яндекс/Google кладутся в `verify/` и отдаются из корня сайта.
