# Contributing

Спасибо за участие в проекте.

## Перед pull request

1. Создайте отдельную ветку от `main`.
2. Не добавляйте `.env`, `.venv`, `*.session` и реальные токены.
3. Запустите:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
ruff check app.py tests
```

4. Опишите изменение и способ проверки в pull request.
5. Для изменений webhook добавьте sanitized fixture без персональных данных.

## Стиль

- Не логируйте токены и payload.
- Не меняйте поведение авторизации без обновления документации.
- Для Telegram-переходов учитывайте идемпотентность `callid`.
- Сохраняйте совместимость с Debian/Ubuntu и Python 3.11.
