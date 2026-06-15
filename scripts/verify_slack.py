"""Production verification script for F1.2 SlackNotifier.

Designed to run as a Cloud Run Job (`product-system-verify-slack`) so that
the real production Cloud Run service's env is NEVER modified. This script
constructs an isolated SlackNotifier from the supplied --mode and reports
what happened, so the operator can verify all three documented branches:

  - --mode=empty   (no-op when SLACK_WEBHOOK_URL is unset, per D-3 initial state)
  - --mode=invalid (HTTP failure is swallowed and logged, not raised)
  - --mode=real    (delivered to the supplied --webhook-url; for use only
                    after D-3 client provides a real Incoming Webhook URL)

Exit codes:
  0 — behavior matched the chosen mode's expectation
  1 — behavior did NOT match (e.g. --mode=empty but a send happened anyway)
  2 — usage error

Usage (Cloud Run Job — recommended):
    gcloud run jobs execute product-system-verify-slack \\
        --args=--mode=empty --wait

    gcloud run jobs execute product-system-verify-slack \\
        --args=--mode=invalid --wait

    # Only AFTER D-3 client provides SLACK_WEBHOOK_URL:
    gcloud run jobs execute product-system-verify-slack \\
        --args=--mode=real,\\
               --webhook-url=https://hooks.slack.com/services/T/B/X --wait
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from typing import Literal

from app.logging import configure_logging, get_logger
from app.notifications.slack import SlackNotifier

log = get_logger(__name__)

Mode = Literal["empty", "invalid", "real"]

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_USAGE = 2

# Invalid URL we use for --mode=invalid. The host resolves but the path is
# guaranteed to 404 — exercises the "HTTP failure is swallowed" branch
# without sending anything visible to a real Slack workspace.
_INVALID_TEST_URL = "https://hooks.slack.com/services/T_TEST_VERIFY/B_TEST_VERIFY/INVALID"


@dataclass(frozen=True, slots=True)
class Args:
    mode: Mode
    webhook_url: str  # only used when mode == "real"


def parse_args(argv: list[str] | None = None) -> Args:
    parser = argparse.ArgumentParser(
        prog="verify_slack",
        description="Verify SlackNotifier behavior in production without "
        "modifying the Cloud Run service env.",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("empty", "invalid", "real"),
        help="Which branch of SlackNotifier to exercise.",
    )
    parser.add_argument(
        "--webhook-url",
        default="",
        help="Real Slack Incoming Webhook URL. Required only with --mode=real.",
    )
    parsed = parser.parse_args(argv)
    if parsed.mode == "real" and not parsed.webhook_url:
        parser.error("--mode=real requires --webhook-url")
    return Args(mode=parsed.mode, webhook_url=parsed.webhook_url)


def build_notifier(args: Args) -> SlackNotifier:
    """Construct a SlackNotifier in isolation — do NOT use the cached
    process-wide get_slack_notifier(), because that one is bound to the
    Cloud Run service env (no URL in production today)."""
    if args.mode == "empty":
        return SlackNotifier(webhook_url="", min_level="error")
    if args.mode == "invalid":
        return SlackNotifier(webhook_url=_INVALID_TEST_URL, min_level="error")
    # mode == "real"
    return SlackNotifier(webhook_url=args.webhook_url, min_level="error")


async def run_verify(args: Args) -> int:
    notifier = build_notifier(args)
    delivered = await notifier.notify(
        level="error",
        title=f"verify_slack mode={args.mode}",
        message=(
            "This is a production verification message. If you can see it, "
            "the SlackNotifier delivery path is healthy. Ignore in real ops."
        ),
        fields=[
            ("mode", args.mode),
            ("source", "scripts/verify_slack.py"),
            ("expectation", expectation_for(args.mode)),
        ],
    )
    actual = "delivered" if delivered else "skipped_or_failed"
    expected = expected_outcome(args.mode)
    sys.stdout.write(f'{{"mode":"{args.mode}","expected":"{expected}","actual":"{actual}"}}\n')
    if actual == expected:
        return EXIT_OK
    return EXIT_MISMATCH


def expectation_for(mode: Mode) -> str:
    if mode == "empty":
        return "no HTTP request, no delivery"
    if mode == "invalid":
        return "HTTP request fails (4xx/5xx) and is swallowed"
    return "delivered to Slack channel"


def expected_outcome(mode: Mode) -> str:
    """What we expect `notify()` to return for this mode.

    `empty` and `invalid` both return False (skipped or HTTP-failed); `real`
    returns True when the webhook accepts the payload. The distinction
    between empty and invalid is visible in the logs (`slack.skip_no_url`
    vs `slack.http_error`), not in the boolean return.
    """
    if mode == "real":
        return "delivered"
    return "skipped_or_failed"


def main(argv: list[str] | None = None) -> int:
    configure_logging("INFO")
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or EXIT_USAGE)
    try:
        return asyncio.run(run_verify(args))
    except Exception:
        log.exception("verify_slack.unexpected_error", mode=args.mode)
        return EXIT_MISMATCH


if __name__ == "__main__":
    sys.exit(main())
