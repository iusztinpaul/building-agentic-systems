import logging


def init_logger(level: int = logging.INFO) -> None:
    """Configure the root logger for script entry points.

    ``force=True`` because importing ``prefect`` (which every script does before
    calling this) installs a root handler at WARNING — without it,
    ``basicConfig`` is a silent no-op and the scripts' INFO logs vanish.
    Prefect's flow-run console logs live on the ``prefect.*`` loggers' own
    handlers, so replacing the root handler does not touch them.
    """

    logging.basicConfig(level=level, format="%(message)s", force=True)
    # httpx logs one INFO line per request — noise at script level.
    logging.getLogger("httpx").setLevel(logging.WARNING)
