"""Authenticating `/internal/jobs/*` in the application, not just at the edge.

WHY THIS EXISTS

Cloud Run is deployed `--no-allow-unauthenticated`, but `allUsers` holds
`roles/run.invoker` — added deliberately (infra/terraform/run.tf) so Shopify
webhooks, which cannot carry a GCP identity token, can reach the service. The
webhook endpoint verifies its own HMAC and rejects anything else, so that part
is sound.

The internal job endpoints were collateral. They had no application-level auth
of any kind, relying entirely on Cloud Run IAM — which, with allUsers granted,
admits everyone. `/internal/jobs/tasks/run` dispatches a registered handler with
a caller-supplied payload; `/internal/jobs/bundle-push` writes stock levels to
Shopify.

WHAT IS VERIFIED

Both Cloud Scheduler (`oidc_token` in scheduler.tf) and Cloud Tasks
(`CLOUD_TASKS_INVOKER_SA`) already send a Google-signed ID token for the app's
service account. Nothing needs to change on the caller side; the token was
always there, and Cloud Run simply stopped checking it once allUsers was
granted. So: the signature is verified against Google's keys, and the token's
`email` must be the expected service account.

The audience is deliberately NOT checked. Cloud Scheduler defaults it to the
full target URL, so it differs per endpoint, and pinning a list of URLs here
would silently reject a job the day someone adds one. Signature plus service
account is the property that matters: only Google can mint it, and only for
that identity.

THREE MODES, DEFAULTING TO THE ONE THAT CANNOT BREAK ANYTHING

Turning verification on in one step risks 401ing every scheduled job at once —
order ingestion included — on a service whose failures this project has already
been slow to notice. `audit` logs the verdict it WOULD have reached and lets the
request through, so production can be observed agreeing before `enforce` is set.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException, Request, status

from app.config import get_settings
from app.logging import get_logger

log = get_logger(__name__)

#: Google's issuers for ID tokens. `accounts.google.com` is the legacy form and
#: is still emitted, so both are accepted.
_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})

AUTH_OFF = "off"
AUTH_AUDIT = "audit"
AUTH_ENFORCE = "enforce"


class _Rejected(Exception):
    """Why a token was not accepted, in terms safe to log."""


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _Rejected("no bearer token")
    return token.strip()


def _verify_sync(token: str, expected_sa: str) -> dict[str, Any]:
    """Blocking signature check. Run off the event loop by the caller.

    google-auth caches Google's signing certificates in process, so this is a
    network call only on the first request after a cold start.
    """
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
    except ImportError as exc:  # pragma: no cover - the gcp extra is installed in the image
        raise _Rejected(f"google-auth unavailable: {exc}") from exc

    try:
        # audience=None skips the audience check; see the module docstring.
        # google-auth ships no stubs, so this is untyped as far as mypy is
        # concerned; the return is a claims dict.
        claims: dict[str, Any] = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
            token, google_requests.Request(), audience=None
        )
    except Exception as exc:
        raise _Rejected(f"invalid token: {type(exc).__name__}") from exc

    if claims.get("iss") not in _ISSUERS:
        raise _Rejected("unexpected issuer")
    email = claims.get("email") or ""
    if not claims.get("email_verified"):
        raise _Rejected("email not verified")
    if email != expected_sa:
        # The address is our own service account, not a user's, so it is safe
        # to log — and it is the one fact that makes a rejection diagnosable.
        raise _Rejected(f"unexpected caller: {email}")
    return claims


async def require_internal_caller(request: Request) -> None:
    """FastAPI dependency guarding the internal job router."""
    settings = get_settings()
    mode = (settings.internal_jobs_auth_mode or AUTH_AUDIT).lower()
    if mode == AUTH_OFF:
        return

    expected = settings.internal_jobs_allowed_sa or settings.cloud_tasks_invoker_sa
    if not expected:
        # Nothing to compare against. Enforcing would reject every caller,
        # including the schedulers, so this degrades to a loud no-op instead.
        log.error("internal.auth.no_expected_sa", path=request.url.path, mode=mode)
        return

    try:
        token = _bearer(request)
        await asyncio.to_thread(_verify_sync, token, expected)
    except _Rejected as exc:
        if mode == AUTH_ENFORCE:
            log.warning("internal.auth.rejected", path=request.url.path, reason=str(exc))
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="internal endpoints require a Google-signed identity token",
            ) from exc
        # audit: say what would have happened and let it through.
        log.warning("internal.auth.would_reject", path=request.url.path, reason=str(exc))
        return

    if mode == AUTH_AUDIT:
        # INFO while auditing, on purpose. The point of the audit period is to
        # see verification AGREEING before it is allowed to reject anything, and
        # the absence of a rejection log cannot distinguish "every token passed"
        # from "the dependency never ran". Silence read as success is the exact
        # shape of the three outages this service has already had.
        log.info("internal.auth.ok", path=request.url.path, mode=mode)
        return
    log.debug("internal.auth.ok", path=request.url.path, mode=mode)


__all__ = ["AUTH_AUDIT", "AUTH_ENFORCE", "AUTH_OFF", "require_internal_caller"]
