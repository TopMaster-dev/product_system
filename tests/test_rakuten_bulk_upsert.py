"""在庫API 2.0 のリクエスト組み立てと応答の読み取り.

Every assertion here traces to a line of the client's RMS spec (2026-10-19).
This module is the only place that knows Rakuten's wire contract, so if Rakuten
revises it, these tests are the diff.
"""

from __future__ import annotations

import json

import pytest

from app.adapters.rakuten_bulk_upsert import (
    MAX_KEY_BYTES,
    MAX_QUANTITY,
    MAX_ROWS_PER_REQUEST,
    InventoryRow,
    RowRejected,
    batches,
    build_body,
    build_variant_body,
    parse_errors,
    rows_to_reread,
    succeeded,
    variant_path,
)

pytestmark = pytest.mark.unit

ROW = InventoryRow("073", "1435", 23)


# --- リクエストの組み立て ----------------------------------------------------


def test_the_bulk_body_is_exactly_the_documented_shape() -> None:
    assert build_body([ROW]) == {
        "inventories": [
            {"manageNumber": "073", "variantId": "1435", "mode": "ABSOLUTE", "quantity": 23}
        ]
    }


def test_the_quantity_is_a_json_number_not_a_string() -> None:
    """The spec is explicit, and a numeric string is a different thing on the
    wire — it would be rejected per row with no per-row status to read."""
    sent = json.loads(json.dumps(build_body([InventoryRow("073", "1435", 7)])))
    assert sent["inventories"][0]["quantity"] == 7
    assert not isinstance(sent["inventories"][0]["quantity"], str)


def test_the_single_sku_body_carries_no_inventories_array() -> None:
    """The PUT form puts the keys in the URL; including the array would be a
    different request entirely."""
    assert build_variant_body(ROW) == {"mode": "ABSOLUTE", "quantity": 23}
    assert variant_path(ROW) == "/es/2.0/inventories/manage-numbers/073/variants/1435"


def test_a_full_batch_is_allowed_and_one_more_is_not() -> None:
    rows = [InventoryRow("073", str(i), 1) for i in range(MAX_ROWS_PER_REQUEST + 1)]
    assert len(build_body(rows[:MAX_ROWS_PER_REQUEST])["inventories"]) == MAX_ROWS_PER_REQUEST
    with pytest.raises(RowRejected, match="400件まで"):
        build_body(rows)


def test_batching_preserves_order_and_loses_nothing() -> None:
    rows = [InventoryRow("073", str(i), i) for i in range(1000)]
    chunks = batches(rows)
    assert [len(c) for c in chunks] == [400, 400, 200]
    assert [row for chunk in chunks for row in chunk] == rows


def test_an_empty_request_is_refused_rather_than_sent() -> None:
    with pytest.raises(RowRejected):
        build_body([])
    assert batches([]) == []


# --- 送る前に止めるもの ------------------------------------------------------


def test_a_negative_quantity_is_refused() -> None:
    with pytest.raises(RowRejected, match="負の在庫数"):
        build_body([InventoryRow("073", "1435", -1)])


def test_a_quantity_over_the_ceiling_is_refused() -> None:
    build_body([InventoryRow("073", "1435", MAX_QUANTITY)])
    with pytest.raises(RowRejected, match="上限"):
        build_body([InventoryRow("073", "1435", MAX_QUANTITY + 1)])


def test_a_key_over_thirty_two_bytes_is_refused() -> None:
    """The limit is BYTES, so a Japanese key hits it at a third of the length a
    character count would suggest."""
    build_body([InventoryRow("a" * MAX_KEY_BYTES, "1435", 1)])
    with pytest.raises(RowRejected, match="32バイト"):
        build_body([InventoryRow("a" * (MAX_KEY_BYTES + 1), "1435", 1)])
    with pytest.raises(RowRejected, match="32バイト"):
        build_body([InventoryRow("073", "あ" * 11, 1)])


def test_an_empty_key_is_refused() -> None:
    with pytest.raises(RowRejected):
        build_body([InventoryRow("", "1435", 1)])
    with pytest.raises(RowRejected):
        build_body([InventoryRow("073", "", 1)])


def test_one_bad_row_stops_the_request_before_it_is_sent() -> None:
    """Rakuten would refuse the whole request mid-batch, taking 399 good rows
    with it. Refusing locally is how the caller learns which row."""
    rows = [InventoryRow("073", str(i), 1) for i in range(10)]
    rows[7] = InventoryRow("073", "7", -5)
    with pytest.raises(RowRejected, match="073/7"):
        build_body(rows)


# --- 応答の読み取り ----------------------------------------------------------


def test_success_is_two_oh_four_and_nothing_else() -> None:
    assert succeeded(204)
    for other in (200, 201, 202, 400, 401, 404, 500):
        assert not succeeded(other)


def test_the_error_array_is_read_including_the_property_path() -> None:
    errors = parse_errors(
        {
            "errors": [
                {
                    "code": "INVALID_VALUE",
                    "message": "quantity is invalid",
                    "metadata": {"propertyPath": "inventories[3].quantity"},
                }
            ]
        }
    )
    assert len(errors) == 1
    assert errors[0].code == "INVALID_VALUE"
    assert errors[0].property_path == "inventories[3].quantity"
    assert "inventories[3].quantity" in str(errors[0])


@pytest.mark.parametrize(
    "body",
    [None, "", [], {}, {"errors": []}, {"errors": "nope"}, "<html>502</html>", {"message": "x"}],
)
def test_a_body_we_cannot_read_never_looks_like_no_error(body: object) -> None:
    """An empty list here would read as "nothing was wrong", which is exactly
    how a rejected write gets recorded as written."""
    errors = parse_errors(body)
    assert errors
    assert errors[0].code == "UNPARSEABLE"


def test_a_malformed_entry_inside_a_good_array_is_still_reported() -> None:
    errors = parse_errors({"errors": [{"code": "A", "message": "m"}, "junk"]})
    assert [e.code for e in errors] == ["A", "UNPARSEABLE"]


# --- 失敗したあとに確かめるもの ----------------------------------------------


def test_nothing_needs_rereading_after_a_clean_success() -> None:
    assert rows_to_reread([ROW], 204) == []


def test_every_row_needs_rereading_after_anything_else() -> None:
    """The response carries no per-row status and the spec does not say whether
    a rejected bulk request applies none, some or all of its rows. Assuming
    "none" would leave a silent partial write on live pages."""
    rows = [InventoryRow("073", str(i), 1) for i in range(5)]
    for status in (400, 401, 409, 500):
        assert rows_to_reread(rows, status) == rows
