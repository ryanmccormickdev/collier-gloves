"""Launch Collier ML Live with PyQt GUI (qasync + BLE loop)."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_repo = Path(__file__).resolve().parents[1]
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))

from qt_env import bootstrap_qt  # noqa: E402

bootstrap_qt()

import qasync  # noqa: E402
from PyQt6 import QtWidgets  # noqa: E402

from collier.config import Config  # noqa: E402

from .config import LiveConfig  # noqa: E402
from .conductor import LiveConductor  # noqa: E402
from .logging_setup import configure_logging  # noqa: E402
from .preview.bridge import LiveBridge  # noqa: E402
from .preview.window import LiveWindow  # noqa: E402


def run_gui(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Collier ML Live (GUI)")
    p.add_argument("--manifest", type=Path, default=None)
    p.add_argument("--regions", type=int, default=4)
    p.add_argument("--osc-host", default="127.0.0.1")
    p.add_argument("--osc-port", type=int, default=7400)
    p.add_argument("--headless", action="store_true", help="No GUI (terminal only)")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--engine", choices=("pick", "ml"), default="pick")
    p.add_argument(
        "--no-filter-pose-gate",
        action="store_true",
        help="Stream /expr/filters without chest-pose gate.",
    )
    p.add_argument(
        "--flex-window-s",
        type=float,
        default=60.0,
        help="Sliding min/max window for finger flex normalization (default: 60).",
    )
    args = p.parse_args(argv)

    configure_logging(debug=args.debug)

    live = LiveConfig(
        manifest_path=args.manifest,
        n_regions=args.regions,
        debug=args.debug,
        engine=args.engine,
    )
    if args.no_filter_pose_gate:
        live.filter_pose.enabled = False
    live.osc.host = args.osc_host
    live.osc.port = args.osc_port
    live.flex_window_s = args.flex_window_s

    if args.headless:
        from .run import run_headless

        run_headless(live)
        return

    manifest_path = live.resolve_manifest()
    bridge = LiveBridge(class_names=[])
    conductor = LiveConductor(live, Config(), bridge=bridge)
    bridge.class_names = conductor._ml.class_names

    app = QtWidgets.QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = LiveWindow(bridge, n_regions=live.n_regions)
    window.show()

    asyncio.ensure_future(conductor.run())

    with loop:
        loop.run_forever()


def main() -> None:
    run_gui()


if __name__ == "__main__":
    main()
