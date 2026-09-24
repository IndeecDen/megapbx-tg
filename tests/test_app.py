import asyncio
import logging

import httpx
import pytest

import app as bot


@pytest.fixture(autouse=True)
def reset_runtime_state(monkeypatch):
    bot.MISSED_INDEX.clear()
    bot.MISSED_LIST.clear()
    bot.MISSED_PENDING.clear()
    bot.MISSED_SEEN.clear()
    bot.MISSED_PHONE_LOCKS.clear()
    bot.MISSED_COUNTER.clear()
    bot.PBX_ACCOUNTS.clear()
    bot.PBX_GROUPS.clear()
    monkeypatch.setattr(bot, "TG_CHAT_ID", -100123)
    monkeypatch.setattr(bot, "TG_BOT_TOKEN", "test-token")
    monkeypatch.setattr(bot, "MEGAPBX_CRM_TOKEN", "crm-secret")
    monkeypatch.setattr(bot, "MEGAPBX_ALLOWED_GROUPS", set())
    monkeypatch.setattr(bot, "MEGAPBX_ALLOWED_DID", set())
    yield
    bot.MISSED_INDEX.clear()
    bot.MISSED_LIST.clear()
    bot.MISSED_PENDING.clear()
    bot.MISSED_SEEN.clear()
    bot.MISSED_PHONE_LOCKS.clear()
    bot.MISSED_COUNTER.clear()


def missed_payload(callid: str = "call-1", phone: str = "+79161234567") -> dict:
    return {
        "cmd": "history",
        "status": "Missed",
        "callid": callid,
        "phone": phone,
        "groupRealName": "Поддержка",
        "wait": 5,
        "duration": 0,
    }


@pytest.mark.asyncio
async def test_duplicate_callid_is_sent_once(monkeypatch):
    sent = []

    async def fake_send(*args, **kwargs):
        await asyncio.sleep(0)
        sent.append(kwargs.get("callid", args[2] if len(args) > 2 else ""))
        bot.MISSED_INDEX[kwargs.get("callid", args[2] if len(args) > 2 else "")] = {
            "chat_id": bot.TG_CHAT_ID,
            "message_id": 10,
            "text": "test",
            "closed": False,
            "who": None,
        }
        return {"ok": True}

    monkeypatch.setattr(bot, "_tg_send_missed", fake_send)
    payload = missed_payload()

    first, second = await asyncio.gather(
        bot._send_missed_once(payload),
        bot._send_missed_once(payload),
    )

    assert sent == ["call-1"]
    assert first.get("duplicate") is not True
    assert second.get("duplicate") is True


@pytest.mark.asyncio
async def test_failed_send_rolls_back_counter(monkeypatch):
    async def fake_send(*args, **kwargs):
        raise bot.TelegramAPIError("sendMessage", 503)

    monkeypatch.setattr(bot, "_tg_send_missed", fake_send)

    with pytest.raises(bot.TelegramAPIError):
        await bot._send_missed_once(missed_payload("call-failed"))

    assert "+79161234567" not in bot.MISSED_COUNTER
    assert "call-failed" not in bot.MISSED_PENDING


@pytest.mark.asyncio
async def test_replayed_closed_callid_does_not_close_new_call(monkeypatch):
    now = bot.time.time()
    old = {
        "chat_id": bot.TG_CHAT_ID,
        "message_id": 1,
        "text": "old",
        "closed": True,
        "who": "old",
    }
    new = {
        "chat_id": bot.TG_CHAT_ID,
        "message_id": 2,
        "text": "new",
        "closed": False,
        "who": None,
    }
    bot.MISSED_INDEX.update({"old-call": old, "new-call": new})
    bot.MISSED_LIST.extend([
        {"callid": "old-call", "phone": "+79161234567", "diversion": "1", "created_ts": now - 2},
        {"callid": "new-call", "phone": "+79161234567", "diversion": "1", "created_ts": now - 1},
    ])
    edited = []

    async def fake_edit(*args, **kwargs):
        edited.append(args[:2])

    async def fake_user(user):
        return "operator"

    monkeypatch.setattr(bot, "_tg_edit_text_with_called_by", fake_edit)
    monkeypatch.setattr(bot, "_pbx_resolve_user_display", fake_user)

    await bot._auto_close_by_event({
        "cmd": "event",
        "type": "ACCEPTED",
        "callid": "old-call",
        "phone": "+79161234567",
    })

    assert edited == []
    assert new["closed"] is False


@pytest.mark.asyncio
async def test_outgoing_event_does_not_close_call(monkeypatch):
    rec = {
        "chat_id": bot.TG_CHAT_ID,
        "message_id": 3,
        "text": "test",
        "closed": False,
        "who": None,
    }
    bot.MISSED_INDEX["call-1"] = rec
    bot.MISSED_LIST.append({
        "callid": "call-1",
        "phone": "+79161234567",
        "diversion": "1",
        "created_ts": bot.time.time(),
    })
    edited = []

    async def fake_edit(*args, **kwargs):
        edited.append(args[:2])

    monkeypatch.setattr(bot, "_tg_edit_text_with_called_by", fake_edit)
    await bot._auto_close_by_event({
        "cmd": "event",
        "type": "OUTGOING",
        "callid": "call-1",
        "phone": "+79161234567",
    })

    assert edited == []
    assert rec["closed"] is False


@pytest.mark.asyncio
async def test_telegram_api_retries_temporary_error(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={"ok": False, "error_code": 503})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    transport = httpx.MockTransport(handler)
    original_client = bot.httpx.AsyncClient

    def client_factory(*args, **kwargs):
        return original_client(transport=transport)

    monkeypatch.setattr(bot.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(bot, "TG_API_MAX_RETRIES", 1)
    monkeypatch.setattr(bot, "TG_API_RETRY_BASE_SEC", 0.01)
    monkeypatch.setattr(bot, "TG_API_RETRY_MAX_SEC", 0.02)

    result = await bot._tg_api("sendMessage", {"chat_id": 1, "text": "test"})

    assert result["ok"] is True
    assert len(calls) == 2
    assert "test-token" not in str(result)


@pytest.mark.asyncio
async def test_telegram_api_rejects_ok_false(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "error_code": 400})

    transport = httpx.MockTransport(handler)
    original_client = bot.httpx.AsyncClient
    monkeypatch.setattr(
        bot.httpx,
        "AsyncClient",
        lambda *args, **kwargs: original_client(transport=transport),
    )
    monkeypatch.setattr(bot, "TG_API_MAX_RETRIES", 0)

    with pytest.raises(bot.TelegramAPIError) as exc_info:
        await bot._tg_api("sendMessage", {"chat_id": 1, "text": "test"})

    assert "test-token" not in str(exc_info.value)


def test_html_escapes_dynamic_values():
    display, phone = bot._display_caller({
        "contact_name": "A & <B>",
        "phone": "9161234567",
    })

    assert display == "A &amp; &lt;B&gt; (<code>+79161234567</code>)"
    assert phone == "+79161234567"


def test_webhook_parser_supports_json_and_nested_form():
    assert bot._parse_webhook_body('{"cmd":"history"}') == {"cmd": "history"}
    assert bot._parse_webhook_body("cmd=history&status=Missed&callid=1")["status"] == "Missed"
    assert bot._parse_webhook_body("payload=%7B%22cmd%22%3A%22history%22%7D") == {"cmd": "history"}

    with pytest.raises(ValueError):
        bot._parse_webhook_body("[]")
    with pytest.raises(ValueError):
        bot._parse_webhook_body("not-a-payload")


@pytest.mark.asyncio
async def test_webhook_requires_token_and_reports_duplicate(monkeypatch):
    sent = []

    async def fake_send_once(payload):
        sent.append(payload["callid"])
        if len(sent) == 1:
            return {"ok": True}
        return {"ok": True, "duplicate": True}

    monkeypatch.setattr(bot, "_send_missed_once", fake_send_once)
    transport = httpx.ASGITransport(app=bot.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = missed_payload("webhook-1")
        unauthorized = await client.post("/megapbx/webhook", json=payload)
        first = await client.post(
            "/megapbx/webhook",
            json=payload,
            headers={"X-CRM-Token": "crm-secret"},
        )
        second = await client.post(
            "/megapbx/webhook",
            json=payload,
            headers={"X-CRM-Token": "crm-secret"},
        )

    assert unauthorized.status_code == 401
    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert sent == ["webhook-1", "webhook-1"]


@pytest.mark.asyncio
async def test_webhook_fails_closed_without_server_token(monkeypatch):
    monkeypatch.setattr(bot, "MEGAPBX_CRM_TOKEN", "")
    transport = httpx.ASGITransport(app=bot.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/megapbx/webhook", json=missed_payload("no-token"))

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_webhook_logs_do_not_contain_payload_pii(monkeypatch, caplog):
    async def fake_send_once(payload):
        return {"ok": True}

    monkeypatch.setattr(bot, "_send_missed_once", fake_send_once)
    caplog.set_level(logging.INFO, logger="megapbx_tg")
    transport = httpx.ASGITransport(app=bot.app)
    secret_phone = "+79165550101"
    secret_name = "Private Client"

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/megapbx/webhook",
            json=missed_payload("private-1", secret_phone) | {"contact_name": secret_name},
            headers={"X-CRM-Token": "crm-secret"},
        )

    assert response.status_code == 200
    assert secret_phone not in caplog.text
    assert secret_name not in caplog.text
    assert "crm-secret" not in caplog.text


@pytest.mark.asyncio
async def test_failed_edit_does_not_change_stored_text(monkeypatch):
    rec = {
        "chat_id": bot.TG_CHAT_ID,
        "message_id": 8,
        "text": "original",
        "closed": False,
        "who": None,
    }

    async def fail_edit(*args, **kwargs):
        raise bot.TelegramAPIError("editMessageText", 500)

    monkeypatch.setattr(bot, "_tg_api", fail_edit)

    with pytest.raises(bot.TelegramAPIError):
        await bot._tg_update_callback_status(rec, "Busy", "operator")



@pytest.mark.asyncio
async def test_webhook_history_missed_callback_reaches_status_branch(monkeypatch):
    sent = []
    status_updates = []

    async def fake_send_once(payload):
        sent.append(payload["callid"])
        return {"ok": True}

    async def fake_update(payload):
        status_updates.append(payload["callid"])

    monkeypatch.setattr(bot, "_send_missed_once", fake_send_once)
    monkeypatch.setattr(bot, "_update_failed_callback", fake_update)
    transport = httpx.ASGITransport(app=bot.app)
    payload = missed_payload("late-history") | {"type": "out", "missedStatus": "2"}

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/megapbx/webhook",
            json=payload,
            headers={"X-CRM-Token": "crm-secret"},
        )

    assert response.status_code == 200
    assert sent == ["late-history"]
    assert status_updates == ["late-history"]


@pytest.mark.asyncio
async def test_unknown_nonempty_callid_does_not_use_phone_fallback(monkeypatch):
    rec = {
        "chat_id": bot.TG_CHAT_ID,
        "message_id": 9,
        "text": "test",
        "closed": False,
        "who": None,
    }
    bot.MISSED_INDEX["current"] = rec
    bot.MISSED_LIST.append({
        "callid": "current",
        "phone": "+79161234567",
        "diversion": "1",
        "created_ts": bot.time.time(),
    })
    edited = []

    async def fake_edit(*args, **kwargs):
        edited.append(args[:2])

    monkeypatch.setattr(bot, "_tg_edit_text_with_called_by", fake_edit)
    await bot._auto_close_by_event({
        "cmd": "event",
        "type": "ACCEPTED",
        "callid": "unknown-after-cleanup",
        "phone": "+79161234567",
    })

    assert edited == []
    assert rec["closed"] is False


@pytest.mark.asyncio
async def test_ambiguous_read_timeout_is_not_retried(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("response lost", request=request)

    transport = httpx.MockTransport(handler)
    original_client = bot.httpx.AsyncClient
    monkeypatch.setattr(
        bot.httpx,
        "AsyncClient",
        lambda *args, **kwargs: original_client(transport=transport),
    )
    monkeypatch.setattr(bot, "TG_API_MAX_RETRIES", 3)

    with pytest.raises(bot.TelegramAPIError):
        await bot._tg_api("sendMessage", {"chat_id": 1, "text": "test"})

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_parallel_same_phone_counter_rollback_is_correct(monkeypatch):
    successful_counts = []

    async def fake_send(*args, **kwargs):
        callid = args[2]
        if callid == "call-a":
            await asyncio.sleep(0.01)
            raise bot.TelegramAPIError("sendMessage", 503)
        successful_counts.append(kwargs["missed_today"])
        return {"ok": True, "result": {"message_id": 11}}

    monkeypatch.setattr(bot, "_tg_send_missed", fake_send)
    results = await asyncio.gather(
        bot._send_missed_once(missed_payload("call-a")),
        bot._send_missed_once(missed_payload("call-b")),
        return_exceptions=True,
    )

    assert any(isinstance(result, bot.TelegramAPIError) for result in results)
    assert successful_counts == [1]
    assert bot.MISSED_COUNTER["+79161234567"]["total"] == 1


@pytest.mark.asyncio
async def test_seen_callid_survives_index_cleanup(monkeypatch):
    bot.MISSED_SEEN["seen-call"] = bot.time.time()
    sent = []

    async def fake_send(*args, **kwargs):
        sent.append(args[2])

    monkeypatch.setattr(bot, "_tg_send_missed", fake_send)
    result = await bot._send_missed_once(missed_payload("seen-call"))

    assert result["duplicate"] is True
    assert sent == []


@pytest.mark.asyncio
async def test_query_crm_token_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(bot, "MEGAPBX_ALLOW_QUERY_TOKEN", False)
    transport = httpx.ASGITransport(app=bot.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/megapbx/webhook",
            params={"token": "crm-secret"},
            json=missed_payload("query-token"),
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_cancelled_auto_close_releases_claim(monkeypatch):
    rec = {
        "chat_id": bot.TG_CHAT_ID,
        "message_id": 12,
        "text": "test",
        "closed": False,
        "who": None,
    }
    bot.MISSED_INDEX["cancel-call"] = rec
    bot.MISSED_LIST.append({
        "callid": "cancel-call",
        "phone": "+79161234567",
        "diversion": "1",
        "created_ts": bot.time.time(),
    })

    async def cancel_user(user):
        raise asyncio.CancelledError

    monkeypatch.setattr(bot, "_pbx_resolve_user_display", cancel_user)
    with pytest.raises(asyncio.CancelledError):
        await bot._auto_close_by_event({
            "cmd": "event",
            "type": "ACCEPTED",
            "callid": "cancel-call",
            "phone": "+79161234567",
        })

    assert rec.get("_closing") is False
