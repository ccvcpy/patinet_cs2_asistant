from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class Account:
    id: str
    name: str
    username: str | None = None
    password: str | None = None
    steam_id64: str | None = None
    shared_secret: str | None = None
    identity_secret: str | None = None
    device_id: str | None = None
    cookies: str | None = None
    trade_url: str | None = None
    c5_api_key: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Account":
        return cls(
            id=str(payload.get("id") or "").strip(),
            name=str(payload.get("name") or "").strip(),
            username=_clean_optional(payload.get("username")),
            password=_clean_optional(payload.get("password")),
            steam_id64=_clean_optional(payload.get("steam_id64")),
            shared_secret=_clean_optional(payload.get("shared_secret")),
            identity_secret=_clean_optional(payload.get("identity_secret")),
            device_id=_clean_optional(payload.get("device_id")),
            cookies=_clean_optional(payload.get("cookies")),
            trade_url=_clean_optional(payload.get("trade_url")),
            c5_api_key=_clean_optional(payload.get("c5_api_key")),
            updated_at=_clean_optional(payload.get("updated_at")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class AccountStore:
    SENSITIVE_FIELDS = {
        "username",
        "password",
        "shared_secret",
        "identity_secret",
        "device_id",
        "cookies",
        "trade_url",
        "c5_api_key",
    }

    def __init__(self, storage_dir: str | Path) -> None:
        self.storage_dir = Path(storage_dir)
        self.file_path = self.storage_dir / "accounts.json"
        self._cache: dict[str, Any] | None = None
        self._fernet: Fernet | None | bool = False

    def list_accounts(self) -> list[Account]:
        payload = self._load_payload()
        return [self._decode_account(raw) for raw in payload.get("accounts", [])]

    def get_account(self, account_id_or_name: str) -> Account | None:
        lookup = str(account_id_or_name or "").strip()
        if not lookup:
            return None
        for account in self.list_accounts():
            if account.id == lookup or account.name == lookup:
                return account
        return None

    def get_current(self) -> Account | None:
        payload = self._load_payload()
        current_id = str(payload.get("current_id") or "").strip()
        accounts = self.list_accounts()
        if not accounts:
            return None
        if not current_id:
            return accounts[0]
        for account in accounts:
            if account.id == current_id:
                return account
        return accounts[0]

    def add_account(self, **kwargs: Any) -> Account:
        payload = self._load_payload()
        accounts_raw = list(payload.get("accounts", []))
        name = str(kwargs.get("name") or "").strip()
        if not name:
            raise ValueError("account name is required")
        if any(str(row.get("name") or "").strip() == name for row in accounts_raw):
            raise ValueError(f"account already exists: {name}")

        account = Account(
            id=str(kwargs.get("id") or uuid.uuid4().hex[:8]),
            name=name,
            username=_clean_optional(kwargs.get("username")),
            password=_clean_optional(kwargs.get("password")),
            steam_id64=_clean_optional(kwargs.get("steam_id64")),
            shared_secret=_clean_optional(kwargs.get("shared_secret")),
            identity_secret=_clean_optional(kwargs.get("identity_secret")),
            device_id=_clean_optional(kwargs.get("device_id")),
            cookies=_clean_optional(kwargs.get("cookies")),
            trade_url=_clean_optional(kwargs.get("trade_url")),
            c5_api_key=_clean_optional(kwargs.get("c5_api_key")),
            updated_at=_utc_now_iso(),
        )
        accounts_raw.append(self._encode_account(account))
        payload["accounts"] = accounts_raw
        if not str(payload.get("current_id") or "").strip():
            payload["current_id"] = account.id
        self._save_payload(payload)
        return account

    def update_account(self, account_id_or_name: str, **kwargs: Any) -> Account | None:
        payload = self._load_payload()
        accounts_raw = list(payload.get("accounts", []))
        target = self.get_account(account_id_or_name)
        if target is None:
            return None

        updates = {key: value for key, value in kwargs.items() if hasattr(target, key)}
        if "name" in updates:
            next_name = str(updates["name"] or "").strip()
            if not next_name:
                raise ValueError("account name cannot be empty")
            for raw in accounts_raw:
                if str(raw.get("id") or "") == target.id:
                    continue
                if str(raw.get("name") or "").strip() == next_name:
                    raise ValueError(f"account already exists: {next_name}")
            target.name = next_name
            updates.pop("name")

        for key, value in updates.items():
            if key in {"id", "updated_at"}:
                continue
            setattr(target, key, _clean_optional(value))
        target.updated_at = _utc_now_iso()

        new_accounts: list[dict[str, Any]] = []
        for raw in accounts_raw:
            if str(raw.get("id") or "") == target.id:
                new_accounts.append(self._encode_account(target))
            else:
                new_accounts.append(raw)
        payload["accounts"] = new_accounts
        self._save_payload(payload)
        return target

    def delete_account(self, account_id_or_name: str) -> bool:
        payload = self._load_payload()
        target = self.get_account(account_id_or_name)
        if target is None:
            return False
        accounts_raw = [
            raw for raw in payload.get("accounts", [])
            if str(raw.get("id") or "") != target.id
        ]
        payload["accounts"] = accounts_raw
        if str(payload.get("current_id") or "") == target.id:
            payload["current_id"] = str(accounts_raw[0].get("id") or "") if accounts_raw else None
        self._save_payload(payload)
        return True

    def set_current(self, account_id_or_name: str) -> bool:
        target = self.get_account(account_id_or_name)
        if target is None:
            return False
        payload = self._load_payload()
        payload["current_id"] = target.id
        self._save_payload(payload)
        return True

    def get_current_id(self) -> str | None:
        payload = self._load_payload()
        current_id = str(payload.get("current_id") or "").strip()
        return current_id or None

    def _load_payload(self) -> dict[str, Any]:
        if self._cache is not None:
            return self._cache
        if not self.file_path.exists():
            self._cache = {"accounts": [], "current_id": None}
            return self._cache
        try:
            payload = json.loads(self.file_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"failed to load accounts store: {exc}") from exc
        if not isinstance(payload, dict):
            payload = {"accounts": [], "current_id": None}
        payload.setdefault("accounts", [])
        payload.setdefault("current_id", None)
        self._cache = payload
        return payload

    def _save_payload(self, payload: dict[str, Any]) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.file_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._cache = payload

    def _encode_account(self, account: Account) -> dict[str, Any]:
        payload = account.to_dict()
        for field in self.SENSITIVE_FIELDS:
            value = payload.get(field)
            if value is None:
                continue
            payload[field] = self._encrypt(value)
        return payload

    def _decode_account(self, payload: dict[str, Any]) -> Account:
        decoded = dict(payload)
        for field in self.SENSITIVE_FIELDS:
            value = decoded.get(field)
            if value is None:
                continue
            decoded[field] = self._decrypt(value)
        return Account.from_dict(decoded)

    def _encrypt(self, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        fernet = self._get_fernet(required=True)
        return f"enc:{fernet.encrypt(text.encode('utf-8')).decode('utf-8')}"

    def _decrypt(self, value: Any) -> str | None:
        text = _clean_optional(value)
        if not text:
            return None
        if not text.startswith("enc:"):
            return text
        token = text[4:]
        fernet = self._get_fernet(required=True)
        try:
            return fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError("failed to decrypt accounts.json; check CS2_MASTER_KEY") from exc

    def _get_fernet(self, *, required: bool) -> Fernet:
        if isinstance(self._fernet, Fernet):
            return self._fernet
        raw_key = os.environ.get("CS2_MASTER_KEY")
        if not raw_key:
            if required:
                raise RuntimeError("missing CS2_MASTER_KEY")
            raise RuntimeError("missing CS2_MASTER_KEY")
        key = self._normalize_master_key(raw_key)
        self._fernet = Fernet(key)
        return self._fernet

    @staticmethod
    def _normalize_master_key(raw_key: str) -> bytes:
        candidate = raw_key.strip().encode("utf-8")
        try:
            decoded = base64.urlsafe_b64decode(candidate)
            if len(decoded) == 32:
                return candidate
        except Exception:
            pass
        digest = hashlib.sha256(candidate).digest()
        return base64.urlsafe_b64encode(digest)
