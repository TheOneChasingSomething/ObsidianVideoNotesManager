"""
logger.py — Configuration centralisée du système de logging.
"""

import logging
import logging.handlers
from pathlib import Path

from rich.logging import RichHandler


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "yt_playlist_dl.log"

    root = logging.getLogger("yt_playlist_dl")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")
    )

    console_handler = RichHandler(rich_tracebacks=True, markup=True, show_path=False)
    console_handler.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not root.handlers:
        root.addHandler(file_handler)
        root.addHandler(console_handler)


def get_logger(name: str) -> logging.Logger:
    full_name = f"yt_playlist_dl.{name}" if not name.startswith("yt_playlist_dl") else name
    return logging.getLogger(full_name)
