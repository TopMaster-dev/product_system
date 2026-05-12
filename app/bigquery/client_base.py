"""BigQueryClient protocol and shared types."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class BigQueryTable:
    """Identifies a destination table in BigQuery."""

    name: str
    partition_field: str | None = None  # e.g. "occurred_at" / "ordered_at"


@runtime_checkable
class BigQueryClient(Protocol):
    """Common interface implemented by every backend."""

    async def load_rows(
        self,
        table: BigQueryTable,
        rows: Iterable[dict[str, Any]],
        *,
        write_mode: str,
    ) -> int:
        """Load `rows` into `table`.

        write_mode: "append" (incremental) or "truncate" (snapshot).
        Returns the number of rows written.
        """
        ...
