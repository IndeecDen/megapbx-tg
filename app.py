import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
from base64 import b64decode
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from html import escape
from typing import Optional, Any, Dict
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI, Request, Header, HTTPException


logger = logging.getLogger("megapbx_tg")


# =========================
# Конфиг из ENV
# =========================
def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid integer ENV %s; using default", name)
        return default
    if value < minimum:
        logger.warning("ENV %s is below minimum; using default", name)
        return default
    return value


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid float ENV %s; using default", name)
        return default
    if value < minimum:
        logger.warning("ENV %s is below minimum; using default", name)
        return default
    return value


TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = _env_int("TG_CHAT_ID", 0, minimum=-(2**63))
MEGAPBX_CRM_TOKEN = os.getenv("MEGAPBX_CRM_TOKEN", "")
TG_DELETE_WEBHOOK_ON_START = os.getenv("TG_DELETE_WEBHOOK_ON_START", "1").lower() not in {"0", "false", "no"}
TG_API_MAX_RETRIES = _env_int("TG_API_MAX_RETRIES", 2)
TG_API_RETRY_BASE_SEC = _env_float("TG_API_RETRY_BASE_SEC", 0.5, minimum=0.05)
TG_API_RETRY_MAX_SEC = _env_float("TG_API_RETRY_MAX_SEC", 8.0, minimum=0.1)
MAX_WEBHOOK_BODY_BYTES = _env_int("MAX_WEBHOOK_BODY_BYTES", 1_048_576, minimum=1)
MEGAPBX_ALLOW_QUERY_TOKEN = os.getenv("MEGAPBX_ALLOW_QUERY_TOKEN", "0").lower() in {"1", "true", "yes"}

MEGAPBX_ALLOWED_GROUPS = {
    x.strip() for x in os.getenv("MEGAPBX_ALLOWED_GROUP", "").split(',') if x.strip()
}
MEGAPBX_ALLOWED_DID = {
    x.strip() for x in os.getenv("MEGAPBX_ALLOWED_DID", "").split(',') if x.strip()
}

# Маппинг DID-номер -> название: "79253283852=Поддержка ДК305,79161234567=Хиттайм"
def _parse_did_names(raw: str) -> dict:
    result = {}
    for part in raw.split(','):
        part = part.strip()
        if '=' in part:
            num, name = part.split('=', 1)
            result[num.strip()] = name.strip()
    return result

MEGAPBX_DID_NAMES: dict = _parse_did_names(os.getenv("MEGAPBX_DID_NAMES", ""))

# Базовый URL вида https://{domain} — без /crmapi/v1
MEGAPBX_API_BASE = os.getenv("MEGAPBX_API_BASE", "").strip().rstrip("/")
MEGAPBX_API_TOKEN = os.getenv("MEGAPBX_API_TOKEN", "").strip()

# Временная зона для отображения времени (по умолчанию МСК = UTC+3)
TZ_OFFSET = _env_int("TZ_OFFSET_HOURS", 3, minimum=-23)

MISSED_MAX_AGE_SEC = _env_int("MISSED_MAX_AGE_SEC", 3600, minimum=1)
MISSED_CLEANUP_INTERVAL_SEC = _env_int("MISSED_CLEANUP_INTERVAL_SEC", 3600, minimum=1)
MISSED_DEDUP_TTL_SEC = _env_int("MISSED_DEDUP_TTL_SEC", 86400, minimum=1)

# =========================
# Состояние
# =========================
PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{5,}\d")
DENY_KEYS_SUBSTR = (
    "token", "secret", "sign", "signature", "auth", "authorization",
    "password", "passwd", "credential", "api_key", "apikey",
)
DENY_KEYS_EXACT = {
    "callid", "id", "start", "wait", "duration", "user", "ext",
    "telnum", "diversion", "grouprealname", "type", "status", "cmd",
}

# Индекс пропущенных по callid
MISSED_INDEX: Dict[str, Dict[str, Any]] = {}
# Список для поиска по номеру
MISSED_LIST: list[dict[str, Any]] = []
# Отправки, которые уже выполняются: защита от concurrent-дубликатов одного callid
MISSED_PENDING: Dict[str, asyncio.Task] = {}
# callid, уже успешно отправленные, но удалённые из основного индекса по TTL
MISSED_SEEN: Dict[str, float] = {}
# Сессионные замки не дают двум одновременным звонкам одного номера получить
# некорректные значения счётчика при откате одного из них.
MISSED_PHONE_LOCKS: Dict[str, asyncio.Lock] = {}
_MISSED_PENDING_LOCK = asyncio.Lock()

# Счётчик пропущенных по номеру телефона: phone -> {"today": N, "total": N, "last_date": "YYYY-MM-DD"}
MISSED_COUNTER: Dict[str, Dict[str, Any]] = {}

PBX_ACCOUNTS: dict = {}
# Кеш групп: {telnum_или_ext: name}
PBX_GROUPS: dict = {}


# =========================
# Вспомогательные
# =========================
def _now_local() -> datetime:
    return datetime.now(timezone(timedelta(hours=TZ_OFFSET)))


def _format_time(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M")


def _fingerprint(value: Any) -> str:
    """Безопасный короткий идентификатор для логов без payload/PII."""
    raw = str(value or "").encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:12]


def _safe_log_value(value: Any, max_len: int = 32) -> str:
    """В логи попадают только известные enum-значения, не произвольный payload."""
    value = str(value or "")[:max_len]
    allowed = {
        "history", "event", "missed", "success", "busy", "cancel",
        "notavailable", "notallowed", "notfound", "accepted", "completed",
        "outgoing", "answered", "connected", "out", "in",
    }
    if value.lower() not in allowed:
        return "other"
    return re.sub(r"[^A-Za-z0-9_.:-]", "?", value)


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _html(value: Any) -> str:
    return escape(_as_text(value), quote=False)


def _is_phone(v: Any) -> bool:
    if v is None or isinstance(v, bool):
        return False
    s = str(v).strip()
    digits = re.sub(r"[^0-9]", "", s)
    return 7 <= len(digits) <= 15 and bool(PHONE_RE.fullmatch(s))


def _key_allowed(key: str) -> bool:
    k = key.lower()
    return k not in DENY_KEYS_EXACT and not any(bad in k for bad in DENY_KEYS_SUBSTR)


def _find_phone_anywhere(d: dict) -> Optional[str]:
    stack = [d]
    seen = set()
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, dict):
            for k, v in cur.items():
                if not _key_allowed(str(k)):
                    continue
                if isinstance(v, (dict, list, tuple)):
                    stack.append(v)
                elif isinstance(v, (str, int)) and _is_phone(v):
                    return str(v).strip()
        elif isinstance(cur, (list, tuple)):
            for v in cur:
                if isinstance(v, (dict, list, tuple)):
                    stack.append(v)
                elif isinstance(v, (str, int)) and _is_phone(v):
                    return str(v).strip()
        elif isinstance(cur, (str, int)) and _is_phone(cur):
            return str(cur).strip()
    return None


def _normalize_phone(number: str) -> str:
    """Нормализует российские номера без искусственного leading zero."""
    digits = re.sub(r"[^0-9]", "", str(number or ""))
    if len(digits) == 10:
        digits = f"7{digits}"
    elif len(digits) == 11 and digits.startswith("8"):
        digits = f"7{digits[1:]}"
    return f"+{digits}"


def _format_phone_link(number: str) -> str:
    """Возвращает номер в теге code — кликабельный в Telegram."""
    normalized = _normalize_phone(number)
    return f'<code>{normalized}</code>'


def _is_missed_call(payload: dict) -> bool:
    cmd = _as_text(payload.get("cmd")).lower()
    if cmd != "history":
        return False
    status = _as_text(payload.get("status")).lower()
    return status == "missed"


def _extract_destination(payload: dict) -> str:
    group = _as_text(payload.get("groupRealName"))
    telnum = _as_text(payload.get("telnum"))
    diversion = _as_text(payload.get("diversion"))
    ext = _as_text(payload.get("ext"))
    user = _as_text(payload.get("user"))

    # Если группа не пришла от АТС — ищем по DID в кеше групп, затем в ручном маппинге
    if not group:
        group = (
            PBX_GROUPS.get(diversion)
            or PBX_GROUPS.get(telnum)
            or MEGAPBX_DID_NAMES.get(diversion)
            or MEGAPBX_DID_NAMES.get(telnum)
            or ""
        )

    for v in (group, telnum, diversion, ext, user):
        if v:
            return _html(v)
    return "неизвестно"


def _is_allowed_destination(payload: dict) -> bool:
    if not MEGAPBX_ALLOWED_GROUPS and not MEGAPBX_ALLOWED_DID:
        return True
    group = _as_text(payload.get("groupRealName"))
    telnum = _as_text(payload.get("telnum"))
    diversion = _as_text(payload.get("diversion"))
    if MEGAPBX_ALLOWED_GROUPS and group in MEGAPBX_ALLOWED_GROUPS:
        return True
    if MEGAPBX_ALLOWED_DID:
        if telnum in MEGAPBX_ALLOWED_DID:
            return True
        if diversion in MEGAPBX_ALLOWED_DID:
            return True
    return False


def _display_caller(payload: dict) -> tuple[str, str]:
    """
    Возвращает (display_text, raw_phone).
    display_text — имя контакта или кликабельный номер.
    raw_phone — нормализованный номер для счётчика.
    """
    name = _html(payload.get("contact_name") or payload.get("name"))
    number = payload.get("phone")

    raw_phone = ""
    if _is_phone(number):
        raw_phone = _normalize_phone(str(number))
    else:
        fallback = _find_phone_anywhere(payload)
        if _is_phone(fallback):
            raw_phone = _normalize_phone(str(fallback))

    if name and raw_phone:
        # Есть имя — показываем имя + кликабельный номер
        display = f"{name} ({_format_phone_link(raw_phone)})"
    elif name:
        display = name
    elif raw_phone:
        display = _format_phone_link(raw_phone)
    else:
        display = "неизвестно"

    return display, raw_phone


def _tg_fullname(user: dict, plain: bool = False) -> str:
    first = _as_text(user.get("first_name"))
    last = _as_text(user.get("last_name"))
    name = (first + " " + last).strip() or _as_text(user.get("username")) or "пользователь"
    uid = user.get("id")
    if plain:
        return name
    return f'<a href="tg://user?id={uid}">{escape(name, quote=False)}</a>' if uid else escape(name, quote=False)


# =========================
# Счётчик пропущенных
# =========================
def _increment_missed_counter(phone: str) -> tuple[int, int]:
    """
    Увеличивает счётчик пропущенных для номера.
    Возвращает (сегодня, всего).
    """
    if not phone:
        return 0, 0
    today = _now_local().strftime("%Y-%m-%d")
    rec = MISSED_COUNTER.setdefault(phone, {"today": 0, "total": 0, "last_date": today})
    if rec["last_date"] != today:
        rec["today"] = 0
        rec["last_date"] = today
    rec["today"] += 1
    rec["total"] += 1
    return rec["today"], rec["total"]


def _rollback_missed_counter(phone: str) -> None:
    """Откатывает предварительное увеличение, если Telegram не принял сообщение."""
    if not phone:
        return
    rec = MISSED_COUNTER.get(phone)
    if not rec:
        return
    rec["today"] = max(0, int(rec.get("today", 0)) - 1)
    rec["total"] = max(0, int(rec.get("total", 0)) - 1)
    if rec["total"] == 0:
        MISSED_COUNTER.pop(phone, None)


# =========================
# Telegram API helpers
# =========================
class TelegramAPIError(RuntimeError):
    """Ошибка Telegram API без попадания токена или payload в текст исключения."""

    def __init__(
        self,
        method: str,
        status_code: Optional[int] = None,
        error_code: Optional[int] = None,
        retry_after: Optional[float] = None,
    ):
        self.method = method
        self.status_code = status_code
        self.error_code = error_code
        self.retry_after = retry_after
        status = status_code if status_code is not None else "network"
        code = f", api_code={error_code}" if error_code is not None else ""
        super().__init__(f"Telegram API {method} failed (status={status}{code})")


def _telegram_retry_delay(response: Optional[httpx.Response], data: Any, attempt: int) -> float:
    retry_after: Any = None
    if isinstance(data, dict):
        parameters = data.get("parameters")
        if isinstance(parameters, dict):
            retry_after = parameters.get("retry_after")
    if retry_after is None and response is not None:
        retry_after = response.headers.get("Retry-After")
    try:
        if retry_after is not None:
            return min(TG_API_RETRY_MAX_SEC, max(0.0, float(retry_after)))
    except (TypeError, ValueError):
        pass
    return min(TG_API_RETRY_MAX_SEC, TG_API_RETRY_BASE_SEC * (2 ** max(0, attempt)))


async def _tg_api(method: str, payload: dict, retry_network: bool = True) -> dict:
    """Вызывает Telegram API, проверяет ok и повторяет временные ошибки."""
    if not TG_BOT_TOKEN:
        raise RuntimeError("TG_BOT_TOKEN is not set")

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    max_retries = min(TG_API_MAX_RETRIES, 10)
    async with httpx.AsyncClient(timeout=30.0) as client:
        for attempt in range(max_retries + 1):
            response: Optional[httpx.Response] = None
            data: Any = None
            try:
                response = await client.post(url, json=payload)
                try:
                    data = response.json()
                except ValueError as exc:
                    if (response.status_code == 429 or response.status_code >= 500) and attempt < max_retries:
                        delay = _telegram_retry_delay(response, None, attempt)
                        logger.warning("Telegram %s returned non-JSON temporary response; retry %d/%d", method, attempt + 1, max_retries)
                        await asyncio.sleep(delay)
                        continue
                    raise TelegramAPIError(method, response.status_code) from exc

                if response.is_success and isinstance(data, dict) and data.get("ok") is True:
                    return data

                status_code = response.status_code
                error_code = data.get("error_code") if isinstance(data, dict) else None
                temporary = status_code == 429 or status_code >= 500 or error_code == 429
                if temporary and attempt < max_retries:
                    delay = _telegram_retry_delay(response, data, attempt)
                    logger.warning("Telegram %s temporary failure; retry %d/%d", method, attempt + 1, max_retries)
                    await asyncio.sleep(delay)
                    continue
                raise TelegramAPIError(
                    method,
                    status_code,
                    error_code,
                    retry_after=(
                        _telegram_retry_delay(response, data, 0)
                        if temporary
                        else None
                    ),
                )
            except httpx.RequestError as exc:
                # read/write timeout или обрыв после отправки неоднозначны:
                # Telegram мог уже принять sendMessage. Не повторяем такие запросы
                # автоматически, иначе можно создать дубликат.
                network_retryable = retry_network and isinstance(
                    exc,
                    (httpx.ConnectError, httpx.ConnectTimeout),
                )
                if not network_retryable or attempt >= max_retries:
                    raise TelegramAPIError(method) from exc
                delay = _telegram_retry_delay(None, None, attempt)
                logger.warning("Telegram %s network failure; retry %d/%d", method, attempt + 1, max_retries)
                await asyncio.sleep(delay)

    raise TelegramAPIError(method)


async def _tg_edit_text_with_called_by(chat_id: int, message_id: int, new_text: str, plain_name: str):
    button_text = f"🤳 Перезвонил {plain_name}"
    rm = {"inline_keyboard": [[{"text": button_text, "callback_data": "call_back_done"}]]}
    await _tg_api("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": new_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": rm,
    })


async def _tg_answer_cb(cb_id: str, text: str = "", alert: bool = False):
    await _tg_api("answerCallbackQuery", {
        "callback_query_id": cb_id,
        "text": text,
        "show_alert": alert,
    })


async def _tg_delete_webhook():
    try:
        await _tg_api("deleteWebhook", {"drop_pending_updates": False})
    except Exception as exc:
        logger.warning("Telegram deleteWebhook failed: %s", type(exc).__name__)


# =========================
# PBX helpers  (REST API: GET /crmapi/v1/users)
# =========================
async def _pbx_fetch_accounts() -> dict:
    """
    Загружает список сотрудников через официальный REST API МегаПБХ.
    Возвращает словарь {login: display_name}.
    """
    if not (MEGAPBX_API_BASE and MEGAPBX_API_TOKEN):
        return {}
    url = f"{MEGAPBX_API_BASE}/crmapi/v1/users"
    headers = {"X-API-KEY": MEGAPBX_API_TOKEN}
    result = {}
    start = 0
    limit = 100
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            while True:
                r = await client.get(url, headers=headers, params={"start": start, "limit": limit})
                r.raise_for_status()
                data = r.json()
                items = data.get("items", [])
                for it in items:
                    login = (it.get("login") or "").strip()
                    name = (it.get("name") or "").strip()
                    if login:
                        result[login] = name or login
                info = data.get("info", {})
                total = info.get("total", 0)
                start += len(items)
                if start >= total or not items:
                    break
    except Exception as exc:
        logger.warning("PBX accounts refresh failed: %s", type(exc).__name__)
    return result


async def _pbx_fetch_groups() -> dict:
    """
    Загружает номера АТС (telnums) и сопоставляет telnum -> название.
    Для обычных номеров берёт group_name / user_name.
    Для IVR-номеров берёт group_name из timeout-кнопки (или первой кнопки).
    Возвращает словарь {телефонный_номер: название}.
    """
    if not (MEGAPBX_API_BASE and MEGAPBX_API_TOKEN):
        return {}
    url = f"{MEGAPBX_API_BASE}/crmapi/v1/telnums"
    headers = {"X-API-KEY": MEGAPBX_API_TOKEN}
    result = {}
    start = 0
    limit = 100
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            while True:
                r = await client.get(url, headers=headers, params={"start": start, "limit": limit})
                r.raise_for_status()
                data = r.json()
                items = data.get("items", [])
                for it in items:
                    telnum = (it.get("telnum") or "").strip()
                    if not telnum:
                        continue
                    route_type = (it.get("type") or "").strip()

                    if route_type == "ivr":
                        # Для IVR берём название из timeout-кнопки, иначе первую группу
                        ivr = it.get("ivr") or {}
                        ivr_items = ivr.get("items") or []
                        name = ""
                        first_group = ""
                        for btn in ivr_items:
                            btn_name = (btn.get("group_name") or "").strip()
                            if btn.get("button") == "timeout" and btn_name:
                                name = btn_name
                                break
                            if not first_group and btn_name:
                                first_group = btn_name
                        if not name:
                            name = first_group
                    else:
                        name = (
                            it.get("group_name")
                            or it.get("user_name")
                            or it.get("name")
                            or ""
                        )
                        name = (name or "").strip()

                    if name:
                        result[telnum] = name

                info = data.get("info", {})
                total = info.get("total", 0)
                start += len(items)
                if start >= total or not items:
                    break
    except Exception as exc:
        logger.warning("PBX groups refresh failed: %s", type(exc).__name__)
    return result


async def _pbx_resolve_user_display(user: str) -> str:
    u = _as_text(user)
    if not u:
        return "сотрудник"
    if not PBX_ACCOUNTS:
        PBX_ACCOUNTS.update(await _pbx_fetch_accounts())
    return PBX_ACCOUNTS.get(u, u)


# =========================
# Telegram: отправка пропущенных
# =========================
async def _tg_send_missed(
    from_text: str,
    to_text: str,
    callid: str,
    wait: Optional[int] = None,
    duration: Optional[int] = None,
    phone: Optional[str] = None,
    diversion: Optional[str] = None,
    missed_today: int = 0,
    missed_total: int = 0,
) -> dict:
    try:
        wait = int(wait or 0)
    except (TypeError, ValueError):
        wait = 0
    try:
        duration = int(duration or 0)
    except (TypeError, ValueError):
        duration = 0

    now_str = _format_time(_now_local())

    parts = [
        "📵 <b>Пропущенный звонок</b>",
        f"🕐 {now_str}",
        f"От: {from_text}",
        f"Кому: {to_text}",
    ]

    extra = []
    if wait > 0:
        extra.append(f"ожидание: {wait} с")
    if duration > 0:
        extra.append(f"длительность: {duration} с")
    if extra:
        parts.append("⏱ Время: " + ", ".join(extra))

    # Счётчик — показываем только если звонков больше одного
    if missed_today > 1 or missed_total > 1:
        counter_parts = []
        if missed_today > 1:
            counter_parts.append(f"сегодня: {missed_today}")
        if missed_total > missed_today:
            counter_parts.append(f"всего: {missed_total}")
        parts.append("🔁 Пропущено (" + ", ".join(counter_parts) + ")")

    text = "\n".join(parts)

    rm = {"inline_keyboard": [[{"text": "📲 Я наберу", "callback_data": "call_back"}]]}
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": rm,
    }
    data = await _tg_api("sendMessage", payload, retry_network=False)
    try:
        msg = data.get("result", {})
        message_id = msg.get("message_id") if isinstance(msg, dict) else None
        if callid and message_id is not None:
            MISSED_INDEX[str(callid)] = {
                "chat_id": TG_CHAT_ID,
                "message_id": message_id,
                "text": text,
                "closed": False,
                "who": None,
            }
            MISSED_LIST.append({
                "callid": str(callid),
                "phone": str(phone or ""),
                "diversion": str(diversion or ""),
                "created_ts": time.time(),
            })
            logger.info("Missed-call notification sent: callid=%s message_id=%s", _fingerprint(callid), message_id)
        else:
            logger.warning("Missed-call notification sent without correlation data: callid=%s", _fingerprint(callid))
    except Exception as exc:
        logger.error("Failed to store missed-call state: %s", type(exc).__name__)
    return data


async def _send_new_missed(payload: dict) -> dict:
    """Выполняет одну попытку уведомления и корректно откатывает счётчик при ошибке."""
    from_text, raw_phone = _display_caller(payload)
    to_text = _extract_destination(payload)
    callid = _as_text(payload.get("callid"))
    diversion = _as_text(payload.get("diversion"))
    lock_key = raw_phone or "<empty-phone>"

    async with _MISSED_PENDING_LOCK:
        phone_lock = MISSED_PHONE_LOCKS.setdefault(lock_key, asyncio.Lock())

    # Счётчик резервируется и откатывается под одним замком на номер.
    # Поэтому два параллельных звонка не могут получить неверные номера.
    async with phone_lock:
        missed_today, missed_total = _increment_missed_counter(raw_phone)
        try:
            result = await _tg_send_missed(
                from_text,
                to_text,
                callid,
                wait=payload.get("wait"),
                duration=payload.get("duration"),
                phone=raw_phone,
                diversion=diversion,
                missed_today=missed_today,
                missed_total=missed_total,
            )
            if callid:
                async with _MISSED_PENDING_LOCK:
                    MISSED_SEEN[callid] = time.time()
            return result
        except BaseException:
            _rollback_missed_counter(raw_phone)
            raise


async def _send_missed_once(payload: dict) -> dict:
    """Отправляет missed webhook не более одного раза для одного callid."""
    callid = _as_text(payload.get("callid"))
    if not callid:
        logger.warning("Missed webhook has no callid; idempotency unavailable")
        return await _send_new_missed(payload)

    async with _MISSED_PENDING_LOCK:
        if callid in MISSED_INDEX:
            logger.info("Ignoring duplicate missed webhook: callid=%s", _fingerprint(callid))
            return {"ok": True, "duplicate": True}
        seen_at = MISSED_SEEN.get(callid)
        if seen_at is not None:
            if time.time() - seen_at <= MISSED_DEDUP_TTL_SEC:
                logger.info("Ignoring duplicate missed webhook after cleanup: callid=%s", _fingerprint(callid))
                return {"ok": True, "duplicate": True}
            MISSED_SEEN.pop(callid, None)
        task = MISSED_PENDING.get(callid)
        owner = task is None
        if owner:
            task = asyncio.create_task(_send_new_missed(payload))
            MISSED_PENDING[callid] = task

    try:
        result = await asyncio.shield(task)
        if not owner:
            return {"ok": True, "duplicate": True}
        return result
    finally:
        if task.done():
            async with _MISSED_PENDING_LOCK:
                if MISSED_PENDING.get(callid) is task:
                    MISSED_PENDING.pop(callid, None)


def _find_recent_missed_by_phone(phone: str, max_age_sec: int = MISSED_MAX_AGE_SEC) -> Optional[str]:
    if not phone:
        return None
    # Нормализуем входящий номер для сравнения
    phone_norm = _normalize_phone(phone)
    now = time.time()
    candidate_callid = None
    candidate_ts = 0.0
    for rec in MISSED_LIST:
        if now - rec.get("created_ts", 0) > max_age_sec:
            continue
        cid = rec.get("callid")
        if not cid:
            continue
        mi = MISSED_INDEX.get(cid)
        if not mi or mi.get("closed") or mi.get("_closing"):
            continue
        rec_phone_norm = _normalize_phone(str(rec.get("phone", "")))
        if rec_phone_norm == phone_norm:
            ts = rec.get("created_ts", 0)
            if ts >= candidate_ts:
                candidate_ts = ts
                candidate_callid = cid
    return candidate_callid


def _claim_missed_record(rec: Optional[dict]) -> bool:
    if not rec or rec.get("closed") or rec.get("_closing"):
        return False
    rec["_closing"] = True
    return True


def _release_missed_record(rec: dict) -> None:
    rec["_closing"] = False


async def _auto_close_by_event(payload: dict):
    """
    Авто‑закрытие пропущенных:
    1) по совпадающему callid,
    2) если(callid отсутствует) — по последнему незакрытому Missed с тем же phone.

    Событие OUTGOING намеренно не считается ответом: оно может означать лишь
    начало исходящего звонка. Для закрытия используются ACCEPTED/COMPLETED
    либо подтверждённая history-ветка ниже.
    """
    callid = _as_text(payload.get("callid"))
    ev_type = _as_text(payload.get("type")).upper()
    if ev_type not in {"ACCEPTED", "COMPLETED"}:
        return

    rec = MISSED_INDEX.get(callid) if callid else None
    if rec is None:
        # Неизвестный непустой callid не должен подменяться phone fallback:
        # после cleanup это может быть replay старого/чужого события.
        if callid:
            return
        phone = _as_text(payload.get("phone"))
        guessed_callid = _find_recent_missed_by_phone(phone)
        if not guessed_callid:
            return
        callid = guessed_callid
        rec = MISSED_INDEX.get(callid)

    # Если exact callid уже закрыт, повторное событие не должно закрывать
    # более новый звонок того же клиента через phone fallback.
    if not _claim_missed_record(rec):
        return

    user_raw = _as_text(payload.get("user"))
    try:
        user_name = await _pbx_resolve_user_display(user_raw)
        await _tg_edit_text_with_called_by(
            rec["chat_id"],
            rec["message_id"],
            rec["text"],
            user_name,
        )
        rec["closed"] = True
        rec["who"] = user_name
        logger.info("Missed call auto-closed by event: callid=%s", _fingerprint(callid))
    except asyncio.CancelledError:
        _release_missed_record(rec)
        raise
    except Exception as exc:
        _release_missed_record(rec)
        logger.warning("Missed call auto-close by event failed: %s", type(exc).__name__)


# Статусы неудачных исходящих звонков
CALL_FAIL_STATUSES = {
    "Missed":       "📵 Не взяли трубку",
    "Busy":         "☎️ Занято",
    "Cancel":       "🚫 Отменён",
    "NotAvailable": "📴 Недоступен",
    "NotAllowed":   "⛔️ Направление запрещено",
    "NotFound":     "❓ Абонент не найден",
}


async def _tg_update_callback_status(rec: dict, status_text: str, who: str):
    """Обновляет текст сообщения — добавляет строку о неудачной попытке перезвона."""
    base_text = rec.get("text", "")
    # Убираем предыдущие строки о попытках чтобы не дублировать
    lines = [line for line in base_text.split("\n") if not line.startswith("↩️")]
    lines.append(f"↩️ {_html(who)}: {_html(status_text)}")
    new_text = "\n".join(lines)

    rm = {"inline_keyboard": [[{"text": "📲 Я наберу", "callback_data": "call_back"}]]}
    await _tg_api("editMessageText", {
        "chat_id": rec["chat_id"],
        "message_id": rec["message_id"],
        "text": new_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": rm,
    })
    # Состояние меняем только после подтверждённого успешного edit.
    rec["text"] = new_text


async def _auto_close_by_callback(payload: dict):
    """
    Закрывает пропущенный звонок когда сотрудник перезвонил клиенту
    (cmd=history, status=Success, type=out, missedStatus=2).
    """
    phone = _as_text(payload.get("phone"))
    user_raw = _as_text(payload.get("user"))

    # Ищем незакрытый пропущенный по номеру клиента
    callid = _find_recent_missed_by_phone(phone)
    if not callid:
        return
    rec = MISSED_INDEX.get(callid)
    if not _claim_missed_record(rec):
        return

    try:
        user_name = await _pbx_resolve_user_display(user_raw)
        await _tg_edit_text_with_called_by(
            rec["chat_id"],
            rec["message_id"],
            rec["text"],
            user_name,
        )
        rec["closed"] = True
        rec["who"] = user_name
        logger.info("Missed call auto-closed by callback history: callid=%s", _fingerprint(callid))
    except asyncio.CancelledError:
        _release_missed_record(rec)
        raise
    except Exception as exc:
        _release_missed_record(rec)
        logger.warning("Missed call auto-close by callback failed: %s", type(exc).__name__)


async def _update_failed_callback(payload: dict):
    """
    Обновляет сообщение о пропущенном если перезвонок не состоялся
    (занято, не взяли трубку, недоступен и т.д.).
    """
    phone = _as_text(payload.get("phone"))
    user_raw = _as_text(payload.get("user"))
    status_raw = _as_text(payload.get("status"))

    callid = _find_recent_missed_by_phone(phone)
    if not callid:
        return
    rec = MISSED_INDEX.get(callid)
    if not _claim_missed_record(rec):
        return

    try:
        user_name = await _pbx_resolve_user_display(user_raw)
        status_key = next(
            (key for key in CALL_FAIL_STATUSES if key.lower() == status_raw.lower()),
            status_raw,
        )
        status_text = CALL_FAIL_STATUSES.get(status_key, f"❌ {status_raw}")
        await _tg_update_callback_status(rec, status_text, user_name)
        _release_missed_record(rec)
        logger.info(
            "Missed call callback status updated: callid=%s status=%s",
            _fingerprint(callid),
            _safe_log_value(status_raw),
        )
    except asyncio.CancelledError:
        _release_missed_record(rec)
        raise
    except Exception as exc:
        _release_missed_record(rec)
        logger.warning("Missed call callback status update failed: %s", type(exc).__name__)


# =========================
# Очистка устаревших записей
# =========================
def _cleanup_missed():
    """Удаляет записи старше MISSED_MAX_AGE_SEC из MISSED_LIST и MISSED_INDEX."""
    now = time.time()
    cutoff = now - MISSED_MAX_AGE_SEC
    old_callids = set()
    remaining = []
    for rec in MISSED_LIST:
        if rec.get("created_ts", 0) < cutoff:
            old_callids.add(rec.get("callid"))
        else:
            remaining.append(rec)
    MISSED_LIST.clear()
    MISSED_LIST.extend(remaining)
    for cid in old_callids:
        if cid and cid in MISSED_INDEX:
            del MISSED_INDEX[cid]
    for callid, task in list(MISSED_PENDING.items()):
        if task.done():
            MISSED_PENDING.pop(callid, None)
    seen_cutoff = now - MISSED_DEDUP_TTL_SEC
    for callid, seen_at in list(MISSED_SEEN.items()):
        if seen_at < seen_cutoff:
            MISSED_SEEN.pop(callid, None)
    if old_callids:
        logger.info("Missed-call cleanup removed %d old records", len(old_callids))


# =========================
# Long polling
# =========================
async def _poll_updates_loop():
    if not TG_BOT_TOKEN:
        logger.warning("Telegram polling disabled: bot token is not configured")
        return
    if TG_DELETE_WEBHOOK_ON_START:
        await _tg_delete_webhook()

    offset = 0
    poll_attempt = 0
    logger.info("Telegram long polling started")
    async with httpx.AsyncClient(timeout=35.0) as client:
        api = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
        while True:
            try:
                resp = await client.get(
                    f"{api}/getUpdates",
                    params={"offset": offset, "timeout": 30, "allowed_updates": ["callback_query"]},
                )
                try:
                    data = resp.json()
                except ValueError as exc:
                    temporary = resp.status_code == 429 or resp.status_code >= 500
                    raise TelegramAPIError(
                        "getUpdates",
                        resp.status_code,
                        retry_after=(
                            _telegram_retry_delay(resp, None, poll_attempt)
                            if temporary
                            else None
                        ),
                    ) from exc

                error_code = data.get("error_code") if isinstance(data, dict) else None
                if not resp.is_success or not isinstance(data, dict) or data.get("ok") is not True:
                    temporary = resp.status_code == 429 or resp.status_code >= 500 or error_code == 429
                    raise TelegramAPIError(
                        "getUpdates",
                        resp.status_code,
                        error_code,
                        retry_after=(
                            _telegram_retry_delay(resp, data, poll_attempt)
                            if temporary
                            else None
                        ),
                    )

                poll_attempt = 0
                result = data.get("result", [])
                if not isinstance(result, list):
                    raise TelegramAPIError("getUpdates", resp.status_code)

                for upd in result:
                    if not isinstance(upd, dict):
                        continue
                    try:
                        update_id = int(upd.get("update_id", 0))
                    except (TypeError, ValueError):
                        logger.warning("Telegram update has invalid update_id")
                        continue
                    update_offset = max(offset, update_id + 1)
                    cb = upd.get("callback_query")
                    if not isinstance(cb, dict):
                        offset = update_offset
                        continue

                    cb_data = _as_text(cb.get("data"))
                    cb_id = _as_text(cb.get("id"))

                    # Кнопка уже нажата другим — просто уведомляем.
                    if cb_data == "call_back_done":
                        try:
                            await _tg_answer_cb(cb_id, "Уже отмечено как перезвонивший!")
                        except Exception as exc:
                            logger.warning("Telegram callback acknowledgement failed: %s", type(exc).__name__)
                        offset = update_offset
                        continue

                    if cb_data != "call_back":
                        offset = update_offset
                        continue

                    msg = cb.get("message") or {}
                    chat = msg.get("chat") or {}
                    chat_id = chat.get("id")
                    message_id = msg.get("message_id")
                    original_text = _as_text(msg.get("text"))
                    who_plain = _tg_fullname(cb.get("from") or {}, plain=True)

                    # Не редактируем сообщения из другого чата.
                    if TG_CHAT_ID and chat_id != TG_CHAT_ID:
                        try:
                            await _tg_answer_cb(cb_id, "Это уведомление недоступно", alert=True)
                        except Exception as exc:
                            logger.warning("Telegram callback rejection failed: %s", type(exc).__name__)
                        offset = update_offset
                        continue

                    if not chat_id or not message_id:
                        try:
                            await _tg_answer_cb(cb_id, "Не удалось определить сообщение", alert=True)
                        except Exception as exc:
                            logger.warning("Telegram callback validation failed: %s", type(exc).__name__)
                        offset = update_offset
                        continue

                    rec = None
                    for candidate in MISSED_INDEX.values():
                        if candidate.get("chat_id") == chat_id and candidate.get("message_id") == message_id:
                            rec = candidate
                            break

                    if rec is not None and not _claim_missed_record(rec):
                        try:
                            await _tg_answer_cb(cb_id, "Уже отмечено как перезвонивший!")
                        except Exception as exc:
                            logger.warning("Telegram callback acknowledgement failed: %s", type(exc).__name__)
                        offset = update_offset
                        continue

                    try:
                        await _tg_edit_text_with_called_by(chat_id, message_id, original_text, who_plain)
                        if rec is not None:
                            rec["closed"] = True
                            rec["who"] = who_plain
                        await _tg_answer_cb(cb_id, "Отметили, спасибо!")
                    except asyncio.CancelledError:
                        if rec is not None:
                            _release_missed_record(rec)
                        raise
                    except Exception as exc:
                        if rec is not None:
                            _release_missed_record(rec)
                        logger.warning("Telegram callback edit failed: %s", type(exc).__name__)

                    offset = update_offset

            except TelegramAPIError as exc:
                poll_attempt += 1
                if exc.status_code in {400, 401, 403, 404}:
                    delay = 30.0
                else:
                    delay = exc.retry_after or _telegram_retry_delay(
                        None, None, min(poll_attempt - 1, 10)
                    )
                logger.warning("Telegram getUpdates failed; retrying in %.1fs: %s", delay, type(exc).__name__)
                await asyncio.sleep(delay)
            except httpx.RequestError as exc:
                poll_attempt += 1
                delay = _telegram_retry_delay(None, None, min(poll_attempt - 1, 10))
                logger.warning("Telegram getUpdates network failure; retrying in %.1fs: %s", delay, type(exc).__name__)
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Telegram polling loop error: %s", type(exc).__name__)
                await asyncio.sleep(3.0)


async def _accounts_refresh_loop():
    while True:
        try:
            if MEGAPBX_API_BASE and MEGAPBX_API_TOKEN:
                fresh = await _pbx_fetch_accounts()
                if fresh:
                    PBX_ACCOUNTS.clear()
                    PBX_ACCOUNTS.update(fresh)
                    logger.info("PBX accounts refreshed: %d users", len(fresh))
                groups = await _pbx_fetch_groups()
                if groups:
                    PBX_GROUPS.clear()
                    PBX_GROUPS.update(groups)
                    logger.info("PBX groups refreshed: %d entries", len(groups))
        except Exception as exc:
            logger.warning("PBX accounts/groups refresh loop failed: %s", type(exc).__name__)
        await asyncio.sleep(600)


async def _missed_cleanup_loop():
    while True:
        await asyncio.sleep(MISSED_CLEANUP_INTERVAL_SEC)
        try:
            _cleanup_missed()
        except Exception as exc:
            logger.warning("Missed-call cleanup loop failed: %s", type(exc).__name__)


# =========================
# FastAPI lifecycle
# =========================
@asynccontextmanager
async def lifespan(app: FastAPI):
    poll_task = asyncio.create_task(_poll_updates_loop())
    acc_task = asyncio.create_task(_accounts_refresh_loop())
    cleanup_task = asyncio.create_task(_missed_cleanup_loop())
    try:
        yield
    finally:
        tasks = (poll_task, acc_task, cleanup_task)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("Background task stopped with error: %s", type(exc).__name__)


app = FastAPI(title="MegaPBX → Telegram (missed calls, long polling)", lifespan=lifespan)


@app.get("/")
async def root():
    return {"status": "ok", "mode": "long-polling"}


# =========================
# MegaPBX webhook
# =========================
def _parse_webhook_body(body_text: str) -> dict:
    """Поддерживает JSON и URL-encoded form, включая JSON в поле payload."""
    try:
        parsed = json.loads(body_text)
        if not isinstance(parsed, dict):
            raise ValueError("JSON payload must be an object")
        return parsed
    except (json.JSONDecodeError, ValueError):
        parsed_form = parse_qs(
            body_text,
            keep_blank_values=True,
            max_num_fields=100,
        )
        if not parsed_form:
            raise ValueError("empty webhook payload")

        if "payload" in parsed_form:
            nested = parsed_form["payload"][0]
            try:
                nested_payload = json.loads(nested)
            except (json.JSONDecodeError, TypeError) as exc:
                raise ValueError("invalid nested payload") from exc
            if not isinstance(nested_payload, dict):
                raise ValueError("nested payload must be an object")
            return nested_payload

        form_payload = {
            key: values[0]
            for key, values in parsed_form.items()
            if values and values[0] != ""
        }
        if not form_payload:
            raise ValueError("empty webhook payload")
        return form_payload


@app.post("/megapbx/webhook")
async def megapbx_webhook(
    request: Request,
    x_crm_token: Optional[str] = Header(default=None),
    token: Optional[str] = None,
):
    auth = request.headers.get("authorization") or ""
    supplied = x_crm_token or request.headers.get("x-crm-token")
    if token and not MEGAPBX_ALLOW_QUERY_TOKEN:
        logger.warning("Ignoring query CRM token; use the X-CRM-Token header")
    if not supplied and MEGAPBX_ALLOW_QUERY_TOKEN:
        supplied = token
    if not supplied and auth:
        low = auth.lower()
        if low.startswith("bearer "):
            supplied = auth[7:].strip()
        elif low.startswith("basic "):
            try:
                userpass = b64decode(auth[6:].strip()).decode("utf-8", "ignore")
                supplied = userpass.split(":", 1)[-1]
            except (ValueError, UnicodeError):
                pass

    # Fail closed: пустой CRM-токен больше не превращает endpoint в открытый webhook.
    if not MEGAPBX_CRM_TOKEN:
        logger.error("MegaPBX webhook rejected: CRM token is not configured")
        raise HTTPException(status_code=503, detail="Webhook authentication is not configured")
    if not supplied or not secrets.compare_digest(str(supplied), MEGAPBX_CRM_TOKEN):
        raise HTTPException(status_code=401, detail="Bad CRM token")

    content_length = request.headers.get("content-length")
    try:
        declared_length = int(content_length) if content_length else 0
    except ValueError:
        declared_length = 0
    if declared_length > MAX_WEBHOOK_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Webhook payload is too large")

    raw_body = await request.body()
    if len(raw_body) > MAX_WEBHOOK_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Webhook payload is too large")

    body_text = raw_body.decode("utf-8", errors="replace")
    try:
        payload = _parse_webhook_body(body_text)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid webhook payload")

    logger.info(
        "MegaPBX webhook received: cmd=%s status=%s callid=%s bytes=%d",
        _safe_log_value(payload.get("cmd")),
        _safe_log_value(payload.get("status")),
        _fingerprint(payload.get("callid")),
        len(raw_body),
    )

    cmd = _as_text(payload.get("cmd")).lower()
    status = _as_text(payload.get("status")).lower()

    notification_result: Optional[dict] = None
    if _is_missed_call(payload) and _is_allowed_destination(payload):
        try:
            notification_result = await _send_missed_once(payload)
        except (TelegramAPIError, RuntimeError) as exc:
            logger.error("Missed-call notification failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Notification service unavailable") from exc

    if cmd == "event":
        await _auto_close_by_event(payload)

    # Закрываем пропущенный если сотрудник перезвонил (history Success + missedStatus=2)
    if (
        cmd == "history"
        and status == "success"
        and _as_text(payload.get("type")).lower() == "out"
        and _as_text(payload.get("missedStatus")) == "2"
    ):
        await _auto_close_by_callback(payload)

    # Обновляем статус если исходящий перезвонок не состоялся
    if (
        cmd == "history"
        and _as_text(payload.get("type")).lower() == "out"
        and status != "success"
        and _as_text(payload.get("missedStatus")) == "2"
    ):
        await _update_failed_callback(payload)

    if notification_result is not None:
        return {"ok": True, "duplicate": bool(notification_result.get("duplicate"))}
    return {"ok": True}
