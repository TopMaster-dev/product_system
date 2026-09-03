"""Unit tests for chart geometry.

The point of keeping this in Python rather than a CDN library is that these
assertions are possible at all. Every degenerate case below is a normal day's
data — a new shop, a SKU that did not move, an out-of-stock item — and each one
either crashes or misleads under the obvious implementation.
"""

from __future__ import annotations

import pytest

from app.ui.charts import (
    axis_ticks,
    bar_chart,
    format_axis_value,
    line_chart,
    nice_ceiling,
    share_bars,
    thin_label_indices,
)

pytestmark = pytest.mark.unit

BLUE = "#4F46E5"


# --- axis scaling --------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, 1),
        (7, 10),
        (10, 10),
        (11, 20),
        (26, 50),
        (2_600, 5_000),
        (2_400, 2_500),
        (99_000, 100_000),
    ],
)
def test_nice_ceiling_rounds_up_to_readable_values(value: float, expected: float) -> None:
    assert nice_ceiling(value) == expected


def test_nice_ceiling_keeps_2_5_so_a_chart_uses_its_height() -> None:
    """Without the 2.5 step a peak of 2,400 scales to 5,000 and the chart draws
    in its bottom half."""
    assert nice_ceiling(2_400) == 2_500


@pytest.mark.parametrize("value", [0, -5, -0.001])
def test_nice_ceiling_is_positive_even_with_no_data(value: float) -> None:
    """A zero maximum divides by zero downstream; an axis needs extent even
    when the data has none."""
    assert nice_ceiling(value) == 1.0


def test_axis_ticks_span_zero_to_max_inclusive() -> None:
    assert axis_ticks(100, 4) == [0, 25, 50, 75, 100]


# --- label thinning ------------------------------------------------------


def test_all_labels_kept_when_they_fit() -> None:
    assert thin_label_indices(5, max_labels=7) == [0, 1, 2, 3, 4]


def test_thinning_always_keeps_both_ends() -> None:
    """A time axis with unlabelled ends leaves the reader unable to tell what
    period they are looking at — the one job the axis has."""
    kept = thin_label_indices(90, max_labels=7)
    assert kept[0] == 0
    assert kept[-1] == 89
    assert len(kept) <= 8


def test_thinning_handles_an_empty_axis() -> None:
    assert thin_label_indices(0) == []


# --- the four degenerate cases -------------------------------------------


def test_an_empty_series_renders_a_frame_not_a_crash() -> None:
    model = line_chart([], [])
    assert model.empty is True
    assert model.series == []
    assert model.baseline_y > 0


def test_a_single_point_is_centred_rather_than_dividing_by_zero() -> None:
    """Day one after go-live. Horizontal spacing divides by (n - 1)."""
    model = line_chart(["2026-08-01"], [("在庫", [40.0], BLUE)])
    assert model.empty is False
    point = model.series[0].points[0]
    assert model.padding_left < point.x < model.width


def test_a_flat_series_sits_mid_chart_not_on_the_floor() -> None:
    """A SKU that held 40 units all month. Drawn at the baseline it would look
    identical to a SKU that held zero."""
    model = line_chart(["a", "b", "c"], [("在庫", [40.0, 40.0, 40.0], BLUE)])
    ys = {p.y for p in model.series[0].points}
    assert len(ys) == 1, "a constant series is a straight line"
    y = ys.pop()
    assert 0 < y < model.baseline_y, "and it must not lie along the axis"


def test_an_all_zero_series_does_not_divide_by_zero() -> None:
    """An out-of-stock SKU over the whole window. The only genuinely degenerate
    case: the axis starts at 0, so a constant NON-zero series scales normally."""
    model = line_chart(["a", "b"], [("在庫", [0.0, 0.0], BLUE)])
    assert model.empty is False
    assert model.y_max > 0
    assert all(p.y > 0 for p in model.series[0].points)


def test_a_short_series_is_padded_rather_than_rejected() -> None:
    """A rollup gap should read as a dip, not a 500 on the dashboard."""
    model = line_chart(["a", "b", "c"], [("在庫", [5.0], BLUE)])
    assert len(model.series[0].points) == 3
    assert model.series[0].points[2].value == 0.0


# --- rendered output -----------------------------------------------------


def test_polyline_is_precomputed_for_the_template() -> None:
    """The macro emits coordinates and does no arithmetic — that separation is
    what makes this file possible."""
    model = line_chart(["a", "b"], [("在庫", [1.0, 2.0], BLUE)])
    poly = model.series[0].polyline
    assert poly.count(",") == 2
    assert " " in poly


def test_area_path_is_empty_for_a_single_point() -> None:
    """Nothing to fill under one point; the template tests this directly."""
    assert line_chart(["a"], [("在庫", [1.0], BLUE)]).series[0].area == ""


def test_higher_values_sit_higher_on_screen() -> None:
    """SVG y grows downward — the sign is easy to invert and the result looks
    plausible upside down."""
    model = line_chart(["a", "b"], [("売上", [10.0, 100.0], BLUE)])
    low, high = model.series[0].points
    assert high.y < low.y


# --- bars ----------------------------------------------------------------


def test_bars_fit_inside_the_plot_area() -> None:
    _, bars = bar_chart(["a", "b", "c"], [1.0, 5.0, 3.0], width=300, padding_left=40)
    assert len(bars) == 3
    assert bars[0].x >= 40
    assert bars[-1].x + bars[-1].width <= 300.001


def test_a_zero_bar_has_no_height() -> None:
    """A sliver would read as a real quantity; the axis already conveys zero."""
    _, bars = bar_chart(["a", "b"], [0.0, 10.0])
    assert bars[0].height == 0


def test_empty_bar_chart_renders_a_frame() -> None:
    model, bars = bar_chart([], [])
    assert model.empty is True
    assert bars == []


# --- share ---------------------------------------------------------------


def test_share_percentages_sum_to_100() -> None:
    bars = share_bars([("A", 50.0), ("B", 30.0), ("C", 20.0)])
    assert sum(b.percent for b in bars) == pytest.approx(100.0)


def test_the_tail_is_grouped_rather_than_dropped() -> None:
    """Percentages are against the FULL total. Rescaling the top-N to 100%
    would overstate every visible bar."""
    items = [(f"S{i}", float(10 - i)) for i in range(10)]
    bars = share_bars(items, limit=3)
    assert len(bars) == 4
    assert bars[-1].label.startswith("その他")
    assert sum(b.percent for b in bars) == pytest.approx(100.0)


def test_share_of_nothing_is_empty_not_a_division_by_zero() -> None:
    assert share_bars([]) == []
    assert share_bars([("A", 0.0), ("B", 0.0)]) == []


def test_share_ignores_negative_values() -> None:
    """A refund-heavy day can make a category negative; a negative share has no
    meaning as a bar width."""
    bars = share_bars([("A", 10.0), ("B", -5.0)])
    assert [b.label for b in bars] == ["A"]
    assert bars[0].percent == pytest.approx(100.0)


# --- formatting ----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "0"), (999, "999"), (1_000, "1k"), (12_500, "12.5k"), (1_500_000, "1.5M")],
)
def test_axis_values_are_compact(value: float, expected: str) -> None:
    assert format_axis_value(value) == expected


# --- the macros render what the module computed ---------------------------


def _render(macro: str, **ctx) -> str:
    """Render one macro from _charts.html in isolation."""
    from app.ui.deps import templates

    source = "{% import '_charts.html' as c %}{{ c." + macro + " }}"
    return templates.env.from_string(source).render(**ctx)


def test_line_macro_emits_the_precomputed_polyline() -> None:
    model = line_chart(["a", "b", "c"], [("在庫", [1.0, 5.0, 3.0], BLUE)])
    html = _render("line_chart(model)", model=model)
    assert model.series[0].polyline in html
    assert "<polyline" in html


def test_line_macro_renders_an_empty_frame_without_svg() -> None:
    """An empty period must not draw axes suggesting there is data to read."""
    html = _render("line_chart(model)", model=line_chart([], []))
    assert "<polyline" not in html
    assert "データがありません" in html


def test_bar_macro_omits_zero_height_rects() -> None:
    """A 1px sliver reads as a real quantity."""
    model, bars = bar_chart(["a", "b"], [0.0, 10.0])
    html = _render("bar_chart(model, bars)", model=model, bars=bars)
    assert html.count("<rect") == 1


def test_share_list_widths_match_the_computed_percentages() -> None:
    bars = share_bars([("A", 75.0), ("B", 25.0)])
    html = _render("share_bar_list(bars)", bars=bars)
    assert "width: 75.0%" in html
    assert "width: 25.0%" in html


def test_kpi_tile_shows_a_dash_when_the_change_is_undefined() -> None:
    """pct_change returns None against a zero baseline. Rendering 0% or +100%
    would invent a figure the client would act on."""
    html = _render("kpi_tile('売上', '¥0', None)", **{})
    assert "前期間比 —" in html
    assert "%" not in html.split("前期間比")[0].split("¥0")[-1]


def test_kpi_tile_colours_a_fall_as_bad_by_default() -> None:
    up = _render("kpi_tile('売上', '¥100', 12.5)")
    down = _render("kpi_tile('売上', '¥100', -12.5)")
    assert "emerald" in up and "+12.5%" in up
    assert "red" in down and "-12.5%" in down


def test_kpi_tile_can_invert_which_direction_is_good() -> None:
    """欠品件数 rising is bad; 売上 rising is good. Same macro, same data."""
    html = _render("kpi_tile('欠品', '12', 30.0, good_when_up=False)")
    assert "red" in html
