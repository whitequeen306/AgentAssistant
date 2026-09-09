"""Cooperative cancel helpers."""

import threading

from agent_assistant.agent.cancellation import (
    bind_cancel,
    is_cancelled,
    request_cancel,
)


def test_bind_and_request_cancel():
    ev = threading.Event()
    assert not is_cancelled()
    with bind_cancel(ev):
        assert not is_cancelled()
        assert request_cancel() is True
        assert is_cancelled()
        assert ev.is_set()
    assert not is_cancelled()


def test_request_cancel_when_unbound():
    assert request_cancel() is False


def test_interleaved_thread_bindings_are_isolated_and_reset():
    first_bound = threading.Event()
    second_bound = threading.Event()
    release = threading.Event()
    first_cancel = threading.Event()
    second_cancel = threading.Event()
    seen = {}

    def run_first():
        with bind_cancel(first_cancel):
            first_bound.set()
            second_bound.wait(timeout=2)
            first_cancel.set()
            seen["first"] = is_cancelled()
            release.wait(timeout=2)
        seen["first_after"] = is_cancelled()

    def run_second():
        first_bound.wait(timeout=2)
        with bind_cancel(second_cancel):
            second_bound.set()
            release.wait(timeout=2)
            seen["second"] = is_cancelled()
        seen["second_after"] = is_cancelled()

    first = threading.Thread(target=run_first)
    second = threading.Thread(target=run_second)
    first.start()
    second.start()
    second_bound.wait(timeout=2)
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert seen == {
        "first": True,
        "first_after": False,
        "second": False,
        "second_after": False,
    }
