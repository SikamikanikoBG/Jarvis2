"""Every module must import on its own.

The app always reaches the feature modules through ``app.py``, which happens to import them in
an order that hides an import cycle. Anything else — a script, a migration helper, a test that
only needs one store, ``python -c "import jarvis_core.features.knowledge"`` — imports a module
first and gets an ImportError. This walks the package and imports each module into a fresh
module table, which is what "first" means.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

import pytest

import jarvis_core

_PACKAGE_ROOT = Path(jarvis_core.__file__).parent


def _module_names() -> list[str]:
    names = [
        m.name
        for m in pkgutil.walk_packages([str(_PACKAGE_ROOT)], prefix="jarvis_core.")
        if not m.ispkg and not m.name.endswith(".main")  # .main starts a server
    ]
    assert len(names) > 25, f"expected the whole package, walked only {names}"
    return sorted(names)


@pytest.mark.parametrize("module", _module_names())
def test_module_imports_first(module: str) -> None:
    saved = {name: mod for name, mod in sys.modules.items() if name.startswith("jarvis_core")}
    for name in saved:
        del sys.modules[name]
    try:
        importlib.import_module(module)
    finally:
        sys.modules.update(saved)
