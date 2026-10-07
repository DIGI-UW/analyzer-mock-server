"""
Unit tests for protocol handlers.
"""

import json
import os
import stat
import unittest
import tempfile
import threading
import http.client
from copy import deepcopy
from http.server import HTTPServer
from unittest.mock import patch

from protocols.astm_handler import ASTMHandler
from protocols.hl7_handler import HL7Handler
from protocols.serial_handler import SerialHandler
from protocols.file_handler import FileHandler
from api import MockAPIHandler as SimulateAPIHandler


def _load_template(name: str):
    # Use the production loader so these tests exercise the real template path:
    # transport/fixtures from templates/<name>.json + assay fields derived from the
    # canonical profile it references. (A raw json.load would bypass the profile
    # derivation and test a stub.)
    from server import _load_template as _load_production_template
    return _load_production_template(name)


class TestASTMHandler(unittest.TestCase):
    def test_generate_mindray_bc5380(self):
        t = _load_template("mindray_bc5380")
        msg = ASTMHandler().generate(t, patient_id="P001", sample_id="DEV01264000000000001")
        self.assertIn("H|", msg)
        self.assertIn("P|", msg)
        self.assertIn("O|", msg)
        self.assertIn("R|", msg)
        self.assertIn("L|", msg)
        self.assertIn("P001", msg)
        self.assertIn("DEV01264000000000001", msg)

    def test_generate_rejects_invalid_explicit_sample_id(self):
        t = _load_template("mindray_bc5380")
        with self.assertRaisesRegex(ValueError, "valid SiteYearNum accession"):
            ASTMHandler().generate(t, sample_id="S001-BAD")

    def test_generate_horiba_pentra60(self):
        t = _load_template("horiba_pentra60")
        msg = ASTMHandler().generate(t)
        self.assertIn("H|", msg)
        self.assertIn("PENTRA", msg.upper())


class TestHL7Handler(unittest.TestCase):
    def test_generate_mindray_bc5380(self):
        t = _load_template("mindray_bc5380")
        msg = HL7Handler().generate(t, patient_id="P001", sample_id="DEV01264000000000001")
        self.assertIn("MSH|", msg)
        self.assertIn("ORU^R01", msg)
        self.assertIn("PID|", msg)
        self.assertIn("OBR|", msg)
        self.assertIn("OBX|", msg)
        self.assertIn("MINDRAY", msg)
        self.assertIn("P001", msg)
        self.assertIn("DEV01264000000000001", msg)

    def test_invalid_hl7_template_sample_seed_fails_loudly(self):
        t = _load_template("mindray_bc5380")
        invalid = deepcopy(t)
        invalid["testSample"]["id"] = "PLACER-INVALID"
        with self.assertRaisesRegex(ValueError, "2-digit lane code"):
            HL7Handler().generate(invalid)

    def test_invalid_hl7_sample_override_fails_loudly(self):
        t = _load_template("mindray_bc5380")
        with self.assertRaisesRegex(ValueError, "valid SiteYearNum accession"):
            HL7Handler().generate(t, sample_id="S001")

    def test_hl7_mints_valid_accession_when_test_sample_missing(self):
        """If testSample is absent, default lane 00 must produce a valid SiteYearNum."""
        minimal = {
            "protocol": {"type": "HL7", "version": "2.5.1"},
            "identification": {"hl7_sending_app": "TEST", "hl7_sending_facility": "LAB"},
            "fields": [{"code": "GLU", "name": "Glucose", "type": "NUMERIC", "seedValue": 5.0, "unit": "mmol/L"}],
        }
        msg = HL7Handler().generate(minimal)
        self.assertIn("ORC|", msg)
        self.assertIn("OBR|", msg)
        for line in msg.split("\r"):
            if line.startswith("OBR|"):
                obr = line.split("|")
                filler = obr[3].split("^")[0] if len(obr) > 3 else ""
                self.assertRegex(filler, r"^DEV01\d{15}$")
                self.assertEqual(len(filler), 20)
                break
        else:
            self.fail("expected OBR segment")

    def test_generate_sysmex_xn(self):
        t = _load_template("sysmex_xn")
        msg = HL7Handler().generate(t)
        self.assertIn("MSH|", msg)
        self.assertIn("SYSMEX", msg)

class TestSerialHandler(unittest.TestCase):
    def test_generate_horiba_pentra60(self):
        t = _load_template("horiba_pentra60")
        msg = SerialHandler().generate(t)
        self.assertIn("H|", msg)
        self.assertIn("PENTRA", msg.upper())


class TestFileHandler(unittest.TestCase):
    def test_generate_quantstudio7(self):
        t = _load_template("quantstudio7")
        csv = FileHandler().generate(t, sample_id="DEV01262000000000001")
        self.assertIn("Sample Name", csv)
        self.assertIn("Target Name", csv)
        self.assertIn("Quantity Mean", csv)
        self.assertIn("DEV01262000000000001", csv)

    def test_generate_hain_fluorocycler(self):
        t = _load_template("hain_fluorocycler")
        csv = FileHandler().generate(t)
        self.assertIn("Sample ID", csv)
        self.assertIn("TargetName", csv)
        self.assertIn("Calc. Conc.", csv)
        self.assertRegex(csv, r"DEV01\d{15}")

    def test_invalid_file_sample_override_fails_loudly(self):
        t = _load_template("quantstudio7")
        with self.assertRaisesRegex(ValueError, "valid SiteYearNum accession"):
            FileHandler().generate(t, sample_id="S001")


class TestFileSimulateAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(("127.0.0.1", 0), SimulateAPIHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=10)

    def test_get_simulate_file_quantstudio7(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/simulate/file/quantstudio7")
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        conn.close()

        self.assertEqual(resp.status, 200)
        self.assertEqual(body.get("status"), "generated")
        # Fixture-backed templates return metadata; synthetic return content
        if "metadata" in body:
            self.assertIn("results", body["metadata"])
            self.assertGreater(len(body["metadata"]["results"]), 0)
        else:
            self.assertIn("Sample Name", body.get("content", ""))

    # ------------------------------------------------------------------
    # Fixture drift guards: every FILE template whose fixture we own must
    # still parse to non-empty metadata results. Catches mismatches between
    # fixture column headers / delimiter / skipRows and the template's
    # fixture.column_mapping — the exact class of bug that broke the
    # Madagascar harness demo flow in CI.
    # ------------------------------------------------------------------

    def _assert_fixture_parses(self, template_name: str):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", f"/simulate/file/{template_name}")
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        conn.close()

        self.assertEqual(resp.status, 200, f"{template_name}: HTTP {resp.status}")
        self.assertEqual(body.get("status"), "generated")
        self.assertIn("metadata", body, f"{template_name}: no metadata in response")
        results = body["metadata"].get("results") or []
        self.assertGreater(
            len(results), 0,
            f"{template_name}: parse_fixture returned 0 results — fixture/profile drift",
        )
        # Every returned result must carry sampleId + result at minimum
        for r in results:
            self.assertIn("sampleId", r, f"{template_name}: result missing sampleId: {r}")
            self.assertIn("result", r, f"{template_name}: result missing value: {r}")

    def test_fixture_parses_hain_fluorocycler(self):
        self._assert_fixture_parses("hain_fluorocycler")

    def test_fixture_parses_wondfo_finecare(self):
        self._assert_fixture_parses("wondfo_finecare")

    def test_fixture_parses_tecan_f50(self):
        self._assert_fixture_parses("tecan_f50")

    def test_fixture_parses_multiskan_fc(self):
        self._assert_fixture_parses("multiskan_fc")

    def test_post_simulate_file_write_target_dir(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            payload = json.dumps({"target_dir": tmpdir, "filename": "sim.csv"})
            headers = {"Content-Type": "application/json"}
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("POST", "/simulate/file/quantstudio7", body=payload, headers=headers)
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            conn.close()

            self.assertEqual(resp.status, 200)
            self.assertEqual(body.get("status"), "completed")
            written_path = body.get("written_path")
            self.assertTrue(written_path)
            self.assertTrue(os.path.exists(written_path))

    def test_post_fixture_file_uses_requested_sample_ids_in_written_xlsx(self):
        from fixture_parser import parse_fixture

        sample_ids = ["DEV01269999999999901", "DEV01269999999999902"]
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            payload = json.dumps({"target_dir": tmpdir, "sample_ids": sample_ids})
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("POST", "/simulate/file/hain_fluorocycler", body=payload,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            conn.close()

            self.assertEqual(resp.status, 200, body)
            results = body["metadata"]["results"]
            self.assertEqual([row["sampleId"] for row in results], sample_ids)
            self.assertEqual([row["result"] for row in results], ["1250", "450"])
            template = _load_template("hain_fluorocycler")
            written = parse_fixture(body["written_path"], template["fixture"])
            self.assertEqual(written, results)
            fixture_path = os.path.join(
                os.path.dirname(__file__), template["fixture"]["file"]
            )
            self.assertEqual(
                stat.S_IMODE(os.stat(body["written_path"]).st_mode),
                stat.S_IMODE(os.stat(fixture_path).st_mode),
            )

    def test_post_filtered_xlsx_fixture_replaces_only_selected_results(self):
        from fixture_parser import parse_fixture

        template = _load_template("quantstudio7")
        fixture = template["fixture"]
        source = os.path.join(os.path.dirname(__file__), fixture["file"])
        expected = parse_fixture(source, fixture)
        sample_ids = [f"DEV0126999999999{index:04d}" for index in range(1, len(expected) + 1)]
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            payload = json.dumps({"target_dir": tmpdir, "sample_ids": sample_ids})
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("POST", "/simulate/file/quantstudio7", body=payload,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            conn.close()

            self.assertEqual(resp.status, 200, body)
            written = parse_fixture(body["written_path"], fixture)
            self.assertEqual([row["sampleId"] for row in written], sample_ids)
            self.assertEqual([row["result"] for row in written],
                             [row["result"] for row in expected])

    def test_post_simulate_file_sanitizes_path_traversal_filename(self):
        """Path traversal in filename is stripped to basename (safe write)."""
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            payload = json.dumps({"target_dir": tmpdir, "filename": "../evil.csv"})
            headers = {"Content-Type": "application/json"}
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("POST", "/simulate/file/quantstudio7", body=payload, headers=headers)
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()
            # Server strips path components — writes "evil.csv" in target_dir (safe)
            self.assertEqual(resp.status, 200)
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "evil.csv")))
            # Verify no file escaped to parent
            self.assertFalse(os.path.exists(os.path.join(os.path.dirname(tmpdir), "evil.csv")))

    def test_post_simulate_file_rejects_invalid_json_body(self):
        headers = {"Content-Type": "application/json"}
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/simulate/file/quantstudio7", body="not json", headers=headers)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertGreaterEqual(resp.status, 400)
        self.assertLess(resp.status, 500)

    def test_simulate_file_rejects_path_traversal_template(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/simulate/file/../../etc/passwd")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertGreaterEqual(resp.status, 400)
        self.assertLess(resp.status, 500)


class TestProfileAdapterQualitativeLogic(unittest.TestCase):
    """Hermetic gate for exact Bridge profile result-type seed mapping."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        with open(os.path.join(self._tmp.name, "synth.json"), "w") as fh:
            json.dump(
                {
                    "profileMeta": {"id": "synth", "displayName": "Synthetic"},
                    "analyzer_name": "Synthetic Analyzer",
                    "manufacturer": "Synthetic Manufacturer",
                    "model": "Synthetic Model",
                    "category": "MOLECULAR",
                    "protocol": {"name": "ASTM", "version": "LIS2-A2"},
                    "catalog": {
                        "revision": 1,
                        "revisionFingerprint": "sha256:synthetic",
                    },
                    "default_test_mappings": [
                        {
                            "test_code": "QUAL1",
                            "loinc": "111-1",
                            "result_type": "qualitative",
                            "values": ["DETECTED", "NOT DETECTED"],
                        },
                        {"test_code": "QUANT1", "loinc": "222-2", "result_type": "quantitative"},
                    ]
                },
                fh,
            )
        self._prev = os.environ.get("ANALYZER_BRIDGE_PROFILES_DIR")
        os.environ["ANALYZER_BRIDGE_PROFILES_DIR"] = self._tmp.name
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._prev is None:
            os.environ.pop("ANALYZER_BRIDGE_PROFILES_DIR", None)
        else:
            os.environ["ANALYZER_BRIDGE_PROFILES_DIR"] = self._prev

    def test_qualitative_seeds_negative_vocab_quantitative_seeds_zero(self):
        from profile_adapter import load_profile_backed_template

        merged = load_profile_backed_template(
            "synth",
            {
                "profileRef": {"profileId": "synth", "revision": 1},
            },
        )
        fields = {f["code"]: f for f in merged["fields"]}
        # qualitative result_type -> QUALITATIVE field seeded to the negative vocab
        self.assertEqual(fields["QUAL1"]["type"], "QUALITATIVE")
        self.assertEqual(fields["QUAL1"]["seedQualitative"], "NOT DETECTED")
        # quantitative -> NUMERIC field, deterministic 0 seed
        self.assertEqual(fields["QUANT1"]["type"], "NUMERIC")
        self.assertEqual(fields["QUANT1"]["seedValue"], 0)


if __name__ == "__main__":
    unittest.main()
