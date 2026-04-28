"""Entry point for OmniRouter.

Boots logging, starts the DICOM SCP, and serves the web UI on ``WEB_PORT``.
"""

from __future__ import annotations

import logging

import uvicorn

from .config import WEB_HOST, WEB_PORT
from .log_bus import configure_logging


def main() -> None:
    configure_logging(level=logging.INFO)
    uvicorn.run(
        "omnirouter.web:app",
        host=WEB_HOST,
        port=WEB_PORT,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
