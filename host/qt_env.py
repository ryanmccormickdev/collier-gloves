"""
Point Qt6 at pip-installed PyQt6 plugins before the GUI loads.

Anaconda base ships Qt5 plugins under $CONDA_PREFIX/plugins. PyQt6 (Qt6) looks
there first and fails to load libqcocoa, or loads Qt5 libraries alongside Qt6.
Call bootstrap_qt() once at process startup, before any PyQt6 import.
"""

from __future__ import annotations

import os
import site
from pathlib import Path


def _pyqt6_plugin_dirs() -> tuple[Path, Path] | None:
    for base in site.getsitepackages() + [site.getusersitepackages()]:
        plugins = Path(base) / "PyQt6" / "Qt6" / "plugins"
        platforms = plugins / "platforms"
        if platforms.is_dir():
            return platforms, plugins
    return None


def bootstrap_qt() -> None:
    """Configure Qt6 plugin search paths (safe to call multiple times)."""
    # Force every Qt-shim library (qasync, qtpy, pyqtgraph, matplotlib, ...) to
    # bind to PyQt6. Without this, anaconda's bundled PyQt5 may get imported
    # first, dragging in libQt5Core alongside Qt6 and segfaulting on macOS.
    os.environ["QT_API"] = "pyqt6"
    os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")
    os.environ.setdefault("MPLBACKEND", "QtAgg")

    # Block any stray `import PyQt5` from succeeding in this process.
    import sys
    sys.modules.setdefault("PyQt5", None)  # type: ignore[assignment]

    found = _pyqt6_plugin_dirs()
    if found is None:
        return
    platforms, plugins = found
    os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", str(platforms))
    os.environ.setdefault("QT_PLUGIN_PATH", str(plugins))
