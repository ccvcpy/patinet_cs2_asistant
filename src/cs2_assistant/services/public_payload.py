from __future__ import annotations

import re
from typing import Any


_SENSITIVE_KEYS = {
    "apikey",
    "appkey",
    "auth",
    "authorization",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "deviceid",
    "devicesecret",
    "guardcode",
    "identitysecret",
    "jwt",
    "password",
    "passwd",
    "proxyauthorization",
    "sessionid",
    "sharedsecret",
    "steamguard",
    "steamguardcode",
    "steamguardsecret",
    "steamloginsecure",
    "styletoken",
    "token",
    "tradeofferurl",
    "tradetoken",
    "tradeurl",
    "twofactorcode",
}

_STEAM_TRADE_URL_RE = re.compile(
    r"https?://steamcommunity\.com/tradeoffer/new/\?[^\s\"'<>]+",
    flags=re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)\b("
    r"sessionid|steamloginsecure|api[_ -]?key|app[_ -]?key|password|passwd|"
    r"cookie|steam[_ -]?guard(?:[_ -]?(?:secret|code))?|identity[_ -]?secret|"
    r"device[_ -]?(?:secret|id)|shared[_ -]?secret|style[_ -]?token|"
    r"access[_ -]?token|refresh[_ -]?token|trade[_ -]?(?:url|token)|token"
    r")\b\s*(?:[:=]|\s+)\s*([^\s,;}\]\)\"']+)",
)


def _normalized_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key or "").lower())


def is_sensitive_public_key(key: Any) -> bool:
    normalized = _normalized_key(key)
    if not normalized:
        return False
    if normalized in _SENSITIVE_KEYS:
        return True
    if "secret" in normalized or "password" in normalized:
        return True
    if "token" in normalized:
        return True
    if "cookie" in normalized or "sessionid" in normalized:
        return True
    if "apikey" in normalized or "appkey" in normalized:
        return True
    if "steamguard" in normalized or normalized.endswith("tradeurl"):
        return True
    return False


def redact_public_string(value: str) -> str:
    redacted = _STEAM_TRADE_URL_RE.sub("[REDACTED_STEAM_TRADE_URL]", value)
    redacted = _BEARER_RE.sub("Bearer [REDACTED]", redacted)
    return _CREDENTIAL_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}=[REDACTED]",
        redacted,
    )


def sanitize_public_payload(value: Any) -> Any:
    """Return a recursively sanitized copy suitable for browser/API output.

    This deliberately does not mutate database rows or internal execution
    structures.  Authentication material is removed by key, while ordinary
    business evidence such as wallet balances, prices, account ids and order
    ids remains available for diagnosis.
    """

    if isinstance(value, dict):
        return {
            key: sanitize_public_payload(item)
            for key, item in value.items()
            if not is_sensitive_public_key(key)
        }
    if isinstance(value, (list, tuple, set)):
        return [sanitize_public_payload(item) for item in value]
    if isinstance(value, str):
        return redact_public_string(value)
    return value
