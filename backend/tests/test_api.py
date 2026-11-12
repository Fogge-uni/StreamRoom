"""Проверки аккаунтов, единственного канала и ответа сервера Owncast."""

import pytest
from fastapi.testclient import TestClient

from backend.app import app, database_connection


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        with database_connection() as connection:
            connection.execute("TRUNCATE TABLE sessions, users RESTART IDENTITY CASCADE")
            connection.execute(
                """
                INSERT INTO channel_settings(id, title, description, owner_id)
                VALUES (1, 'StreamRoom — прямой эфир', 'Тестовое описание', NULL)
                ON CONFLICT (id) DO UPDATE
                SET title = EXCLUDED.title,
                    description = EXCLUDED.description,
                    owner_id = NULL
                """
            )
        yield test_client


def register_user(client: TestClient, username: str = "student1") -> dict:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "test-password-123"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_health_endpoint(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_register_and_login(client: TestClient):
    registered = register_user(client)
    assert registered["username"] == "student1"
    assert registered["access_token"]
    assert registered["is_channel_owner"] is True

    response = client.post(
        "/api/auth/login",
        json={"username": "student1", "password": "test-password-123"},
    )
    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"
    assert response.json()["is_channel_owner"] is True


def test_duplicate_username_is_rejected(client: TestClient):
    register_user(client)
    response = client.post(
        "/api/auth/register",
        json={"username": "student1", "password": "another-password"},
    )
    assert response.status_code == 409


def test_only_one_channel_owner_is_assigned(client: TestClient):
    first = register_user(client, "channel_owner")
    second = register_user(client, "viewer_two")

    assert first["is_channel_owner"] is True
    assert second["is_channel_owner"] is False


def test_channel_status_is_read_from_owncast(client: TestClient, monkeypatch):
    from backend import app as backend_module

    monkeypatch.setattr(
        backend_module,
        "read_owncast_status",
        lambda: {
            "available": True,
            "online": True,
            "viewer_count": 4,
            "live_title": "Тестовый эфир",
            "last_connect_time": "2026-11-06T10:00:00Z",
        },
    )

    response = client.get("/api/channel")
    assert response.status_code == 200
    data = response.json()
    assert data["state"] == "live"
    assert data["online"] is True
    assert data["viewer_count"] == 4
    assert data["live_title"] == "Тестовый эфир"


def test_channel_is_offline_when_owncast_has_no_active_stream(client: TestClient, monkeypatch):
    from backend import app as backend_module

    monkeypatch.setattr(
        backend_module,
        "read_owncast_status",
        lambda: {
            "available": True,
            "online": False,
            "viewer_count": 0,
            "live_title": "",
            "last_connect_time": None,
        },
    )

    response = client.get("/api/channel")
    assert response.status_code == 200
    assert response.json()["state"] == "offline"


def test_channel_shows_unavailable_when_owncast_cannot_be_reached(client: TestClient, monkeypatch):
    from backend import app as backend_module

    monkeypatch.setattr(
        backend_module,
        "read_owncast_status",
        lambda: {
            "available": False,
            "online": False,
            "viewer_count": 0,
            "live_title": "",
            "last_connect_time": None,
        },
    )

    response = client.get("/api/channel")
    assert response.status_code == 200
    assert response.json()["state"] == "unavailable"


def test_channel_owner_can_update_channel(client: TestClient):
    account = register_user(client)
    headers = {"Authorization": f"Bearer {account['access_token']}"}

    response = client.put(
        "/api/channel",
        headers=headers,
        json={"title": "Мой канал", "description": "Описание канала"},
    )
    assert response.status_code == 200

    channel = client.get("/api/channel").json()
    assert channel["title"] == "Мой канал"
    assert channel["description"] == "Описание канала"
    assert channel["owner"] == "student1"


def test_regular_user_cannot_update_channel(client: TestClient):
    register_user(client, "channel_owner")
    viewer = register_user(client, "viewer_two")
    headers = {"Authorization": f"Bearer {viewer['access_token']}"}

    response = client.put(
        "/api/channel",
        headers=headers,
        json={"title": "Попытка изменить канал", "description": ""},
    )
    assert response.status_code == 403


def test_channel_update_requires_login(client: TestClient):
    response = client.put(
        "/api/channel",
        json={"title": "Без входа", "description": ""},
    )
    assert response.status_code == 401


def test_logout_invalidates_token(client: TestClient):
    account = register_user(client)
    headers = {"Authorization": f"Bearer {account['access_token']}"}

    assert client.get("/api/auth/me", headers=headers).status_code == 200
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_invalid_username_and_short_password_are_rejected(client: TestClient):
    response = client.post(
        "/api/auth/register",
        json={"username": "bad name", "password": "short"},
    )
    assert response.status_code == 422


def test_multiple_stream_catalog_endpoints_are_not_part_of_api(client: TestClient):
    schema = client.get("/api/openapi.json").json()
    assert "/api/streams" not in schema["paths"]
    assert "/api/channel" in schema["paths"]


def test_chat_is_publicly_readable_but_requires_login_to_send(client: TestClient):
    response = client.get("/api/chat/messages")
    assert response.status_code == 200
    assert response.json()["messages"] == []

    denied = client.post("/api/chat/messages", json={"message": "Привет"})
    assert denied.status_code == 401


def test_chat_message_uses_authenticated_username(client: TestClient):
    account = register_user(client, "chat_student")
    headers = {"Authorization": f"Bearer {account['access_token']}"}

    response = client.post(
        "/api/chat/messages",
        headers=headers,
        json={"message": "Всем привет!", "username": "spoofed_name"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["username"] == "chat_student"
    assert response.json()["message"] == "Всем привет!"

    public_messages = client.get("/api/chat/messages").json()["messages"]
    assert len(public_messages) == 1
    assert public_messages[0]["username"] == "chat_student"


def test_chat_rejects_blank_and_overlong_messages(client: TestClient):
    account = register_user(client)
    headers = {"Authorization": f"Bearer {account['access_token']}"}

    blank = client.post("/api/chat/messages", headers=headers, json={"message": "   "})
    long_message = client.post("/api/chat/messages", headers=headers, json={"message": "x" * 501})
    assert blank.status_code == 422
    assert long_message.status_code == 422


def test_chat_rate_limit(client: TestClient):
    account = register_user(client)
    headers = {"Authorization": f"Bearer {account['access_token']}"}
    first = client.post("/api/chat/messages", headers=headers, json={"message": "Первое"})
    second = client.post("/api/chat/messages", headers=headers, json={"message": "Второе"})
    assert first.status_code == 201
    assert second.status_code == 429


def test_only_owner_can_delete_chat_messages(client: TestClient):
    owner = register_user(client, "channel_owner")
    viewer = register_user(client, "viewer_two")
    owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
    viewer_headers = {"Authorization": f"Bearer {viewer['access_token']}"}

    sent = client.post("/api/chat/messages", headers=viewer_headers, json={"message": "Удалите меня"})
    message_id = sent.json()["id"]
    denied = client.delete(f"/api/chat/messages/{message_id}", headers=viewer_headers)
    allowed = client.delete(f"/api/chat/messages/{message_id}", headers=owner_headers)

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert client.get("/api/chat/messages").json()["messages"] == []


def test_owner_can_transfer_ownership_and_roles_update(client: TestClient):
    owner = register_user(client, "first_owner")
    viewer = register_user(client, "new_owner")
    owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
    viewer_headers = {"Authorization": f"Bearer {viewer['access_token']}"}

    response = client.post(
        "/api/channel/transfer-ownership",
        headers=owner_headers,
        json={"username": "new_owner"},
    )
    assert response.status_code == 200
    assert client.get("/api/auth/me", headers=owner_headers).json()["is_channel_owner"] is False
    assert client.get("/api/auth/me", headers=viewer_headers).json()["is_channel_owner"] is True
    assert client.get("/api/channel").json()["owner"] == "new_owner"

    old_owner_update = client.put(
        "/api/channel",
        headers=owner_headers,
        json={"title": "Не должно сработать", "description": ""},
    )
    assert old_owner_update.status_code == 403


def test_owner_cannot_transfer_ownership_to_self_or_unknown_user(client: TestClient):
    owner = register_user(client, "first_owner")
    headers = {"Authorization": f"Bearer {owner['access_token']}"}

    self_transfer = client.post(
        "/api/channel/transfer-ownership", headers=headers, json={"username": "first_owner"}
    )
    unknown_transfer = client.post(
        "/api/channel/transfer-ownership", headers=headers, json={"username": "missing_user"}
    )
    assert self_transfer.status_code == 400
    assert unknown_transfer.status_code == 404
