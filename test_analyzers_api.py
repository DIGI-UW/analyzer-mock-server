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
