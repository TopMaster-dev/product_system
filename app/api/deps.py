"""FastAPI dependency providers (DB session, settings, etc.).

Concrete dependencies are wired up in Sprint 1+.
"""

from __future__ import annotations

from app.config import Settings, get_settings

__all__ = ["Settings", "get_settings"]
