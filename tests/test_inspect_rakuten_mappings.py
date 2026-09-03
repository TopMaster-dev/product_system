"""The mapping-shape report's SQL is Postgres-only and prod-only.

It uses the `~` regex operator, so SQLite cannot stand in for it, and the only
database it ever runs against is production. That combination means a broken
statement is discovered by running it on prod — which is exactly the situation
this file exists to avoid. Compiling each statement against the PostgreSQL
dialect with literal binds exercises the whole thing without a connection.

The channel-scoping assertion is not decoration: `channel` lives on `orders`,
not `order_items`, so a missing join silently reports Shopify's unmapped lines
as Rakuten's — a wrong number that looks entirely plausible.
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from app.cli.inspect_rakuten_mappings import collect

pytestmark = pytest.mark.unit


class _CompilingSession:
    """Compiles each statement instead of executing it."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def scalar(self, statement: object) -> int:
        self.statements.append(
            str(
                statement.compile(  # type: ignore[attr-defined]
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )
        )
        return 0


async def _compile_all() -> tuple[_CompilingSession, dict[str, object]]:
    session = _CompilingSession()
    stats = await collect(session, placeholder_pattern=r"^r-sku\d+$")  # type: ignore[arg-type]
    return session, stats


async def test_every_statement_compiles_for_postgres() -> None:
    session, stats = await _compile_all()
    assert session.statements, "collect issued no queries"
    assert len(stats) == len(session.statements), "a reported figure ran no query"


async def test_unmapped_counts_are_scoped_to_rakuten() -> None:
    """Without the join to orders these count every channel's unmapped lines."""
    session, _ = await _compile_all()
    joined = " ".join(session.statements)
    scoped = [s for s in session.statements if "order_items" in s]
    assert scoped, "no order-line query emitted"
    for statement in scoped:
        assert "orders.channel = 'rakuten'" in statement, statement
    assert "channel_sku_mappings.channel = 'rakuten'" in joined


async def test_the_placeholder_pattern_reaches_the_query() -> None:
    """A pattern that never lands in the SQL reports 0 placeholders forever."""
    session, _ = await _compile_all()
    assert any("r-sku" in s for s in session.statements)
