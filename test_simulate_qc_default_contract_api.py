"""
Contract tests for scenario-only QC API triggers.

These tests pin the external contract boundary:
clients send scenario intent only to /simulate endpoints, while the mock
resolves internal delivery details to bridge/OpenELIS.
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
    "analyzer": {"name": "Cepheid GeneXpert", "manufacturer": "Cepheid"},
    "protocol": {"type": "ASTM"},
    "fields": [{"name": "HIV-VL", "code": "HIV-VL", "type": "NUMERIC"}],
    "qc_controls": [{"field_code": "HIV-VL", "lot_number": "LOT-HIVVL-N", "target": 1250.0}],
}

MINDRAY_TEMPLATE = {
    "analyzer": {"name": "Mindray BS-200", "manufacturer": "Mindray"},
    "protocol": {"type": "HL7"},
    "fields": [{"name": "GLUCOSE", "code": "GLU", "type": "NUMERIC"}],
    "qc_controls": [{
        "field_code": "GLU",
        "lot_number": "LOT-GLU-N",
        "level": "NORMAL",
        "target": 100.0,
        "sd": 5.0,
    }],
}

QUANTSTUDIO_TEMPLATE = {
    "analyzer": {"name": "QuantStudio 5", "manufacturer": "Thermo Fisher"},
    "protocol": {"type": "FILE"},
    "file_config": {"format": "EXCEL"},
    "fields": [{"name": "VIH-1", "code": "VIH-1", "type": "NUMERIC"}],
    "qc_controls": [{
        "field_code": "VIH-1",
        "lot_number": "LOT-LPC-26B",
        "level": "LPC",
        "task": "STANDARD",
        "target": 32.0,
        "sd": 0.5,
    }],
}


class TestScenarioOnlyQcApiContract(unittest.TestCase):
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

    def _post(self, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, payload

    def test_astm_qc_uses_default_delivery_when_destination_omitted(self):
        with patch.object(api, "_load_template", return_value=GENEXPERT_TEMPLATE), patch.object(
            api, "push_astm_to_destination", return_value=True
        ) as mock_push:
            status, body = self._post(
                "/simulate/astm/genexpert_astm",
                {"qc": True, "qc_deviation": 3.5},
            )

        self.assertEqual(status, 200)
        self.assertTrue(body["qc"])
        mock_push.assert_called_once()
        args, kwargs = mock_push.call_args
        self.assertEqual(args[0], "tcp://openelis-analyzer-bridge:12001")
        self.assertEqual(kwargs.get("source_ip"), "10.42.20.10")

    def test_hl7_qc_uses_default_delivery_when_destination_omitted(self):
        with patch.object(api, "_load_template", return_value=MINDRAY_TEMPLATE), patch.object(
            api, "push_hl7_to_destination", return_value=True
        ) as mock_push:
            status, body = self._post(
                "/simulate/hl7/mindray_bs200",
                {"qc": True, "qc_deviation": 3.5},
            )

        self.assertEqual(status, 200)
        self.assertTrue(body["qc"])
        mock_push.assert_called_once()
        args, kwargs = mock_push.call_args
        self.assertEqual(args[0], "mllp://openelis-analyzer-bridge:2575")
        self.assertEqual(kwargs.get("source_ip"), "10.42.22.10")

    def test_file_qc_uses_default_bridge_upload_when_not_provided(self):
        def _fake_upload(self, template_name, template, content, bridge_upload, qc_deviation):
            self._send_json(
                200,
                {
                    "status": "uploaded",
                    "template": template_name,
                    "qc": True,
                    "qc_deviation": qc_deviation,
                },
            )

        with patch.object(api, "_load_template", return_value=QUANTSTUDIO_TEMPLATE), patch.object(
            MockAPIHandler, "_upload_qc_content_to_bridge", autospec=True, side_effect=_fake_upload
        ) as mock_upload:
            status, body = self._post(
                "/simulate/file/quantstudio5",
                {"qc": True, "qc_deviation": 3.0},
            )

        self.assertEqual(status, 200)
        self.assertEqual(body.get("status"), "uploaded")
        mock_upload.assert_called_once()
        bridge_upload_arg = mock_upload.call_args[0][4]
        self.assertEqual(bridge_upload_arg.get("test_code"), "VIH-1")


if __name__ == "__main__":
    unittest.main()
