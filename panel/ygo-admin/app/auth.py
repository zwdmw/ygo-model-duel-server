"""Single-owner authentication: scrypt passwords, revocable sessions and optional TOTP."""
from collections import OrderedDict, deque
import hashlib
import hmac
import secrets
import time

import pyotp


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                            n=32768, r=8, p=1, maxmem=64 * 1024 * 1024).hex()
    return {"salt": salt, "hash": digest}


def password_matches(password, saved):
    if not isinstance(password, str) or not 1 <= len(password) <= 256:
        return False
    return hmac.compare_digest(password_hash(password, saved["salt"])["hash"], saved["hash"])


class RateLimit:
    def __init__(self, attempts=8, seconds=300, capacity=512):
        self.attempts, self.seconds, self.capacity = attempts, seconds, capacity
        self.entries = OrderedDict()
        self.global_entries = deque()

    def allow(self, identity):
        now = time.monotonic()
        while self.global_entries and self.global_entries[0] < now - self.seconds:
            self.global_entries.popleft()
        if len(self.global_entries) >= 40:
            return False
        history = self.entries.setdefault(identity, deque())
        self.entries.move_to_end(identity)
        while len(self.entries) > self.capacity:
            self.entries.popitem(last=False)
        while history and history[0] < now - self.seconds:
            history.popleft()
        if len(history) >= self.attempts:
            return False
        history.append(now)
        self.global_entries.append(now)
        return True


class Sessions:
    def __init__(self):
        self.items = OrderedDict()

    def create(self):
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        value = {"csrf": secrets.token_urlsafe(24), "created": now, "seen": now}
        self.items[hashlib.sha256(token.encode()).hexdigest()] = value
        while len(self.items) > 16:
            self.items.popitem(last=False)
        return token, value

    def get(self, token):
        key = hashlib.sha256((token or "").encode()).hexdigest()
        value = self.items.get(key)
        now = time.monotonic()
        if value and (now - value["created"] > 8 * 3600 or now - value["seen"] > 1800):
            self.items.pop(key, None)
            return None
        if value:
            value["seen"] = now
        return value

    def remove(self, token):
        self.items.pop(hashlib.sha256((token or "").encode()).hexdigest(), None)


def verify_second_factor(user, code):
    if not user.get("totp_secret"):
        return True
    if not isinstance(code, str) or len(code) > 64:
        return False
    digest = hashlib.sha256(code.strip().encode()).hexdigest()
    for saved in user.get("recovery", []):
        if hmac.compare_digest(digest, saved):
            user["recovery"].remove(saved)
            return True
    now = int(time.time()) // 30
    generator = pyotp.TOTP(user["totp_secret"])
    for counter in range(now - 1, now + 2):
        if counter > user.get("last_totp_counter", -1) and hmac.compare_digest(
                generator.at(counter * 30), code.strip()):
            user["last_totp_counter"] = counter
            return True
    return False
