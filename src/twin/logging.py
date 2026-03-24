import logging


def init_logger(level: int = logging.INFO) -> None:
    """Configure the root logger for script entry points."""

    logging.basicConfig(level=level, format="%(message)s")
