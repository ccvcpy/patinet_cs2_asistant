from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

from cs2_assistant.services.profit_trade_logging import (
    PROFIT_TRADE_LOG_DIR_ENV,
    reset_profit_trade_event_loggers,
)


_COLLECTION_LOG_DIRECTORY: tempfile.TemporaryDirectory[str] | None = None
_PREVIOUS_LOG_DIRECTORY: str | None = None


def pytest_configure(config: pytest.Config) -> None:
    """Install an isolated default before pytest imports test modules."""

    del config
    global _COLLECTION_LOG_DIRECTORY, _PREVIOUS_LOG_DIRECTORY
    if _COLLECTION_LOG_DIRECTORY is not None:
        return
    _PREVIOUS_LOG_DIRECTORY = os.environ.get(PROFIT_TRADE_LOG_DIR_ENV)
    _COLLECTION_LOG_DIRECTORY = tempfile.TemporaryDirectory(
        prefix="cs2-profit-trade-pytest-",
    )
    os.environ[PROFIT_TRADE_LOG_DIR_ENV] = _COLLECTION_LOG_DIRECTORY.name
    reset_profit_trade_event_loggers()


@pytest.fixture(autouse=True)
def isolate_default_profit_trade_logger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Give every test its own default JSONL directory and broker singleton."""

    log_dir = tmp_path / "profit-trade-logs"
    monkeypatch.setenv(PROFIT_TRADE_LOG_DIR_ENV, str(log_dir))
    reset_profit_trade_event_loggers()
    yield log_dir
    reset_profit_trade_event_loggers()


def pytest_unconfigure(config: pytest.Config) -> None:
    del config
    global _COLLECTION_LOG_DIRECTORY, _PREVIOUS_LOG_DIRECTORY
    reset_profit_trade_event_loggers()
    if _PREVIOUS_LOG_DIRECTORY is None:
        os.environ.pop(PROFIT_TRADE_LOG_DIR_ENV, None)
    else:
        os.environ[PROFIT_TRADE_LOG_DIR_ENV] = _PREVIOUS_LOG_DIRECTORY
    if _COLLECTION_LOG_DIRECTORY is not None:
        _COLLECTION_LOG_DIRECTORY.cleanup()
    _COLLECTION_LOG_DIRECTORY = None
    _PREVIOUS_LOG_DIRECTORY = None
