import logging
import threading

from obp_sentinel.logs import FORMAT, InstanceFilter, about, for_instance


def test_lines_name_the_instance_url_of_their_thread():
    lines = []
    handler = logging.Handler()
    handler.emit = lambda record: lines.append(logging.Formatter(FORMAT).format(record))
    handler.addFilter(InstanceFilter())
    log = logging.getLogger("test-logs")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        t = for_instance(threading.Thread(target=lambda: log.info("polled")), "https://staging.example.com")
        t.start()
        t.join()
        with about("http://localhost:8080"):
            log.info("scheduled")
        log.info("page up")
    finally:
        log.removeHandler(handler)
    assert "[https://staging.example.com] test-logs: polled" in lines[0]
    assert "[http://localhost:8080] test-logs: scheduled" in lines[1]
    assert lines[2].endswith(" test-logs: page up")
