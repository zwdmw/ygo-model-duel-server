"""Owner-only outbound connection presets. Saving never opens a connection."""
import ipaddress
import json
from pathlib import Path
import re
import secrets


ID = re.compile(r"[0-9a-f]{16}")
SHA = re.compile(r"[0-9a-f]{64}")
MAX_PROFILES = 32
MAX_BYTES = 128 * 1024
PUBLIC_FIELDS = ("id", "name", "host", "port", "model_id", "deck_id",
                 "deck_source", "auth_mode", "account_name")
DEFAULTS = {"deck_source": "local", "auth_mode": "none", "account_name": "",
            "_account_password": ""}


class RevisionConflict(ValueError):
    pass


def host_name(value):
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ValueError("Invalid server host")
    if value != value.strip() or any(ord(c) < 33 for c in value):
        raise ValueError("Invalid server host")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    try:
        name = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("Invalid server host") from None
    if len(name) > 253 or not all(re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in name.split(".")):
        raise ValueError("Invalid server host")
    # Do not accept invalid dotted IPv4 as a hostname.
    if re.fullmatch(r"[0-9.]+", name):
        raise ValueError("Invalid server host")
    return name


def validated_fields(body):
    name = body.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError("Server name must have 1-80 characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("Invalid server name")
    port = body.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Server port must be an integer from 1 to 65535")
    model, deck = body.get("model_id", ""), body.get("deck_id", "builtin")
    if not isinstance(model, str) or (model and not SHA.fullmatch(model)):
        raise ValueError("Invalid model identity")
    if not isinstance(deck, str) or (deck not in ("", "builtin") and not ID.fullmatch(deck)):
        raise ValueError("Invalid deck identity")
    source = body.get("deck_source", "local")
    auth = body.get("auth_mode", "none")
    account = body.get("account_name", "")
    if source not in ("local", "server_random") or auth not in ("none", "duelchronicle"):
        raise ValueError("Invalid deck source or account protocol")
    if not isinstance(account, str) or len(account.encode("utf-16-le")) > 38 or any(
            ord(c) < 33 or ord(c) == 127 for c in account):
        raise ValueError("Invalid account name")
    if auth == "duelchronicle" and (source != "server_random" or not account):
        raise ValueError("DuelChronicle requires a server-assigned deck and account name")
    return dict(name=name.strip(), host=host_name(body.get("host")), port=port,
                model_id=model, deck_id=deck, deck_source=source, auth_mode=auth,
                account_name=account)


def account_password(value):
    if not isinstance(value, str) or len(value) > 64 or any(
            ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError("Invalid account password")
    return value


def valid_password(value):
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Invalid room password")
    try:
        size = len(value.encode("utf-16-le"))
    except UnicodeError:
        raise ValueError("Invalid room password") from None
    if size > 38:
        raise ValueError("Room password exceeds 19 UTF-16 characters")
    return value


class ServerProfiles:
    def __init__(self, path, save):
        self.path, self.save = Path(path), save

    def _read(self):
        try:
            if self.path.is_symlink():
                raise ValueError("Invalid preset storage")
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
        except FileNotFoundError:
            return {"schema": 1, "revision": 0, "selected": "2330000000000001",
                    "items": [{"id": "2330000000000001", "name": "YGO233",
                               "host": "s1.ygo233.com", "port": 233,
                               "model_id": "", "deck_id": "builtin", "_password": "", **DEFAULTS}]}
        # Fail closed on damaged data. Never overwrite it with empty defaults.
        try:
            if len(raw) > MAX_BYTES:
                raise ValueError()
            value = json.loads(raw)
            if (value["schema"] != 1 or type(value["revision"]) is not int
                    or value["revision"] < 0 or not isinstance(value["items"], list)
                    or len(value["items"]) > MAX_PROFILES):
                raise ValueError()
            ids = set()
            for row in value["items"]:
                for key, default in DEFAULTS.items():
                    row.setdefault(key, default)
                if not ID.fullmatch(row["id"]) or row["id"] in ids:
                    raise ValueError()
                ids.add(row["id"])
                if any(row[key] != field for key, field in validated_fields(row).items()):
                    raise ValueError()
                valid_password(row["_password"])
                account_password(row["_account_password"])
            if value["selected"] != "" and value["selected"] not in ids:
                raise ValueError()
            return value
        except (ValueError, KeyError, TypeError):
            raise ValueError("Preset storage is damaged; restore a backup before saving") from None

    @staticmethod
    def public(value):
        return {"revision": value["revision"], "selected": value["selected"],
                "items": [{**{key: row[key] for key in PUBLIC_FIELDS},
                           "password_saved": bool(row["_password"]),
                           "account_password_saved": bool(row["_account_password"])}
                          for row in value["items"]]}

    def listing(self):
        return self.public(self._read())

    def connection(self, identity, revision):
        value = self._read()
        if type(revision) is not int or revision != value["revision"]:
            raise RevisionConflict("Presets changed; refresh before connecting")
        row = next((row for row in value["items"] if row["id"] == identity), None)
        if row is None:
            raise ValueError("Save a server preset before connecting")
        if not row["model_id"] or (row["deck_source"] == "local" and not row["deck_id"]):
            raise ValueError("Select a model and deck before connecting")
        if row["auth_mode"] != "none" and not row["_account_password"]:
            raise ValueError("Save the account password before connecting")
        return {key: row[key] for key in (
            "host", "port", "model_id", "deck_id", "deck_source", "auth_mode", "account_name")} | {
            "password": row["_password"], "account_password":
                row["_account_password"] if row["auth_mode"] != "none" else ""}

    def change(self, body):
        if not isinstance(body, dict):
            raise ValueError("Expected an object")
        action = body.get("action")
        allowed = {"action", "revision", "id"}
        if action == "save":
            allowed |= {"name", "host", "port", "model_id", "deck_id", "password_mode", "password",
                        "deck_source", "auth_mode", "account_name",
                        "account_password_mode", "account_password"}
        if action not in ("save", "select", "delete") or set(body) - allowed:
            raise ValueError("Invalid preset operation")
        value = self._read()
        if type(body.get("revision")) is not int or body["revision"] != value["revision"]:
            raise RevisionConflict("Presets changed in another session; refresh before saving")
        identity = body.get("id", "")
        if not isinstance(identity, str) or (identity and not ID.fullmatch(identity)):
            raise ValueError("Invalid server identity")
        previous = next((row for row in value["items"] if row["id"] == identity), None)
        if (identity or action != "save") and previous is None:
            raise ValueError("Server preset no longer exists")
        if action == "save":
            row = validated_fields(body)
            mode = body.get("password_mode", "keep")
            if mode not in ("keep", "replace", "forget"):
                raise ValueError("Invalid password operation")
            if mode != "replace" and "password" in body:
                raise ValueError("Password requires an explicit replace operation")
            password = previous["_password"] if previous else ""
            if (password and mode == "keep" and
                    (row["host"], row["port"]) != (previous["host"], previous["port"])):
                raise ValueError("Re-enter or forget the password when changing the server")
            if mode == "replace":
                password = valid_password(body.get("password"))
            elif mode == "forget":
                password = ""
            account_mode = body.get("account_password_mode", "keep")
            if account_mode not in ("keep", "replace", "forget"):
                raise ValueError("Invalid account password operation")
            if account_mode != "replace" and "account_password" in body:
                raise ValueError("Account password requires an explicit replace operation")
            account_secret = previous["_account_password"] if previous else ""
            if account_secret and account_mode == "keep" and any(
                    row[key] != previous[key] for key in ("host", "port", "account_name", "auth_mode")):
                raise ValueError("Re-enter or forget the account password when changing its destination or identity")
            if account_mode == "replace":
                account_secret = account_password(body.get("account_password"))
            elif account_mode == "forget":
                account_secret = ""
            if not previous and len(value["items"]) >= MAX_PROFILES:
                raise ValueError("At most 32 server presets can be saved")
            row.update(id=identity or secrets.token_hex(8), _password=password,
                       _account_password=account_secret)
            if previous:
                value["items"][value["items"].index(previous)] = row
            else:
                value["items"].append(row)
            value["selected"] = row["id"]
        elif action == "select":
            value["selected"] = identity
        else:
            value["items"].remove(previous)
            if value["selected"] == identity:
                value["selected"] = value["items"][0]["id"] if value["items"] else ""
        value["revision"] += 1
        # atomic_json writes a mode-0600 temporary file, fsyncs, then replaces.
        self.save(self.path, value)
        return self.public(value)
