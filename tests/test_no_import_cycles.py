"""Every CLI must be importable, and services must not import the UI.

`app.cli.import_categories` had never been runnable. Importing it raised
`ImportError: cannot import name 'load_overview' from partially initialized
module 'app.services.categories'`, and the cycle behind it was:

    app.services.categories  ->  app.ui.csv_intake
                             ->  app/ui/__init__.py  (imports every route)
                             ->  app.ui.routes.categories
                             ->  app.services.categories   (still initialising)

Nothing caught it because the unit tests import the pure helpers directly and
the app itself imports `app.ui` first, which happens to order the cycle
harmlessly. Only a CLI entry point — the one path nothing tested — hit it.

Two guards. The first imports every CLI module, which is cheap and would have
caught this the day it appeared. The second states the rule that caused it: the
service and CLI layers do not import the UI layer. `csv_intake` moved to
`app/csv_intake.py` to satisfy it.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _cli_module_names() -> list[str]:
    import app.cli

    return sorted(m.name for m in pkgutil.iter_modules(app.cli.__path__))


@pytest.mark.parametrize("name", _cli_module_names())
def test_every_cli_module_imports(name: str) -> None:
    """A CLI that cannot be imported cannot be run, and nothing else in the
    suite would notice — every other test imports the helpers directly."""
    importlib.import_module(f"app.cli.{name}")


def test_the_application_imports() -> None:
    importlib.import_module("app.main")


#: `from app.ui...` / `import app.ui...` anywhere below the UI layer.
_UI_IMPORT = re.compile(r"^\s*(?:from|import)\s+app\.ui\b", re.MULTILINE)

#: Rendering a CSV download means returning a FastAPI Response, which is a UI
#: concern by definition. Parsing an uploaded one is not, which is why only the
#: export half still lives there.
_ALLOWED = {
    "app/ui",  # the UI layer may import itself
}


def test_services_and_clis_do_not_import_the_ui_layer() -> None:
    """The direction that caused the cycle.

    `app/ui/__init__.py` imports every route, so ANY `app.ui.*` import from a
    lower layer drags the whole UI in — and any route importing that lower
    module closes the loop. The rule is one-directional: UI may import services,
    never the reverse.
    """
    offenders: list[str] = []
    for folder in ("app/services", "app/cli", "app/adapters", "app/models", "app/queue"):
        for path in sorted(Path(folder).rglob("*.py")):
            rel = path.as_posix()
            if any(rel.startswith(a) for a in _ALLOWED):
                continue
            if _UI_IMPORT.search(path.read_text(encoding="utf-8")):
                offenders.append(rel)
    assert not offenders, (
        "these import app.ui from below the UI layer, which pulls in every route "
        f"and risks the import cycle that made import_categories unrunnable: {offenders}"
    )
