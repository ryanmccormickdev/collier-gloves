"""Logging setup for collier_live CLI — keeps BLE/asyncio quiet."""
from __future__ import annotations

import logging


def configure_logging(*, debug: bool) -> None:
    """``--debug`` enables collier_live diagnostics only, not bleak scan spam."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
        force=True,
    )
    for noisy in (
        "asyncio",
        "bleak",
        "bleak.backends",
        "bleak.backends.corebluetooth",
        "urllib3",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if debug:
        logging.getLogger("collier_live").setLevel(logging.DEBUG)
        logging.getLogger("collier").setLevel(logging.INFO)
    else:
        logging.getLogger("collier_live").setLevel(logging.INFO)
        logging.getLogger("collier").setLevel(logging.WARNING)
