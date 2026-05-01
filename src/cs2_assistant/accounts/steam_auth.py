from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

import requests
import urllib3

from cs2_assistant.accounts.store import Account, AccountStore

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_AUTO_RELOGIN_LOCK = threading.Lock()
_AUTO_RELOGIN_LAST_SUCCESS = 0.0


def _verify_steam_cookies_valid(cookie_str: str, steam_id: str = "") -> bool:
    cookie_dict: dict[str, str] = {}
    for part in (cookie_str or "").split(";"):
        segment = part.strip()
        if "=" not in segment:
            continue
        key, _, value = segment.partition("=")
        cookie_dict[key.strip()] = value.strip()
    if not cookie_dict.get("steamLoginSecure"):
        return False

    session = requests.Session()
    session.verify = False
    session.cookies.update(cookie_dict)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
    )
    try:
        response = session.get(
            "https://store.steampowered.com/pointssummary/ajaxgetasyncconfig",
            timeout=12,
        )
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                if payload.get("logged_in") is True:
                    return True
                if payload.get("logged_in") is False:
                    return False
    except Exception:
        pass

    profile_url = f"https://steamcommunity.com/profiles/{steam_id}" if steam_id else "https://steamcommunity.com/my/profile"
    try:
        response = session.get(profile_url, timeout=12, allow_redirects=True)
        final_url = (response.url or "").lower()
        if "login" in final_url:
            return False
        if response.status_code in (401, 403):
            return False
        return True
    except Exception:
        return True


def _normalize_secret(raw: str | None) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    text = text.replace("\\u002B", "+").replace("\\u002b", "+")
    text = text.replace("\u002B", "+").replace("\u002b", "+")
    return text.replace("\\/", "/")


def _build_steam_guard_dict(account: Account) -> dict[str, str] | None:
    shared_secret = _normalize_secret(account.shared_secret)
    if not shared_secret:
        return None
    return {
        "steamid": account.steam_id64 or "",
        "shared_secret": shared_secret,
        "identity_secret": _normalize_secret(account.identity_secret),
        "device_id": account.device_id or "",
    }


def _do_steampy_login(
    username: str,
    password: str,
    steam_guard_dict: dict[str, str] | None,
) -> tuple[bool, str, dict[str, str]]:
    import requests as requests_module
    import requests.utils as requests_utils

    urllib3.disable_warnings()
    previous_request = requests_module.Session.request

    def _bypass_ssl(self: requests_module.Session, method: str, url: str, **kwargs: Any):
        kwargs["verify"] = False
        kwargs.setdefault("proxies", {})
        kwargs["proxies"] = {}
        return previous_request(self, method, url, **kwargs)

    requests_module.Session.request = _bypass_ssl
    try:
        from steampy.client import SteamClient

        steam_guard_json = json.dumps(steam_guard_dict) if steam_guard_dict else None
        client = SteamClient(
            api_key="",
            username=username,
            password=password,
            steam_guard=steam_guard_json,
        )
        client._session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Origin": "https://steamcommunity.com",
                "Referer": "https://steamcommunity.com/",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        client.login()
        if not client.is_session_alive():
            return False, "session_dead", {}
        # 先取全部 cookie（包含 steamMachineAuth 等），再用 domain 精确匹配的覆盖
        merged = dict(requests_utils.dict_from_cookiejar(client._session.cookies))
        community_cookies = client._session.cookies.get_dict(domain="steamcommunity.com")
        store_cookies = client._session.cookies.get_dict(domain="store.steampowered.com")
        merged.update(store_cookies)
        merged.update(community_cookies)
        if not merged.get("steamLoginSecure"):
            return False, "session_dead", {}
        return True, "", merged
    except Exception as exc:
        message = str(exc).lower()
        if any(token in message for token in ("invalid", "incorrect", "wrong", "bad credentials")):
            return False, "wrong_creds", {}
        if any(token in message for token in ("two-factor", "twofactor", "2fa", "guard")):
            return False, "need_2fa", {}
        if "captcha" in message:
            return False, "captcha", {}
        if "expecting value" in message or "no response" in message:
            return False, "ip_blocked", {}
        return False, str(exc)[:120], {}
    finally:
        requests_module.Session.request = previous_request


def _extract_creds_from_cookie_dict(cookie_dict: dict[str, str]) -> tuple[str, str, str]:
    cookie_str = "; ".join(f"{key}={value}" for key, value in cookie_dict.items())
    session_id = cookie_dict.get("sessionid", "")
    steam_id = ""
    steam_login_secure = cookie_dict.get("steamLoginSecure", "")
    if "%7C%7C" in steam_login_secure:
        steam_id = steam_login_secure.split("%7C%7C")[0].strip()
    elif "||" in steam_login_secure:
        steam_id = steam_login_secure.split("||")[0].strip()
    else:
        decoded = requests.utils.unquote(steam_login_secure)
        match = re.match(r"(\d{16,17})", decoded)
        if match:
            steam_id = match.group(1)
    return cookie_str, session_id, steam_id


def try_steam_auto_relogin(
    store: AccountStore,
    *,
    account_id: str | None = None,
    force_login: bool = False,
) -> tuple[bool, str, Account | None]:
    global _AUTO_RELOGIN_LAST_SUCCESS
    if not _AUTO_RELOGIN_LOCK.acquire(blocking=False):
        if time.time() - _AUTO_RELOGIN_LAST_SUCCESS < 30:
            return True, "auto_ok", store.get_account(account_id) if account_id else store.get_current()
        return False, "busy", None

    try:
        account = store.get_account(account_id) if account_id else store.get_current()
        if account is None:
            return False, "no_account", None

        if account.cookies and not force_login:
            try:
                if _verify_steam_cookies_valid(account.cookies, account.steam_id64 or ""):
                    _AUTO_RELOGIN_LAST_SUCCESS = time.time()
                    return True, "cookie_valid", account
            except Exception:
                pass

        if not account.username or not account.password:
            return False, "no_creds", account

        steam_guard_dict = _build_steam_guard_dict(account)
        ok, err_code, cookie_dict = _do_steampy_login(
            account.username,
            account.password,
            steam_guard_dict,
        )
        if ok and cookie_dict.get("steamLoginSecure"):
            cookie_str, _, steam_id = _extract_creds_from_cookie_dict(cookie_dict)
            updated = store.update_account(
                account.id,
                cookies=cookie_str,
                steam_id64=steam_id or account.steam_id64,
            )
            _AUTO_RELOGIN_LAST_SUCCESS = time.time()
            return True, "auto_ok", updated or store.get_account(account.id)
        return False, err_code or "error", account
    finally:
        _AUTO_RELOGIN_LOCK.release()
