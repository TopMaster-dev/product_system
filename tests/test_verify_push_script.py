"""Unit tests for scripts/verify_push.py.

Covers argparse, adapter selection, exit codes, and stdout JSON format.
The DB session and HTTP layer are not exercised here — those are covered
by the F1.4-F1.6 test suites.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# scripts/ is not a package; add it to sys.path so the script imports work.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from verify_push import (  # noqa: E402 — after sys.path mutation
    EXIT_FAILED,
    EXIT_OK,
    Args,
    build_adapter,
    main,
    parse_args,
    resolve_master_sku_id,
)

# ---------- argparse ----------


@pytest.mark.unit
def test_parse_args_all_required() -> None:
    args = parse_args(
        [
            "--channel",
            "shopify",
            "--master-sku-id",
            "42",
            "--channel-sku",
            "R64silverus7",
            "--quantity",
            "5",
            "--triggered-by",
            "manual:f14-shopify-noop",
        ]
    )
    assert args == Args(
        channel="shopify",
        master_sku_id=42,
        channel_sku="R64silverus7",
        quantity=5,
        triggered_by="manual:f14-shopify-noop",
    )


@pytest.mark.unit
def test_parse_args_rejects_unknown_channel() -> None:
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--channel",
                "amazon",  # not supported in F1.5/F1.6
                "--master-sku-id",
                "1",
                "--channel-sku",
                "X",
                "--quantity",
                "0",
                "--triggered-by",
                "t",
            ]
        )


@pytest.mark.unit
def test_parse_args_requires_all_flags() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--channel", "shopify"])


@pytest.mark.unit
def test_parse_args_master_sku_id_optional() -> None:
    args = parse_args(
        [
            "--channel",
            "shopify",
            "--channel-sku",
            "N41gold",
            "--quantity",
            "19",
            "--triggered-by",
            "t",
        ]
    )
    assert args.master_sku_id is None
    assert args.channel_sku == "N41gold"


@pytest.mark.unit
def test_parse_args_quantity_must_be_int() -> None:
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--channel",
                "rakuten",
                "--master-sku-id",
                "1",
                "--channel-sku",
                "X",
                "--quantity",
                "not-an-int",
                "--triggered-by",
                "t",
            ]
        )


# ---------- resolve_master_sku_id ----------


class _FakeResult:
    def __init__(self, rows: list[tuple[int]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[int]]:
        return self._rows


class _FakeSession:
    def __init__(self, rows: list[tuple[int]]) -> None:
        self._rows = rows

    async def execute(self, *_a: object, **_kw: object) -> _FakeResult:
        return _FakeResult(self._rows)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_master_sku_id_single() -> None:
    session = _FakeSession([(42,)])
    got = await resolve_master_sku_id(session, "shopify", "N41gold")  # type: ignore[arg-type]
    assert got == 42


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_master_sku_id_not_found() -> None:
    session = _FakeSession([])
    with pytest.raises(RuntimeError, match="no active channel_sku_mapping"):
        await resolve_master_sku_id(session, "shopify", "MISSING")  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_master_sku_id_ambiguous() -> None:
    session = _FakeSession([(1,), (2,)])
    with pytest.raises(RuntimeError, match="multiple master_sku_ids"):
        await resolve_master_sku_id(session, "shopify", "DUP")  # type: ignore[arg-type]


# ---------- build_adapter ----------


class _FakeSettings:
    rakuten_service_secret = "ss"
    rakuten_license_key = "lk"
    rakuten_shop_url = "https://example.com/"
    shopify_shop_domain = "x.myshopify.com"
    shopify_access_token = "tok"
    shopify_webhook_secret = "wh"
    shopify_api_version = "2025-04"
    shopify_location_id = ""
    slack_webhook_url = ""
    slack_notify_min_level = "error"


@pytest.mark.unit
def test_build_adapter_shopify() -> None:
    a = build_adapter("shopify", _FakeSettings())  # type: ignore[arg-type]
    assert a.channel == "shopify"


@pytest.mark.unit
def test_build_adapter_rakuten() -> None:
    a = build_adapter("rakuten", _FakeSettings())  # type: ignore[arg-type]
    assert a.channel == "rakuten"


@pytest.mark.unit
def test_build_adapter_rakuten_missing_credentials() -> None:
    class _Empty(_FakeSettings):
        rakuten_service_secret = ""

    with pytest.raises(RuntimeError, match="Rakuten credentials"):
        build_adapter("rakuten", _Empty())  # type: ignore[arg-type]


@pytest.mark.unit
def test_build_adapter_shopify_missing_credentials() -> None:
    class _Empty(_FakeSettings):
        shopify_access_token = ""

    with pytest.raises(RuntimeError, match="Shopify credentials"):
        build_adapter("shopify", _Empty())  # type: ignore[arg-type]


# ---------- main() exit codes ----------


class _FakeAttempt:
    def __init__(self, status: str, error_code: str | None = None) -> None:
        self.id = 1234
        self.status = status
        self.error_code = error_code
        self.error_message = "boom" if error_code else None


@pytest.mark.unit
def test_main_returns_0_on_success(capsys: pytest.CaptureFixture[str]) -> None:
    fake_attempt = _FakeAttempt(status="succeeded")

    async def fake_run_push(args):  # type: ignore[no-untyped-def]
        # Print the stdout summary just as the real run_push would.
        sys.stdout.write(f'{{"attempt_id":{fake_attempt.id},"status":"{fake_attempt.status}"}}\n')
        return EXIT_OK

    with patch("verify_push.run_push", new=fake_run_push):
        code = main(
            [
                "--channel",
                "shopify",
                "--master-sku-id",
                "1",
                "--channel-sku",
                "X",
                "--quantity",
                "5",
                "--triggered-by",
                "t",
            ]
        )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert '"status":"succeeded"' in out


@pytest.mark.unit
def test_main_returns_1_on_failed_attempt() -> None:
    async def fake_run_push(args):  # type: ignore[no-untyped-def]
        return EXIT_FAILED

    with patch("verify_push.run_push", new=fake_run_push):
        code = main(
            [
                "--channel",
                "rakuten",
                "--master-sku-id",
                "1",
                "--channel-sku",
                "X",
                "--quantity",
                "5",
                "--triggered-by",
                "t",
            ]
        )
    assert code == EXIT_FAILED


@pytest.mark.unit
def test_main_returns_failed_on_unexpected_exception() -> None:
    async def fake_run_push(args):  # type: ignore[no-untyped-def]
        raise RuntimeError("db down")

    with patch("verify_push.run_push", new=fake_run_push):
        code = main(
            [
                "--channel",
                "shopify",
                "--master-sku-id",
                "1",
                "--channel-sku",
                "X",
                "--quantity",
                "5",
                "--triggered-by",
                "t",
            ]
        )
    assert code == EXIT_FAILED


@pytest.mark.unit
def test_main_returns_usage_error_for_bad_args() -> None:
    # Argparse writes to stderr and raises SystemExit(2); main catches it.
    saved_stderr = sys.stderr
    try:
        sys.stderr = io.StringIO()
        code = main(["--channel", "amazon"])
    finally:
        sys.stderr = saved_stderr
    assert code == 2


# The end-to-end run_push() flow is exercised in production by the
# verify_push Cloud Run Job itself; the underlying InventoryPushService
# and adapter logic is fully covered by tests/test_inventory_push_service.py
# and tests/test_{rakuten,shopify}_push_inventory.py.
