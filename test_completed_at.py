"""
Tests for fixed instrument test times (`completed_at`).

A real GeneXpert reports when it ran a test, often days before the result reaches the
LIS. `completed_at` lets a caller send a known, back-dated completion time so a receiver
can be checked for keeping it: in the GeneXpert layout it is R.13 and the start time R.12
is 90 minutes earlier. Without `completed_at` the times still come from the clock.
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
    "fields": [{"name": "MTB-RIF", "code": "MTB-RIF_ULTRA 2", "type": "QUALITATIVE",
                "possibleValues": ["MTB NOT DETECTED"]}],
}


class TestSimulateAstmCompletedAt(_ApiServer):

    def setUp(self):
        tpl = patch.object(api, "_load_template", return_value=GENEXPERT_TEMPLATE)
        tpl.start()
        self.addCleanup(tpl.stop)
        push = patch.object(api, "push_astm_to_destination", return_value=(True, None))
        self.mock_push = push.start()
        self.addCleanup(push.stop)

    def _pushed_result(self):
        message = self.mock_push.call_args.args[1]
        return next(line for line in message.split("\n") if line.startswith("R|1|")).split("|")

    def test_completed_at_fixes_the_completion_and_start_times(self):
        status, _ = self._post("/simulate/astm/genexpert_astm", {
            "destination": "tcp://bridge:12001",
            "completed_at": "20251021161230",
        })

        self.assertEqual(status, 200)
        result = self._pushed_result()
        self.assertEqual(result[12], "20251021161230", "R.13 is the completion time")
        self.assertEqual(result[11], "20251021144230", "R.12 is 90 minutes earlier")

    def test_without_completed_at_the_times_come_from_the_clock(self):
        status, _ = self._post("/simulate/astm/genexpert_astm", {
            "destination": "tcp://bridge:12001",
        })

        self.assertEqual(status, 200)
        result = self._pushed_result()
        self.assertRegex(result[12], r"^\d{14}$")
        self.assertNotEqual(result[12], "20251021161230")

    def test_an_unreadable_completed_at_is_rejected_and_nothing_is_sent(self):
        status, body = self._post("/simulate/astm/genexpert_astm", {
            "destination": "tcp://bridge:12001",
            "completed_at": "2025-10-21T16:12:30",
        })

        self.assertEqual(status, 400)
        self.assertIn("YYYYMMDDHHMMSS", body["error"])
        self.mock_push.assert_not_called()


if __name__ == "__main__":
    unittest.main()
