"""Read-only: does the configured Rakuten RMS credential pair authenticate?

Written on 2026-09-22, after Rakuten order polling was found returning 401 from
`searchOrder` for 24.7 days. There was no way to test the credentials without
waiting for the scheduler, which meant a rotation could not be confirmed until
the next tick — and, if it was still wrong, would look identical to the outage
it was meant to fix.

WHAT IT DOES AND DOES NOT PRINT

It reports whether the call was accepted, and on failure what RMS said. It never
prints the secret, the licence key, or the base64 Authorization token. A support
transcript is exactly where a credential gets pasted by accident, and this is the
tool someone reaches for while debugging one.

RMS auth is `Authorization: ESA base64(serviceSecret:licenseKey)`, so a 401
cannot say WHICH of the two is wrong — the pair is verified as a unit. Rotating
only the licence key while the service secret has also changed fails exactly the
same way as not rotating at all, which the message below says out loud.

Read-only. It performs one order SEARCH over a one-hour window and writes
nothing, to the database or to Rakuten.

    powershell -File scripts/run_cli.ps1 -Cli inspect_rakuten_auth -WithRakuten
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta

import httpx

from app.adapters import RakutenAdapter
from app.config import get_settings
from app.logging import configure_logging, get_logger

log = get_logger(__name__)

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_UNCONFIGURED = 2


def _fingerprint(value: str) -> str:
    """Enough to tell two credentials apart, not enough to use one.

    A rotation goes wrong in two ways that look the same from outside: the new
    value never reached the service, or it reached it and is also wrong. The
    prefix plus the length separates those without disclosing the secret.
    """
    cleaned = value.strip()
    if not cleaned:
        return "(未設定)"
    head = cleaned[:2]
    return f"{head}…/{len(cleaned)}文字"


async def run(*, lookback_minutes: int = 60) -> int:
    settings = get_settings()
    secret, key = settings.rakuten_service_secret, settings.rakuten_license_key

    print("\n--- 楽天RMS 認証確認 ---")
    print(f"  service_secret : {_fingerprint(secret)}")
    print(f"  license_key    : {_fingerprint(key)}")

    if not secret or not key:
        print("\n  認証情報が設定されていません。")
        print("  -WithRakuten を付けて実行するか、Secret Manager の値をご確認ください。")
        return EXIT_UNCONFIGURED

    # Whitespace is stripped by the adapter, but a value that NEEDED stripping is
    # worth saying: a trailing CR from a Windows copy corrupts the base64 token
    # and has caused a production auth failure on this project before.
    for label, value in (("service_secret", secret), ("license_key", key)):
        if value != value.strip():
            print(f"  ⚠ {label} に前後の空白または改行が含まれています。除去して送信します")

    until = datetime.now(UTC)
    since = until - timedelta(minutes=lookback_minutes)

    adapter = RakutenAdapter(
        service_secret=secret,
        license_key=key,
        shop_url=settings.rakuten_shop_url or None,
    )
    try:
        orders = await adapter.fetch_orders(since=since)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        print(f"\n  認証されませんでした: HTTP {status}")
        if status == 401:
            print("")
            print("  401 は serviceSecret と licenseKey の組が拒否された、の意味です。")
            print("  どちらが誤っているかはRMS側からは区別できません。")
            print("  licenseKey のみ更新された場合でも、serviceSecret が併せて変更されていれば")
            print("  同じ 401 になります。両方の値をご確認ください。")
        body = (exc.response.text or "")[:300]
        if body:
            print(f"\n  RMS応答: {body}")
        log.error("rakuten_auth.rejected", status=status)
        return EXIT_REJECTED
    except Exception as exc:
        print(f"\n  接続に失敗しました: {exc!r}")
        log.exception("rakuten_auth.failed")
        return EXIT_REJECTED
    finally:
        close = getattr(adapter, "aclose", None) or getattr(adapter, "close", None)
        if close:
            await close()

    print(f"\n  認証に成功しました。直近{lookback_minutes}分の受注: {len(orders)}件")
    print("  0件でも異常ではありません。認証が通った時点でこの確認の目的は達しています。")
    log.info("rakuten_auth.ok", orders=len(orders), lookback_minutes=lookback_minutes)
    return EXIT_OK


def main() -> None:
    p = argparse.ArgumentParser(
        description="Check whether the Rakuten RMS credentials authenticate"
    )
    p.add_argument("--lookback-minutes", type=int, default=60)
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(lookback_minutes=args.lookback_minutes)))


if __name__ == "__main__":
    main()
