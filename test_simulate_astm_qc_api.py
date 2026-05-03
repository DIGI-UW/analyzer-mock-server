"""
Unit tests for POST /simulate/astm/{template} QC and source_ip plumbing.

Covers the JSON-body schema added so callers can drive QC violations through
the always-on API instead of `docker exec ... server.py --qc --push ...`:

    POST /simulate/astm/genexpert_astm
      { "destination": "tcp://bridge:12001",
        "qc": true, "qc_deviation": 3.5,
        "source_ip": "10.42.20.10" }

Push is mocked at module level — these tests do not open real TCP sockets.
"""

import http.client
import json
import threading
import unittest
from http.server import HTTPServer
from unittest.mock import patch

import api
from api import MockAPIHandler


GENEXPERT_TEMPLATE = {
    "analyzer": {"name": "GeneXpert", "manufacturer": "Cepheid", "model": "GX",
                 "category": "MOLECULAR"},
    "protocol": {"type": "ASTM"},
    "fields": [{"name": "HIV-VL", "astmRef": "R|1|^^^HIV-VL", "type": "NUMERIC",
                "unit": "copies/mL"}],
    "qc_controls": [{"field_code": "HIV-VL", "control_id": "LOT-HIVVL-N",
                     "target": 1250.0, "sd_pct": 10.0}],
}


class TestSimulateAstmQcApi(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(("127.0.0.1", 0), MockAPIHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=10)

    def setUp(self):
        # Always serve the GeneXpert QC template; patched per-test if needed.
        tpl_patcher = patch.object(api, "_load_template", return_value=GENEXPERT_TEMPLATE)
        tpl_patcher.start()
        self.addCleanup(tpl_patcher.stop)

        push_patcher = patch.object(api, "push_astm_to_destination", return_value=True)
        self.mock_push = push_patcher.start()
        self.addCleanup(push_patcher.stop)

    def _post(self, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(
            "POST", "/simulate/astm/genexpert_astm",
            body=json.dumps(body),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def test_qc_true_invokes_generate_qc_and_pushes_qc_message(self):
        status, body = self._post({
            "destination": "tcp://bridge:12001",
            "qc": True,
            "qc_deviation": 3.5,
            "source_ip": "10.42.20.10",
        })
        self.assertEqual(status, 200)
        self.assertTrue(body["qc"])
        self.assertEqual(body["pushed"], 1)

        # push_astm_to_destination was called with source_ip forwarded
        self.mock_push.assert_called_once()
        args, kwargs = self.mock_push.call_args
        self.assertEqual(args[0], "tcp://bridge:12001")
        # Wire format must have action code "Q" at O.12 (1-indexed ASTM field
        # number; 0-indexed array idx 11 when the segment ID is field 0).
        # Parse the O-record explicitly so a stray "|Q|" elsewhere in the
        # payload (e.g. inside another segment) cannot false-positive.
        wire = args[1]
        o_records = [ln for ln in wire.replace("\r", "\n").split("\n") if ln.startswith("O|")]
        self.assertEqual(len(o_records), 1, f"Expected exactly one O-record, got {o_records}")
        o_fields = o_records[0].split("|")
        self.assertGreaterEqual(len(o_fields), 12,
                                f"O-record has only {len(o_fields)} fields, need >=12")
        self.assertEqual(o_fields[11], "Q",
                         f"Expected action code Q at O.12, got {o_fields[11]!r}")
        self.assertEqual(kwargs.get("source_ip"), "10.42.20.10")

    def test_qc_false_uses_normal_generate_no_q_action_code(self):
        status, body = self._post({
            "destination": "tcp://bridge:12001",
            "count": 1,
        })
        self.assertEqual(status, 200)
        self.assertFalse(body["qc"])
        wire = self.mock_push.call_args[0][1]
        # Normal patient result has no Q action code at O.12
        self.assertNotIn("|Q|", wire)

    def test_source_ip_omitted_passes_none(self):
        self._post({"destination": "tcp://bridge:12001"})
        kwargs = self.mock_push.call_args[1]
        self.assertIsNone(kwargs.get("source_ip"))

    def test_qc_without_qc_controls_returns_400(self):
        bad_template = dict(GENEXPERT_TEMPLATE)
        bad_template["qc_controls"] = []
        with patch.object(api, "_load_template", return_value=bad_template):
            status, body = self._post({"qc": True, "destination": "tcp://bridge:12001"})
        self.assertEqual(status, 400)
        self.assertIn("qc_controls", body["error"])


if __name__ == "__main__":
    unittest.main()
