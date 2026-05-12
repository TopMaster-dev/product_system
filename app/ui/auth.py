"""HTTP Basic Auth for the admin UI.

Phase 1-A: simple username/password from settings, intended to be replaced
by IAP / IAM / IdP-backed auth in production. The authenticated username
is captured as the "operator" in audit-relevant events (manual adjustments,
mapping resolutions).
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import Settings, get_settings

_basic = HTTPBasic(realm="Product System Admin")


def get_current_operator(
    credentials: Annotated[HTTPBasicCredentials, Depends(_basic)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    expected_user = settings.admin_username.encode("utf-8")
    expected_pass = settings.admin_password.encode("utf-8")
    actual_user = credentials.username.encode("utf-8")
    actual_pass = credentials.password.encode("utf-8")
    if not (
        secrets.compare_digest(expected_user, actual_user)
        and secrets.compare_digest(expected_pass, actual_pass)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


OperatorDep = Annotated[str, Depends(get_current_operator)]
