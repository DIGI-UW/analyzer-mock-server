"""Manufacturer-shaped messages replayed from the vendor's own examples (rule 15)."""

import http.client
import json
import threading
import unittest
from http.server import HTTPServer
from unittest.mock import patch

import api
import fixture_messages
from api import MockAPIHandler


class TestFixtureMessages(unittest.TestCase):

    def test_every_fixture_is_listed_by_assay_and_outcome(self):
        listed = fixture_messages.list_fixtures("genexpert")

        self.assertEqual(
            {"hivvl", "cov-flu-rsv-plus", "cov-flu-plus", "cov-plus"}, set(listed))
        self.assertIn("below-40", listed["hivvl"])
        self.assertIn("no-result", listed["cov-flu-rsv-plus"])

    def test_the_sample_patient_and_instrument_codes_are_substituted(self):
        message = fixture_messages.render(
            "genexpert", "hivvl", "below-40", sample_id="ACC-1", patient_id="MRN-9",
            patient_name="Roe^Jane", instrument_codes={"HIVVL": "HIVU"})

        records = message.strip().split("\n")
        self.assertEqual("MRN-9", records[1].split("|")[4])
        self.assertEqual("Roe^Jane", records[1].split("|")[5])
        self.assertTrue(records[2].startswith("O|1|ACC-1||^^^HIVU|"), records[2])
        self.assertIn("^^^HIVU^Xpert HIV-1 Viral Load XC^3^^|DETECTED^|", message)
        self.assertNotIn("{", message)

    def test_without_overrides_the_profiles_code_is_sent_and_no_patient(self):
        message = fixture_messages.render("genexpert", "hivvl", "quantified", sample_id="ACC-2")

        records = message.strip().split("\n")
        self.assertEqual("", records[1].split("|")[4])
        self.assertEqual("^^^^", records[1].split("|")[5])
        self.assertTrue(records[2].startswith("O|1|ACC-2||^^^HIVVL|"))

    def test_an_unknown_fixture_is_refused(self):
        with self.assertRaises(fixture_messages.FixtureNotFound):
            fixture_messages.render("genexpert", "hivvl", "no-such-outcome", sample_id="X")
        with self.assertRaises(fixture_messages.FixtureNotFound):
            fixture_messages.render("../templates", "genexpert_astm", "x", sample_id="X")


class TestSimulateFixtureApi(unittest.TestCase):

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
        template = patch.object(
            api, "_load_template",
            return_value={"fixtures": "genexpert", "protocol": {"type": "ASTM"}})
        template.start()
        self.addCleanup(template.stop)
        push = patch.object(api, "push_astm_to_destination", return_value=(True, None))
        self.mock_push = push.start()
        self.addCleanup(push.stop)

    def _post(self, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", path, body=json.dumps(body),
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, data

    def test_posting_a_fixture_pushes_the_rendered_message_to_the_destination(self):
        status, body = self._post("/simulate/fixture/genexpert_astm/hivvl/below-40", {
            "destination": "tcp://bridge:12001", "sample_id": "ACC-7", "sender_id": "GX-1",
            "patient": {"id": "MRN-9", "name": "Roe^Jane"},
            "instrument_codes": {"HIVVL": "HIVU"}})

        self.assertEqual(200, status, body)
        self.assertEqual(1, body["pushed"])
        pushed = self.mock_push.call_args[0][1]
        self.assertTrue(pushed.startswith("H|@^\\|URM-9h6IUTYA-05||GX-1^GeneXpert^1.0|"), pushed[:80])
        self.assertIn("O|1|ACC-7||^^^HIVU|", pushed)
        self.assertIn("P|1|||MRN-9|Roe^Jane|", pushed)

    def test_a_fixture_needs_a_destination_and_a_sample_id(self):
        status, body = self._post("/simulate/fixture/genexpert_astm/hivvl/below-40",
                                  {"sample_id": "ACC-7"})
        self.assertEqual(400, status)
        status, body = self._post("/simulate/fixture/genexpert_astm/hivvl/below-40",
                                  {"destination": "tcp://bridge:12001"})
        self.assertEqual(400, status)

    def test_an_unknown_fixture_is_not_found(self):
        status, body = self._post("/simulate/fixture/genexpert_astm/hivvl/nonsense",
                                  {"destination": "tcp://bridge:12001", "sample_id": "A"})
        self.assertEqual(404, status)
        self.mock_push.assert_not_called()


if __name__ == "__main__":
    unittest.main()
