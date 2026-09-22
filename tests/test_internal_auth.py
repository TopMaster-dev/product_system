"""Authenticating /internal/jobs/* in the application.

`allUsers` holds run.invoker so Shopify webhooks can reach the service, which
left the internal job endpoints reachable by anyone — `tasks/run` dispatches a
caller-supplied payload, `bundle-push` writes stock to Shopify. Cloud Scheduler
and Cloud Tasks were already sending a Google-signed token for the app's service
account; nobody was checking it.

Two failure directions, and they are not symmetric.

Letting a stranger through is the vulnerability. Rejecting the schedulers is an
outage across every scheduled job at once — order ingestion included — on a
system that has now twice failed to notice exactly that. So the default mode
admits the request and logs the verdict it WOULD have reached, and the
misconfigurations below (no expected SA, absent library) degrade the same way
rather than locking the door with nobody holding a key.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from app.api import auth_internal
from app.api.auth_internal import (
    AUTH_AUDIT,
    AUTH_ENFORCE,
    AUTH_OFF,
    require_internal_caller,
)
from app.config import Settings

pytestmark = pytest.mark.unit

CALLER = "product-system-app@inventory-496204.iam.gserviceaccount.com"


class _Request:
    """The two things the dependency reads off a request."""

    def __init__(self, authorization: str | None = None, path: str = "/internal/jobs/x") -> None:
        self.headers = {"authorization": authorization} if authorization else {}
        self.url = type("U", (), {"path": path})()


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "internal_jobs_auth_mode": AUTH_ENFORCE,
        "internal_jobs_allowed_sa": CALLER,
    }
    return Settings(**{**base, **kw})


@pytest.fixture
def _accepts(monkeypatch):
    """Stand in for google-auth, accepting one token and rejecting the rest."""

    def verify(token: str, expected_sa: str) -> dict[str, Any]:
        if token != "good":
            raise auth_internal._Rejected("invalid token: ValueError")
        return {
            "iss": "https://accounts.google.com",
            "email": expected_sa,
            "email_verified": True,
        }

    monkeypatch.setattr(auth_internal, "_verify_sync", verify)


# --- enforce --------------------------------------------------------------


async def test_a_valid_scheduler_token_is_admitted(monkeypatch, _accepts) -> None:
    monkeypatch.setattr(auth_internal, "get_settings", _settings)
    await require_internal_caller(_Request("Bearer good"))  # type: ignore[arg-type]


async def test_no_authorization_header_is_rejected(monkeypatch, _accepts) -> None:
    """The shape an internet caller arrives in — the whole point of the change."""
    monkeypatch.setattr(auth_internal, "get_settings", _settings)
    with pytest.raises(HTTPException) as raised:
        await require_internal_caller(_Request())  # type: ignore[arg-type]
    assert raised.value.status_code == 401


async def test_a_forged_token_is_rejected(monkeypatch, _accepts) -> None:
    monkeypatch.setattr(auth_internal, "get_settings", _settings)
    with pytest.raises(HTTPException):
        await require_internal_caller(_Request("Bearer nonsense"))  # type: ignore[arg-type]


async def test_a_non_bearer_scheme_is_rejected(monkeypatch, _accepts) -> None:
    monkeypatch.setattr(auth_internal, "get_settings", _settings)
    with pytest.raises(HTTPException):
        await require_internal_caller(_Request("Basic good"))  # type: ignore[arg-type]


# --- claim checks ---------------------------------------------------------


def _claims(**kw: Any) -> dict[str, Any]:
    base = {"iss": "https://accounts.google.com", "email": CALLER, "email_verified": True}
    return {**base, **kw}


def _patch_google(monkeypatch, claims: dict[str, Any]) -> None:
    """Replace ONLY the signature step, on the real google-auth function.

    The claim checks below then run against `_verify_sync` itself. An earlier
    version of this helper re-implemented those checks in the test, which meant
    the assertions passed while saying nothing about the code that ships.
    """
    monkeypatch.setattr(
        "google.oauth2.id_token.verify_oauth2_token",
        lambda *args, **kwargs: claims,
    )


async def test_a_token_for_another_service_account_is_rejected(monkeypatch) -> None:
    """A Google-signed token is not enough on its own — anyone with a GCP
    project can mint one for their own service account."""
    monkeypatch.setattr(auth_internal, "get_settings", _settings)
    _patch_google(monkeypatch, _claims(email="someone-else@evil.iam.gserviceaccount.com"))
    with pytest.raises(HTTPException):
        await require_internal_caller(_Request("Bearer x"))  # type: ignore[arg-type]


async def test_an_unverified_email_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(auth_internal, "get_settings", _settings)
    _patch_google(monkeypatch, _claims(email_verified=False))
    with pytest.raises(HTTPException):
        await require_internal_caller(_Request("Bearer x"))  # type: ignore[arg-type]


async def test_the_legacy_issuer_form_is_accepted(monkeypatch) -> None:
    """Google still emits the bare host form. Rejecting it would 401 real
    schedulers for a cosmetic difference."""
    monkeypatch.setattr(auth_internal, "get_settings", _settings)
    _patch_google(monkeypatch, _claims(iss="accounts.google.com"))
    await require_internal_caller(_Request("Bearer x"))  # type: ignore[arg-type]


async def test_a_foreign_issuer_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(auth_internal, "get_settings", _settings)
    _patch_google(monkeypatch, _claims(iss="https://evil.example.com"))
    with pytest.raises(HTTPException):
        await require_internal_caller(_Request("Bearer x"))  # type: ignore[arg-type]


# --- the modes ------------------------------------------------------------


async def test_audit_admits_a_request_it_would_have_rejected(monkeypatch, _accepts) -> None:
    """The default. Verification is observed agreeing in production before it
    is allowed to reject anything."""
    monkeypatch.setattr(
        auth_internal, "get_settings", lambda: _settings(internal_jobs_auth_mode=AUTH_AUDIT)
    )
    await require_internal_caller(_Request())  # type: ignore[arg-type]


async def test_off_skips_verification_entirely(monkeypatch) -> None:
    monkeypatch.setattr(
        auth_internal, "get_settings", lambda: _settings(internal_jobs_auth_mode=AUTH_OFF)
    )
    await require_internal_caller(_Request())  # type: ignore[arg-type]


async def test_the_default_mode_is_audit_not_enforce() -> None:
    """A deploy that silently started enforcing would take every scheduled job
    down at once, and this project has twice been slow to notice exactly that."""
    assert Settings().internal_jobs_auth_mode == AUTH_AUDIT


async def test_an_unknown_mode_does_not_enforce(monkeypatch, _accepts) -> None:
    """A typo in configuration must not lock out the schedulers."""
    monkeypatch.setattr(
        auth_internal, "get_settings", lambda: _settings(internal_jobs_auth_mode="enfroce")
    )
    await require_internal_caller(_Request())  # type: ignore[arg-type]


async def test_no_expected_service_account_degrades_open_and_logs(monkeypatch, _accepts) -> None:
    """Enforcing with nothing to compare against would reject every caller,
    including the schedulers. Locking the door with no key is the worse
    failure, so it admits and logs loudly."""
    monkeypatch.setattr(
        auth_internal,
        "get_settings",
        lambda: _settings(internal_jobs_allowed_sa="", cloud_tasks_invoker_sa=""),
    )
    await require_internal_caller(_Request())  # type: ignore[arg-type]


async def test_the_expected_sa_falls_back_to_the_cloud_tasks_invoker(monkeypatch, _accepts) -> None:
    """Scheduler and Tasks both sign as that identity, so it is already
    configured in production and needs no second setting."""
    monkeypatch.setattr(
        auth_internal,
        "get_settings",
        lambda: _settings(internal_jobs_allowed_sa="", cloud_tasks_invoker_sa=CALLER),
    )
    await require_internal_caller(_Request("Bearer good"))  # type: ignore[arg-type]


# --- coverage of the router ----------------------------------------------


def test_every_internal_route_is_guarded() -> None:
    """Listing endpoints individually is how one gets forgotten, and the one
    most worth forgetting is tasks/run."""
    from app.api import internal_jobs

    guards = [
        d
        for d in (internal_jobs.router.dependencies or [])
        if getattr(d, "dependency", None) is require_internal_caller
    ]
    assert guards, "the router itself must carry the dependency"


class _LogSpy:
    """structlog is configured with its own pipeline, so the module logger is
    replaced rather than captured through stdlib logging."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def _record(self, level: str):
        def emit(event: str, **_: Any) -> None:
            self.events.append((level, event))

        return emit

    def __getattr__(self, name: str):
        return self._record(name)


async def test_audit_mode_records_a_pass_not_only_a_failure(monkeypatch, _accepts) -> None:
    """The audit period exists to observe verification agreeing. If only
    rejections were logged, an empty log would mean either "every token passed"
    or "the dependency never ran" — and switching to enforce on that basis is a
    guess that takes every scheduled job down at once."""
    spy = _LogSpy()
    monkeypatch.setattr(auth_internal, "log", spy)
    monkeypatch.setattr(
        auth_internal, "get_settings", lambda: _settings(internal_jobs_auth_mode=AUTH_AUDIT)
    )
    await require_internal_caller(_Request("Bearer good"))  # type: ignore[arg-type]
    assert ("info", "internal.auth.ok") in spy.events


async def test_enforce_mode_does_not_log_every_successful_call_at_info(
    monkeypatch, _accepts
) -> None:
    """Once enforcing, a line per job run every few minutes is noise that
    buries the rejections worth reading."""
    spy = _LogSpy()
    monkeypatch.setattr(auth_internal, "log", spy)
    monkeypatch.setattr(auth_internal, "get_settings", _settings)
    await require_internal_caller(_Request("Bearer good"))  # type: ignore[arg-type]
    assert ("info", "internal.auth.ok") not in spy.events
    assert ("debug", "internal.auth.ok") in spy.events
