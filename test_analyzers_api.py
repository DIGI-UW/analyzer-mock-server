"""
Unit tests for /analyzers HTTP API endpoints.

Tests POST /analyzers, GET /analyzers, and DELETE /analyzers/{name} with a
mocked network manager (Docker is not required).
"""

import json
import threading
import unittest
import http.client
from http.server import HTTPServer
from unittest.mock import MagicMock, patch

from api import MockAPIHandler as SimulateAPIHandler


class TestAnalyzersAPI(unittest.TestCase):
    """Tests for /analyzers CRUD endpoints with mocked network manager."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(("127.0.0.1", 0), SimulateAPIHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

        # Build a mock network manager used by all tests
        cls.mock_mgr = MagicMock()
        cls.mock_mgr.list_analyzers.return_value = [
            {"name": "test-analyzer", "ip": "172.20.0.2", "template": "mindray_bc5380"},
        ]
        cls.mock_mgr.create_analyzer.return_value = {
            "name": "my-analyzer",
            "ip": "172.20.0.3",
            "template": "mindray_bc5380",
        }
        cls.mock_mgr.remove_analyzer.side_effect = lambda n: n != "nonexistent"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=10)
        # Reset the class-level attribute so other test modules aren't affected
        SimulateAPIHandler._network_manager = None

    def setUp(self):
        # Patch _get_network_manager for every test so Docker is never touched
        patcher = patch.object(
            SimulateAPIHandler, "_get_network_manager", return_value=self.mock_mgr
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def _post_analyzers(self, body=None, headers=None, raw_body=None):
        """Send POST /analyzers and return (status, parsed_body)."""
        conn = self._conn()
        hdrs = headers or {}
        if body is not None and raw_body is None:
            raw_body = json.dumps(body)
        if raw_body is not None and "Content-Length" not in hdrs:
            hdrs.setdefault("Content-Type", "application/json")
        conn.request("POST", "/analyzers", body=raw_body, headers=hdrs)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        status = resp.status
        conn.close()
        return status, data

    def _get_analyzers(self):
        conn = self._conn()
        conn.request("GET", "/analyzers")
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        status = resp.status
        conn.close()
        return status, data

    def _delete_analyzer(self, name, query=""):
        path = f"/analyzers/{name}{query}"
        conn = self._conn()
        conn.request("DELETE", path)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        status = resp.status
        conn.close()
        return status, data

    def test_send_json_flushes_after_write(self):
        class RecordingWFile:
            def __init__(self):
                self.events = []
                self.payload = b""

            def write(self, data):
                self.events.append("write")
                self.payload += data

            def flush(self):
                self.events.append("flush")

        handler = SimulateAPIHandler.__new__(SimulateAPIHandler)
        handler.wfile = RecordingWFile()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler._send_json(201, {"name": "my-analyzer", "ip": "172.20.0.3"})

        self.assertEqual(handler.wfile.events, ["write", "flush"])
        self.assertEqual(
            json.loads(handler.wfile.payload.decode("utf-8")),
            {"name": "my-analyzer", "ip": "172.20.0.3"},
        )

    # ------------------------------------------------------------------
    # GET /analyzers
    # ------------------------------------------------------------------

    def test_get_analyzers_returns_200_with_list(self):
        status, body = self._get_analyzers()
        self.assertEqual(status, 200)
        self.assertIn("analyzers", body)
        self.assertIsInstance(body["analyzers"], list)
        self.assertEqual(body["analyzers"][0]["name"], "test-analyzer")

    # ------------------------------------------------------------------
    # POST /analyzers — success
    # ------------------------------------------------------------------

    def test_post_valid_analyzer_returns_201(self):
        status, body = self._post_analyzers({"name": "my-analyzer", "template": "mindray_bc5380"})
        self.assertEqual(status, 201)
        self.assertEqual(body["name"], "my-analyzer")

    def test_post_calls_create_with_connect_mock_false(self):
        """The 201 response path creates the network synchronously but must
        defer docker-attach so the HTTP caller doesn't wait for it."""
        self.mock_mgr.create_analyzer.reset_mock()
        status, _ = self._post_analyzers({"name": "async-1", "template": "t"})
        self.assertEqual(status, 201)
        self.mock_mgr.create_analyzer.assert_called_once()
        _, kwargs = self.mock_mgr.create_analyzer.call_args
        self.assertIs(kwargs.get("connect_mock"), False)

    def test_post_invokes_connect_mock_after_response(self):
        """The background thread must call connect_mock_to_analyzer(name).
        Patch threading.Thread so the target runs synchronously and we can
        assert on mgr.connect_mock_to_analyzer without a sleep race."""
        self.mock_mgr.connect_mock_to_analyzer.reset_mock()
        self.mock_mgr.connect_mock_to_analyzer.return_value = True

        real_thread = threading.Thread

        def _inline_thread(target=None, args=(), kwargs=None, daemon=None):
            # Run synchronously in the current thread so the assertion below
            # is deterministic. Return a thread-like object with start()=noop.
            if target:
                target(*args, **(kwargs or {}))
            return MagicMock(start=lambda: None)

        with patch("api.threading.Thread", side_effect=_inline_thread):
            status, _ = self._post_analyzers({"name": "async-2", "template": "t"})

        self.assertEqual(status, 201)
        self.mock_mgr.connect_mock_to_analyzer.assert_called_once_with("async-2")
        # Sanity: we patched api.threading.Thread, not the global one
        self.assertIs(threading.Thread, real_thread)

    def test_post_logs_when_connect_mock_raises(self):
        """If connect_mock_to_analyzer raises, the error must be logged —
        otherwise silent background failures leave analyzers created-but-
        unreachable with no signal to the operator."""
        self.mock_mgr.connect_mock_to_analyzer.reset_mock()
        self.mock_mgr.connect_mock_to_analyzer.side_effect = RuntimeError(
            "docker down"
        )

        def _inline_thread(target=None, args=(), kwargs=None, daemon=None):
            if target:
                target(*args, **(kwargs or {}))
            return MagicMock(start=lambda: None)

        # Patch the module logger directly (assertLogs misses cross-thread
        # emissions from httpd's worker thread).
        with patch("api.threading.Thread", side_effect=_inline_thread), \
             patch("api.logger") as mock_logger:
            status, _ = self._post_analyzers({"name": "async-3", "template": "t"})

        self.assertEqual(status, 201)
        mock_logger.exception.assert_called_once()
        msg = mock_logger.exception.call_args[0][0]
        self.assertIn("connect_mock_to_analyzer raised", msg)

    def test_post_valid_name_with_underscores_and_digits(self):
        status, _ = self._post_analyzers({"name": "analyzer_01", "template": "t"})
        self.assertEqual(status, 201)

    def test_post_valid_name_with_dashes(self):
        status, _ = self._post_analyzers({"name": "my-cool-analyzer", "template": "t"})
        self.assertEqual(status, 201)

    # ------------------------------------------------------------------
    # POST /analyzers — name validation
    # ------------------------------------------------------------------

    def test_post_invalid_name_slash_returns_400(self):
        status, body = self._post_analyzers({"name": "foo/bar", "template": "t"})
        self.assertEqual(status, 400)
        self.assertIn("alphanumeric", body["error"])

    def test_post_invalid_name_space_returns_400(self):
        status, body = self._post_analyzers({"name": "foo bar", "template": "t"})
        self.assertEqual(status, 400)
        self.assertIn("alphanumeric", body["error"])

    def test_post_invalid_name_dot_returns_400(self):
        status, body = self._post_analyzers({"name": "foo.bar", "template": "t"})
        self.assertEqual(status, 400)
        self.assertIn("alphanumeric", body["error"])

    def test_post_invalid_name_special_chars_returns_400(self):
        status, body = self._post_analyzers({"name": "foo@bar!", "template": "t"})
        self.assertEqual(status, 400)
        self.assertIn("alphanumeric", body["error"])

    # ------------------------------------------------------------------
    # POST /analyzers — missing fields
    # ------------------------------------------------------------------

    def test_post_missing_name_returns_400(self):
        status, body = self._post_analyzers({"template": "t"})
        self.assertEqual(status, 400)
        self.assertIn("required", body["error"])

    def test_post_missing_template_returns_400(self):
        status, body = self._post_analyzers({"name": "ok"})
        self.assertEqual(status, 400)
        self.assertIn("required", body["error"])

    def test_post_empty_name_returns_400(self):
        status, body = self._post_analyzers({"name": "", "template": "t"})
        self.assertEqual(status, 400)
        self.assertIn("required", body["error"])

    # ------------------------------------------------------------------
    # POST /analyzers — bad Content-Length
    # ------------------------------------------------------------------

    def test_post_invalid_content_length_returns_400(self):
        conn = self._conn()
        headers = {"Content-Length": "abc", "Content-Type": "application/json"}
        conn.request("POST", "/analyzers", body=b"", headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        self.assertEqual(resp.status, 400)
        self.assertIn("Content-Length", data["error"])

    # ------------------------------------------------------------------
    # POST /analyzers — empty / malformed body
    # ------------------------------------------------------------------

    def test_post_empty_body_returns_400(self):
        conn = self._conn()
        headers = {"Content-Length": "0", "Content-Type": "application/json"}
        conn.request("POST", "/analyzers", body=b"", headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        self.assertEqual(resp.status, 400)
        self.assertIn("body required", data["error"])

    def test_post_invalid_json_returns_400(self):
        status, body = self._post_analyzers(raw_body="not json {{{")
        self.assertEqual(status, 400)
        self.assertIn("Invalid JSON", body["error"])

    # ------------------------------------------------------------------
    # DELETE /analyzers/{name}
    # ------------------------------------------------------------------

    def test_delete_existing_analyzer_returns_200(self):
        status, body = self._delete_analyzer("valid-name")
        self.assertEqual(status, 200)
        self.assertTrue(body["removed"])
        self.assertEqual(body["name"], "valid-name")

    def test_delete_with_query_string_strips_query(self):
        status, body = self._delete_analyzer("valid-name", "?query=1")
        self.assertEqual(status, 200)
        self.assertTrue(body["removed"])
        self.assertEqual(body["name"], "valid-name")

    def test_delete_nonexistent_analyzer_returns_404(self):
        status, body = self._delete_analyzer("nonexistent")
        self.assertEqual(status, 404)
        self.assertFalse(body["removed"])
        self.assertIn("not found", body["error"])


if __name__ == "__main__":
    unittest.main()
