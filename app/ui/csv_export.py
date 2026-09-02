"""The single CSV *download* helper — the counterpart to `csv_intake`.

Every CSV this system emits is opened in Excel on Windows, and Excel decides a
CSV's encoding by one rule: **if there is a UTF-8 BOM, decode UTF-8; otherwise
use the system ANSI codepage.** It ignores the HTTP `charset` entirely, because
by the time the file is on disk there is no HTTP header left.

That rule broke both exports we handed a human on 2026-08-29:

* `categories.csv` was UTF-8 with no BOM, so Excel fell back to the system
  codepage and mangled 大分類 on *every* locale, Japanese included.
* `rakuten_sku_defects.csv` was CP932 with no BOM, which is correct only when
  the reader's ANSI codepage is 932. Opened on an English-locale Windows it
  decoded as Windows-1252: 仮値 (`89 BC 92 6C`) rendered as `‰¼'l`.

UTF-8 with a BOM is the one encoding that works on every locale without asking
the recipient to do anything, so it is what every export uses. It also
round-trips: `csv_intake` tries `utf-8-sig` first, so a file exported here can
be edited and re-uploaded — which is exactly the categories workflow.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from typing import Any

from fastapi.responses import Response

#: Excel's only reliable signal. Without it Excel guesses, and on a
#: non-Japanese Windows it guesses wrong.
UTF8_BOM = "﻿"


def csv_body(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """Render a CSV to a string. No BOM here — `csv_response` adds it."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


def csv_response(body: str, *, filename: str) -> Response:
    """Serve `body` as a downloadable CSV that Excel decodes correctly.

    `filename` must be ASCII: a non-ASCII value needs RFC 5987 encoding in the
    Content-Disposition header, and every filename we emit is ASCII by design
    so the header stays simple.
    """
    # A plain Response, not StreamingResponse: `body` is already a fully
    # materialised string, so streaming adds machinery without saving memory —
    # and it keeps the bytes inspectable, which is what lets a test assert the
    # BOM is there.
    return Response(
        content=(UTF8_BOM + body).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


__all__ = ["UTF8_BOM", "csv_body", "csv_response"]
