"""Exercise Streamlit reruns against the real API handlers, without network/LLM."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

UI = Path(__file__).resolve().parents[1] / "ui" / "app.py"


@pytest.fixture
def console(client, monkeypatch):
    import requests

    def request(method, url, **kwargs):
        kwargs.pop("timeout", None)
        return client.request(method, url.split("8139", 1)[1], **kwargs)

    monkeypatch.setattr(requests, "request", request)
    return AppTest.from_file(str(UI), default_timeout=90).run()


def button(app, label):
    return next(item for item in app.button if item.label == label)


def test_chat_session_display_and_close(console):
    assert not console.exception
    console.chat_input[0].set_value("Hello").run()
    assert not console.exception
    session_id = console.session_state["session_id"]
    assert session_id
    assert console.text_input(key="session_display").value == session_id
    assert len(console.chat_message) == 2
    button(console, "Close session").click().run()
    assert not console.exception
    assert console.session_state["session_id"] is None
    assert console.text_input(key="session_display").value == ""


def test_unknown_kb_document_shows_error_without_crashing(console):
    field = next(item for item in console.text_input if item.label == "Document id")
    field.set_value("does-not-exist")
    button(console, "Show versions").click().run()
    assert not console.exception
    assert console.error


def test_configuration_and_sweep(console):
    button(console, "Apply override").click().run()
    assert not console.exception
    assert any(item.value == "override applied" for item in console.success)
    button(console, "Run every sweep").click().run()
    assert not console.exception
