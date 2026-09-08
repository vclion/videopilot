# 用户注册登录、bcrypt 密码哈希与 JWT 认证。
from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
import uuid

import bcrypt
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "membership.db"
LOCK = threading.Lock()

JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me-please-32chars")
JWT_EXPIRES_MINUTES = int(os.getenv("JWT_EXPIRES_MINUTES", "10080"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "wcldsg666")

bearer_scheme = HTTPBearer(auto_error=False)


def _now() -> int:
    return int(time.time())


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def init_db() -> None:
    with LOCK:
        conn = _connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS users (\n"
                " id TEXT PRIMARY KEY,\n"
                " username TEXT UNIQUE,\n"
                " email TEXT UNIQUE NOT NULL,\n"
                " password_hash TEXT NOT NULL,\n"
                " role TEXT NOT NULL,\n"
                " created_at INTEGER NOT NULL\n"
                ")"
            )
            existing = conn.execute("SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO users (id, username, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        uuid.uuid4().hex,
                        ADMIN_USERNAME,
                        ADMIN_EMAIL,
                        hash_password(ADMIN_PASSWORD),
                        "admin",
                        _now(),
                    ),
                )
            conn.commit()
        finally:
            conn.close()


def create_access_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": _now() + JWT_EXPIRES_MINUTES * 60,
        "iat": _now(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录") from exc


def _user_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "role": row["role"],
        "created_at": row["created_at"],
    }


def get_user_by_id(user_id: str):
    with LOCK:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return _user_dict(row) if row else None
        finally:
            conn.close()


def get_user_by_email(email: str):
    with LOCK:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
            return _user_dict(row) if row else None
        finally:
            conn.close()


def get_user_by_username(username: str):
    with LOCK:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
            return _user_dict(row) if row else None
        finally:
            conn.close()


def get_user_by_account(account: str):
    account = account.strip()
    if "@" in account:
        return get_user_by_email(account)
    return get_user_by_username(account)


def register_user(email: str, password: str) -> dict[str, Any]:
    email = (email or "").strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ValueError("邮箱格式不正确")
    if len(password or "") < 8:
        raise ValueError("密码长度至少 8 位")

    with LOCK:
        conn = _connect()
        try:
            existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if existing:
                raise ValueError("该邮箱已被注册")
            username = email.split("@", 1)[0]
            username_row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            if username_row:
                username = f"{username}_{uuid.uuid4().hex[:6]}"
            user_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO users (id, username, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, username, email, hash_password(password), "user", _now()),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return _user_dict(row)
        finally:
            conn.close()


def authenticate(account: str, password: str) -> dict[str, Any]:
    account = (account or "").strip()
    if not account or not password:
        raise ValueError("请输入账号和密码")
    with LOCK:
        conn = _connect()
        try:
            if "@" in account:
                row = conn.execute("SELECT * FROM users WHERE email = ?", (account.lower(),)).fetchone()
            else:
                row = conn.execute("SELECT * FROM users WHERE username = ?", (account,)).fetchone()
            if not row or not verify_password(password, row["password_hash"]):
                raise ValueError("账号或密码错误")
            return _user_dict(row)
        finally:
            conn.close()


def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> dict[str, Any]:
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    payload = decode_access_token(credentials.credentials)
    user = get_user_by_id(payload.get("sub") or "")
    if not user:
        raise HTTPException(status_code=401, detail="账号不存在")
    return user


def get_optional_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> dict[str, Any] | None:
    if not credentials or not credentials.credentials:
        return None
    try:
        payload = decode_access_token(credentials.credentials)
        return get_user_by_id(payload.get("sub") or "")
    except HTTPException:
        return None
