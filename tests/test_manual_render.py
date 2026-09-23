"""操作手順書 (P2-048).

The manual is the deliverable the client actually operates from, and it is the
one document that cannot drift: it is served from the running app, so a screen
that exists and a manual that does not mention it are visibly inconsistent.

These tests pin the two things a reader has to be told, because the screen
cannot tell them on its own:

  * the rules that make a partial stocktake safe, and
  * why two numbers that look comparable are not (flows vs balances, a
    per-SKU low-stock threshold, 在庫管理対象外 vs アーカイブ).

They check content, not layout. Rewording is expected; dropping a rule is not.
"""

from __future__ import annotations

import pytest

from app.ui.deps import templates

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def html() -> str:
    return templates.env.get_template("manual.html").render(
        operator="tester", version="test", current_path="/admin/manual"
    )


def test_the_manual_renders(html: str) -> None:
    assert "管理画面 操作手順書" in html


def test_every_phase_2_screen_is_documented(html: str) -> None:
    """A screen in the navigation and not in the manual is the drift this test
    exists to catch."""
    for screen in ("在庫一覧", "欠品リスク", "棚卸", "データ品質", "カテゴリ", "手動調整"):
        assert screen in html, screen


def test_the_inline_ui_labels_are_not_escaped(html: str) -> None:
    """The macros emit markup. If a helper stringifies it, the page fills with
    visible &lt;span&gt; instead of key caps."""
    assert "&lt;span" not in html


# --- 棚卸: 部分実施を安全にする3つのルール --------------------------------


def test_the_manual_states_that_absent_skus_are_untouched(html: str) -> None:
    """The rule the whole split stocktake rests on. Without it, counting rings
    on Monday looks like it will zero the necklaces."""
    assert "シートに無いSKUは変更されません" in html


def test_the_manual_states_that_a_blank_count_is_uncounted(html: str) -> None:
    assert "未計数" in html


def test_the_manual_states_that_duplicates_are_not_summed(html: str) -> None:
    """Summing two counts of one shelf doubles the stock."""
    assert "合算はしません" in html


def test_the_manual_warns_against_copying_the_printed_stock(html: str) -> None:
    """Copying the printed figure turns a count into a confirmation."""
    assert "書き写さないでください" in html


def test_the_counting_date_is_distinguished_from_the_upload_date(html: str) -> None:
    assert "実際に数えた日" in html


# --- 読み間違えやすい数字 --------------------------------------------------


def test_flows_and_balances_are_explained(html: str) -> None:
    """28 days of "we hold 5,000" is not 140,000. Nobody catches a stock figure
    28x too high by looking at it."""
    assert "期間の合計" in html
    assert "期間の最終日" in html


def test_turnover_is_stated_as_period_based(html: str) -> None:
    """Annualising a 7-day window and comparing it against a 90-day one is the
    misreading this sentence prevents."""
    assert "年換算していません" in html


def test_the_low_stock_threshold_is_explained_as_per_sku(html: str) -> None:
    """Otherwise two SKUs at the same quantity showing different badges reads
    as a bug."""
    assert "SKUごとに変わります" in html


def test_insufficient_data_is_explained_rather_than_left_blank(html: str) -> None:
    assert "予測不可" in html
    assert "データ不足" in html


def test_unmapped_revenue_is_explained(html: str) -> None:
    """It is deliberately outside the sales figure, and the client will ask why
    the two numbers differ."""
    assert "未マッピング売上" in html
    assert "別枠" in html


# --- 混同されやすい概念 ----------------------------------------------------


def test_unmanaged_and_archived_are_distinguished(html: str) -> None:
    """They look alike on screen and mean opposite things — one has no stock,
    the other has no future but a real past."""
    assert "在庫管理対象外" in html
    assert "アーカイブ" in html
    assert "過去の期間の売上集計には含まれます" in html


def test_the_bundle_attribution_rule_is_stated(html: str) -> None:
    """P2-042 requires agreeing this is the specification, not a defect. Both
    halves matter: where the sale lands, and where the stock comes off."""
    assert "売上は注文された商品" in html
    assert "在庫は共有している構成品から減らします" in html


def test_the_operations_that_change_past_numbers_are_listed(html: str) -> None:
    """A client who sees last month's figure change without warning stops
    trusting all of them."""
    assert "数字が変わる操作" in html
    assert "過去の注文のキャンセル" in html


def test_the_blank_sku_alert_is_called_out(html: str) -> None:
    """Resolving it would attribute several products to one master."""
    assert "商品コードが空欄のアラートは解決できません" in html
