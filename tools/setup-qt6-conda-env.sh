#!/usr/bin/env bash
# One-time setup: make PyQt6 work in Anaconda without reinstalling before each run.
#
# Usage (from any directory):
#   bash tools/setup-qt6-conda-env.sh
#
# This writes activate/deactivate hooks into the active conda env so Qt6 always
# finds pip PyQt6's cocoa plugin instead of conda's Qt5 plugins.

set -euo pipefail

if [[ -z "${CONDA_PREFIX:-}" ]]; then
  echo "No conda env active. Run: conda activate base   (or your env)" >&2
  exit 1
fi

ACTIVATE_DIR="$CONDA_PREFIX/etc/conda/activate.d"
DEACTIVATE_DIR="$CONDA_PREFIX/etc/conda/deactivate.d"
mkdir -p "$ACTIVATE_DIR" "$DEACTIVATE_DIR"

ACTIVATE_FILE="$ACTIVATE_DIR/qt6-pyqt6.sh"
DEACTIVATE_FILE="$DEACTIVATE_DIR/qt6-pyqt6.sh"

cat > "$ACTIVATE_FILE" <<'EOF'
# Prefer pip PyQt6 (Qt6) platform plugins over conda Qt5.
_qt6_pyqt6_plugins() {
  python - <<'PY'
import site
from pathlib import Path
for base in site.getsitepackages() + [site.getusersitepackages()]:
    root = Path(base) / "PyQt6" / "Qt6" / "plugins"
    if (root / "platforms").is_dir():
        print(root)
        break
PY
}

_qt6_root="$(_qt6_pyqt6_plugins)"
if [[ -n "$_qt6_root" ]]; then
  export _QT6_SAVED_QPA="${QT_QPA_PLATFORM_PLUGIN_PATH-}"
  export _QT6_SAVED_PLUGIN="${QT_PLUGIN_PATH-}"
  export QT_QPA_PLATFORM_PLUGIN_PATH="$_qt6_root/platforms"
  export QT_PLUGIN_PATH="$_qt6_root"
fi
unset _qt6_pyqt6_plugins _qt6_root
EOF

cat > "$DEACTIVATE_FILE" <<'EOF'
if [[ -n "${_QT6_SAVED_QPA+x}" ]]; then
  export QT_QPA_PLATFORM_PLUGIN_PATH="$_QT6_SAVED_QPA"
  unset _QT6_SAVED_QPA
else
  unset QT_QPA_PLATFORM_PLUGIN_PATH
fi
if [[ -n "${_QT6_SAVED_PLUGIN+x}" ]]; then
  export QT_PLUGIN_PATH="$_QT6_SAVED_PLUGIN"
  unset _QT6_SAVED_PLUGIN
else
  unset QT_PLUGIN_PATH
fi
EOF

chmod +x "$ACTIVATE_FILE" "$DEACTIVATE_FILE"

echo "Installed Qt6 hooks for env: $CONDA_PREFIX"
echo "  activate:   $ACTIVATE_FILE"
echo "  deactivate: $DEACTIVATE_FILE"
echo ""
echo "Run:  conda deactivate && conda activate"
echo "Then: cd host && python -m collier_live"
