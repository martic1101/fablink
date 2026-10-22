"""Hang diagnostics.

pytest-timeout (configured in pyproject.toml) fails a test that runs too long,
so CI can never hang for hours. Before that happens, this prints every pending
asyncio task with where it is waiting, which a plain thread dump can't show.
"""

import asyncio
import gc
import sys
import threading

import pytest

REPORT_AFTER = 45  # seconds; pytest-timeout kills the test later


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    timer = threading.Timer(REPORT_AFTER, _report_pending_tasks, args=(item.nodeid,))
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


def _report_pending_tasks(nodeid: str) -> None:
    lines = [f"\n=== {nodeid} still running after {REPORT_AFTER}s. Pending asyncio tasks: ==="]
    for obj in gc.get_objects():
        if isinstance(obj, asyncio.Task) and not obj.done():
            lines.append(f"--- {obj!r}")
            for frame in obj.get_stack(limit=10):
                lines.append(f"      {frame.f_code.co_filename}:{frame.f_lineno} in {frame.f_code.co_name}")
    sys.__stderr__.write("\n".join(lines) + "\n")
    sys.__stderr__.flush()
