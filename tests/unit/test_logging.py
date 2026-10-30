import json
import logging
import sys

from app.logging_config import HANDLER_NAME, JsonFormatter, TextFormatter, configure_logging
from tests.support import build_settings


def make_record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Hello %s",
        args=("world",),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_includes_context_and_extras() -> None:
    formatter = JsonFormatter("svc", "test")

    payload = json.loads(formatter.format(make_record(transaction_id="txn_1", attempt=2)))

    assert payload["message"] == "Hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["service"] == "svc"
    assert payload["environment"] == "test"
    assert payload["transaction_id"] == "txn_1"
    assert payload["attempt"] == 2
    assert payload["timestamp"].endswith("+00:00")


def test_json_formatter_includes_exception() -> None:
    formatter = JsonFormatter("svc", "test")
    try:
        raise ValueError("boom")
    except ValueError:
        record = make_record()
        record.exc_info = sys.exc_info()

    payload = json.loads(formatter.format(record))

    assert "ValueError: boom" in payload["exception"]


def test_text_formatter_appends_extras() -> None:
    formatter = TextFormatter()

    assert formatter.format(make_record()).endswith("app.test: Hello world")
    assert formatter.format(make_record(transaction_id="txn_1")).endswith("| transaction_id=txn_1")


def test_configure_logging_is_idempotent_and_preserves_foreign_handlers() -> None:
    root = logging.getLogger()
    foreign = logging.NullHandler()
    root.addHandler(foreign)
    try:
        configure_logging(build_settings(log_json=True, log_level="INFO"))
        configure_logging(build_settings(log_json=False, log_level="DEBUG"))

        owned = [handler for handler in root.handlers if handler.get_name() == HANDLER_NAME]
        assert len(owned) == 1
        assert isinstance(owned[0].formatter, TextFormatter)
        assert foreign in root.handlers
        assert root.level == logging.DEBUG
    finally:
        root.removeHandler(foreign)
        configure_logging(build_settings())
