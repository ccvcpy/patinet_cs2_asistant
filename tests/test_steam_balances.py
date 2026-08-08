from __future__ import annotations

import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cs2_assistant.accounts import Account
from cs2_assistant.config import Settings
from cs2_assistant.services.steam_balances import (
    load_steam_account_balances,
    refresh_steam_account_balances,
    update_steam_account_balance_snapshot,
)


class FakeAccountStore:
    def list_accounts(self) -> list[Account]:
        return [
            Account(id="one", name="account-one", steam_id64="76561190000000001", cookies="a=1"),
            Account(id="two", name="account-two", steam_id64="76561190000000002", cookies="b=2"),
            Account(id="none", name="without-cookie", steam_id64="76561190000000003"),
        ]


class FakeSteampyClient:
    calls: list[tuple[str, bool]] = []

    def __init__(self, **kwargs) -> None:
        self.steam_id = json.loads(kwargs["steam_guard"])["steamid"]
        self.login_cookies = kwargs["login_cookies"]
        self._session = self

    def get(self, *_args, **_kwargs):
        currency_id = 23 if self.steam_id.endswith("1") else 1
        balance = "1234" if self.steam_id.endswith("1") else "500"
        delayed = "123" if self.steam_id.endswith("1") else "0"
        wallet_info = json.dumps(
            {
                "wallet_balance": balance,
                "wallet_delayed_balance": delayed,
                "wallet_currency": currency_id,
            }
        )
        return type("Response", (), {"text": f"var g_rgWalletInfo = {wallet_info};"})()

    def get_wallet_balance(self, *, on_hold: bool = False) -> Decimal:
        self.calls.append((self.steam_id, on_hold))
        self._session.get("https://steamcommunity.com/market")
        if self.steam_id.endswith("1"):
            return Decimal("1.23") if on_hold else Decimal("12.34")
        return Decimal("0.00") if on_hold else Decimal("5.00")


class FailingSteampyClient(FakeSteampyClient):
    def get_wallet_balance(self, *, on_hold: bool = False) -> Decimal:
        raise RuntimeError("Steam unavailable")


class SteamBalancesTestCase(unittest.TestCase):
    def test_single_wallet_update_merges_into_shared_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "balances.json"
            refresh_steam_account_balances(
                Settings(),
                account_store=FakeAccountStore(),
                client_factory=FakeSteampyClient,
                snapshot_path=snapshot_path,
            )

            account = FakeAccountStore().list_accounts()[0]
            payload = update_steam_account_balance_snapshot(
                Settings(),
                account=account,
                wallet={
                    "balance": 9.87,
                    "delayed_balance": 0.45,
                    "currency": "CNY",
                    "currency_id": 23,
                },
                snapshot_path=snapshot_path,
            )

            self.assertEqual(2, len(payload["accounts"]))
            self.assertEqual(9.87, payload["accounts"][0]["realBalance"])
            self.assertEqual(0.45, payload["accounts"][0]["pendingBalance"])
            self.assertEqual(10.32, payload["accounts"][0]["totalBalance"])
            self.assertEqual(5.0, payload["accounts"][1]["realBalance"])
            self.assertEqual("live", payload["source"])

    def test_refresh_uses_steampy_for_both_wallet_fields_and_saves_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "balances.json"
            FakeSteampyClient.calls = []
            payload = refresh_steam_account_balances(
                Settings(),
                account_store=FakeAccountStore(),
                client_factory=FakeSteampyClient,
                snapshot_path=snapshot_path,
            )

            self.assertEqual(["account-one", "account-two"], [row["account"] for row in payload["accounts"]])
            self.assertEqual(2, payload["summary"]["accountCount"])
            self.assertEqual(2, payload["summary"]["successfulCount"])
            self.assertIsNone(payload["summary"]["totalBalance"])
            self.assertEqual(["CNY", "USD"], [row["currency"] for row in payload["summary"]["currencies"]])
            self.assertEqual(
                [
                    ("76561190000000001", False),
                    ("76561190000000002", False),
                ],
                FakeSteampyClient.calls,
            )
            self.assertTrue(snapshot_path.exists())

            cached = load_steam_account_balances(
                Settings(),
                account_store=FakeAccountStore(),
                snapshot_path=snapshot_path,
            )
            self.assertTrue(cached["hasSnapshot"])
            self.assertEqual("cache", cached["source"])
            self.assertIsNone(cached["summary"]["totalBalance"])
            self.assertEqual(23, cached["accounts"][0]["currencyId"])
            self.assertEqual(1, cached["accounts"][1]["currencyId"])

    def test_failed_refresh_preserves_previous_successful_values_as_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "balances.json"
            refresh_steam_account_balances(
                Settings(),
                account_store=FakeAccountStore(),
                client_factory=FakeSteampyClient,
                snapshot_path=snapshot_path,
            )
            payload = refresh_steam_account_balances(
                Settings(),
                account_store=FakeAccountStore(),
                client_factory=FailingSteampyClient,
                snapshot_path=snapshot_path,
            )

            self.assertEqual(0, payload["summary"]["successfulCount"])
            self.assertEqual("error", payload["accounts"][0]["status"])
            self.assertTrue(payload["accounts"][0]["stale"])
            self.assertEqual(12.34, payload["accounts"][0]["realBalance"])


if __name__ == "__main__":
    unittest.main()
