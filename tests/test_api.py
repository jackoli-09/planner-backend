"""
API-тесты бэкенда. Требуют PostgreSQL: TEST_DATABASE_URL (по умолчанию локальный).
Запуск: pytest -q
"""
import hashlib
import hmac
import json
import os
import time
import uuid
from urllib.parse import urlencode

import pytest

BOT_TOKEN = "123456:TEST_TOKEN_FOR_TESTS_ONLY"
os.environ["BOT_TOKEN"] = BOT_TOKEN
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres@127.0.0.1:5433/planner"
)
os.environ["APP_ENV"] = "test"
os.environ["ALLOW_INSECURE_DEMO"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402


def make_init_data(user_id: int, bot_token: str = BOT_TOKEN, auth_date: int | None = None) -> str:
    """Подписывает initData так же, как это делает Telegram."""
    values = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAH-test",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={values[k]}" for k in sorted(values))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


@pytest.fixture(scope="module")
def client():
    async def no_menu():
        return False
    main.configure_telegram_menu_button = no_menu
    with TestClient(main.app) as c:
        yield c


def tg(user_id: int) -> dict:
    return {"X-Telegram-Init-Data": make_init_data(user_id)}


def new_user() -> int:
    return int(uuid.uuid4().int % 10**9) + 10**9


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["database"] == "ok"


def test_requires_auth(client):
    assert client.get("/api/bootstrap").status_code == 401


def test_rejects_forged_init_data(client):
    forged = make_init_data(new_user(), bot_token="999:WRONG")
    r = client.get("/api/bootstrap", headers={"X-Telegram-Init-Data": forged})
    assert r.status_code == 401


def test_rejects_expired_init_data(client):
    old = make_init_data(new_user(), auth_date=int(time.time()) - 3 * 86400)
    r = client.get("/api/bootstrap", headers={"X-Telegram-Init-Data": old})
    assert r.status_code == 401


def test_demo_header_ignored_in_production_mode(client):
    r = client.get("/api/bootstrap", headers={"X-User-Id": "42"})
    assert r.status_code == 401


def test_session_token_roundtrip(client):
    uid = new_user()
    r = client.post("/api/auth/session", headers=tg(uid))
    assert r.status_code == 200
    token = r.json()["token"]
    r = client.get("/api/tasks", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_tampered_session_token_rejected(client):
    uid = new_user()
    token = client.post("/api/auth/session", headers=tg(uid)).json()["token"]
    parts = token.split(":")
    parts[1] = str(uid + 1)  # пытаемся выдать себя за другого
    r = client.get("/api/tasks", headers={"Authorization": "Bearer " + ":".join(parts)})
    assert r.status_code == 401


def task(task_id: str, text: str = "Тест", done: bool = False) -> dict:
    return {"id": task_id, "text": text, "prio": "m", "done": done, "dl": "2026-09-25"}


def test_task_upsert_is_idempotent(client):
    uid = new_user()
    tid = "t-" + uuid.uuid4().hex
    for _ in range(3):  # повтор после сетевого таймаута
        assert client.post("/api/tasks", json=task(tid), headers=tg(uid)).status_code == 200
    tasks = client.get("/api/tasks", headers=tg(uid)).json()
    assert [t["id"] for t in tasks] == [tid]
    client.post("/api/tasks", json=task(tid, text="Обновлено", done=True), headers=tg(uid))
    tasks = client.get("/api/tasks", headers=tg(uid)).json()
    assert tasks[0]["text"] == "Обновлено" and tasks[0]["done"] is True


def test_users_are_isolated(client):
    owner, stranger = new_user(), new_user()
    tid = "t-" + uuid.uuid4().hex
    client.post("/api/tasks", json=task(tid), headers=tg(owner))
    assert client.get("/api/tasks", headers=tg(stranger)).json() == []
    # чужое удаление не должно задевать данные владельца
    client.delete(f"/api/tasks/{tid}", headers=tg(stranger))
    assert len(client.get("/api/tasks", headers=tg(owner)).json()) == 1


def test_bootstrap_and_export_contain_user_data(client):
    uid = new_user()
    tid = "t-" + uuid.uuid4().hex
    client.post("/api/tasks", json=task(tid), headers=tg(uid))
    boot = client.get("/api/bootstrap", headers=tg(uid))
    assert boot.status_code == 200
    export = client.get("/api/export", headers=tg(uid)).json()
    assert export["user_id"] == uid
    assert any(t["id"] == tid for t in export["tasks"])


def test_delete_account_requires_confirmation(client):
    uid = new_user()
    assert client.delete("/api/account", headers=tg(uid)).status_code == 400


def test_delete_account_removes_everything(client):
    uid, other = new_user(), new_user()
    client.post("/api/tasks", json=task("t-" + uuid.uuid4().hex), headers=tg(uid))
    client.post("/api/tasks", json=task("t-" + uuid.uuid4().hex), headers=tg(other))
    r = client.delete("/api/account", headers=tg(uid) | {"X-Confirm-Delete": "DELETE"})
    assert r.status_code == 200
    assert r.json()["rows"]["tasks"] == 1
    assert client.get("/api/tasks", headers=tg(uid)).json() == []
    assert len(client.get("/api/tasks", headers=tg(other)).json()) == 1


# ── Уведомления ─────────────────────────────────────────────────
from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402


def test_scheduled_due_window():
    msk = ZoneInfo("Europe/Moscow")
    now = datetime(2026, 9, 25, 9, 7, tzinfo=msk)
    assert main.scheduled_due("09:00", now, 10)
    assert not main.scheduled_due("09:00", now, 5)
    assert not main.scheduled_due("09:10", now, 30)  # ещё не наступило
    assert not main.scheduled_due("bad", now, 30)


def test_cron_requires_secret(client):
    assert client.post("/api/cron/notifications").status_code == 401
    assert client.post("/api/cron/notifications", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_cron_sends_once_per_day(client, monkeypatch):
    uid = new_user()
    client.get("/api/tasks", headers=tg(uid))  # создаёт user_settings
    client.post("/api/tasks", json=task("t-" + uuid.uuid4().hex), headers=tg(uid))
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    past = now.replace(minute=(now.minute // 5) * 5, second=0).strftime("%H:%M")
    r = client.post("/api/settings/notifications", headers=tg(uid), json={
        "notif_morning_on": False, "notif_workout_on": False, "notif_evening_on": False,
        "notif_weekly_on": False, "notif_tasks_on": True, "notif_tasks": past,
        "timezone": "Europe/Moscow",
    })
    assert r.status_code == 200, r.text
    sent = []

    async def fake_send(user_id, text):
        sent.append(user_id)
    monkeypatch.setattr(main, "send_telegram", fake_send)
    monkeypatch.setattr(main, "CRON_SECRET", "s3cret")
    for _ in range(2):  # повторный запуск cron не должен дублировать сообщение
        r = client.post("/api/cron/notifications", headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 200
    assert sent.count(uid) == 1


# ── Продуктовые метрики и отзывы ────────────────────────────────
def test_activity_day_recorded_and_deleted(client):
    import asyncio
    uid = new_user()
    client.get("/api/bootstrap", headers=tg(uid))

    async def count():
        async with main.pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM user_activity_days WHERE user_id=$1", uid)
    assert client.portal.call(count) == 1
    client.delete("/api/account", headers=tg(uid) | {"X-Confirm-Delete": "DELETE"})
    assert client.portal.call(count) == 0


def test_bootstrap_owner_flag(client, monkeypatch):
    owner, other = new_user(), new_user()
    monkeypatch.setattr(main, "OWNER_USER_ID", owner)
    assert client.get("/api/bootstrap", headers=tg(owner)).json()["is_owner"] is True
    assert client.get("/api/bootstrap", headers=tg(other)).json()["is_owner"] is False


def test_stats_only_for_owner(client, monkeypatch):
    owner, other = new_user(), new_user()
    monkeypatch.setattr(main, "OWNER_USER_ID", owner)
    assert client.get("/api/admin/stats", headers=tg(other)).status_code == 403
    r = client.get("/api/admin/stats", headers=tg(owner))
    assert r.status_code == 200
    st = r.json()
    assert st["users_total"] >= 1 and "retention_d7" in st
    assert "Удержание" in main.format_stats(st)


def test_feedback_forwarded_to_owner(client, monkeypatch):
    owner, uid = new_user(), new_user()
    monkeypatch.setattr(main, "OWNER_USER_ID", owner)
    sent = []

    async def fake_send(user_id, text):
        sent.append((user_id, text))
    monkeypatch.setattr(main, "send_telegram", fake_send)
    r = client.post("/api/feedback", json={"text": "Добавьте таймер <отдыха>"}, headers=tg(uid))
    assert r.status_code == 200
    assert sent and sent[0][0] == owner and "&lt;отдыха&gt;" in sent[0][1]
    assert client.post("/api/feedback", json={"text": ""}, headers=tg(uid)).status_code == 422


def test_retention_counts_returning_user(client):
    from datetime import timedelta
    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    uid = new_user()

    async def seed_and_stats():
        async with main.pool.acquire() as conn:
            for offset in (40, 32):  # пришёл 40 дней назад, вернулся на 8-й день
                await conn.execute("INSERT INTO user_activity_days VALUES ($1,$2) ON CONFLICT DO NOTHING",
                                   uid, today - timedelta(days=offset))
        return await main.product_stats()
    st = client.portal.call(seed_and_stats)
    assert st["retention_d7"]["eligible"] >= 1 and st["retention_d7"]["returned"] >= 1


def test_workout_reminder_uses_user_program():
    import json as _json
    program = [{"weekday": d, "rest": d != 1, "group": "Грудь + Трицепс" if d == 1 else None} for d in range(7)]
    raw = _json.dumps(program, ensure_ascii=False)
    monday = main.workout_reminder_text(raw, 0)  # JS weekday 1
    assert "Грудь + Трицепс" in monday and "понедельник" in monday
    assert main.workout_reminder_text(raw, 1) is None  # вторник — отдых
    assert "вторник" in main.workout_reminder_text(None, 1)  # без программы — общее напоминание


def test_old_v1_session_rejected(client):
    import base64 as _b64, hashlib as _h, hmac as _hm, time as _t
    uid = new_user()
    payload = f"v1:{uid}:{int(_t.time()) + 3600}"
    sig = _b64.urlsafe_b64encode(_hm.new(BOT_TOKEN.encode(), payload.encode(), _h.sha256).digest()).decode().rstrip("=")
    r = client.get("/api/tasks", headers={"Authorization": f"Bearer {payload}:{sig}"})
    assert r.status_code == 401
