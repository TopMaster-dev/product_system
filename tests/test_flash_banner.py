"""完了・エラーのメッセージは1か所でだけ描画する.

`base.html` draws the banner for every screen from `flash.kind`. The stocktake
screen also drew its own from `flash.level`, so every message appeared twice —
and the shared banner, finding no `kind`, painted error messages in the success
colour. Found on 2026-10-02 while preparing the 検収, where the stocktake
screen is one the client operates by hand.

Two rules, both read from source so a new screen is covered the day it lands.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

TEMPLATES = Path("app/ui/templates")
ROUTES = Path("app/ui/routes")


def test_only_the_shared_layout_renders_the_banner() -> None:
    """A screen that renders `flash` itself shows every message twice."""
    offenders = [
        p.name
        for p in sorted(TEMPLATES.glob("*.html"))
        if p.name != "base.html" and "{% if flash %}" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"these render the banner a second time: {offenders}"


def test_every_route_hands_the_layout_a_kind() -> None:
    """`base.html` colours by `flash.kind`. A route returning anything else gets
    its errors painted green, which reads as success."""
    offenders = [
        p.name
        for p in sorted(ROUTES.glob("*.py"))
        if re.search(r'"level"\s*:', p.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"these return flash['level'] instead of flash['kind']: {offenders}"


def test_the_shared_layout_still_keys_on_kind() -> None:
    """The guard above is only meaningful while the layout reads `kind`."""
    assert "flash.kind" in (TEMPLATES / "base.html").read_text(encoding="utf-8")


def test_an_error_flash_from_the_stocktake_screen_is_an_error() -> None:
    from app.ui.routes.stocktake import _flash

    assert _flash("badcsv") == {
        "kind": "error",
        "message": "CSVを読み取れませんでした。もう一度アップロードしてください",
    }
    assert _flash("created")["kind"] == "ok"  # type: ignore[index]
    assert _flash("unknown") is None
