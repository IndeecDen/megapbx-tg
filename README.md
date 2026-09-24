# MegaPBX → Telegram

Бот уведомляет рабочий чат Telegram о пропущенных входящих звонках MegaPBX.

Релиз: [`megapbx-tg-v0.1.1`](https://github.com/IndeecDen/megapbx-tg/releases/tag/megapbx-tg-v0.1.1)

## Возможности

- принимает webhook на `POST /megapbx/webhook`;
- фильтрует звонки по группам и DID;
- отправляет сообщение с именем/номером клиента и кнопкой `Я наберу`;
- не отправляет повторные уведомления для одного `callid` в пределах процесса;
- повторяет временно недоступные запросы Telegram;
- проверяет `ok` Telegram API и не помечает звонок закрытым при ошибке редактирования;
- автоматически закрывает уведомление по подтверждённому событию или успешному перезвону;
- добавляет статус неудачного перезвонка (`Busy`, `Missed`, `NotAvailable` и т. п.);
- экранирует HTML и не пишет raw payload, токены и данные клиентов в application-лог.

## Быстрая установка на Debian/Ubuntu

Рекомендуемый способ — скачать зафиксированный тег, сохранить installer локально и только затем запускать от root. Не используйте `curl | sudo bash`: так сложнее проверить код и вводить секреты.

Для публичного репозитория:

```bash
curl -fsSL https://raw.githubusercontent.com/IndeecDen/megapbx-tg/megapbx-tg-v0.1.1/install.sh -o /tmp/megapbx-tg-install.sh && sudo bash /tmp/megapbx-tg-install.sh
```

### Если репозиторий закрыт (`private`)

GitHub не разрешает анонимный `raw.githubusercontent.com` для private-репозитория. Используйте fine-grained PAT с доступом только **Contents: Read** для этого репозитория и передайте его установщику через файл с правами `0600`:

```bash
sudo install -m 600 /dev/null /root/.megapbx-github-token
sudoedit /root/.megapbx-github-token       # вставить PAT
curl -fsSL -H "Authorization: Bearer $(cat /root/.megapbx-github-token)" \
  https://raw.githubusercontent.com/IndeecDen/megapbx-tg/megapbx-tg-v0.1.1/install.sh \
  -o /tmp/megapbx-tg-install.sh
sudo bash /tmp/megapbx-tg-install.sh --github-token-file /root/.megapbx-github-token
```

После успешной установки файл с PAT можно удалить:

```bash
sudo rm -f /root/.megapbx-github-token
```

Для анонимной команды без PAT сначала сделайте репозиторий публичным; это изменение видимости отдельно не выполняется installer-ом.

Установщик:

1. проверит Debian/Ubuntu и systemd;
2. установит Python, `venv`, зависимости и системные утилиты;
3. создаст системного пользователя `megapbx`;
4. развернёт приложение в `/opt/megapbx-tg/releases/...`;
5. создаст `/etc/megapbx-tg.env` с правами `0600`;
6. установит и включит `megapbx-tg.service`;
7. выполнит локальный health-check;
8. опционально настроит Nginx и TLS.

Во время установки секреты вводятся без отображения. Скрипт не копирует `.env`, `.venv`, `*.session`, старые backup-файлы или кэши.

### Что будет запрошено

- `TG_BOT_TOKEN` — токен Telegram-бота;
- `TG_CHAT_ID` — ID чата/группы Telegram;
- `MEGAPBX_CRM_TOKEN` — общий секрет webhook;
- `MEGAPBX_ALLOWED_GROUP` и/или `MEGAPBX_ALLOWED_DID` — фильтр направлений;
- `MEGAPBX_DID_NAMES` — необязательное отображение DID → название;
- `MEGAPBX_API_BASE` и `MEGAPBX_API_TOKEN` — необязательное обогащение имён/групп;
- домен и email — если выбран Nginx/TLS.

Пустой allowlist не принимается: нужно явно выбрать группу/DID или разрешить все направления.

## Установка без интерактивного ввода

Подготовьте защищённый файл с простыми строками `KEY=VALUE`:

```bash
sudo install -m 600 /dev/null /root/megapbx-tg.env
sudoedit /root/megapbx-tg.env
```

Пример структуры:

```text
TG_BOT_TOKEN=...
TG_CHAT_ID=-1001234567890
MEGAPBX_CRM_TOKEN=...
MEGAPBX_ALLOWED_GROUP=Поддержка
MEGAPBX_ALLOWED_DID=DID_FROM_MEGAPBX
MEGAPBX_API_BASE=https://megapbx.example.com
MEGAPBX_API_TOKEN=...
```

Запуск:

```bash
sudo bash /tmp/megapbx-tg-install.sh --non-interactive --env-file /root/megapbx-tg.env --no-nginx
```

Для unattended-режима нельзя получать интерактивные вводы или молча соглашаться на замену существующей конфигурации. Для повторной установки используйте явный `--replace-config` только после резервного копирования.

## Проверка сервиса

```bash
sudo systemctl status megapbx-tg --no-pager
sudo systemctl is-enabled megapbx-tg
sudo journalctl -u megapbx-tg -n 100 --no-pager
curl -fsS http://127.0.0.1:8000/
```

Если включён Nginx, webhook будет доступен по адресу:

```text
https://<domain>/megapbx/webhook
```

Передавайте токен в заголовке:

```text
X-CRM-Token: <значение из конфигурации>
```

Query-параметр `token` отключён по умолчанию. Не передавайте секреты в URL.

## Ручной запуск для разработки

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app:app --host 127.0.0.1 --port 8000 --env-file .env --no-access-log
```

Приложение само не загружает `.env`: переменные должны приходить из `--env-file`, systemd или окружения контейнера.

## Проверка качества

```bash
pip install -r requirements-dev.txt
pytest -q
ruff check app.py tests
```

## Обновление и откат

Повторный запуск installer с тем же тегом:

- сохраняет `/etc/megapbx-tg.env`;
- создаёт новый release-каталог;
- переключает `/opt/megapbx-tg/current` только после успешного health-check;
- сохраняет предыдущие release-каталоги и transaction backup в `/var/backups/megapbx-tg`.

Перед обновлением можно остановить сервис вручную:

```bash
sudo systemctl stop megapbx-tg
sudo bash /tmp/megapbx-tg-install.sh
```

## Важные ограничения

- Состояние пропущенных звонков, счётчики и корреляция пока хранятся в памяти процесса.
- После перезапуска они сбрасываются.
- Запускайте один worker: Telegram long polling не поддерживает несколько независимых consumer-ов.
- Перед production-развёртыванием используйте HTTPS.
- Не публикуйте `.env`, `.venv`, `*.session` и резервные копии.
- Следующий технический этап — постоянное состояние в SQLite/PostgreSQL/Redis.

## Документы релиза

- [CHANGELOG.md](CHANGELOG.md)
- [RELEASE_NOTES.md](RELEASE_NOTES.md)
- [SECURITY.md](SECURITY.md)
- [CONTRIBUTING.md](CONTRIBUTING.md)
- [LICENSE](LICENSE)
