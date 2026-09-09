"""Third-party HTTP loggers must not dump request bodies into agent.log."""

import logging

from agent_assistant.main import quiet_third_party_loggers


def test_quiet_third_party_loggers_sets_warning():
    httpcore = logging.getLogger("httpcore")
    openai = logging.getLogger("openai")
    prev_http = httpcore.level
    prev_oa = openai.level
    try:
        httpcore.setLevel(logging.DEBUG)
        openai.setLevel(logging.DEBUG)
        quiet_third_party_loggers()
        assert httpcore.level == logging.WARNING
        assert openai.level == logging.WARNING
        assert logging.getLogger("httpx").level == logging.WARNING
    finally:
        httpcore.setLevel(prev_http)
        openai.setLevel(prev_oa)
