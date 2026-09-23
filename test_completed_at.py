"""
Tests for fixed instrument test times (`completed_at`).

A real GeneXpert reports when it ran a test, often days before the result reaches the
LIS. `completed_at` lets a caller send a known, back-dated completion time so a receiver
can be checked for keeping it: with an astm_config layout it is R.13 on every result
record, the start time R.12 is 90 minutes earlier, and R.10 stays empty, so a receiver
reading the wrong field cannot pass. Without `completed_at` the times still come from
the clock.

These tests use a small in-file template with astm_config; the shipped, profile-backed
genexpert_astm template (which needs ANALYZER_BRIDGE_PROFILES_DIR) is exercised end to end
only by the Analyzer Bridge e2e suite.
"""

import unittest
from unittest.mock import patch

import api
from test_sender_identity import _ApiServer


GENEXPERT_TEMPLATE = {
    "analyzer": {"name": "GeneXpert", "manufacturer": "Cepheid", "model": "GX",
                 "category": "MOLECULAR"},
    "protocol": {"type": "ASTM"},
    "identification": {"astm_header": "GENEXPERT^GeneXpert^6.2"},
    "astm_config": {"version_number": "1394-97"},
    "fields": [
        {"name": "MTB-RIF", "code": "MTB-RIF_ULTRA 2", "type": "QUALITATIVE",
         "possibleValues": ["MTB NOT DETECTED"]},
        {"name": "SPC Ct", "code": "SPC", "type": "NUMERIC", "unit": "cycles",
         "normalRange": "0-40"},
    ],
}


class TestSimulateAstmCompletedAt(_ApiServer):

    def setUp(self):
        tpl = patch.object(api, "_load_template", return_value=GENEXPERT_TEMPLATE)
        tpl.start()
        self.addCleanup(tpl.stop)
        push = patch.object(api, "push_astm_to_destination", return_value=(True, None))
        self.mock_push = push.start()
        self.addCleanup(push.stop)

    def _pushed_results(self):
        message = self.mock_push.call_args.args[1]
        return [line.split("|") for line in message.split("\n") if line.startswith("R|")]

    def test_completed_at_fixes_the_completion_and_start_times_on_every_result(self):
        # Crossing midnight also checks the start time is a real 90 minutes earlier.
        status, _ = self._post("/simulate/astm/genexpert_astm", {
            "destination": "tcp://bridge:12001",
            "completed_at": "20251022003000",
        })

        self.assertEqual(status, 200)
        results = self._pushed_results()
        self.assertEqual(len(results), 2)
        for result in results:
            self.assertEqual(result[12], "20251022003000", "R.13 is the completion time")
            self.assertEqual(result[11], "20251021230000", "R.12 is 90 minutes earlier")
            self.assertEqual(result[9], "", "R.10 carries no time")

    def test_without_completed_at_the_times_come_from_the_clock(self):
        status, _ = self._post("/simulate/astm/genexpert_astm", {
            "destination": "tcp://bridge:12001",
        })

        self.assertEqual(status, 200)
        for result in self._pushed_results():
            self.assertRegex(result[12], r"^\d{14}$")
            self.assertNotEqual(result[12], "20251022003000")

    def test_an_unreadable_completed_at_is_rejected_and_nothing_is_sent(self):
        for unreadable in ("2025-10-21T16:12:30", "202510211612", "2025102116123", "20251321161230"):
            with self.subTest(completed_at=unreadable):
                self.mock_push.reset_mock()

                status, body = self._post("/simulate/astm/genexpert_astm", {
                    "destination": "tcp://bridge:12001",
                    "completed_at": unreadable,
                })

                self.assertEqual(status, 400)
                self.assertIn("YYYYMMDDHHMMSS", body["error"])
                self.mock_push.assert_not_called()


if __name__ == "__main__":
    unittest.main()
