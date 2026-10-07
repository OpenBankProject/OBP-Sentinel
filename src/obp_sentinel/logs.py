"""Logging. Sentinel may watch several OBP-API instances, so every line names the one it is about, by its URL.

Each instance's work runs in its own threads (collector, analyst); a thread is tagged with its instance's URL
when it starts, and every line logged from it carries that URL.
"""

import logging
import threading
from contextlib import contextmanager

FORMAT = "%(asctime)s %(levelname)s %(instance)s%(name)s: %(message)s"


def for_instance(thread: threading.Thread, obp_base_url: str) -> threading.Thread:
    """Tag `thread` (before starting it) so its log lines name `obp_base_url`."""
    thread.obp_base_url = obp_base_url
    return thread


@contextmanager
def about(obp_base_url: str):
    """Within this block, lines logged from the current thread name `obp_base_url` (for threads shared by
    all instances: the main thread, the web page's)."""
    thread = threading.current_thread()
    before = getattr(thread, "obp_base_url", None)
    thread.obp_base_url = obp_base_url
    try:
        yield
    finally:
        thread.obp_base_url = before


class InstanceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        url = getattr(threading.current_thread(), "obp_base_url", None)
        record.instance = f"[{url}] " if url else ""
        return True


def setup(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=FORMAT)
    for handler in logging.getLogger().handlers:
        handler.addFilter(InstanceFilter())
