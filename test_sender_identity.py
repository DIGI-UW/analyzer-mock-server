"""
Tests for per-instance sender identity (`sender_id`).

A real GeneXpert names itself with the System Name from its own configuration in
component 1 of ASTM H.5 and HL7 MSH-3. `sender_id` lets one template stand in for
several distinct instruments, which the bridge needs to exercise sender-based
attribution on a shared listener. Without `sender_id` every message must stay
byte-identical to what the template produced before this option existed.
"""

import http.client
import json
import threading
import unittest
from http.server import HTTPServer
from unittest.mock import patch

import api
from api import MockAPIHandler
from protocols.sender_identity import with_astm_sender_id, with_hl7_sender_id


ASTM_MESSAGE = (
    "H|\\^&|||GENEXPERT^GeneXpert^4.6.0|||||LIS||P|1394-97|20260922101500\n"
    "P|1\n"
    "O|1|SAMPLE-1||^^^MTB-RIF\n"
    "R|1|^^^MTB-RIF|NOT DETECTED\n"
    "L|1|N\n"
)

HL7_MESSAGE = (
    "MSH|^~\\&|GeneXpert^GeneXpert^6.1|LAB|OpenELIS|LAB|20260922101500||ORU^R01|42|P|2.5\r"
    "PID|1||P-1^^^HOSPITAL\r"
    "OBX|1|ST|MTB-RIF||NOT DETECTED\r"
)


class TestAstmSenderId(unittest.TestCase):

    def test_absent_sender_id_leaves_message_byte_identical(self):
        self.assertEqual(with_astm_sender_id(ASTM_MESSAGE, None), ASTM_MESSAGE)
        self.assertEqual(with_astm_sender_id(ASTM_MESSAGE, ""), ASTM_MESSAGE)

    def test_replaces_only_the_system_name_component_of_h5(self):
        rewritten = with_astm_sender_id(ASTM_MESSAGE, "GX-LAB-A")

        header = rewritten.split("\n")[0]
        self.assertEqual(header.split("|")[4], "GX-LAB-A^GeneXpert^4.6.0")
        # Every other record and every other header field is untouched.
        self.assertEqual(
            rewritten.replace("GX-LAB-A^", "GENEXPERT^", 1),
            ASTM_MESSAGE,
        )

    def test_rewrites_every_header_when_messages_are_concatenated(self):
        combined = ASTM_MESSAGE + ASTM_MESSAGE

        rewritten = with_astm_sender_id(combined, "GX-LAB-B")

        headers = [line for line in rewritten.split("\n") if line.startswith("H|")]
        self.assertEqual(len(headers), 2)
        self.assertTrue(all(h.split("|")[4].startswith("GX-LAB-B^") for h in headers))

    def test_sets_h5_when_the_template_left_it_empty(self):
        message = "H|\\^&|||||||||||LIS2-A2\nL|1|N\n"

        rewritten = with_astm_sender_id(message, "GX-LAB-C")

        self.assertEqual(rewritten.split("\n")[0].split("|")[4], "GX-LAB-C")


class TestHl7SenderId(unittest.TestCase):

    def test_absent_sender_id_leaves_message_byte_identical(self):
        self.assertEqual(with_hl7_sender_id(HL7_MESSAGE, None), HL7_MESSAGE)

    def test_replaces_only_the_first_component_of_msh3(self):
        rewritten = with_hl7_sender_id(HL7_MESSAGE, "GX-LAB-A")

        msh = rewritten.split("\r")[0]
        self.assertEqual(msh.split("|")[2], "GX-LAB-A^GeneXpert^6.1")
        self.assertEqual(msh.split("|")[3], "LAB")
        self.assertEqual(rewritten.split("\r")[1:], HL7_MESSAGE.split("\r")[1:])


GENEXPERT_ASTM_TEMPLATE = {
    "analyzer": {"name": "GeneXpert", "manufacturer": "Cepheid", "model": "GX",
                 "category": "MOLECULAR"},
    "protocol": {"type": "ASTM"},
    "identification": {"astm_header": "GENEXPERT^GeneXpert^4.6.0"},
    "fields": [{"name": "MTB-RIF", "astmRef": "R|1|^^^MTB-RIF", "type": "QUALITATIVE",
                "possibleValues": ["NOT DETECTED"]}],
}


class _ApiServer(unittest.TestCase):

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
        conn.request("POST", path, body=json.dumps(body),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data


class TestSimulateAstmSenderId(_ApiServer):

    def setUp(self):
        tpl = patch.object(api, "_load_template", return_value=GENEXPERT_ASTM_TEMPLATE)
        tpl.start()
        self.addCleanup(tpl.stop)
        push = patch.object(api, "push_astm_to_destination", return_value=(True, None))
        self.mock_push = push.start()
        self.addCleanup(push.stop)

    def _pushed_header(self):
        message = self.mock_push.call_args.args[1]
        return next(line for line in message.split("\n") if line.startswith("H|"))

    def test_sender_id_names_the_instrument_in_the_pushed_header(self):
        status, body = self._post("/simulate/astm/genexpert_astm", {
            "destination": "tcp://bridge:12001",
            "sender_id": "GX-LAB-A",
            "source_ip": "10.42.20.11",
        })

        self.assertEqual(status, 200)
        self.assertEqual(body["sender_id"], "GX-LAB-A")
        self.assertEqual(self._pushed_header().split("|")[4].split("^")[0], "GX-LAB-A")
        self.assertEqual(self.mock_push.call_args.kwargs["source_ip"], "10.42.20.11")

    def test_without_sender_id_the_header_keeps_the_template_token(self):
        status, body = self._post("/simulate/astm/genexpert_astm", {
            "destination": "tcp://bridge:12001",
        })

        self.assertEqual(status, 200)
        self.assertIsNone(body["sender_id"])
        self.assertEqual(self._pushed_header().split("|")[4], "GENEXPERT^GeneXpert^4.6.0")


class TestSimulateHl7SenderAndSourceIp(_ApiServer):

    def setUp(self):
        tpl = patch.object(api, "_load_template", return_value={
            "analyzer": {"name": "GeneXpert"},
            "protocol": {"type": "HL7"},
            "fields": [{"name": "MTB-RIF", "type": "QUALITATIVE",
                        "possibleValues": ["NOT DETECTED"]}],
        })
        tpl.start()
        self.addCleanup(tpl.stop)
        push = patch.object(api, "push_hl7_to_destination", return_value=(True, None))
        self.mock_push = push.start()
        self.addCleanup(push.stop)

    def test_sender_id_and_source_ip_reach_the_hl7_push(self):
        status, _ = self._post("/simulate/hl7/genexpert", {
            "destination": "mllp://bridge:2575",
            "sender_id": "GX-LAB-B",
            "source_ip": "10.42.22.11",
        })

        self.assertEqual(status, 200)
        message = self.mock_push.call_args.args[1]
        msh = message.split("\r")[0]
        self.assertEqual(msh.split("|")[2].split("^")[0], "GX-LAB-B")
        self.assertEqual(self.mock_push.call_args.kwargs["source_ip"], "10.42.22.11")


if __name__ == "__main__":
    unittest.main()
