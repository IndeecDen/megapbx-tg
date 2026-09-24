# Release notes — megapbx-tg-v0.1.1

**Дата:** 2026-09-24  
**Тип:** первый стабильный релиз после перехода на FastAPI-структуру

## Что входит

- Интерактивная установка на Debian 11+/Ubuntu 22.04+ одной командой.
- Установка Python 3, `venv`, зависимостей и системного пользователя.
- systemd-сервис `megapbx-tg.service` с автозапуском и restart policy.
- Конфигурация в `/etc/megapbx-tg.env` с правами `0600`.
- Опциональный Nginx и TLS через Let's Encrypt.
- Проверка `/` после установки и понятный вывод адреса webhook.

## Установка

```bash
curl -fsSL https://raw.githubusercontent.com/IndeecDen/megapbx-tg/megapbx-tg-v0.1.1/install.sh -o /tmp/megapbx-tg-install.sh && sudo bash /tmp/megapbx-tg-install.sh
```

Если репозиторий закрыт, скачайте installer с PAT формата `Authorization: Bearer ...` и передайте путь к файлу PAT через `--github-token-file`; анонимный raw URL для private-репозитория недоступен по умолчанию.

```bash
sudo bash /tmp/megapbx-tg-install.sh --github-token-file /root/.megapbx-github-token
```

Установщик запросит:

- Telegram bot token;
- ID чата Telegram;
- CRM-токен MegaPBX;
- разрешённые группы/DID;
- URL и API-токен MegaPBX (если нужны имена сотрудников и групп);
- домен и параметры reverse proxy/TLS (если выбран Nginx).

Секреты вводятся без отображения и не публикуются в GitHub.

## Проверка после установки

```bash
sudo systemctl status megapbx-tg
sudo journalctl -u megapbx-tg -n 100 --no-pager
curl -fsS http://127.0.0.1:8000/
```

Webhook должен быть доступен по адресу:

```text
https://<domain>/megapbx/webhook
```

Передавайте токен в заголовке `X-CRM-Token`, не в query string.

## Обновление

Повторный запуск установщика с тем же тегом обновляет приложение и зависимости, сохраняя `/etc/megapbx-tg.env`. Перед обновлением установщик создаёт резервную копию текущей версии.

## Важные ограничения

- Состояние пропущенных звонков и счётчики пока находятся в памяти процесса.
- Запускайте один worker, пока не подключено постоянное хранилище.
- Для webhook необходим HTTPS в production.
- При использовании Nginx не включайте access log для маршрута `/megapbx/webhook`, чтобы query-параметры не попадали в логи.

## Исправления и безопасность

- Telegram `ok=false`, HTTP-ошибки и таймауты больше не считаются успешной отправкой.
- Временные ошибки Telegram повторяются с backoff.
- Неоднозначный read timeout после `sendMessage` не повторяется автоматически, чтобы не создавать дубликат.
- Повторные webhook с одним `callid` дедуплицируются в пределах процесса.
- Raw payload, токены и персональные данные не пишутся application-логгером.
- HTML-данные экранируются.
- Пустой CRM-токен больше не открывает webhook.
