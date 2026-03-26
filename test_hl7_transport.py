#!/usr/bin/env python3
"""
HL7 transport tests for simulator push paths.
"""

import os
import socket
import sys
import threading

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
    ok = push_module.push_hl7_mllp("127.0.0.1", port, message, timeout=5)
    thread.join(timeout=2)

    assert ok is True
    assert b"\x0b" in received["payload"]
    assert b"\x1c\x0d" in received["payload"]
    assert b"ORU^R01|CTRL1|" in received["payload"]


def test_push_hl7_destination_routes_mllp_scheme():
    """Destination parser routes mllp://host:port to MLLP transport."""
    port, _, thread = _start_ack_server()
    message = "MSH|^~\\&|MINDRAY|BS-300|OE|LAB|20260310120000||ORU^R01|CTRL2|P|2.3.1\r"
    ok = push_module.push_hl7_to_destination(f"mllp://127.0.0.1:{port}", message)
    thread.join(timeout=2)
    assert ok is True
