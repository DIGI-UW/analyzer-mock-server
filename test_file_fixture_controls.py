"""FILE fixtures carry control rows as the instruments name them, and a caller's sample IDs replace
only the patient rows (openelis-work QuantStudio spec 7; analyzer-mock-server#54)."""

import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import HTTPServer
from unittest.mock import patch

import api
from api import MockAPIHandler
from fixture_parser import parse_fixture

HERE = os.path.dirname(os.path.abspath(__file__))


def _template(name):
    with open(os.path.join(HERE, "templates", name + ".json"), encoding="utf-8") as handle:
        template = json.load(handle)
    template["protocol"] = {"type": "FILE"}
    template["analyzer"] = {"name": name}
    return template


class TestQuantStudioFixtureShape(unittest.TestCase):

    def test_every_well_reports_its_target_and_internal_control_under_one_sample_name(self):
        import openpyxl
        sheet = openpyxl.load_workbook(os.path.join(HERE, "fixtures/quantstudio7/results.xlsx")).active
        rows = list(sheet.iter_rows(values_only=True))
        header_index = next(i for i, row in enumerate(rows) if row and row[0] == "Well")
        self.assertEqual("Block Type", rows[0][0])
        header = rows[header_index]
        column = {name: header.index(name) for name in ("Well Position", "Sample Name", "Target Name", "Task")}
        by_well = {}
        for row in rows[header_index + 1:]:
            by_well.setdefault(row[column["Well Position"]], []).append(row)
        for position, well_rows in by_well.items():
            self.assertEqual(["VIH-1", "IC"], [r[column["Target Name"]] for r in well_rows], position)
            self.assertEqual(1, len({r[column["Sample Name"]] for r in well_rows}), position)
        tasks = {row[column["Task"]] for row in rows[header_index + 1:]}
        self.assertEqual({"STANDARD", "NTC", "UNKNOWN"}, tasks)


class TestFileFixtureControlRows(unittest.TestCase):

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
        self.target = tempfile.mkdtemp(prefix="mock-file-controls-")
        self.addCleanup(shutil.rmtree, self.target, True)
        allowed = patch.object(api, "_is_allowed_file_output_dir", return_value=True)
        allowed.start()
        self.addCleanup(allowed.stop)

    def _post(self, template_name, body):
        with patch.object(api, "_load_template", return_value=_template(template_name)):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("POST", f"/simulate/file/{template_name}",
                         body=json.dumps({"target_dir": self.target, **body}),
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            data = json.loads(response.read().decode("utf-8"))
            conn.close()
        return response.status, data

    def test_sample_ids_replace_only_the_patient_rows(self):
        patients = [f"ACC-{n}" for n in range(1, 6)]
        status, body = self._post("quantstudio7", {"sample_ids": patients})

        self.assertEqual(200, status, body)
        written = [row["sampleId"] for row in body["metadata"]["results"]]
        self.assertEqual(["STD1_E7", "STD3_E5", "STD5_E3", "PC", *patients], written)
        fixture_cfg = _template("quantstudio7")["fixture"]
        on_disk = [row["sampleId"] for row in parse_fixture(body["written_path"], fixture_cfg)]
        self.assertEqual(written, on_disk)

    def test_control_ids_rename_the_control_rows(self):
        status, body = self._post("quantstudio7", {
            "sample_ids": [f"ACC-{n}" for n in range(1, 6)],
            "control_ids": ["STD-A", "STD-B", "STD-C", "pos"]})

        self.assertEqual(200, status, body)
        self.assertEqual(["STD-A", "STD-B", "STD-C", "pos"],
                         [row["sampleId"] for row in body["metadata"]["results"]][:4])

    def test_the_counts_name_the_patient_and_control_rows(self):
        status, body = self._post("quantstudio7", {"sample_ids": ["ACC-1"]})
        self.assertEqual(400, status)
        self.assertIn("5", body["error"])
        status, body = self._post("quantstudio7", {
            "sample_ids": [f"ACC-{n}" for n in range(1, 6)], "control_ids": ["PC"]})
        self.assertEqual(400, status)
        self.assertIn("4", body["error"])

    def test_a_variant_replays_its_own_workbook(self):
        status, body = self._post("quantstudio7", {"variant": "control-failing"})

        self.assertEqual(200, status, body)
        self.assertIn({"sampleId": "NTC", "result": "120.4", "testCode": "VIH-1"},
                      body["metadata"]["results"])
        self.assertTrue(body["metadata"]["fixture"].endswith("results-control-failing.xlsx"))

    def test_an_unknown_variant_is_refused_and_the_known_ones_named(self):
        status, body = self._post("quantstudio7", {"variant": "nonsense"})
        self.assertEqual(400, status)
        self.assertIn("control-failing", body["error"])

    def test_fluorocycler_control_rows_ride_in_their_variants(self):
        status, body = self._post("hain_fluorocycler", {"sample_ids": ["ACC-1", "ACC-2"]})
        self.assertEqual(200, status, body)
        self.assertEqual(["ACC-1", "ACC-2"], [r["sampleId"] for r in body["metadata"]["results"]])

        status, body = self._post("hain_fluorocycler", {
            "variant": "with-controls", "sample_ids": ["ACC-1", "ACC-2"]})
        self.assertEqual(200, status, body)
        self.assertIn("PC", [r["sampleId"] for r in body["metadata"]["results"]])


if __name__ == "__main__":
    unittest.main()
