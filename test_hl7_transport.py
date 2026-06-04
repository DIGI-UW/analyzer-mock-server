#!/usr/bin/env python3
"""
HL7 transport tests for simulator push paths.
"""

import os
import socket
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import push as push_module


def _start_ack_server():
    """Start a tiny MLLP server that returns an AA ACK."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    received = {}

    def run():
        conn, _ = listener.accept()
        with conn:
            data = b""
            while b"\x1c\x0d" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            received["payload"] = data
            ack = (
                b"\x0b"
                b"MSH|^~\\&|BRIDGE|LAB|SIM|LAB|20260310120000||ACK^R01|ACK1|P|2.3.1\r"
                b"MSA|AA|CTRL1\r"
                b"\x1c\x0d"
            )
            conn.sendall(ack)
        listener.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return port, received, thread


def test_push_hl7_mllp_success():
    """MLLP push succeeds when receiver returns MSA|AA."""
    port, received, thread = _start_ack_server()
    message = "MSH|^~\\&|MINDRAY|BS-200|OE|LAB|20260310120000||ORU^R01|CTRL1|P|2.3.1\rOBX|1|NM|^^^GLU||102|mg/dL\r"
    ok, _ = push_module.push_hl7_mllp("127.0.0.1", port, message, timeout=5)
    thread.join(timeout=2)

    assert ok is True
    assert b"\x0b" in received["payload"]
    assert b"\x1c\x0d" in received["payload"]
    assert b"ORU^R01|CTRL1|" in received["payload"]


def test_push_hl7_destination_routes_mllp_scheme():
    """Destination parser routes mllp://host:port to MLLP transport."""
    port, _, thread = _start_ack_server()
    message = "MSH|^~\\&|MINDRAY|BS-300|OE|LAB|20260310120000||ORU^R01|CTRL2|P|2.3.1\r"
    ok, _ = push_module.push_hl7_to_destination(f"mllp://127.0.0.1:{port}", message)
    thread.join(timeout=2)
    assert ok is True


# --- source-IP readiness (the analyzer-network attach race) ---------------

def test_wait_source_ip_bindable_local_ip():
    """127.0.0.1 is always bindable, so the readiness probe returns True."""
    assert push_module._wait_source_ip_bindable("127.0.0.1") is True


def test_wait_source_ip_bindable_times_out_for_absent_ip():
    """An IP not configured on any interface never becomes bindable → bounded False."""
    assert push_module._wait_source_ip_bindable(
        "10.255.255.254", timeout_s=0.5, interval_s=0.1) is False


def test_push_hl7_mllp_surfaces_unready_source_ip_error():
    """When the per-analyzer network attach hasn't landed (source IP not yet
    bindable), the MLLP push returns (False, reason) naming the IP — never a
    silent False. This is the bug behind the harness "results never arrive"."""
    with patch.object(push_module, "_wait_source_ip_bindable", return_value=False):
        ok, err = push_module.push_hl7_mllp(
            "127.0.0.1", 2575, "MSH|^~\\&|X\r", source_ip="10.42.59.10")
    assert ok is False
    assert err and "10.42.59.10" in err and "not configured" in err


def test_push_astm_tcp_surfaces_unready_source_ip_error():
    """Same readiness contract for the ASTM TCP path."""
    with patch.object(push_module, "_wait_source_ip_bindable", return_value=False):
        ok, err = push_module.push_astm_tcp(
            "127.0.0.1", 12001, "H|\\^&\r", source_ip="10.42.59.10")
    assert ok is False
    assert err and "10.42.59.10" in err
