from __future__ import annotations

from pathlib import Path

from cs2_assistant.accounts.store import Account, AccountStore
from cs2_assistant.accounts.steam_auth import _extract_creds_from_cookie_dict


def _profile_dir(store: AccountStore, account_id: str) -> Path:
    return store.storage_dir / "playwright_steam" / account_id


def relogin_with_browser(
    store: AccountStore,
    *,
    account_id: str | None = None,
) -> tuple[bool, str, Account | None]:
    account = store.get_account(account_id) if account_id else store.get_current()
    if account is None:
        return False, "no_account", None

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return False, f"playwright_unavailable: {exc}", account

    profile_dir = _profile_dir(store, account.id)
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://store.steampowered.com/login/", wait_until="domcontentloaded", timeout=30000)
            input("请在弹出的浏览器中完成 Steam 登录，完成后回到终端按回车继续...")
            try:
                page.goto("https://steamcommunity.com/market/", wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            browser_cookies = context.cookies()
        finally:
            try:
                context.close()
            except Exception:
                pass

    # 取全部 Steam 相关 cookie（含 steamMachineAuth）
    selected = [
        cookie for cookie in browser_cookies
        if "steam" in (cookie.get("domain") or "").lower()
    ]
    if not selected:
        selected = list(browser_cookies)
    cookie_dict = {
        str(cookie.get("name") or ""): str(cookie.get("value") or "")
        for cookie in selected
        if str(cookie.get("name") or "").strip()
    }
    cookie_str, _, steam_id = _extract_creds_from_cookie_dict(cookie_dict)
    if not cookie_str or "steamLoginSecure" not in cookie_dict:
        return False, "missing_cookies", account

    updated = store.update_account(
        account.id,
        cookies=cookie_str,
        steam_id64=steam_id or account.steam_id64,
    )
    return True, "browser_ok", updated or store.get_account(account.id)
