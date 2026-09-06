#!/usr/bin/env python3
"""
ASTM TCP payload encoding tests.

ASTM is a byte protocol, so what the simulator puts on the wire has to be the
bytes a real analyzer would send. Encoding the payload as ASCII rewrote accented
result text to '?', which meant no test could exercise a receiver's handling of
non-ASCII payloads — the gap behind MG-97.
"""

import os
import socket
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import push as push_module

ENQ = b"\x05"
ACK = b"\x06"
EOT = b"\x04"
STX = b"\x02"
ETX = b"\x03"
ETB = b"\x17"

ACCENTED_RECORD = "R|1|^^^EV|NON DÉTECTÉ^|copies/mL|A||F"


def _start_astm_ack_server():
    """Tiny ASTM receiver that ACKs everything and records the raw bytes."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    received = {"frames": b""}

    def run():
        conn, _ = listener.accept()
        with conn:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                received["frames"] += chunk
                if chunk == EOT or chunk.endswith(EOT):
                    break
                # ACK the establishment and each frame.
                conn.sendall(ACK)
        listener.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return port, received, thread


def _send(records, encoding):
    port, received, thread = _start_astm_ack_server()
    original = push_module.ASTM_TCP_ENCODING
    push_module.ASTM_TCP_ENCODING = encoding
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.settimeout(10)
            push_module.send_astm_session(sock, records, "test")
    finally:
        push_module.ASTM_TCP_ENCODING = original
    thread.join(timeout=5)
    return received["frames"]


def test_default_encoding_is_byte_transparent_for_latin1():
    """Accented text must reach the wire as the single bytes an analyzer sends."""
    assert push_module.ASTM_TCP_ENCODING == "iso-8859-1", (
        "default must keep accented payloads intact"
    )
    wire = _send([ACCENTED_RECORD], "iso-8859-1")
    assert bytes([0xC9]) in wire, f"0xC9 (E-acute) missing from wire: {wire!r}"
    assert b"?TECT" not in wire, f"accented character was replaced: {wire!r}"


def test_checksum_is_computed_over_the_bytes_actually_sent():
    """A receiver recomputing the checksum from the wire bytes must agree."""
    wire = _send([ACCENTED_RECORD], "iso-8859-1")
    start = wire.index(STX)
    terminator = ETX if ETX in wire else ETB
    end = wire.index(terminator, start)
    frame_number = wire[start + 1 : start + 2]
    body = wire[start + 2 : end]
    expected = (sum(frame_number) + sum(body) + sum(terminator)) % 256
    sent_checksum = int(wire[end + 1 : end + 3].decode("ascii"), 16)
    assert sent_checksum == expected, (
        f"checksum {sent_checksum:02X} does not match bytes on wire {expected:02X}"
    )


def test_ascii_payloads_are_unchanged_by_the_default():
    """Existing ASCII fixtures must produce byte-identical frames."""
    ascii_record = "R|1|^^^EV|NOT DETECTED^|copies/mL|A||F"
    assert _send([ascii_record], "iso-8859-1") == _send([ascii_record], "ascii")


def test_encoding_is_overridable():
    """An analyzer that speaks UTF-8 can be simulated too."""
    wire = _send([ACCENTED_RECORD], "utf-8")
    assert "NON DÉTECTÉ".encode("utf-8") in wire, f"utf-8 payload not on wire: {wire!r}"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{'FAILED' if failures else 'OK'} — {failures} failure(s)")
    sys.exit(1 if failures else 0)
