"""
Простая авторизация: пользователи и сессии — в SQLite на persistent-томе
/data (app.db, см. db.py, шаг 3.5 плана доработки), пароли pbkdf2-хэш,
сессии хранятся на сервере. Только stdlib + db.py.
"""
import os
import re
import json
import time
import hmac
import base64
import hashlib
import sqlite3
import secrets
import logging
import threading

import db

# Настраивается здесь, а не в app.py: этот модуль импортируется первым (прямо
# или через currency.py) почти во всех точках входа, включая тесты — поэтому
# формат логов задаётся до первого print->logger вызова, даже при запуске не
# через app.py/uvicorn (см. tests/run_fake.py, легаси-скрипты).
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

COOKIE_NAME = "invix_session"
SESSION_TTL = 7 * 24 * 3600  # 7 дней
_LOCK = threading.Lock()


def _check_writable(d: str):
    os.makedirs(d, exist_ok=True)
    t = os.path.join(d, f".wtest_{os.getpid()}_{secrets.token_hex(4)}")
    with open(t, "w") as f:
        f.write("1")
    os.remove(t)


def _pick_data_dir():
    explicit = os.getenv("DATA_DIR")
    if explicit:
        # явно заданный том обязан работать — иначе данные уйдут «в никуда»
        try:
            _check_writable(explicit)
        except Exception as e:
            raise RuntimeError(f"DATA_DIR={explicit} недоступна на запись: {e}") from e
        return explicit
    try:
        _check_writable("/data")
        return "/data"
    except Exception as e:
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        _check_writable(d)
        logger.warning("/data недоступна (%s). Данные пишутся в %s — они ВРЕМЕННЫЕ "
                       "и пропадут при редеплое. Задайте DATA_DIR.", e, d)
        return d


DATA_DIR = _pick_data_dir()
db.init(DATA_DIR)
# Старые пути — только для разового переноса данных уже развёрнутого
# инстанса (см. _migrate_from_json ниже), новый код их не пишет.
_OLD_USERS_FILE = os.path.join(DATA_DIR, "users.json")
_OLD_SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
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


class UsersFileError(RuntimeError):
    """БД пользователей существует, но не читается — перезаписывать её нельзя."""


def _load() -> dict:
    try:
        con = db.connect()
        try:
            rows = con.execute(
                "SELECT username, salt, hash, role, groups_json, "
                "tokens_valid_after FROM users").fetchall()
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        raise UsersFileError(f"{db.path()} повреждён или не читается: {e}. "
                             f"Восстановите его из резервной копии") from e
    return {r["username"]: {"salt": r["salt"], "hash": r["hash"],
                            "role": r["role"], "groups": json.loads(r["groups_json"]),
                            "tokens_valid_after": r["tokens_valid_after"]}
            for r in rows}


def _save(users: dict):
    with db.tx() as con:
        con.execute("DELETE FROM users")
        con.executemany(
            "INSERT INTO users (username, salt, hash, role, groups_json, "
            "tokens_valid_after) VALUES (?, ?, ?, ?, ?, ?)",
            [(u, v["salt"], v["hash"], v.get("role", "user"),
              json.dumps(v.get("groups", [])), v.get("tokens_valid_after", 0))
             for u, v in users.items()])


RESET_MARKER = os.path.join(DATA_DIR, ".reset_token")


INITIAL_PW_FILE = os.path.join(DATA_DIR, "initial_admin_password.txt")


def _write_initial_password(admin: str, pw: str):
    """Сгенерированный пароль первого админа — в файл с правами только владельца."""
    fd = os.open(INITIAL_PW_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"login: {admin}\npassword: {pw}\n")
    os.chmod(INITIAL_PW_FILE, 0o600)  # если файл уже был с другими правами


def _drop_initial_password(username: str):
    """Пароль сменён — файл с первичным паролем этого админа больше не нужен."""
    try:
        with open(INITIAL_PW_FILE, encoding="utf-8") as f:
            first = f.readline().strip()
    except FileNotFoundError:
        return
    if first == f"login: {username}":
        os.remove(INITIAL_PW_FILE)


def _write_reset_marker(token: str):
    try:
        with open(RESET_MARKER, "w", encoding="utf-8") as f:
            f.write(token)
    except Exception as e:  # noqa: BLE001
        logger.warning("не удалось записать маркер сброса: %s", e)


def init_admin():
    """Засев админа, если учёток нет. Если задан ADMIN_RESET_TOKEN и он новый —
    сбрасывает пароль ТОЛЬКО учётки ADMIN_USER на ADMIN_PASSWORD (создаёт её,
    если нет). Остальные пользователи НЕ удаляются."""
    with _LOCK:
        token = os.getenv("ADMIN_RESET_TOKEN", "").strip()
        applied = ""
        try:
            with open(RESET_MARKER, encoding="utf-8") as f:
                applied = f.read().strip()
        except Exception:
            pass
        force = bool(token) and token != applied

        try:
            users = _load()
        except UsersFileError as e:
            # не стартуем: иначе засев перезапишет файл и все учётки пропадут
            logger.error("ОСТАНОВ: %s", e)
            raise
        if users and not force:
            return

        admin = os.getenv("ADMIN_USER", "admin")
        env_pw = os.getenv("ADMIN_PASSWORD")

        if users:  # сброс пароля админа при существующих учётках
            if not env_pw:
                logger.warning("ADMIN_RESET_TOKEN задан, но ADMIN_PASSWORD пуст — "
                               "сброс пропущен")
                return
            entry = users.get(admin) or {"groups": []}
            _apply_password(entry, env_pw)
            entry["role"] = "admin"
            users[admin] = entry
            _save(users)
            revoke_user_sessions(admin)
            _drop_initial_password(admin)
            _write_reset_marker(token)
            logger.info("СБРОС: пароль администратора '%s' обновлён, "
                       "остальные учётки не тронуты", admin)
            return

        # учёток нет — первичный засев (пароль в лог НЕ пишем)
        pw = env_pw or secrets.token_urlsafe(12)
        salt = secrets.token_bytes(16)
        users = {admin: {"salt": salt.hex(), "hash": _hash(pw, salt),
                         "role": "admin", "groups": []}}
        _save(users)
        if token:
            _write_reset_marker(token)
        if env_pw:
            logger.info("Засеян администратор '%s' (пароль из ADMIN_PASSWORD)", admin)
        else:
            _write_initial_password(admin, pw)
            logger.info("Засеян администратор '%s'. Пароль — в файле %s; "
                       "удалите его после первого входа (удалится сам при смене пароля)",
                       admin, INITIAL_PW_FILE)


def _user_public(username: str, u: dict) -> dict:
    return {"username": username, "role": u.get("role", "user"),
            "groups": u.get("groups", [])}


def verify(username: str, pw: str):
    try:
        u = _load().get(username)
    except UsersFileError as e:
        logger.error("%s", e)
        return None
    if not u:
        return None
    if hmac.compare_digest(_hash(pw, bytes.fromhex(u["salt"])), u["hash"]):
        return _user_public(username, u)
    return None


MIN_PASSWORD_LEN = 8


def _check_password(pw: str):
    if len(pw or "") < MIN_PASSWORD_LEN:
        raise ValueError(f"Пароль слишком короткий (мин. {MIN_PASSWORD_LEN} символов)")


_USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{3,40}")


def create_user(username: str, pw: str, role: str = "user", groups=None):
    username = (username or "").strip()
    if not username or not pw:
        raise ValueError("Нужны логин и пароль")
    if not _USERNAME_RE.fullmatch(username):
        raise ValueError("Логин: 3–40 символов, только латиница, цифры и _ . -")
    _check_password(pw)
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


def _apply_password(entry: dict, pw: str):
    salt = secrets.token_bytes(16)
    entry["salt"] = salt.hex()
    entry["hash"] = _hash(pw, salt)
    # куки старого формата, выданные до этого момента, больше не принимаются
    entry["tokens_valid_after"] = int(time.time())


def set_password(username: str, pw: str):
    """Меняет пароль и завершает ВСЕ сессии пользователя на всех устройствах."""
    _check_password(pw)
    with _LOCK:
        users = _load()
        if username not in users:
            raise ValueError("Нет такого пользователя")
        _apply_password(users[username], pw)
        _save(users)
    revoke_user_sessions(username)
    _drop_initial_password(username)


def delete_user(username: str):
    with _LOCK:
        users = _load()
        u = users.get(username)
        if u and u.get("role") != "admin":
            del users[username]
            _save(users)
        else:
            return False
    revoke_user_sessions(username)
    return True


def list_users():
    return sorted(
        (_user_public(k, v) for k, v in _load().items()),
        key=lambda x: (x["role"] != "admin", x["username"]),
    )


# ---- лимит неудачных попыток пароля (в памяти процесса) ----
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW = 15 * 60
_FAILS = {}  # ключ -> [время неудачных попыток]
_FAILS_LOCK = threading.Lock()


def fail_wait_minutes(key) -> int:
    """0 — можно пробовать; иначе сколько минут ждать (лимит исчерпан)."""
    now = time.time()
    with _FAILS_LOCK:
        ts = [t for t in _FAILS.get(key, []) if t > now - LOGIN_WINDOW]
        if ts:
            _FAILS[key] = ts
        else:
            _FAILS.pop(key, None)
        if len(ts) < LOGIN_MAX_FAILS:
            return 0
        return int((ts[0] + LOGIN_WINDOW - now) // 60) + 1


def fail_add(key):
    now = time.time()
    with _FAILS_LOCK:
        if len(_FAILS) > 10000:  # защита памяти от перебора случайных логинов
            for k in [k for k, v in _FAILS.items() if v[-1] <= now - LOGIN_WINDOW]:
                del _FAILS[k]
        _FAILS.setdefault(key, []).append(now)


def fail_reset(key):
    with _FAILS_LOCK:
        _FAILS.pop(key, None)


# ---- сессии (хранятся на сервере) ----
# В куке — случайный идентификатор сессии; в таблице sessions лежит только
# его sha256, поэтому утечка БД не даёт войти. Выход завершает только
# текущее устройство, смена пароля — все устройства пользователя.
_SESS_LOCK = threading.Lock()


def _sid_key(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()


def _sess_load() -> dict:
    try:
        con = db.connect()
        try:
            rows = con.execute(
                "SELECT sid_hash, username, created, exp FROM sessions").fetchall()
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        # потеря сессий не страшна — пользователи просто войдут заново
        logger.warning("сессии не читаются (%s) — все сессии сброшены", e)
        return {}
    return {r["sid_hash"]: {"user": r["username"], "created": r["created"],
                            "exp": r["exp"]} for r in rows}


def _sess_save(sessions: dict):
    with db.tx() as con:
        con.execute("DELETE FROM sessions")
        con.executemany(
            "INSERT INTO sessions (sid_hash, username, created, exp) "
            "VALUES (?, ?, ?, ?)",
            [(k, v["user"], v["created"], v["exp"]) for k, v in sessions.items()])


def create_session(username: str) -> str:
    sid = secrets.token_urlsafe(32)
    now = int(time.time())
    with _SESS_LOCK:
        sessions = {k: v for k, v in _sess_load().items() if v.get("exp", 0) > now}
        sessions[_sid_key(sid)] = {"user": username, "created": now,
                                   "exp": now + SESSION_TTL}
        _sess_save(sessions)
    return sid


def revoke_user_sessions(username: str):
    """Завершает все сессии пользователя (все устройства)."""
    with _SESS_LOCK:
        sessions = _sess_load()
        left = {k: v for k, v in sessions.items() if v.get("user") != username}
        if len(left) != len(sessions):
            _sess_save(left)


def _read_legacy_token(tok: str):
    """Кука старого формата base64(user|exp|hmac). Возвращает (user, выдана_в) или None."""
    try:
        raw = base64.urlsafe_b64decode(tok.encode()).decode()
        username, exp, sig = raw.split("|")
        good = hmac.new(SECRET, f"{username}|{exp}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig) or int(exp) < time.time():
            return None
        return username, int(exp) - SESSION_TTL
    except Exception:
        return None


def read_session(cookie: str):
    """Кука -> (пользователь или None, новый sid или None).
    Новый sid возвращается, когда старая кука переведена в серверную сессию:
    его надо выставить в ответе, пользователь при этом не разлогинивается."""
    if not cookie:
        return None, None
    try:
        users = _load()
    except UsersFileError as e:
        logger.error("%s", e)
        return None, None
    s = _sess_load().get(_sid_key(cookie))
    if s:
        if s.get("exp", 0) < time.time() or s.get("user") not in users:
            return None, None
        return _user_public(s["user"], users[s["user"]]), None
    legacy = _read_legacy_token(cookie)
    if legacy:
        username, issued = legacy
        u = users.get(username)
        if u and issued >= u.get("tokens_valid_after", 0):
            return _user_public(username, u), create_session(username)
    return None, None


def logout(cookie: str):
    """Завершает только текущую сессию (это устройство)."""
    if not cookie:
        return
    key = _sid_key(cookie)
    with _SESS_LOCK:
        sessions = _sess_load()
        if sessions.pop(key, None) is not None:
            _sess_save(sessions)
            return
    # старая кука: отдельно её не отозвать — отзываем все куки старого формата
    legacy = _read_legacy_token(cookie)
    if legacy:
        with _LOCK:
            users = _load()
            if legacy[0] in users:
                users[legacy[0]]["tokens_valid_after"] = int(time.time()) + 1
                _save(users)


def _migrate_from_json():
    """Разовый перенос данных уже развёрнутого инстанса из старых
    users.json/sessions.json в SQLite — только если таблицы пустые, а
    файлы существуют. Файлы переименовываются в *.migrated, а не
    удаляются: откат на предыдущую версию кода не должен терять данные."""
    if not _load() and os.path.exists(_OLD_USERS_FILE):
        try:
            with open(_OLD_USERS_FILE, encoding="utf-8") as f:
                old_users = json.load(f)
            if isinstance(old_users, dict) and old_users:
                _save(old_users)
                logger.info("users.json перенесён в SQLite (%d учёток)", len(old_users))
            os.rename(_OLD_USERS_FILE, _OLD_USERS_FILE + ".migrated")
        except Exception as e:  # noqa: BLE001
            logger.error("не удалось перенести users.json в SQLite: %s", e)
    if not _sess_load() and os.path.exists(_OLD_SESSIONS_FILE):
        try:
            with open(_OLD_SESSIONS_FILE, encoding="utf-8") as f:
                old_sessions = json.load(f)
            if isinstance(old_sessions, dict) and old_sessions:
                _sess_save(old_sessions)
                logger.info("sessions.json перенесён в SQLite (%d сессий)",
                           len(old_sessions))
            os.rename(_OLD_SESSIONS_FILE, _OLD_SESSIONS_FILE + ".migrated")
        except Exception as e:  # noqa: BLE001
            logger.error("не удалось перенести sessions.json в SQLite: %s", e)


_migrate_from_json()
