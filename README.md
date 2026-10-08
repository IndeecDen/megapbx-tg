# MegaPBX → Telegram

Бот получает пропущенные звонки из MegaPBX и отправляет уведомления в рабочий чат Telegram. В сообщении есть кликабельный номер клиента и кнопка **«📲 Я наберу»**.

**Релиз:** [`megapbx-tg-v0.1.1`](https://github.com/IndeecDen/megapbx-tg/releases/tag/megapbx-tg-v0.1.1)

## Как это работает

```text
MegaPBX
   │  POST /megapbx/webhook
   ▼
Фильтр групп/DID → проверка CRM-токена
   │
   ▼
Telegram: «Пропущенный звонок» + кнопка «Я наберу»
   │
   ├── сотрудник нажал кнопку / произошёл возвратный звонок
   ▼
Сообщение помечается «Перезвонил …»
```

Бот также:

- не отправляет повторное уведомление для одного `callid` в пределах процесса;
- повторяет временно недоступные запросы Telegram;
- не считает сообщение успешно отправленным при ошибке Telegram;
- добавляет статус неудачного перезвона: `Занято`, `Не взяли трубку`, `Недоступен` и другие;
- не пишет в application-лог raw payload, токены и данные клиентов;
- работает как один процесс с одним Telegram long-polling consumer.

## Требования

- Debian 11+ или Ubuntu 22.04+;
- root-доступ для установки;
- systemd;
- Telegram-бот и ID чата/группы Telegram;
- доступный из интернета HTTPS-адрес для webhook.

Прямой запуск возможен и без Nginx, но для production используйте HTTPS.

## Быстрая установка

Репозиторий публичный, поэтому installer можно скачать anonymously. Команда скачивает зафиксированный тег, сохраняет installer локально и запускает его от root:

```bash
curl -fsSL https://raw.githubusercontent.com/IndeecDen/megapbx-tg/megapbx-tg-v0.1.1/install.sh -o /tmp/megapbx-tg-install.sh && sudo bash /tmp/megapbx-tg-install.sh
```

Установщик:

1. проверит Debian/Ubuntu и systemd;
2. установит Python, `venv`, зависимости и системные утилиты;
3. создаст системного пользователя `megapbx`;
4. развернёт приложение в `/opt/megapbx-tg/releases/...`;
5. переключит `/opt/megapbx-tg/current` только после успешной проверки;
6. создаст `/etc/megapbx-tg.env` с правами `0600`;
7. установит и включит `megapbx-tg.service`;
8. выполнит локальный health-check;
9. опционально настроит Nginx и TLS.

Скрипт не копирует `.env`, `.venv`, `*.session`, старые backup-файлы и кэши.

### Что будет запрошено

- `TG_BOT_TOKEN` — токен Telegram-бота;
- `TG_CHAT_ID` — ID чата или супергруппы (ID супергруппы обычно отрицательный);
- `MEGAPBX_CRM_TOKEN` — секрет для webhook-запросов MegaPBX;
- `MEGAPBX_ALLOWED_GROUP` и/или `MEGAPBX_ALLOWED_DID` — фильтр направлений;
- `MEGAPBX_DID_NAMES` — необязательное отображение DID → название;
- `MEGAPBX_API_BASE` и `MEGAPBX_API_TOKEN` — необязательное получение имён сотрудников и групп;
- домен и email — если выбран Nginx/TLS.

Секреты вводятся без отображения и записываются только в `/etc/megapbx-tg.env`.

## Настройки

Все настройки задаются в `/etc/megapbx-tg.env`. Полный шаблон находится в [`.env.example`](.env.example).

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `TG_BOT_TOKEN` | Токен Telegram-бота | обязательно |
| `TG_CHAT_ID` | ID чата/группы Telegram | обязательно |
| `MEGAPBX_CRM_TOKEN` | Секрет авторизации webhook | обязательно |
| `MEGAPBX_ALLOWED_GROUP` | Разрешённые группы через запятую | пусто |
| `MEGAPBX_ALLOWED_DID` | Разрешённые DID через запятую | пусто |
| `MEGAPBX_DID_NAMES` | Отображение DID → название | пусто |
| `MEGAPBX_API_BASE` | Базовый URL MegaPBX API | пусто |
| `MEGAPBX_API_TOKEN` | API-токен MegaPBX | пусто |
| `TZ_OFFSET_HOURS` | Часовой пояс от UTC | `3` |
| `MISSED_MAX_AGE_SEC` | Время корреляции пропущенного звонка | `3600` |
| `MISSED_CLEANUP_INTERVAL_SEC` | Интервал очистки состояния | `3600` |
| `MISSED_DEDUP_TTL_SEC` | Время дедупликации `callid` | `86400` |
| `TG_API_MAX_RETRIES` | Повторы временных ошибок Telegram | `2` |
| `TG_API_RETRY_BASE_SEC` | Начальная задержка retry | `0.5` |
| `TG_API_RETRY_MAX_SEC` | Максимальная задержка retry | `8` |
| `MAX_WEBHOOK_BODY_BYTES` | Максимальный размер webhook | `1048576` |
| `MEGAPBX_ALLOW_QUERY_TOKEN` | Query-токен; оставьте `0` | `0` |
| `TG_DELETE_WEBHOOK_ON_START` | Удалять Telegram webhook при старте | `1` |

Для фильтрации необходимо заполнить хотя бы `MEGAPBX_ALLOWED_GROUP` или `MEGAPBX_ALLOWED_DID`. Установщик не включает режим «разрешить все направления» молча.

## Настройка MegaPBX

Webhook должен отправлять `POST` на:

```text
https://<domain>/megapbx/webhook
```

CRM-токен передаётся в заголовке:

```text
X-CRM-Token: <значение MEGAPBX_CRM_TOKEN>
```

Поддерживаются JSON и URL-encoded form, включая JSON внутри поля `payload`. Бот ожидает события MegaPBX с `cmd=history`, `status=Missed`, а также события/историю исходящих перезвонов для автоматического закрытия уведомления.

Минимальный sanitized пример полезного payload:

```json
{
  "cmd": "history",
  "status": "Missed",
  "callid": "example-call-id",
  "phone": "<caller_phone>",
  "groupRealName": "<group>",
  "wait": 5,
  "duration": 0
}
```

Не передавайте токен в query string.

## Nginx и HTTPS

Если installer настроил Nginx, конфигурация доступна по адресу:

```text
https://<domain>/megapbx/webhook
```

Nginx:

- принимает только `POST /megapbx/webhook`;
- проксирует запрос на локальный Uvicorn;
- отключает access log для маршрута webhook;
- ограничивает размер body;
- проксирует заголовок `X-CRM-Token`.

TLS выпускается только при явном выборе Let's Encrypt и успешной проверке DNS/порта 80.

## Установка без интерактивного ввода

Подготовьте защищённый файл с простыми строками `KEY=VALUE`:

```bash
sudo install -m 600 /dev/null /root/megapbx-tg.env
sudoedit /root/megapbx-tg.env
```

Пример:

```text
TG_BOT_TOKEN=...
TG_CHAT_ID=-1001234567890
MEGAPBX_CRM_TOKEN=...
MEGAPBX_ALLOWED_GROUP=Поддержка
MEGAPBX_ALLOWED_DID=DID_FROM_MEGAPBX
```

Запуск:

```bash
sudo bash /tmp/megapbx-tg-install.sh \
  --non-interactive \
  --env-file /root/megapbx-tg.env \
  --no-nginx
```

## Управление сервисом

```bash
sudo systemctl status megapbx-tg --no-pager
sudo systemctl restart megapbx-tg
sudo systemctl stop megapbx-tg
sudo systemctl enable megapbx-tg
sudo journalctl -u megapbx-tg -f
```

Локальная проверка:

```bash
curl -fsS http://127.0.0.1:8000/
```

## Обновление и откат

Повторный запуск installer с тем же тегом:

- сохраняет `/etc/megapbx-tg.env`;
- создаёт новый release-каталог;
- переключает `current` только после успешного health-check;
- сохраняет предыдущие release-каталоги и transaction backup в `/var/backups/megapbx-tg`.

## Безопасность

- Не публикуйте `.env`, `.venv`, `*.session` и резервные копии.
- Передавайте CRM-токен только через `X-CRM-Token`.
- Используйте HTTPS.
- Не запускайте несколько worker: состояние и Telegram long polling рассчитаны на один процесс.
- При подозрении на утечку срочно ротируйте Telegram bot token и API-токены MegaPBX.

Подробности — в [SECURITY.md](SECURITY.md).

## Ограничения текущей версии

- Состояние пропущенных звонков, счётчики и корреляция хранятся в памяти процесса.
- После перезапуска состояние сбрасывается.
- Следующий технический этап — постоянное хранилище SQLite/PostgreSQL/Redis.

## Разработка и проверки

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
ruff check app.py tests
bash -n install.sh
```

Дополнительные документы:

- [CHANGELOG.md](CHANGELOG.md)
- [RELEASE_NOTES.md](RELEASE_NOTES.md)
- [CONTRIBUTING.md](CONTRIBUTING.md)
- [LICENSE](LICENSE)