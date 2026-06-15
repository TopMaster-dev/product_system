"""Unit tests for scripts/verify_slack.py."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from verify_slack import (  # noqa: E402
    EXIT_MISMATCH,
    EXIT_OK,
    Args,
    build_notifier,
    main,
    parse_args,
    run_verify,
)

# ---------- argparse ----------


@pytest.mark.unit
def test_parse_args_empty_mode_no_url_needed() -> None:
    args = parse_args(["--mode", "empty"])
    assert args == Args(mode="empty", webhook_url="")


@pytest.mark.unit
def test_parse_args_invalid_mode_no_url_needed() -> None:
    args = parse_args(["--mode", "invalid"])
    assert args == Args(mode="invalid", webhook_url="")


@pytest.mark.unit
def test_parse_args_real_mode_requires_url() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--mode", "real"])


@pytest.mark.unit
def test_parse_args_real_mode_with_url() -> None:
    args = parse_args(["--mode", "real", "--webhook-url", "https://hooks.slack.com/x"])
    assert args.mode == "real"
    assert args.webhook_url == "https://hooks.slack.com/x"


@pytest.mark.unit
def test_parse_args_rejects_unknown_mode() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--mode", "bogus"])


# ---------- build_notifier ----------


@pytest.mark.unit
def test_build_notifier_empty_mode() -> None:
    n = build_notifier(Args(mode="empty", webhook_url=""))
    assert n.is_enabled is False


@pytest.mark.unit
def test_build_notifier_invalid_mode() -> None:
    n = build_notifier(Args(mode="invalid", webhook_url=""))
    assert n.is_enabled is True
    # The invalid URL is used so it shouldn't accidentally hit a real workspace.
    assert "T_TEST_VERIFY" in n._webhook_url  # type: ignore[attr-defined]


@pytest.mark.unit
def test_build_notifier_real_mode_uses_supplied_url() -> None:
    n = build_notifier(Args(mode="real", webhook_url="https://hooks.slack.com/real/x"))
    assert n._webhook_url == "https://hooks.slack.com/real/x"  # type: ignore[attr-defined]


# ---------- run_verify exit codes ----------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_verify_empty_mode_exits_ok(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = await run_verify(Args(mode="empty", webhook_url=""))
    # empty: notify() returns False (no URL), expected outcome is
    # 'skipped_or_failed' — actual matches expected -> EXIT_OK
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert '"mode":"empty"' in out
    assert '"expected":"skipped_or_failed"' in out
    assert '"actual":"skipped_or_failed"' in out


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_verify_invalid_mode_swallows_http_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When the URL is invalid the notifier should get a non-2xx, return
    False, and run_verify should report 'skipped_or_failed' = expected."""
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(404, text="not found")

    transport = httpx.MockTransport(handler)
    original_async_client = httpx.AsyncClient

    def patched_client(*a, **kw):  # type: ignore[no-untyped-def]
        return original_async_client(transport=transport, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)

    code = await run_verify(Args(mode="invalid", webhook_url=""))
    assert code == EXIT_OK
    assert len(captured) == 1
    out = capsys.readouterr().out
    assert '"actual":"skipped_or_failed"' in out


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_verify_real_mode_delivered(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    original_async_client = httpx.AsyncClient

    def patched_client(*a, **kw):  # type: ignore[no-untyped-def]
        return original_async_client(transport=transport, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)

    code = await run_verify(Args(mode="real", webhook_url="https://hooks.slack.com/services/T/B/X"))
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert '"actual":"delivered"' in out


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_verify_real_mode_unexpected_failure_returns_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If we asked for delivered but the webhook returns 5xx the notifier
    returns False — that's a real verification failure, exit MISMATCH."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    transport = httpx.MockTransport(handler)
    original_async_client = httpx.AsyncClient

    def patched_client(*a, **kw):  # type: ignore[no-untyped-def]
        return original_async_client(transport=transport, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)

    code = await run_verify(Args(mode="real", webhook_url="https://hooks.slack.com/services/T/B/X"))
    assert code == EXIT_MISMATCH


# ---------- main() ----------


@pytest.mark.unit
def test_main_usage_error_returns_2() -> None:
    saved = sys.stderr
    try:
        sys.stderr = io.StringIO()
        code = main(["--mode", "real"])  # missing --webhook-url
    finally:
        sys.stderr = saved
    assert code == 2
