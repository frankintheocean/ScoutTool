import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler
from database import DB_NAME


def _build_logger(name="scoutbot"):
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 🪟 In a --windowed PyInstaller build there is no console at all, so
    # sys.stdout is None (not just closed) and StreamHandler(None) would
    # blow up on the very first log call. Skip the console handler in
    # that case — bot.log below still captures everything.
    if sys.stdout is not None:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(Path(DB_NAME).parent / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


logger = _build_logger()
