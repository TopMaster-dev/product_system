"""Chart geometry in pure Python. The templates only emit coordinates.

No CDN charting library, and the reason is testability rather than page weight.
A chart is the most reviewable artefact this system produces — the client will
act on what it shows — so the arithmetic behind it has to be assertable. Handing
a JSON array to a library moves every scaling decision somewhere no test can
reach, and the failures that matter are silent ones: an axis that rounds a peak
off the top, a flat line drawn at the bottom of its own chart, a share bar that
sums to 103%.

So this module returns finished coordinates and `_charts.html` renders them
without doing a single calculation.

THE FOUR DEGENERATE CASES, ALL OF WHICH OCCUR HERE
--------------------------------------------------
* **Empty series** — a SKU with no sales in the window, or a brand-new shop.
  Must render an empty frame, not divide by zero.
* **One point** — the first day after go-live. Horizontal spacing divides by
  (n - 1), which is zero.
* **Every value identical** — a SKU that held 40 units all month. The value
  range is zero, so a naive scale divides by zero; the line must sit in the
  middle of the frame rather than on its floor.
* **All zeros** — an out-of-stock SKU. Same as above but the axis also has to
  choose a maximum out of nothing.

Each of these is a normal day's data, not an edge case, and every one of them
crashes or misleads under the obvious implementation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

#: Multipliers that make an axis maximum readable. 2.5 earns its place: without
#: it a peak of 2,600 jumps to 5,000 and the chart uses half its height.
_NICE_STEPS = (1.0, 2.0, 2.5, 5.0, 10.0)

#: An all-zero series still needs a frame. Drawn at this fraction of the
#: height so the line is visible rather than hidden under the axis rule.
_FLAT_LINE_POSITION = 0.5


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float
    value: float
    label: str


@dataclass(frozen=True, slots=True)
class RenderedSeries:
    name: str
    color: str
    points: list[Point]

    @property
    def polyline(self) -> str:
        """SVG `points` attribute — precomputed so the template never joins."""
        return " ".join(f"{p.x:.1f},{p.y:.1f}" for p in self.points)

    @property
    def area(self) -> str:
        """Closed path for the fill under a line. Empty when there is nothing
        to fill, so the template can test it directly."""
        if len(self.points) < 2:
            return ""
        first, last = self.points[0], self.points[-1]
        return self.polyline + f" {last.x:.1f},{first.y:.1f}"


@dataclass(frozen=True, slots=True)
class Tick:
    value: float
    y: float
    label: str


@dataclass(frozen=True, slots=True)
class ChartModel:
    width: int
    height: int
    padding_left: int
    padding_bottom: int
    series: list[RenderedSeries] = field(default_factory=list)
    ticks: list[Tick] = field(default_factory=list)
    x_labels: list[tuple[float, str]] = field(default_factory=list)
    y_max: float = 1.0
    empty: bool = True
    baseline_y: float = 0.0


def nice_ceiling(value: float) -> float:
    """Round up to a readable axis maximum.

    0 and negatives return 1.0: an axis needs a positive extent even when the
    data has none, and a zero maximum would divide by zero downstream.
    """
    if value <= 0:
        return 1.0
    exponent = math.floor(math.log10(value))
    base = 10.0**exponent
    for step in _NICE_STEPS:
        if value <= step * base:
            return step * base
    return 10.0 * base


def axis_ticks(y_max: float, count: int = 4) -> list[float]:
    """`count` + 1 evenly spaced values from 0 to y_max inclusive."""
    if count < 1:
        return [0.0, y_max]
    return [y_max * i / count for i in range(count + 1)]


def thin_label_indices(total: int, max_labels: int = 7) -> list[int]:
    """Which x positions get a label, so they never overlap.

    Always keeps the first and last: a time axis whose ends are unlabelled
    leaves the reader unable to tell what period they are looking at, which is
    the one thing the axis is for.
    """
    if total <= 0:
        return []
    if total <= max_labels:
        return list(range(total))
    step = math.ceil(total / max_labels)
    kept = list(range(0, total, step))
    if kept[-1] != total - 1:
        # Drop the penultimate rather than let it collide with the last.
        if len(kept) > 1 and (total - 1) - kept[-1] < step / 2:
            kept.pop()
        kept.append(total - 1)
    return kept


def format_axis_value(value: float) -> str:
    """Compact axis labels. 12,000 -> 12k; 1,500,000 -> 1.5M."""
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    if value != int(value):
        return f"{value:.1f}"
    return str(int(value))


def line_chart(
    labels: list[str],
    series: list[tuple[str, list[float], str]],
    *,
    width: int = 720,
    height: int = 220,
    padding_left: int = 44,
    padding_bottom: int = 24,
    padding_top: int = 10,
    max_labels: int = 7,
) -> ChartModel:
    """Coordinates for a multi-series line chart.

    `series` is (name, values, colour). Every series must be the same length as
    `labels`; a shorter one is padded rather than rejected, because a rollup gap
    should render as a dip rather than a 500.
    """
    plot_width = width - padding_left
    plot_height = height - padding_bottom - padding_top
    count = len(labels)

    if count == 0 or not series:
        return ChartModel(
            width=width,
            height=height,
            padding_left=padding_left,
            padding_bottom=padding_bottom,
            empty=True,
            baseline_y=height - padding_bottom,
        )

    padded = [
        (name, list(values) + [0.0] * (count - len(values)), color)
        for name, values, color in series
    ]
    highest = max((max(values[:count], default=0.0) for _, values, _ in padded), default=0.0)

    # Only the all-zero case is degenerate. The axis starts at 0, so a
    # constant non-zero series (a SKU that held 40 all month) scales
    # normally; it is a flat line partway up, which is correct.
    all_zero = highest <= 0
    y_max = nice_ceiling(highest)

    def x_for(index: int) -> float:
        # One point cannot be spaced across a width; centre it instead of
        # dividing by zero.
        if count == 1:
            return padding_left + plot_width / 2
        return padding_left + plot_width * index / (count - 1)

    def y_for(value: float) -> float:
        if all_zero:
            return padding_top + plot_height * _FLAT_LINE_POSITION
        return padding_top + plot_height * (1 - value / y_max)

    rendered = [
        RenderedSeries(
            name=name,
            color=color,
            points=[
                Point(x=x_for(i), y=y_for(values[i]), value=values[i], label=labels[i])
                for i in range(count)
            ],
        )
        for name, values, color in padded
    ]

    ticks = [
        Tick(
            value=v,
            y=padding_top + plot_height * (1 - v / y_max),
            label=format_axis_value(v),
        )
        for v in axis_ticks(y_max)
    ]

    return ChartModel(
        width=width,
        height=height,
        padding_left=padding_left,
        padding_bottom=padding_bottom,
        series=rendered,
        ticks=ticks,
        x_labels=[(x_for(i), labels[i]) for i in thin_label_indices(count, max_labels)],
        y_max=y_max,
        empty=False,
        baseline_y=padding_top + plot_height,
    )


@dataclass(frozen=True, slots=True)
class Bar:
    label: str
    value: float
    x: float
    y: float
    width: float
    height: float
    display: str


def bar_chart(
    labels: list[str],
    values: list[float],
    *,
    width: int = 720,
    height: int = 220,
    padding_left: int = 44,
    padding_bottom: int = 24,
    padding_top: int = 10,
    gap_ratio: float = 0.25,
) -> tuple[ChartModel, list[Bar]]:
    count = min(len(labels), len(values))
    plot_width = width - padding_left
    plot_height = height - padding_bottom - padding_top

    if count == 0:
        return (
            ChartModel(
                width=width,
                height=height,
                padding_left=padding_left,
                padding_bottom=padding_bottom,
                empty=True,
                baseline_y=height - padding_bottom,
            ),
            [],
        )

    highest = max(values[:count], default=0.0)
    y_max = nice_ceiling(highest)
    slot = plot_width / count
    bar_width = slot * (1 - gap_ratio)

    bars = [
        Bar(
            label=labels[i],
            value=values[i],
            x=padding_left + slot * i + (slot - bar_width) / 2,
            # A zero-height rect is invisible; the axis line already conveys
            # zero, and a sliver would read as a real quantity.
            y=padding_top + plot_height * (1 - max(values[i], 0.0) / y_max),
            width=bar_width,
            height=plot_height * max(values[i], 0.0) / y_max,
            display=format_axis_value(values[i]),
        )
        for i in range(count)
    ]

    model = ChartModel(
        width=width,
        height=height,
        padding_left=padding_left,
        padding_bottom=padding_bottom,
        ticks=[
            Tick(value=v, y=padding_top + plot_height * (1 - v / y_max), label=format_axis_value(v))
            for v in axis_ticks(y_max)
        ],
        x_labels=[
            (padding_left + slot * i + slot / 2, labels[i])
            for i in thin_label_indices(count, max_labels=10)
        ],
        y_max=y_max,
        empty=False,
        baseline_y=padding_top + plot_height,
    )
    return model, bars


@dataclass(frozen=True, slots=True)
class ShareBar:
    label: str
    value: float
    percent: float
    display: str


def share_bars(items: list[tuple[str, float]], *, limit: int = 10) -> list[ShareBar]:
    """Proportions of a whole, largest first, with the tail grouped as その他.

    Percentages are computed against the FULL total, not the truncated top-N,
    so the visible bars plus その他 sum to 100. Showing the top 10 rescaled to
    100% would overstate every one of them.
    """
    positive = [(label, value) for label, value in items if value > 0]
    total = sum(value for _, value in positive)
    if total <= 0:
        return []

    ranked = sorted(positive, key=lambda kv: kv[1], reverse=True)
    head, tail = ranked[:limit], ranked[limit:]
    bars = [
        ShareBar(
            label=label, value=value, percent=value / total * 100, display=format_axis_value(value)
        )
        for label, value in head
    ]
    if tail:
        rest = sum(value for _, value in tail)
        bars.append(
            ShareBar(
                label=f"その他 ({len(tail)}件)",
                value=rest,
                percent=rest / total * 100,
                display=format_axis_value(rest),
            )
        )
    return bars


__all__ = [
    "Bar",
    "ChartModel",
    "Point",
    "RenderedSeries",
    "ShareBar",
    "Tick",
    "axis_ticks",
    "bar_chart",
    "format_axis_value",
    "line_chart",
    "nice_ceiling",
    "share_bars",
    "thin_label_indices",
]
