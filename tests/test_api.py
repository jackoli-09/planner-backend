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
