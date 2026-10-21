"""
Простая авторизация: пользователи в JSON на persistent-томе /data,
пароли pbkdf2-хэш, сессии — подписанная HMAC-кука. Только stdlib.
"""
import os
import json
import time
import hmac
import base64
import hashlib
import secrets
import threading

COOKIE_NAME = "invix_session"
SESSION_TTL = 7 * 24 * 3600  # 7 дней
_LOCK = threading.Lock()


def _pick_data_dir():
    d = os.getenv("DATA_DIR", "/data")
    try:
        os.makedirs(d, exist_ok=True)
        t = os.path.join(d, ".wtest")
        with open(t, "w") as f:
            f.write("1")
        os.remove(t)
        return d
    except Exception:
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(d, exist_ok=True)
        return d


DATA_DIR = _pick_data_dir()
USERS_FILE = os.path.join(DATA_DIR, "users.json")
SECRET_FILE = os.path.join(DATA_DIR, "secret.key")


def _secret():
    try:
        with open(SECRET_FILE, "rb") as f:
            return f.read()
    except Exception:
        s = secrets.token_bytes(32)
        with open(SECRET_FILE, "wb") as f:
            f.write(s)
        return s


SECRET = _secret()


def _hash(pw: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200_000).hex()


def _load() -> dict:
    try:
        with open(USERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(users: dict):
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USERS_FILE)


RESET_MARKER = os.path.join(DATA_DIR, ".reset_token")


def init_admin():
    """Засев админа. Если задан ADMIN_RESET_TOKEN и он новый — СНОСИТ всех
    пользователей и пересоздаёт единственного админа из env (одноразово)."""
    with _LOCK:
        token = os.getenv("ADMIN_RESET_TOKEN", "").strip()
        applied = ""
        try:
            with open(RESET_MARKER, encoding="utf-8") as f:
                applied = f.read().strip()
        except Exception:
            pass
        force = bool(token) and token != applied

        users = _load()
        if users and not force:
            return

        admin = os.getenv("ADMIN_USER", "admin")
        pw = os.getenv("ADMIN_PASSWORD") or secrets.token_urlsafe(9)
        salt = secrets.token_bytes(16)
        users = {admin: {"salt": salt.hex(), "hash": _hash(pw, salt),
                         "role": "admin", "groups": []}}  # все старые снесены
        _save(users)
        if force:
            try:
                with open(RESET_MARKER, "w", encoding="utf-8") as f:
                    f.write(token)
            except Exception:
                pass
            print(f"[auth] СБРОС: все учётки удалены, новый админ '{admin}'", flush=True)
        else:
            print(f"[auth] Засеян администратор '{admin}', пароль: '{pw}'", flush=True)


def _user_public(username: str, u: dict) -> dict:
    return {"username": username, "role": u.get("role", "user"),
            "groups": u.get("groups", [])}


def verify(username: str, pw: str):
    u = _load().get(username)
    if not u:
        return None
    if hmac.compare_digest(_hash(pw, bytes.fromhex(u["salt"])), u["hash"]):
        return _user_public(username, u)
    return None


def create_user(username: str, pw: str, role: str = "user", groups=None):
    username = (username or "").strip()
    if not username or not pw:
        raise ValueError("Нужны логин и пароль")
    if role not in ("user", "admin"):
        role = "user"
    with _LOCK:
        users = _load()
        if username in users:
            raise ValueError("Пользователь уже существует")
        salt = secrets.token_bytes(16)
        users[username] = {"salt": salt.hex(), "hash": _hash(pw, salt),
                           "role": role, "groups": list(groups or [])}
        _save(users)


def set_groups(username: str, groups):
    with _LOCK:
        users = _load()
        if username not in users:
            raise ValueError("Нет такого пользователя")
        users[username]["groups"] = list(groups or [])
        _save(users)


def set_password(username: str, pw: str):
    if not pw:
        raise ValueError("Пустой пароль")
    with _LOCK:
        users = _load()
        if username not in users:
            raise ValueError("Нет такого пользователя")
        salt = secrets.token_bytes(16)
        users[username]["salt"] = salt.hex()
        users[username]["hash"] = _hash(pw, salt)
        _save(users)


def delete_user(username: str):
    with _LOCK:
        users = _load()
        u = users.get(username)
        if u and u.get("role") != "admin":
            del users[username]
            _save(users)
            return True
    return False


def list_users():
    return sorted(
        (_user_public(k, v) for k, v in _load().items()),
        key=lambda x: (x["role"] != "admin", x["username"]),
    )


# ---- сессии (подписанная кука) ----
def make_token(username: str) -> str:
    exp = int(time.time()) + SESSION_TTL
    msg = f"{username}|{exp}"
    sig = hmac.new(SECRET, msg.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{msg}|{sig}".encode()).decode()


def read_token(tok: str):
    try:
        raw = base64.urlsafe_b64decode(tok.encode()).decode()
        username, exp, sig = raw.split("|")
        good = hmac.new(SECRET, f"{username}|{exp}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig):
            return None
        if int(exp) < time.time():
            return None
        users = _load()
        if username not in users:
            return None
        return _user_public(username, users[username])
    except Exception:
        return None
