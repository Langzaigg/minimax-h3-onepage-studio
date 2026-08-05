import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import ACCESS_DATA_FOLDER, ACCESS_SESSION_MAX_AGE_SECONDS, SERVICE_ADMIN_KEY


COOKIE_NAME = "minimax_h3_studio_access_session"
ANONYMOUS_KEY_ID = "anonymous"
ADMIN_KEY_ID = "admin"
KEYS_PATH = Path(ACCESS_DATA_FOLDER) / "api_keys.json"
USERS_PATH = Path(ACCESS_DATA_FOLDER) / "users.json"
STATS_PATH = Path(ACCESS_DATA_FOLDER) / "usage_stats.json"
_lock = threading.RLock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _read_json(path: Path, default: dict) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(value, ensure_ascii=False, indent=2)
    # Windows 杀软可能瞬时锁定新写入的 .tmp 文件，短退避重试避免偶发 WinError 5。
    for attempt in range(5):
        try:
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, path)
            return
        except OSError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def _key_store() -> dict:
    return _read_json(KEYS_PATH, {"version": 1, "keys": []})


def list_api_keys() -> list[dict[str, Any]]:
    with _lock:
        rows = _key_store().get("keys", [])
    return [public_key_record(row) for row in sorted(rows, key=lambda row: row["created_at"], reverse=True)]


def public_key_record(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["active"] = is_key_active(row)
    return result


def is_key_active(row: dict[str, Any]) -> bool:
    if row.get("revoked_at"):
        return False
    try:
        return datetime.fromisoformat(row["expires_at"]) > _now()
    except (KeyError, TypeError, ValueError):
        return False


def create_api_key(name: str, valid_for_hours: int) -> dict[str, Any]:
    created = _now()
    row = {
        "id": uuid.uuid4().hex,
        "name": name.strip(),
        "api_key": "dc_" + secrets.token_urlsafe(32),
        "created_at": _iso(created),
        "expires_at": _iso(created + timedelta(hours=valid_for_hours)),
        "last_used_at": None,
        "revoked_at": None,
    }
    with _lock:
        store = _key_store()
        store.setdefault("keys", []).append(row)
        _write_json(KEYS_PATH, store)
    return public_key_record(row)


def revoke_api_key(key_id: str) -> dict[str, Any]:
    with _lock:
        store = _key_store()
        for row in store.get("keys", []):
            if row.get("id") == key_id:
                if not row.get("revoked_at"):
                    row["revoked_at"] = _iso(_now())
                _write_json(KEYS_PATH, store)
                return public_key_record(row)
    raise FileNotFoundError(key_id)


def verify_api_key(secret: str, touch: bool = True) -> dict[str, Any] | None:
    if not secret:
        return None
    with _lock:
        store = _key_store()
        for row in store.get("keys", []):
            if hmac.compare_digest(str(row.get("api_key", "")), secret) and is_key_active(row):
                if touch:
                    now = _now()
                    try:
                        last_used = datetime.fromisoformat(row.get("last_used_at") or "")
                    except ValueError:
                        last_used = None
                    if last_used is None or now - last_used >= timedelta(minutes=1):
                        row["last_used_at"] = _iso(now)
                        _write_json(KEYS_PATH, store)
                return public_key_record(row)
    return None


def is_admin_secret(secret: str | None) -> bool:
    return hmac.compare_digest(secret or "", SERVICE_ADMIN_KEY)


def _users_store() -> dict:
    return _read_json(USERS_PATH, {"version": 1, "users": []})


def register_user(name: str) -> dict[str, Any]:
    """注册模式：用用户名注册/登录（无密码）。同名用户直接复用。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("用户名不能为空")
    if len(name) > 32:
        raise ValueError("用户名最长 32 个字符")
    if any(ord(ch) < 32 for ch in name):
        raise ValueError("用户名包含非法字符")
    with _lock:
        store = _users_store()
        users = store.setdefault("users", [])
        for row in users:
            if str(row.get("name", "")).casefold() == name.casefold():
                row["last_used_at"] = _iso(_now())
                _write_json(USERS_PATH, store)
                return dict(row)
        row = {
            "id": uuid.uuid4().hex,
            "name": name,
            "created_at": _iso(_now()),
            "last_used_at": _iso(_now()),
        }
        users.append(row)
        _write_json(USERS_PATH, store)
        return dict(row)


def find_user(user_id: str) -> dict[str, Any] | None:
    with _lock:
        for row in _users_store().get("users", []):
            if row.get("id") == user_id:
                return dict(row)
    return None


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def create_session(role: str, key_id: str | None = None, expires_at: datetime | None = None) -> str:
    maximum = _now() + timedelta(seconds=ACCESS_SESSION_MAX_AGE_SECONDS)
    expiry = min(expires_at or maximum, maximum)
    payload = _b64encode(json.dumps({"role": role, "key_id": key_id, "exp": int(expiry.timestamp())}, separators=(",", ":")).encode())
    signature = _b64encode(hmac.new(SERVICE_ADMIN_KEY.encode(), payload.encode(), hashlib.sha256).digest())
    return payload + "." + signature


def verify_session(token: str | None) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    payload, signature = token.split(".", 1)
    expected = _b64encode(hmac.new(SERVICE_ADMIN_KEY.encode(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        claims = json.loads(_b64decode(payload))
        if int(claims["exp"]) <= int(_now().timestamp()):
            return None
    except (ValueError, KeyError, json.JSONDecodeError):
        return None
    if claims.get("role") == "admin":
        return {"role": "admin", "key_id": ADMIN_KEY_ID, "name": "管理员"}
    if claims.get("role") == "user":
        user = find_user(str(claims.get("key_id") or ""))
        if user:
            return {"role": "user", "key_id": user["id"], "name": user["name"]}
    if claims.get("role") == "api_key":
        with _lock:
            for row in _key_store().get("keys", []):
                if row.get("id") == claims.get("key_id") and is_key_active(row):
                    return {"role": "api_key", "key_id": row["id"], "name": row["name"]}
    return None


def extract_header_secret(headers: Any) -> str:
    direct = headers.get("x-api-key", "").strip()
    if direct:
        return direct
    authorization = headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def identify_access(headers: Any, cookies: Any, touch: bool = False) -> dict[str, Any] | None:
    admin_key = headers.get("x-admin-key", "").strip()
    if is_admin_secret(admin_key):
        return {"role": "admin", "key_id": ADMIN_KEY_ID, "name": "管理员"}
    secret = extract_header_secret(headers)
    row = verify_api_key(secret, touch=touch)
    if row:
        return {"role": "api_key", "key_id": row["id"], "name": row["name"]}
    return verify_session(cookies.get(COOKIE_NAME))


def record_api_call(endpoint: str, key_id: str) -> None:
    day = datetime.now().astimezone().date().isoformat()
    with _lock:
        store = _read_json(STATS_PATH, {"version": 1, "days": {}})
        endpoint_counts = store.setdefault("days", {}).setdefault(day, {}).setdefault(endpoint, {})
        endpoint_counts[key_id] = int(endpoint_counts.get(key_id, 0)) + 1
        _write_json(STATS_PATH, store)


def usage_stats(start_date: date, end_date: date, key_id: str | None = None) -> dict[str, Any]:
    with _lock:
        stats = _read_json(STATS_PATH, {"version": 1, "days": {}})
        key_rows = _key_store().get("keys", [])
    labels = {ANONYMOUS_KEY_ID: "未认证", ADMIN_KEY_ID: "管理员"}
    labels.update({row["id"]: row["name"] for row in key_rows})
    with _lock:
        for row in _users_store().get("users", []):
            labels.setdefault(row["id"], row["name"])
    totals: dict[tuple[str, str, str], int] = {}
    weeks: set[str] = set()
    endpoints: set[str] = set()
    for day_text, endpoint_map in stats.get("days", {}).items():
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            continue
        if day < start_date or day > end_date:
            continue
        iso = day.isocalendar()
        week = f"{iso.year}-W{iso.week:02d}"
        weeks.add(week)
        for endpoint, key_counts in endpoint_map.items():
            endpoints.add(endpoint)
            for current_key_id, count in key_counts.items():
                if key_id and current_key_id != key_id:
                    continue
                key = (week, endpoint, current_key_id)
                totals[key] = totals.get(key, 0) + int(count)
    rows = [
        {"week": week, "endpoint": endpoint, "key_id": current_key_id, "key_name": labels.get(current_key_id, "已删除 Key"), "count": count}
        for (week, endpoint, current_key_id), count in sorted(totals.items())
    ]
    return {
        "start_date": start_date,
        "end_date": end_date,
        "weeks": sorted(weeks),
        "endpoints": sorted(endpoints),
        "keys": [{"id": item_id, "name": name} for item_id, name in labels.items()],
        "rows": rows,
    }
