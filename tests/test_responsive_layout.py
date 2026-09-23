"""スマホ幅で崩れないこと (P2-044).

One rule does most of the work: **horizontal scrolling must stay inside the
table**. A table wider than the phone viewport that is not in a scroll
container makes the whole PAGE scroll sideways — the header, the nav, the
buttons, everything — and every screen on the site is a table.

A table is "can be wider than the viewport" when it declares a `min-width`, or
when any cell inside it is `whitespace-nowrap`. Both are deliberate: a SKU code
or a date that wraps is unreadable, so the columns are pinned and the table is
given a scroll container of its own. A table with neither just wraps and is
fine at any width — `categories_preview` and `reconcile_preview` each have a
two-column 行/理由 list like that, and wrapping them into an
`overflow-x-auto` would be noise, not safety.

These parse the template source rather than a rendered page, so a screen that
needs elaborate context is covered like any other, and a new screen is covered
the moment it is added.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

TEMPLATES = Path("app/ui/templates")

#: Jinja is stripped rather than executed. What is left is the markup exactly
#: as authored, including branches that would not render together — which is
#: what we want: a table inside an `{% if %}` still has to be wrapped.
_JINJA = re.compile(r"\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}", re.S)

_VOID = {
    "area", "base", "br", "col", "embed", "hr", "img",
    "input", "link", "meta", "source", "track", "wbr",
}  # fmt: skip

#: Anything wider than this on a phone pushes the page sideways. iPhone SE is
#: 375 CSS px; the layout reserves a 16px gutter each side.
PHONE_CONTENT_PX = 343


@dataclass
class Table:
    template: str
    line: int
    scrollable: bool
    min_width: bool
    nowrap: bool = False

    @property
    def can_exceed_viewport(self) -> bool:
        return self.min_width or self.nowrap

    @property
    def ok(self) -> bool:
        return self.scrollable or not self.can_exceed_viewport

    def __str__(self) -> str:
        why = "min-width" if self.min_width else "whitespace-nowrap"
        return f"{self.template}:{self.line} ({why}, no overflow-x-auto ancestor)"


@dataclass
class _Scan(HTMLParser):
    """Tracks the open-tag stack so a table's ANCESTORS can be inspected.

    A flat "does this file contain overflow-x-auto somewhere?" check passes for
    a file where one table is wrapped and another is not, which is exactly the
    shape of the bug.
    """

    template: str
    stack: list[tuple[str, str]] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    wide_px: list[str] = field(default_factory=list)
    unbalanced: list[str] = field(default_factory=list)
    _open_table: Table | None = None
    _table_depth: int = -1

    def __post_init__(self) -> None:
        super().__init__(convert_charrefs=True)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        cls, style = a.get("class", ""), a.get("style", "")

        if tag == "table":
            self._open_table = Table(
                template=self.template,
                line=self.getpos()[0],
                scrollable=any("overflow-x-auto" in c for _, c in self.stack),
                min_width="min-width" in style or "min-w-[" in cls,
            )
            self._table_depth = len(self.stack)
            self.tables.append(self._open_table)
        elif self._open_table is not None and "whitespace-nowrap" in cls:
            self._open_table.nowrap = True

        # A hard `width: NNNpx` cannot shrink. max-width and % can.
        for px in re.findall(r"(?<!max-)width:\s*(\d+)px", style):
            if int(px) > PHONE_CONTENT_PX and not any(
                "overflow-x-auto" in c for _, c in self.stack
            ):
                self.wide_px.append(f"{self.template}:{self.getpos()[0]} width:{px}px")

        if tag not in _VOID:
            self.stack.append((tag, cls))

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                if tag == "table" and len(self.stack) - 1 == self._table_depth:
                    self._open_table = None
                del self.stack[i:]
                return
        self.unbalanced.append(f"{self.template}:{self.getpos()[0]} </{tag}>")


def _scan(path: Path) -> _Scan:
    scan = _Scan(template=path.name)
    scan.feed(_JINJA.sub("", path.read_text(encoding="utf-8")))
    return scan


@pytest.fixture(scope="module")
def scans() -> list[_Scan]:
    paths = sorted(TEMPLATES.glob("*.html"))
    assert paths, "no templates found — is the suite running from the repo root?"
    return [_scan(p) for p in paths]


def test_the_parser_is_actually_reading_the_markup(scans: list[_Scan]) -> None:
    """Without this every other assertion here passes on an empty list.

    If stripping Jinja ever mangles the markup, the scan finds no tables and
    the screens all read as compliant.
    """
    tables = [t for s in scans for t in s.tables]
    assert len(tables) > 15, f"only found {len(tables)} tables; the scan is broken"
    assert any(t.can_exceed_viewport for t in tables)
    assert any(not t.can_exceed_viewport for t in tables)


def test_the_templates_have_balanced_tags(scans: list[_Scan]) -> None:
    """A stray closing tag unwinds the ancestor stack and would make a wrapped
    table look unwrapped — or worse, the reverse."""
    stray = [u for s in scans for u in s.unbalanced]
    assert not stray, f"unbalanced closing tags: {stray}"


def test_horizontal_scrolling_stays_inside_the_table(scans: list[_Scan]) -> None:
    """The rule the whole screen set depends on. A wide table outside a scroll
    container scrolls the entire page sideways on a phone."""
    offenders = [str(t) for s in scans for t in s.tables if not t.ok]
    assert not offenders, "these tables scroll the page instead of themselves:\n  " + "\n  ".join(
        offenders
    )


def test_nothing_is_pinned_wider_than_a_phone_outside_a_scroll_area(
    scans: list[_Scan],
) -> None:
    """`max-width` and percentages shrink; a bare `width: 600px` does not."""
    offenders = [w for s in scans for w in s.wide_px]
    assert not offenders, f"fixed widths wider than a phone: {offenders}"


def test_every_screen_with_a_table_is_covered(scans: list[_Scan]) -> None:
    """Names the Phase 2 screens explicitly, so a new one silently skipping the
    scan is a failure rather than a smaller number nobody reads."""
    covered = {s.template for s in scans if s.tables}
    for screen in (
        "inventory_list.html",
        "analytics_sales.html",
        "analytics_stockout_risk.html",
        "stocktake_detail.html",
        "stocktake_list.html",
        "categories.html",
        "events.html",
    ):
        assert screen in covered, screen


# --- ナビゲーション --------------------------------------------------------


@pytest.fixture(scope="module")
def base_html() -> str:
    return (TEMPLATES / "base.html").read_text(encoding="utf-8")


def test_the_navigation_has_a_desktop_and_a_mobile_form(base_html: str) -> None:
    """One nav sized for a laptop is unusable on a phone; one sized for a phone
    wastes a laptop. The grouped dropdowns and the drawer are the same links in
    two shapes, and both have to exist."""
    assert "hidden md:flex" in base_html
    assert "md:hidden" in base_html
    assert 'id="mobileNav"' in base_html


def test_the_mobile_drawer_starts_closed(base_html: str) -> None:
    """It slides down under the header. Rendered open, it covers the page on
    every load."""
    drawer = next(ln for ln in base_html.splitlines() if 'id="mobileNav"' in ln)
    assert "hidden" in drawer


def test_every_navigation_group_is_reachable_on_mobile(base_html: str) -> None:
    """The drawer is a second copy of nav_groups. A group added to the desktop
    dropdowns and not the drawer is invisible on a phone."""
    head, _, tail = base_html.partition('id="mobileNav"')
    assert "nav_groups" in head, "nav_groups must be defined before both navs"
    assert "for group_label, items in nav_groups" in tail, (
        "the mobile drawer must iterate nav_groups, not repeat the links"
    )
