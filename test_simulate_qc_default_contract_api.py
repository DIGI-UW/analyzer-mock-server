"""Contract tests for explicit Bridge analyzer-traffic destinations."""

import http.client
import json
import os
import tempfile
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


class TestExplicitBridgeDestinationContract(unittest.TestCase):
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

    def test_astm_qc_rejects_missing_bridge_destination(self):
        with patch.object(api, "_load_template", return_value=GENEXPERT_TEMPLATE), patch.object(
            api, "push_astm_to_destination", return_value=(True, None)
        ) as mock_push:
            status, body = self._post(
                "/simulate/astm/arbitrary_astm_profile",
                {"qc": True, "qc_deviation": 3.5},
            )

        self.assertEqual(status, 400)
        self.assertIn("destination", body["error"])
        mock_push.assert_not_called()

    def test_hl7_qc_rejects_missing_bridge_destination(self):
        with patch.object(api, "_load_template", return_value=MINDRAY_TEMPLATE), patch.object(
            api, "push_hl7_to_destination", return_value=(True, None)
        ) as mock_push:
            status, body = self._post(
                "/simulate/hl7/arbitrary_hl7_profile",
                {"qc": True, "qc_deviation": 3.5},
            )

        self.assertEqual(status, 400)
        self.assertIn("destination", body["error"])
        mock_push.assert_not_called()

    def test_file_qc_writes_to_bridge_watch_directory(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as target_dir, patch.object(
            api, "_load_template", return_value=QUANTSTUDIO_TEMPLATE
        ):
            status, body = self._post(
                "/simulate/file/arbitrary_file_profile",
                {
                    "qc": True,
                    "qc_deviation": 3.0,
                    "target_dir": target_dir,
                },
            )

            self.assertEqual(status, 200)
            self.assertEqual(body.get("status"), "completed")
            self.assertTrue(
                os.path.realpath(body.get("written_path", "")).startswith(
                    os.path.realpath(target_dir)
                )
            )
            self.assertTrue(os.path.isfile(body["written_path"]))


if __name__ == "__main__":
    unittest.main()
