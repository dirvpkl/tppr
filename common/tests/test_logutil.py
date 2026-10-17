import io
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import logutil


def _capture(service: str) -> tuple[logging.Logger, io.StringIO]:
    """Configure `service` logging into an in-memory stream and return both."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        logging.Formatter(logutil.LOG_FORMAT, datefmt=logutil.DATE_FORMAT)
    )
    handler.addFilter(logutil._CorrelationFilter())
    logger = logging.getLogger(service)
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger, stream


class FormatTests(unittest.TestCase):
    def test_format_has_service_and_rid_fields(self) -> None:
        self.assertIn("service=%(name)s", logutil.LOG_FORMAT)
        self.assertIn("rid=%(rid)s", logutil.LOG_FORMAT)
        self.assertIn("level=%(levelname)s", logutil.LOG_FORMAT)

    def test_line_carries_service_and_bound_rid(self) -> None:
        logger, stream = _capture("tppr-test")
        with logutil.bound_correlation_id("abc123"):
            logger.info("hello")
        line = stream.getvalue()
        self.assertIn("service=tppr-test", line)
        self.assertIn("rid=abc123", line)
        self.assertIn("hello", line)


class CorrelationTests(unittest.TestCase):
    def test_unbound_rid_defaults_to_dash(self) -> None:
        logger, stream = _capture("tppr-test")
        logger.info("outside")
        self.assertIn("rid=-", stream.getvalue())

    def test_generated_rid_is_short_and_restored(self) -> None:
        before = logutil.current_correlation_id()
        with logutil.bound_correlation_id() as rid:
            self.assertEqual(logutil.current_correlation_id(), rid)
            self.assertEqual(len(rid), 12)
        self.assertEqual(logutil.current_correlation_id(), before)

    def test_nested_rid_restores_parent(self) -> None:
        with logutil.bound_correlation_id("outer"):
            with logutil.bound_correlation_id("inner"):
                self.assertEqual(logutil.current_correlation_id(), "inner")
            self.assertEqual(logutil.current_correlation_id(), "outer")


class RedactionTests(unittest.TestCase):
    def test_redact_account_truncates(self) -> None:
        self.assertEqual(logutil.redact_account("supersecretaccount"), "supersec...")
        self.assertEqual(logutil.redact_account(""), "-")

    def test_redact_hides_full_identifier(self) -> None:
        self.assertNotIn(
            "supersecretaccount", logutil.redact_account("supersecretaccount")
        )

    def test_redact_path_masks_identifier_segments(self) -> None:
        self.assertEqual(logutil.redact_path("/accounts/opencode-acc"), "/accounts/...")
        self.assertEqual(logutil.redact_path("/leases/pool/user"), "/leases/...")
        self.assertEqual(logutil.redact_path("/status"), "/status")
        self.assertEqual(logutil.redact_path("/accounts"), "/accounts")
        self.assertNotIn("opencode-acc", logutil.redact_path("/accounts/opencode-acc"))

    def test_node_token_stable_and_opaque(self) -> None:
        first = logutil.node_token("http", "10.0.0.1", 8080)
        second = logutil.node_token("http", "10.0.0.1", 8080)
        other = logutil.node_token("socks5", "10.0.0.1", 8080)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertEqual(len(first), 8)
        # No proxy material leaks through the token.
        self.assertNotIn("10.0.0.1", first)


class ConfigureLoggingTests(unittest.TestCase):
    def test_configure_logging_returns_service_logger(self) -> None:
        previous = list(logging.getLogger().handlers)
        try:
            logger = logutil.configure_logging("tppr-cfg-test")
            self.assertEqual(logger.name, "tppr-cfg-test")
            root = logging.getLogger()
            self.assertTrue(root.handlers)
            # Every handler carries the correlation filter.
            for handler in root.handlers:
                self.assertTrue(
                    any(
                        isinstance(f, logutil._CorrelationFilter)
                        for f in handler.filters
                    )
                )
        finally:
            for handler in list(logging.getLogger().handlers):
                logging.getLogger().removeHandler(handler)
            for handler in previous:
                logging.getLogger().addHandler(handler)


if __name__ == "__main__":
    unittest.main()
