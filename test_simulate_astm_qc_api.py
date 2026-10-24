"""
Unit tests for POST /simulate/astm/{template}: scenarios, source_ip plumbing, and
the refusal of the removed qc option (ASTM controls are fixture outcomes).

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
                "unit": "copies/mL"}]
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

        push_patcher = patch.object(api, "push_astm_to_destination", return_value=(True, None))
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

    def test_qc_is_refused_and_names_the_control_fixtures(self):
        for body in ({"qc": True}, {"qc_deviation": 3.5}):
            status, response = self._post({"destination": "tcp://bridge:12001", **body})
            self.assertEqual(status, 400, body)
            self.assertIn("/simulate/fixture/", response["error"])
        self.mock_push.assert_not_called()

    def test_qc_false_uses_normal_generate_no_q_action_code(self):
        status, body = self._post({
            "destination": "tcp://bridge:12001",
            "count": 1,
        })
        self.assertEqual(status, 200)
        wire = self.mock_push.call_args[0][1]
        # Normal patient result has no Q action code at O.12
        self.assertNotIn("|Q|", wire)

    def test_explicit_result_scenario_is_emitted_on_the_wire(self):
        status, _ = self._post({
            "destination": "tcp://bridge:12001",
            "results": [{"test_code": "UNMAPPED-MTB", "value": "REVIEW REQUIRED"}],
        })

        self.assertEqual(status, 200)
        wire = self.mock_push.call_args[0][1]
        self.assertIn("UNMAPPED-MTB", wire)
        self.assertIn("REVIEW REQUIRED", wire)

    def test_source_ip_omitted_passes_none(self):
        self._post({"destination": "tcp://bridge:12001"})
        kwargs = self.mock_push.call_args[1]
        self.assertIsNone(kwargs.get("source_ip"))

    def test_numeric_scenario_rejects_invalid_values_before_sending(self):
        for value in ["not-a-number", True, {}, [], "NaN", "Infinity"]:
            with self.subTest(value=value):
                self.mock_push.reset_mock()
                status, body = self._post({
                    "destination": "tcp://bridge:12001",
                    "results": [{"test_code": "SYNTHETIC-NUMERIC", "type": "NUMERIC", "value": value}],
                })
                self.assertEqual(400, status)
                self.assertIn("finite numeric value", body["error"])
                self.mock_push.assert_not_called()

    def test_numeric_scenario_accepts_numeric_strings(self):
        status, _ = self._post({
            "destination": "tcp://bridge:12001",
            "results": [{"test_code": "SYNTHETIC-NUMERIC", "type": "NUMERIC", "value": "12.5"}],
        })
        self.assertEqual(200, status)
        self.assertIn("12.5", self.mock_push.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
