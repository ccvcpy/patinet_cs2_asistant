from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
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
    if not cookie_dict.get("steamLoginSecure") or not cookie_dict.get("sessionid"):
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
    # Market access is the strictest useful definition of a "good" Steam
    # session for this project. A cookie that still opens profile/store pages
    # but gets 400/401 on /market/mylistings is not healthy enough for listing
    # or listing-status checks.
    try:
        response = session.get(
            "https://steamcommunity.com/market/mylistings",
            params={"start": 0, "count": 1, "norender": 1},
            timeout=12,
            allow_redirects=True,
        )
        final_url = (response.url or "").lower()
        if "login" in final_url:
            return False
        if response.status_code in (400, 401, 403):
            return False
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict) and payload.get("success") in (1, True):
                return True
    except Exception:
        pass

    # Fall back to broader storefront/profile checks only when the market
    # probe failed due to ambiguous transport issues. This avoids treating
    # temporary SSLEOF/timeout bursts as definite cookie invalidation.
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


class _Cs2SteamLoginExecutor:
    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        from steampy.login import LoginExecutor

        class _Executor(LoginExecutor):
            def _login_empty_response_status(self, login_response: requests.Response) -> str:
                eresult = str(login_response.headers.get("x-eresult") or "").strip()
                if eresult == "87":
                    return "steam_auth_throttled"
                if eresult == "5":
                    return "wrong_creds"
                if eresult:
                    return f"steam_auth_empty_response_eresult_{eresult}"
                return "steam_auth_empty_response"

            def login(self) -> requests.Session:
                from steampy.exceptions import ApiException, InvalidCredentials

                login_response = self._send_login_request()
                try:
                    login_payload = login_response.json()
                except ValueError as exc:
                    raise ApiException("steam_auth_invalid_response") from exc

                if not login_payload.get("response"):
                    status = self._login_empty_response_status(login_response)
                    if status == "wrong_creds":
                        raise InvalidCredentials(status)
                    raise ApiException(status)

                self._check_for_captcha(login_response)
                self._update_steam_guard(login_response)
                finalized_response = self._finalize_login()
                self._perform_redirects(finalized_response.json())
                self.set_sessionid_cookies()
                return self.session

            def _prepare_login_request_data(self, encrypted_password: bytes | str, rsa_timestamp: str) -> dict[str, Any]:
                password_payload = (
                    encrypted_password.decode("ascii")
                    if isinstance(encrypted_password, bytes)
                    else str(encrypted_password)
                )
                device_name = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome"
                return {
                    "persistence": "1",
                    "remember_login": True,
                    "encrypted_password": password_payload,
                    "account_name": self.username,
                    "encryption_timestamp": rsa_timestamp,
                    "website_id": "Community",
                    "platform_type": 2,
                    "device_friendly_name": device_name,
                    "language": 0,
                    "device_details": {
                        "device_friendly_name": device_name,
                        "platform_type": 2,
                    },
                }

            def _send_login_request(self) -> requests.Response:
                from steampy.models import SteamUrl

                rsa_params = self._fetch_rsa_params()
                encrypted_password = self._encrypt_password(rsa_params)
                request_data = self._prepare_login_request_data(encrypted_password, rsa_params["rsa_timestamp"])
                url = f"{SteamUrl.API_URL}/IAuthenticationService/BeginAuthSessionViaCredentials/v1"
                headers = {
                    "Referer": f"{SteamUrl.COMMUNITY_URL}/",
                    "Origin": SteamUrl.COMMUNITY_URL,
                    "Accept": "application/json, text/plain, */*",
                }
                return self.session.post(url, data={"input_json": json.dumps(request_data)}, headers=headers)

        return _Executor(*args, **kwargs)


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
        import steampy.client as steampy_client_module

        previous_login_executor = steampy_client_module.LoginExecutor
        steampy_client_module.LoginExecutor = _Cs2SteamLoginExecutor

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
        key = str(exc).strip("'\"")
        if key in {"client_id", "steamid", "request_id", "refresh_token"}:
            return False, "steam_auth_session_unavailable", {}
        message = str(exc).lower()
        if any(token in message for token in ("invalid", "incorrect", "wrong", "bad credentials")):
            return False, "wrong_creds", {}
        if "steam_auth_throttled" in message:
            return False, "steam_auth_throttled", {}
        if any(token in message for token in ("two-factor", "twofactor", "2fa", "guard")):
            return False, "need_2fa", {}
        if "captcha" in message:
            return False, "captcha", {}
        if "expecting value" in message or "no response" in message:
            return False, "ip_blocked", {}
        return False, str(exc)[:120], {}
    finally:
        if "previous_login_executor" in locals() and "steampy_client_module" in locals():
            steampy_client_module.LoginExecutor = previous_login_executor
        requests_module.Session.request = previous_request


def _visible_input_count(page: Any, selector: str) -> int:
    try:
        return int(page.locator(selector).count())
    except Exception:
        return 0


def _do_playwright_login(
    username: str,
    password: str,
    shared_secret: str,
    profile_dir: str | Path,
) -> tuple[bool, str, dict[str, str]]:
    try:
        from playwright.sync_api import sync_playwright
        from steampy import guard
    except Exception as exc:
        return False, f"playwright_unavailable: {exc}", {}

    profile_path = Path(profile_dir)
    profile_path.mkdir(parents=True, exist_ok=True)
    browser_cookies: list[dict[str, Any]] = []
    body_hint = ""

    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile_path),
                headless=True,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto("https://store.steampowered.com/login/", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2500)

                text_inputs = page.locator('input[type="text"]:visible')
                password_inputs = page.locator('input[type="password"]:visible')
                if _visible_input_count(page, 'input[type="password"]:visible') < 1:
                    browser_cookies = context.cookies()
                else:
                    username_index = 1 if _visible_input_count(page, 'input[type="text"]:visible') > 1 else 0
                    text_inputs.nth(username_index).fill(username, timeout=10000)
                    password_inputs.nth(0).fill(password, timeout=10000)
                    page.get_by_role("button", name=re.compile(r"sign in", re.IGNORECASE)).click(timeout=10000)
                    page.wait_for_timeout(5000)

                    try:
                        body_hint = " ".join(page.locator("body").inner_text(timeout=5000).split()).lower()
                    except Exception:
                        body_hint = ""
                    if "check your password and account name" in body_hint:
                        return False, "wrong_creds", {}
                    if "too many retries" in body_hint or "try again later" in body_hint:
                        return False, "steam_auth_throttled", {}

                    code = guard.generate_one_time_code(shared_secret)
                    code_filled = False
                    code_inputs = page.locator('input[type="text"]:visible')
                    for index in range(_visible_input_count(page, 'input[type="text"]:visible')):
                        try:
                            placeholder = (code_inputs.nth(index).get_attribute("placeholder") or "").lower()
                            if "code" in placeholder:
                                code_inputs.nth(index).fill(code, timeout=3000)
                                code_filled = True
                                break
                        except Exception:
                            continue
                    if not code_filled:
                        try:
                            page.keyboard.type(code, delay=50)
                            code_filled = True
                        except Exception:
                            pass
                    if code_filled:
                        page.wait_for_timeout(5000)
                        try:
                            continue_button = page.get_by_role("button", name=re.compile(r"continue", re.IGNORECASE))
                            if continue_button.is_visible(timeout=1500):
                                continue_button.click(timeout=3000)
                        except Exception:
                            pass
                        page.wait_for_timeout(8000)

                    try:
                        page.goto("https://steamcommunity.com/market/", wait_until="domcontentloaded", timeout=20000)
                    except Exception:
                        pass
                    page.wait_for_timeout(3000)
                    browser_cookies = context.cookies()
            finally:
                try:
                    context.close()
                except Exception:
                    pass
    except Exception as exc:
        return False, f"browser_auto_failed: {str(exc)[:80]}", {}

    selected = [
        cookie for cookie in browser_cookies
        if "steam" in str(cookie.get("domain") or "").lower()
    ] or list(browser_cookies)
    cookie_dict = {
        str(cookie.get("name") or ""): str(cookie.get("value") or "")
        for cookie in selected
        if str(cookie.get("name") or "").strip()
    }
    if cookie_dict.get("steamLoginSecure"):
        return True, "browser_auto_ok", cookie_dict
    if "check your password and account name" in body_hint:
        return False, "wrong_creds", {}
    return False, "browser_auto_no_cookie", {}


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
        if (
            not ok
            and err_code in {"ip_blocked", "steam_auth_session_unavailable", "steam_auth_throttled"}
            and steam_guard_dict
            and steam_guard_dict.get("shared_secret")
        ):
            ok, err_code, cookie_dict = _do_playwright_login(
                account.username,
                account.password,
                steam_guard_dict["shared_secret"],
                Path(store.storage_dir) / "playwright_steam" / account.id,
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
