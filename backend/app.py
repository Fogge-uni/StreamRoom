import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from urllib.error import URLError
from urllib.request import Request, urlopen

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, status
from psycopg import errors
from psycopg.rows import dict_row
from pydantic import BaseModel, Field, field_validator

PASSWORD_ITERATIONS = 260_000
SESSION_DAYS = 7
OWNCAST_PUBLIC_URL = os.getenv("OWNCAST_PUBLIC_URL", "http://localhost:8081").rstrip("/")
OWNCAST_INTERNAL_URL = os.getenv("OWNCAST_INTERNAL_URL", "http://owncast:8080").rstrip("/")
DEFAULT_CHANNEL_TITLE = os.getenv("CHANNEL_TITLE", "StreamRoom — прямой эфир")
DEFAULT_CHANNEL_DESCRIPTION = os.getenv(
    "CHANNEL_DESCRIPTION",
    "Один общий канал для прямых трансляций. Зрители могут смотреть эфир одновременно.",
)


def read_database_password() -> str:
    """Читает пароль БД из Docker Secret или переменной окружения."""
    password_file = os.getenv("DATABASE_PASSWORD_FILE")
    if password_file:
        return Path(password_file).read_text(encoding="utf-8").strip()
    return os.getenv("DATABASE_PASSWORD", "")


def database_connection() -> psycopg.Connection:
    """Создаёт соединение с PostgreSQL."""
    password = read_database_password()
    if not password:
        raise RuntimeError("Не задан пароль базы данных (DATABASE_PASSWORD или DATABASE_PASSWORD_FILE)")

    return psycopg.connect(
        host=os.getenv("DATABASE_HOST", "database"),
        port=int(os.getenv("DATABASE_PORT", "5432")),
        dbname=os.getenv("DATABASE_NAME", "streamroom"),
        user=os.getenv("DATABASE_USER", "streamroom"),
        password=password,
        connect_timeout=5,
        row_factory=dict_row,
    )


SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS users (
        id BIGSERIAL PRIMARY KEY,
        username VARCHAR(32) NOT NULL UNIQUE,
        password_hash VARCHAR(256) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS sessions (
        token_hash CHAR(64) PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS channel_settings (
        id SMALLINT PRIMARY KEY CHECK (id = 1),
        title VARCHAR(100) NOT NULL,
        description VARCHAR(500) NOT NULL DEFAULT '',
        owner_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id)",
    """CREATE TABLE IF NOT EXISTS chat_messages (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        username VARCHAR(32) NOT NULL,
        message VARCHAR(500) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        deleted_at TIMESTAMPTZ
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chat_messages_created_at ON chat_messages(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_chat_messages_user_created_at ON chat_messages(user_id, created_at DESC)",
)


def initialize_database() -> None:
    """Создаёт таблицы и единственную запись канала; повторный запуск безопасен."""
    attempts = 30
    for attempt in range(1, attempts + 1):
        try:
            with database_connection() as connection:
                for statement in SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO channel_settings(id, title, description)
                    VALUES (1, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (DEFAULT_CHANNEL_TITLE, DEFAULT_CHANNEL_DESCRIPTION),
                )
            print("База данных StreamRoom готова.", flush=True)
            return
        except psycopg.OperationalError:
            if attempt == attempts:
                raise
            print(f"PostgreSQL пока недоступен. Повтор подключения: {attempt}/{attempts}", flush=True)
            time.sleep(2)


def make_password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return f"{salt.hex()}${digest.hex()}"


def check_password(password: str, saved_hash: str) -> bool:
    try:
        salt_hex, expected_digest = saved_hash.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        actual_digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
        ).hex()
        return hmac.compare_digest(actual_digest, expected_digest)
    except (ValueError, TypeError):
        return False


def hash_token(token: str) -> str:
    """В базе хранится хеш токена, а не сам токен авторизации."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with database_connection() as connection:
        connection.execute(
            "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
            (hash_token(token), user_id, datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)),
        )
    return token


def extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Сначала войдите в аккаунт")

    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Некорректный токен авторизации")
    return token.strip()


def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    token = extract_bearer_token(authorization)
    with database_connection() as connection:
        user = connection.execute(
            """
            SELECT users.id, users.username,
                   COALESCE(channel_settings.owner_id = users.id, FALSE) AS is_channel_owner
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            LEFT JOIN channel_settings ON channel_settings.id = 1
            WHERE sessions.token_hash = %s AND sessions.expires_at > CURRENT_TIMESTAMP
            """,
            (hash_token(token),),
        ).fetchone()

    if user is None:
        raise HTTPException(status_code=401, detail="Сеанс истёк или не найден. Войдите снова.")
    return dict(user)


class AuthInput(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_]+$")
    password: str = Field(min_length=8, max_length=128)


class ChatMessageInput(BaseModel):
    message: str = Field(min_length=1, max_length=500)

    @field_validator("message")
    @classmethod
    def clean_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Сообщение не должно быть пустым")
        return value


class OwnershipTransferInput(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_]+$")


class ChannelUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Название канала не должно быть пустым")
        return value

    @field_validator("description")
    @classmethod
    def clean_description(cls, value: str) -> str:
        return value.strip()


def read_owncast_status() -> dict:
    """Получает текущий статус эфира из HTTP API Owncast.

    /api/status — внутренний endpoint Owncast. Версия образа закреплена в
    Docker-конфигурации, чтобы случайное обновление не изменило формат ответа.
    """
    request = Request(
        f"{OWNCAST_INTERNAL_URL}/api/status",
        headers={"Accept": "application/json", "User-Agent": "StreamRoom/1.0"},
    )
    try:
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))

        if not isinstance(payload, dict) or not isinstance(payload.get("online"), bool):
            raise ValueError("Owncast вернул неожиданный формат статуса")

        viewer_count = payload.get("viewerCount", 0)
        try:
            viewer_count = max(0, int(viewer_count))
        except (TypeError, ValueError):
            viewer_count = 0

        return {
            "available": True,
            "online": payload["online"],
            "viewer_count": viewer_count,
            "live_title": str(payload.get("streamTitle") or "").strip(),
            "last_connect_time": payload.get("lastConnectTime"),
        }
    except (OSError, URLError, TimeoutError, UnicodeError, ValueError):
        return {
            "available": False,
            "online": False,
            "viewer_count": 0,
            "live_title": "",
            "last_connect_time": None,
        }


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(
    title="StreamRoom API",
    description="Аккаунты и настройки одного канала. Видеопоток обслуживает Owncast.",
    version="1.3.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)


@app.get("/api/health", tags=["Служебные"])
def health() -> dict:
    with database_connection() as connection:
        connection.execute("SELECT 1")
    return {"status": "ok", "database": "connected"}


@app.get("/api/channel", tags=["Канал"])
def get_channel() -> dict:
    """Возвращает настройки единственного канала и реальный статус Owncast."""
    with database_connection() as connection:
        channel = connection.execute(
            """
            SELECT channel_settings.title, channel_settings.description,
                   users.username AS owner
            FROM channel_settings
            LEFT JOIN users ON users.id = channel_settings.owner_id
            WHERE channel_settings.id = 1
            """
        ).fetchone()

    if channel is None:
        raise HTTPException(status_code=503, detail="Настройки канала ещё не созданы")

    owncast = read_owncast_status()
    if not owncast["available"]:
        state = "unavailable"
    elif owncast["online"]:
        state = "live"
    else:
        state = "offline"

    return {
        "title": channel["title"],
        "description": channel["description"],
        "owner": channel["owner"] or "Пока не назначен",
        "watch_url": OWNCAST_PUBLIC_URL,
        "state": state,
        "online": owncast["online"],
        "server_available": owncast["available"],
        "viewer_count": owncast["viewer_count"],
        "live_title": owncast["live_title"],
        "last_connect_time": owncast["last_connect_time"],
    }


@app.put("/api/channel", tags=["Канал"])
def update_channel(
    data: ChannelUpdate,
    user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Изменять описание общего канала может только его владелец."""
    if not user["is_channel_owner"]:
        raise HTTPException(status_code=403, detail="Изменять настройки канала может только его владелец")

    with database_connection() as connection:
        connection.execute(
            """
            UPDATE channel_settings
            SET title = %s, description = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = 1 AND owner_id = %s
            """,
            (data.title, data.description, user["id"]),
        )
    return {"message": "Настройки канала сохранены"}


@app.post("/api/auth/register", status_code=status.HTTP_201_CREATED, tags=["Аккаунт"])
def register(data: AuthInput) -> dict:
    try:
        with database_connection() as connection:
            user = connection.execute(
                """
                INSERT INTO users(username, password_hash)
                VALUES (%s, %s)
                RETURNING id, username
                """,
                (data.username, make_password_hash(data.password)),
            ).fetchone()
            owner_claim = connection.execute(
                """
                UPDATE channel_settings
                SET owner_id = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = 1 AND owner_id IS NULL
                RETURNING owner_id
                """,
                (user["id"],),
            ).fetchone()
    except errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="Это имя пользователя уже занято")

    token = create_session(user["id"])
    is_owner = owner_claim is not None
    return {
        "access_token": token,
        "token_type": "bearer",
        "id": user["id"],
        "username": user["username"],
        "is_channel_owner": is_owner,
        "message": "Аккаунт владельца канала создан" if is_owner else "Регистрация выполнена",
    }


@app.post("/api/auth/login", tags=["Аккаунт"])
def login(data: AuthInput) -> dict:
    with database_connection() as connection:
        user = connection.execute(
            """
            SELECT users.id, users.username, users.password_hash,
                   COALESCE(channel_settings.owner_id = users.id, FALSE) AS is_channel_owner
            FROM users
            LEFT JOIN channel_settings ON channel_settings.id = 1
            WHERE users.username = %s
            """,
            (data.username,),
        ).fetchone()

    if user is None or not check_password(data.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Неверное имя пользователя или пароль")

    token = create_session(user["id"])
    return {
        "access_token": token,
        "token_type": "bearer",
        "id": user["id"],
        "username": user["username"],
        "is_channel_owner": user["is_channel_owner"],
        "message": "Вход выполнен",
    }


@app.get("/api/auth/me", tags=["Аккаунт"])
def me(user: Annotated[dict, Depends(get_current_user)]) -> dict:
    return user


@app.post("/api/auth/logout", tags=["Аккаунт"])
def logout(
    authorization: Annotated[str | None, Header()] = None,
    _: dict = Depends(get_current_user),
) -> dict:
    token = extract_bearer_token(authorization)
    with database_connection() as connection:
        connection.execute("DELETE FROM sessions WHERE token_hash = %s", (hash_token(token),))
    return {"message": "Сеанс завершён"}


@app.post("/api/channel/transfer-ownership", tags=["Канал"])
def transfer_channel_ownership(
    data: OwnershipTransferInput,
    user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Передаёт права владельца существующему пользователю."""
    with database_connection() as connection:
        channel = connection.execute(
            "SELECT owner_id FROM channel_settings WHERE id = 1 FOR UPDATE"
        ).fetchone()
        if channel is None or channel["owner_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="Передать права может только текущий владелец")

        target = connection.execute(
            "SELECT id, username FROM users WHERE username = %s",
            (data.username,),
        ).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="Пользователь с таким именем не найден")
        if target["id"] == user["id"]:
            raise HTTPException(status_code=400, detail="Нельзя передать права самому себе")

        connection.execute(
            "UPDATE channel_settings SET owner_id = %s, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
            (target["id"],),
        )
    return {"message": f"Права владельца переданы пользователю {target['username']}"}


@app.get("/api/chat/messages", tags=["Чат"])
def get_chat_messages() -> dict:
    """Публично возвращает последние 100 сообщений чата."""
    with database_connection() as connection:
        messages = connection.execute(
            """SELECT id, username, message, created_at
               FROM chat_messages
               WHERE deleted_at IS NULL
               ORDER BY id DESC LIMIT 100"""
        ).fetchall()
    messages.reverse()
    return {"messages": [dict(message) for message in messages]}


@app.post("/api/chat/messages", status_code=status.HTTP_201_CREATED, tags=["Чат"])
def send_chat_message(
    data: ChatMessageInput,
    user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Добавляет сообщение от имени пользователя из действующего токена."""
    with database_connection() as connection:
        # Блокировка строки пользователя не позволяет двум параллельным запросам
        # обойти ограничение частоты отправки.
        connection.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user["id"],))
        recent = connection.execute(
            """SELECT id FROM chat_messages
               WHERE user_id = %s AND created_at > CURRENT_TIMESTAMP - INTERVAL '2 seconds'
               ORDER BY created_at DESC LIMIT 1""",
            (user["id"],),
        ).fetchone()
        if recent is not None:
            raise HTTPException(status_code=429, detail="Отправлять сообщения можно не чаще одного раза в 2 секунды")

        message = connection.execute(
            """INSERT INTO chat_messages(user_id, username, message)
               VALUES (%s, %s, %s)
               RETURNING id, username, message, created_at""",
            (user["id"], user["username"], data.message),
        ).fetchone()
    return dict(message)


@app.delete("/api/chat/messages/{message_id}", tags=["Чат"])
def delete_chat_message(
    message_id: int,
    user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Удаляет сообщение из общего чата; доступно только владельцу канала."""
    if not user["is_channel_owner"]:
        raise HTTPException(status_code=403, detail="Удалять сообщения может только владелец канала")
    with database_connection() as connection:
        deleted = connection.execute(
            """UPDATE chat_messages SET deleted_at = CURRENT_TIMESTAMP
               WHERE id = %s AND deleted_at IS NULL RETURNING id""",
            (message_id,),
        ).fetchone()
    if deleted is None:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")
    return {"message": "Сообщение удалено"}
