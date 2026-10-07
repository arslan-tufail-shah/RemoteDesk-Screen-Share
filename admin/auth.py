import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
import ctypes
from ctypes import wintypes


APP_DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "RemoteDesk")
USERS_FILE = os.path.join(APP_DATA_DIR, "users.dat")
SESSION_FILE = os.path.join(APP_DATA_DIR, "session.dat")
SESSION_TTL_SECONDS = 8 * 60 * 60


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _protect(data):
    source = ctypes.create_string_buffer(data)
    source_blob = _DATA_BLOB(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    result_blob = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source_blob), None, None, None, None, 0, ctypes.byref(result_blob)):
        raise OSError("Windows credential protection failed")
    try:
        return ctypes.string_at(result_blob.pbData, result_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result_blob.pbData)


def _unprotect(data):
    source = ctypes.create_string_buffer(data)
    source_blob = _DATA_BLOB(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    result_blob = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source_blob), None, None, None, None, 0, ctypes.byref(result_blob)):
        raise OSError("Windows credential recovery failed")
    try:
        return ctypes.string_at(result_blob.pbData, result_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result_blob.pbData)


class UserStore:
    def __init__(self, path=USERS_FILE):
        self.path = path
        self._lock = threading.RLock()
        self._users = {}
        self._mtime_ns = None
        self._load()

    def _load(self):
        with self._lock:
            if not os.path.exists(self.path):
                self._users = {}
                self._mtime_ns = None
                return
            mtime_ns = os.stat(self.path).st_mtime_ns
            if mtime_ns == self._mtime_ns:
                return
            try:
                with open(self.path, "rb") as handle:
                    data = json.loads(_unprotect(handle.read()).decode("utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                data = {}
            self._users = data if isinstance(data, dict) else {}
            self._mtime_ns = mtime_ns

    @staticmethod
    def _password_record(password):
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
        return {"salt": salt.hex(), "hash": digest.hex()}

    @staticmethod
    def _password_matches(password, record):
        try:
            digest = hashlib.pbkdf2_hmac(
                "sha256", password.encode(), bytes.fromhex(record["salt"]), 200_000
            ).hex()
            return secrets.compare_digest(digest, record["hash"])
        except (KeyError, TypeError, ValueError):
            return False

    def _save(self):
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(prefix="users-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                protected = _protect(json.dumps(self._users).encode("utf-8"))
                handle.write(protected)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            self._mtime_ns = os.stat(self.path).st_mtime_ns
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)

    def remembered_user(self):
        try:
            with open(SESSION_FILE, "rb") as handle:
                session = json.loads(_unprotect(handle.read()).decode("utf-8"))
            if time.time() - float(session["created_at"]) <= SESSION_TTL_SECONDS:
                user = self.authenticate(session["username"], session["token"])
                if user:
                    return user
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
        self.clear_session()
        return None

    def save_session(self, username, password):
        user = self.authenticate(username, password)
        if not user:
            return False
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._users[username]["session_token"] = self._password_record(token)
            self._save()
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        with open(SESSION_FILE, "wb") as handle:
            handle.write(_protect(json.dumps({"username": username, "token": token, "created_at": time.time()}).encode("utf-8")))
        return True

    def clear_session(self):
        try:
            os.remove(SESSION_FILE)
        except FileNotFoundError:
            pass

    def users(self):
        self._load()
        with self._lock:
            return {name: dict(value) for name, value in self._users.items()}

    def authenticate(self, username, password):
        self._load()
        with self._lock:
            record = self._users.get(username)
            if isinstance(record, dict) and (
                self._password_matches(password, record)
                or self._password_matches(password, record.get("session_token", {}))
            ):
                return {"username": username, "role": record.get("role", "supervisor")}
        return None

    def add_user(self, username, password, role):
        self._load()
        username = username.strip()
        if not username or role not in {"manager", "supervisor"}:
            raise ValueError("Username and a valid role are required")
        with self._lock:
            if username in self._users:
                raise ValueError("That username already exists")
            self._users[username] = {"role": role, **self._password_record(password)}
            self._save()

    def edit_user(self, username, password=None, role=None):
        self._load()
        with self._lock:
            if username not in self._users:
                raise ValueError("User does not exist")
            if role is not None:
                if role not in {"manager", "supervisor"}:
                    raise ValueError("Invalid role")
                self._users[username]["role"] = role
            if password:
                self._users[username].update(self._password_record(password))
            self._save()

    def delete_user(self, username):
        self._load()
        with self._lock:
            if username not in self._users:
                raise ValueError("User does not exist")
            if self._users[username].get("role") == "manager" and sum(
                value.get("role") == "manager" for value in self._users.values()
            ) <= 1:
                raise ValueError("At least one manager must remain")
            del self._users[username]
            self._save()