"""Long text stays out of tables; the row's panel shows it in full.

The 2026-10-03 responsive review found product titles wrapping a 75px column
into seventeen lines, and short columns squeezed to one character per line.
The client chose a row-click detail panel over hover tooltips. These guards
read the templates, so a new screen inherits the rule the day it lands.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ui.deps import templates

pytestmark = pytest.mark.unit

TEMPLATES = Path("app/ui/templates")

#: Fields that hold free text from a channel or a person. In a table cell they
#: go through `ui.clip()`, never straight out.
LONG_FIELDS = r"(name|product_name|master_sku_name|reason|guidance|masters|error_message)"
#: A cell marked `wrap` is a sentence meant to wrap (a CSV row's validation
#: message), opted out of the one-line rule on purpose.
RAW_IN_CELL = re.compile(
    r"<td(?![^>]*\bwrap\b)[^>]*>\s*(<span>)?\{\{\s*[a-z]+\." + LONG_FIELDS + r"\s*\}\}"
)


def _sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(TEMPLATES.glob("*.html"))}


def test_no_table_cell_prints_long_text_straight_out() -> None:
    offenders = [
        f"{name}: {m.group(0)}"
        for name, text in _sources().items()
        if name != "manual.html"
        for m in RAW_IN_CELL.finditer(text)
    ]
    assert not offenders, f"long text printed in full inside a table cell: {offenders}"


@pytest.mark.parametrize(
    ("template", "field"),
    [
        ("inventory_list.html", "row.name"),
        ("alerts.html", "a.product_name"),
        ("analytics_sales.html", "row.name"),
        ("analytics_stockout_risk.html", "r.name"),
        ("events.html", "ev.reason"),
        ("sync_errors.html", "row.guidance"),
        ("data_quality_rakuten.html", "row.product_name"),
        ("stocktake_detail.html", "d.name"),
        ("stocktake_preview.html", "row.name"),
        ("reconcile_detail.html", "d.name"),
        ("reconcile_preview.html", "d.name"),
        ("mappings_list.html", "row.master_sku_name"),
    ],
)
def test_the_reviewed_screens_clip_their_long_text(template: str, field: str) -> None:
    assert f"ui.clip({field}" in (TEMPLATES / template).read_text(encoding="utf-8")


def test_every_clickable_row_has_a_panel_to_open() -> None:
    """A row that announces a panel but has none is a click that does nothing."""
    missing = []
    for name, text in _sources().items():
        for prefix in set(re.findall(r"row_attrs\('([a-z]+-)'", text)):
            if f"ui.panel('{prefix}'" not in text:
                missing.append(f"{name}: {prefix}")
    assert not missing, f"rows with no panel: {missing}"


def _render(source: str, **ctx: object) -> str:
    return templates.env.from_string('{% import "_detail.html" as ui %}' + source).render(**ctx)


def test_clip_is_one_line_with_a_width_cap() -> None:
    html = _render("{{ ui.clip(text, 'max-w-[9rem]') }}", text="長い商品名" * 20)
    assert "truncate" in html and "max-w-[9rem]" in html


def test_clip_of_nothing_is_a_dash() -> None:
    assert "—" in _render("{{ ui.clip(none) }}")


def test_a_panel_is_an_inert_template_until_opened() -> None:
    html = _render(
        "{% call ui.panel('x-1', 'SKU-1') %}{{ ui.field('商品名', name) }}{% endcall %}",
        name="全文<b>",
    )
    assert '<template id="x-1" data-title="SKU-1">' in html
    assert "全文&lt;b&gt;" in html  # autoescaped, never injected


def test_row_attrs_make_the_row_reachable_by_keyboard() -> None:
    html = _render("<tr {{ ui.row_attrs('x-1') }}>")
    assert 'data-detail="x-1"' in html and 'tabindex="0"' in html


def test_the_layout_carries_the_panel_and_leaves_controls_alone() -> None:
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert 'id="detailPanel"' in base
    # Links, buttons and form fields inside a row keep their own behaviour.
    assert "a, button, input, select, textarea, label" in base
    assert ".data-table th, .data-table td { white-space: nowrap; }" in base


def test_select_padding_cannot_be_overridden_by_utility_classes() -> None:
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "padding-right: 2.25rem !important;" in base


# --- 欠品リスクの件数 ---------------------------------------------------------


def test_the_risk_counters_count_every_row_not_the_page() -> None:
    """With 100+ SKUs out of stock, counting the 100 shown made all three
    counters read exactly 100."""
    from app.ui.routes.analytics import RISK_LIMIT, risk_counts

    def risk(qty: int, below: bool, days: float | None) -> SimpleNamespace:
        return SimpleNamespace(on_hand_qty=qty, is_below_threshold=below, days_remaining=days)

    every = [risk(0, True, None)] * 150 + [risk(5, True, 3.0)] * 20 + [risk(50, False, 40.0)] * 30
    assert len(every) > RISK_LIMIT
    assert risk_counts(every) == {
        "out_of_stock": 150,
        "below_threshold": 170,
        "unforecastable": 150,
    }


# --- KPI タイル -----------------------------------------------------------------


def test_the_yen_tile_gets_room_at_every_width() -> None:
    overview = (TEMPLATES / "analytics_overview.html").read_text(encoding="utf-8")
    assert "grid-cols-2 md:grid-cols-3 xl:grid-cols-7" in overview
    assert "span='col-span-2 md:col-span-1 xl:col-span-2'" in overview
    charts = (TEMPLATES / "_charts.html").read_text(encoding="utf-8")
    assert "whitespace-nowrap" in charts and "min-w-0 {{ span }}" in charts


def test_the_last_axis_label_is_anchored_inside_the_frame() -> None:
    from app.ui import charts

    model = charts.line_chart(["09/01", "09/02", "09/03"], [("x", [1.0, 2.0, 3.0], "#000")])
    html = templates.env.from_string(
        '{% from "_charts.html" import line_chart %}{{ line_chart(model) }}'
    ).render(model=model)
    assert 'text-anchor="end"' in html
