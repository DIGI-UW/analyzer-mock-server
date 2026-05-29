#!/usr/bin/env python3
"""
Socket-level integration tests for LIS-initiated order → result round-trip.

Per the mock constitution (Principle I: "behave like a rigorous real-world
analyzer"; Principle III: integration tests "verify the full protocol handshake
and data exchange") and OE2's Inversion Test (a test must fail if the SUT is
replaced by a hardcoded/fixture return).

These tests drive a REAL order through the mock's listener over a loopback
socket and capture what the mock pushes back to a stand-in "bridge inbound"
listener. They assert the result corresponds to the ORDERED tests — not the
analyzer's full template. A template-replay implementation MUST fail these.

Topology per test:
    [test as bridge] --order--> [mock handler on socketpair]
                                      |
                                      | _push_order_result / send_order_response
                                      v
                            [capture listener] (stands in for bridge:2575 / :12001)
    test then inspects the captured result message.
"""

import os
import socket
import threading
import time

import pytest

from protocols.mllp_listener import MLLPProtocolHandler
from server import ASTMProtocolHandler
from push import send_astm_session

VT = b"\x0B"
FS = b"\x1C"
CR = b"\x0D"
LF = b"\x0A"
ENQ = b"\x05"
ACK = b"\x06"
EOT = b"\x04"
STX = b"\x02"
ETX = b"\x03"
ETB = b"\x17"

GENEXPERT_ASTM = {
    "analyzer": {"name": "Cepheid GeneXpert", "model": "GeneXpert"},
    "protocol": {"type": "ASTM", "version": "E-1394-97"},
    # proactive_enq intentionally OFF so the test handshake is deterministic.
    "astm_config": {"proactive_enq": False},
    "fields": [
        {"code": "MTB-RIF", "name": "Xpert MTB/RIF", "type": "QUALITATIVE",
         "possibleValues": ["MTB DETECTED", "NOT DETECTED"], "seedQualitative": "NOT DETECTED"},
        {"code": "RIF", "name": "Rif Resistance", "type": "QUALITATIVE",
         "possibleValues": ["DETECTED", "NOT DETECTED"], "seedQualitative": "NOT DETECTED"},
        {"code": "HIV-VL", "name": "HIV-1 Viral Load", "type": "NUMERIC", "seedValue": 1250, "unit": "copies/mL"},
        {"code": "COVID19", "name": "SARS-CoV-2", "type": "QUALITATIVE",
         "possibleValues": ["POSITIVE", "NEGATIVE"], "seedQualitative": "NEGATIVE"},
    ],
    "testPatient": {"id": "PAT003", "name": "RAKOTO^JEAN"},
}

MINDRAY_BC5380 = {
    "analyzer": {"name": "Mindray BC-5380", "model": "BC-5380"},
    "protocol": {"type": "HL7", "version": "2.3.1"},
    "identification": {"hl7_sending_app": "MINDRAY", "hl7_sending_facility": "BC-5380"},
    "fields": [
        {"code": "WBC", "name": "White Blood Cell", "type": "NUMERIC", "seedValue": 7.5, "unit": "10*3/uL"},
        {"code": "RBC", "name": "Red Blood Cell", "type": "NUMERIC", "seedValue": 4.82, "unit": "10*6/uL"},
        {"code": "HGB", "name": "Hemoglobin", "type": "NUMERIC", "seedValue": 14.2, "unit": "g/dL"},
        {"code": "HCT", "name": "Hematocrit", "type": "NUMERIC", "seedValue": 42.0, "unit": "%"},
    ],
    "testPatient": {"id": "PAT001", "name": "DOE^JOHN", "sex": "M", "dob": "19800115"},
    "testSample": {"id": "40", "type": "CBC^COMPLETE BLOOD COUNT"},
}


def _socketpair_tcp(local_ip="127.0.0.1"):
    """A connected (client, server) TCP pair. `local_ip` (any 127.0.0.0/8 address)
    becomes the server side's local address — i.e. server.getsockname()[0] == local_ip.
    Used to simulate the mock listening on a specific per-analyzer interface so the
    result push-back's source IP can be asserted."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind((local_ip, 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect((local_ip, port))
    server, _ = listener.accept()
    listener.close()
    return client, server


def _mllp_frame(message: str) -> bytes:
    return VT + message.encode("utf-8") + FS + CR


def _recv_mllp(sock, timeout=5.0) -> str:
    sock.settimeout(timeout)
    data = b""
    while FS + CR not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("socket closed before FS+CR")
        data += chunk
    if data.startswith(VT):
        data = data[1:]
    return data[: data.index(FS + CR)].decode("utf-8")


class _MllpCaptureServer:
    """Stand-in for the bridge's MLLP inbound listener. Accepts one connection,
    reads one MLLP-framed message, returns a positive ACK, stores the message."""

    def __init__(self):
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self.captured = None
        self.peer_ip = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            conn, peer = self._listener.accept()
            self.peer_ip = peer[0]
        except OSError:
            return
        try:
            buf = b""
            conn.settimeout(5.0)
            while FS + CR not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            if buf.startswith(VT):
                body = buf[1: buf.index(FS + CR)].decode("utf-8", errors="replace")
                self.captured = body
            ack = "MSH|^~\\&|BRIDGE|LAB|MINDRAY|BC-5380|20260101||ACK^R01|A1|P|2.3.1\rMSA|AA|CAP\r"
            conn.sendall(VT + ack.encode("utf-8") + FS + CR)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def wait(self, timeout=5.0):
        deadline = time.time() + timeout
        while self.captured is None and time.time() < deadline:
            time.sleep(0.05)
        return self.captured

    def close(self):
        try:
            self._listener.close()
        except Exception:
            pass


def _obx_codes(oru_message: str):
    """Extract the test code from each OBX segment's OBX-3 (`^^^CODE^NAME`)."""
    codes = []
    for seg in oru_message.split("\r"):
        if seg.startswith("OBX|"):
            fields = seg.split("|")
            if len(fields) > 3:
                comps = fields[3].split("^")
                if len(comps) > 3 and comps[3]:
                    codes.append(comps[3])
    return codes


def _orm_for(accession: str, codes) -> str:
    """Build an ORM^O01 with one ORC/OBR group per ordered test (OBR-4 = ^^^CODE^NAME).
    This is the contract OE2's generateOrmO01 must emit and the mock must parse."""
    segs = [
        "MSH|^~\\&|OE2|LAB|MINDRAY|BC-5380|20260101120000||ORM^O01|CTRL-INT|P|2.3.1",
        f"PID|1||PAT-1^^^HOSP||DOE^JOHN||19800101|M",
    ]
    for i, code in enumerate(codes, start=1):
        segs.append(f"ORC|NW|{accession}|{accession}")
        segs.append(f"OBR|{i}|{accession}|{accession}|^^^{code}^{code}")
    return "\r".join(segs) + "\r"


def _drive_order(template, accession, codes, timeout=5.0):
    """Send an ORM to a mock MLLP handler over a socketpair; capture the pushed ORU."""
    capture = _MllpCaptureServer()
    env = {
        "ORDER_RESULT_PUSH_HOST": "127.0.0.1",
        "ORDER_RESULT_PUSH_PORT": str(capture.port),
    }
    client, server = _socketpair_tcp()
    handler = MLLPProtocolHandler(
        conn=server, addr=("127.0.0.1", 0), template=template,
        template_name="mindray_bc5380", response_delay_ms=0,
    )
    t = threading.Thread(target=handler.handle, daemon=True)
    with _patch_env(env):
        t.start()
        try:
            client.sendall(_mllp_frame(_orm_for(accession, codes)))
            _recv_mllp(client)  # the order ACK
            captured = capture.wait(timeout)
        finally:
            client.close()
            t.join(timeout=3)
            capture.close()
    return captured


class _patch_env:
    def __init__(self, env):
        self.env = env
        self._saved = {}

    def __enter__(self):
        for k, v in self.env.items():
            self._saved[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, old in self._saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


class _AstmCaptureServer:
    """Stand-in for the bridge's ASTM inbound listener (port 12001). Acts as an
    ASTM receiver: ACKs the ENQ, ACKs each frame, collects record content, stops
    on EOT, and stores the records the mock pushed."""

    def __init__(self):
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self.records = None
        self.peer_ip = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            conn, peer = self._listener.accept()
            self.peer_ip = peer[0]
        except OSError:
            return
        records = []
        conn.settimeout(5.0)
        try:
            while True:
                b = conn.recv(1)
                if not b:
                    break
                if b == ENQ:
                    conn.sendall(ACK)
                elif b == EOT:
                    break
                elif b == STX:
                    frame = b""
                    while not frame.endswith(CR + LF):
                        chunk = conn.recv(1)
                        if not chunk:
                            break
                        frame += chunk
                    term = frame.find(ETX)
                    if term == -1:
                        term = frame.find(ETB)
                    content = frame[1:term] if term > 0 else frame
                    records.append(content.decode("utf-8", errors="replace"))
                    conn.sendall(ACK)
            self.records = records
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def wait(self, timeout=5.0):
        deadline = time.time() + timeout
        while self.records is None and time.time() < deadline:
            time.sleep(0.05)
        return self.records

    def close(self):
        try:
            self._listener.close()
        except Exception:
            pass


def _astm_r_codes(records):
    """Extract test codes from ASTM R-records (`R|seq|^^^CODE|value|...`)."""
    codes = []
    for rec in records or []:
        if rec.startswith("R|"):
            parts = rec.split("|")
            if len(parts) > 2:
                comps = parts[2].split("^")
                code = comps[3] if len(comps) > 3 else ""
                if code:
                    codes.append(code)
    return codes


def _drive_astm_order(template, accession, codes, timeout=5.0):
    """Drive an ASTM order into the mock handler over a socketpair; capture the
    pushed result at a stand-in bridge ASTM receiver."""
    capture = _AstmCaptureServer()
    env = {
        "ORDER_RESULT_PUSH_HOST": "127.0.0.1",
        "ORDER_RESULT_PUSH_ASTM_PORT": str(capture.port),
    }
    client, server = _socketpair_tcp()
    handler = ASTMProtocolHandler(
        conn=server, addr=("127.0.0.1", 0), fields_config={},
        response_delay_ms=0, astm_template=template,
    )
    t = threading.Thread(target=handler.handle, daemon=True)
    records = ["H|\\^&|||OE2^Order^1.0|||||||LIS2-A2", f"P|1|||PAT-1"]
    for i, code in enumerate(codes, start=1):
        records.append(f"O|{i}|{accession}||^^^{code}|R")
    records.append("L|1|N")
    with _patch_env(env):
        t.start()
        try:
            send_astm_session(client, records, "test-order")
            captured = capture.wait(timeout)
        finally:
            client.close()
            t.join(timeout=3)
            capture.close()
    return captured


class TestAstmOrderRoundtrip:
    """ASTM already honors ordered codes; this pins that + requires unknown codes
    to surface as error results rather than being silently dropped."""

    def test_subset_order_returns_only_ordered_tests(self):
        # GeneXpert template has MTB-RIF, RIF, HIV-VL, COVID19. Order only MTB-RIF.
        records = _drive_astm_order(GENEXPERT_ASTM, "ACC-AST-1", ["MTB-RIF"])
        codes = _astm_r_codes(records)
        assert set(codes) == {"MTB-RIF"}, (
            f"ASTM result must contain exactly the ordered tests; got {codes}"
        )

    def test_unknown_ordered_code_yields_error_result(self):
        records = _drive_astm_order(GENEXPERT_ASTM, "ACC-AST-2", ["MTB-RIF", "BOGUS"])
        codes = _astm_r_codes(records)
        assert "MTB-RIF" in codes, f"known ordered test must be resulted; got {codes}"
        assert "BOGUS" in codes, (
            f"unknown ordered test must surface as an (error-flagged) R-record, not be "
            f"silently dropped; got {codes}"
        )


class TestHl7OrderRoundtrip:
    """The mock must return results for exactly the ORDERED tests, driven by the
    order content — not the analyzer's full template."""

    def test_subset_order_returns_only_ordered_tests(self):
        # Mindray template has WBC, RBC, HGB, HCT. Order only WBC + HGB.
        captured = _drive_order(MINDRAY_BC5380, "ACC-INT-1", ["WBC", "HGB"])
        assert captured is not None, "mock did not push a result to the capture listener"
        codes = _obx_codes(captured)
        assert set(codes) == {"WBC", "HGB"}, (
            f"result must contain exactly the ordered tests; got {codes}. "
            "Returning all template fields means the mock replays its template "
            "instead of processing the order."
        )

    def test_unknown_ordered_code_yields_error_result(self):
        # Order one known (WBC) + one unknown (BOGUS). Faithful analyzer reports
        # it cannot run BOGUS rather than silently omitting it.
        captured = _drive_order(MINDRAY_BC5380, "ACC-INT-2", ["WBC", "BOGUS"])
        assert captured is not None, "mock did not push a result"
        codes = _obx_codes(captured)
        assert "WBC" in codes, f"known ordered test WBC must be resulted; got {codes}"
        assert "BOGUS" in codes, (
            f"unknown ordered test must surface as an (error-flagged) result, not be "
            f"silently dropped; got {codes}"
        )

    def test_accession_is_echoed_for_correlation(self):
        captured = _drive_order(MINDRAY_BC5380, "ACC-INT-3", ["HGB"])
        assert captured is not None
        assert "ACC-INT-3" in captured, "originating accession must be echoed (OBR-3/ORC) for correlation"


class TestResultPushSourceInterface:
    """The mock is attached to one Docker network per analyzer; the bridge identifies
    the source analyzer by the push connection's SOURCE IP. The result push MUST source
    from the interface the order arrived on — the inbound connection's LOCAL address
    (self.conn.getsockname()[0], = the analyzer's IP), NOT self.addr (the bridge's
    address) and NOT the kernel's default route. Without binding, a multi-homed mock
    pushes from an arbitrary interface and the bridge mis-identifies the analyzer.

    Asserted with distinct 127.0.0.0/8 loopback addresses: the mock side listens on
    127.0.0.2/.3 (the 'analyzer interface'); the stand-in bridge listens on 127.0.0.1
    and records the push's source IP.
    """

    def test_astm_push_sources_from_ordering_analyzer_interface(self):
        capture = _AstmCaptureServer()
        env = {"ORDER_RESULT_PUSH_HOST": "127.0.0.1",
               "ORDER_RESULT_PUSH_ASTM_PORT": str(capture.port)}
        client, server = _socketpair_tcp("127.0.0.2")  # mock's per-analyzer interface
        handler = ASTMProtocolHandler(
            conn=server, addr=("127.0.0.1", 0), fields_config={},
            response_delay_ms=0, astm_template=GENEXPERT_ASTM,
        )
        t = threading.Thread(target=handler.handle, daemon=True)
        records = ["H|\\^&|||OE2^Order^1.0|||||||LIS2-A2", "P|1|||PAT-1",
                   "O|1|ACC-SRC-1||^^^MTB-RIF|R", "L|1|N"]
        with _patch_env(env):
            t.start()
            try:
                send_astm_session(client, records, "src-test")
                capture.wait(5)
            finally:
                client.close()
                t.join(timeout=3)
                capture.close()
        assert capture.peer_ip == "127.0.0.2", (
            "ASTM result push must source from the ordering analyzer's interface "
            "(self.conn.getsockname()[0]), so the bridge identifies the right "
            f"analyzer; got source {capture.peer_ip}"
        )

    def test_mllp_push_sources_from_ordering_analyzer_interface(self):
        capture = _MllpCaptureServer()
        env = {"ORDER_RESULT_PUSH_HOST": "127.0.0.1",
               "ORDER_RESULT_PUSH_PORT": str(capture.port)}
        client, server = _socketpair_tcp("127.0.0.3")
        handler = MLLPProtocolHandler(
            conn=server, addr=("127.0.0.1", 0), template=MINDRAY_BC5380,
            template_name="mindray_bc5380", response_delay_ms=0,
        )
        t = threading.Thread(target=handler.handle, daemon=True)
        with _patch_env(env):
            t.start()
            try:
                client.sendall(_mllp_frame(_orm_for("ACC-SRC-2", ["WBC"])))
                _recv_mllp(client)
                capture.wait(5)
            finally:
                client.close()
                t.join(timeout=3)
                capture.close()
        assert capture.peer_ip == "127.0.0.3", (
            "MLLP ORU push must source from the ordering analyzer's interface "
            f"(self.conn.getsockname()[0]); got source {capture.peer_ip}"
        )


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
