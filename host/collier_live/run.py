"""CLI entry for collier_live."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from collier.config import Config

from .config import LiveConfig
from .conductor import LiveConductor
from .logging_setup import configure_logging


def run_headless(live: LiveConfig, collier_cfg: Config | None = None) -> None:
    conductor = LiveConductor(live, collier_cfg or Config())
    asyncio.run(conductor.run())


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Collier ML Live — glove gestures to OSC")
    p.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to gesture manifest JSON (overrides active.json)",
    )
    p.add_argument("--regions", type=int, default=4)
    p.add_argument("--osc-host", default="127.0.0.1")
    p.add_argument("--osc-port", type=int, default=7400)
    p.add_argument("--headless", action="store_true", help="Terminal only, no GUI")
    p.add_argument("--debug", action="store_true")
    p.add_argument(
        "--engine",
        choices=("pick", "ml"),
        default="pick",
        help="'pick' (default) = heuristic state-machine engine, calibration-drift "
             "resistant. 'ml' = legacy TCN inference path.",
    )
    p.add_argument(
        "--no-filter-pose-gate",
        action="store_true",
        help="Stream /expr/filters every tick (old behavior). Default: only "
             "while holding the chest pose.",
    )
    p.add_argument(
        "--flex-window-s",
        type=float,
        default=60.0,
        help="Sliding min/max window for finger flex normalization (default: 60).",
    )
    args = p.parse_args(argv)

    if not args.headless:
        from .gui import run_gui

        run_gui(argv)
        return

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
    run_headless(live, Config())


if __name__ == "__main__":
    main()
