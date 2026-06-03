#!/usr/bin/env python3
"""Real-socket test: POST /simulate/{proto}/{instance} must source the result
push from the provisioned analyzer IP.

This closes the inversion-test gap behind the `harness 1/2` flake: every other
`/simulate` push test mocks `push_*_to_destination` (`return_value=(True, None)`),
so none ever exercised the source-IP bind — the bug (the push leaving from the
mock's default interface instead of the analyzer's provisioned IP) was invisible
to the suite. The only real-socket source-IP assertion lived on the *order*
path (`test_order_roundtrip_integration.py::TestResultPushSourceInterface`); this
is its parity on the `/simulate` path.

Topology (mirrors the order-path test): a provisioned instance resolves to a
per-analyzer interface IP (a 127.0.0.0/8 loopback alias stands in for
`10.42.N.10`); the push must arrive at the stand-in bridge listener *from* that
address. A push that ignored the provisioned IP would arrive from 127.0.0.1 and
fail the assertion — and the contrast test proves exactly that.
"""

import http.client
import json
import threading
import unittest
from http.server import HTTPServer
from unittest.mock import patch

from api import MockAPIHandler
from test_order_roundtrip_integration import _MllpCaptureServer, _AstmCaptureServer

# Stands in for the provisioned per-analyzer interface IP (10.42.N.10). A
# 127.0.0.0/8 alias is bindable on Linux, so the push's source bind succeeds and
# the capture server records it as the peer address.
ANALYZER_IFACE_IP = "127.0.0.2"


class _FakeManager:
    """Minimal stand-in for AnalyzerNetworkManager: only `get_analyzer` is used
    by `_resolve_instance`, returning the instance's {template, ip} record."""

    def __init__(self, mapping):
        self._mapping = mapping

    def get_analyzer(self, name):
        return self._mapping.get(name)


class TestSimulateSourcesFromProvisionedIp(unittest.TestCase):

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
        # Provisioned instances: instance name -> {real template, interface IP}.
        # The instance is the single identity key; addressing it must yield both
        # the template (message/sender shape) and the source IP.
        mgr = _FakeManager({
            "demo-bs200": {"template": "mindray_bs200", "ip": ANALYZER_IFACE_IP},
            "demo-genexpert": {"template": "genexpert_astm", "ip": ANALYZER_IFACE_IP},
        })
        p = patch.object(MockAPIHandler, "_get_network_manager",
                         classmethod(lambda cls: mgr))
        p.start()
        self.addCleanup(p.stop)

    def _post(self, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.request("POST", path, body=json.dumps(body),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def test_hl7_simulate_sources_from_provisioned_ip(self):
        capture = _MllpCaptureServer()
        try:
            status, body = self._post(
                "/simulate/hl7/demo-bs200",
                {"destination": f"mllp://127.0.0.1:{capture.port}"},
            )
            capture.wait(10)
        finally:
            capture.close()
        self.assertEqual(status, 200)
        self.assertTrue(body["results"][0]["pushed"], body["results"][0].get("error"))
        self.assertEqual(
            capture.peer_ip, ANALYZER_IFACE_IP,
            f"/simulate HL7 push must source from the provisioned analyzer IP "
            f"(the instance record's ip); got source {capture.peer_ip}")

    def test_astm_simulate_sources_from_provisioned_ip(self):
        capture = _AstmCaptureServer()
        try:
            status, body = self._post(
                "/simulate/astm/demo-genexpert",
                {"destination": f"tcp://127.0.0.1:{capture.port}"},
            )
            capture.wait(10)
        finally:
            capture.close()
        self.assertEqual(status, 200)
        self.assertTrue(body["results"][0]["pushed"], body["results"][0].get("error"))
        self.assertEqual(
            capture.peer_ip, ANALYZER_IFACE_IP,
            f"/simulate ASTM push must source from the provisioned analyzer IP "
            f"(the instance record's ip); got source {capture.peer_ip}")

    def test_bare_template_without_instance_does_not_source_from_analyzer_ip(self):
        """Contrast (proves the assertion discriminates): addressing a bare
        template that is NOT a provisioned instance has no IP to source from, so
        the push arrives from the default interface — never the analyzer IP."""
        capture = _MllpCaptureServer()
        try:
            status, body = self._post(
                "/simulate/hl7/mindray_bs200",  # template name, no instance registered
                {"destination": f"mllp://127.0.0.1:{capture.port}"},
            )
            capture.wait(10)
        finally:
            capture.close()
        self.assertEqual(status, 200)
        self.assertNotEqual(
            capture.peer_ip, ANALYZER_IFACE_IP,
            "a bare-template push (no provisioned instance) must NOT spoof the "
            "analyzer interface IP")


if __name__ == "__main__":
    unittest.main()
