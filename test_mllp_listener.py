#!/usr/bin/env python3
"""
Tests for MLLP listener (protocols/mllp_listener.py).

Covers:
- Single frame roundtrip with ACK validation
- Persistent connection (multiple messages on one socket)
- MLLP framing byte validation (VT/FS/CR)
- Idle timeout behavior (M3 fix)
- Malformed frame handling
- Pipelined frames in a single TCP send (M4 fix)
- Clean exit on client disconnect
- ACK sending facility field
"""

import os
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from protocols.mllp_listener import MLLPProtocolHandler

# MLLP framing bytes
VT = b"\x0B"
FS = b"\x1C"
CR = b"\x0D"

# Minimal HL7 message template
_HL7_TEMPLATE = (
    "MSH|^~\\&|ANALYZER|FACILITY|OE|LAB|20260326120000||ORU^R01|{ctrl_id}|P|2.3.1\r"
    "OBX|1|NM|^^^GLU||102|mg/dL\r"
)


def _make_hl7(ctrl_id: str = "CTRL001") -> str:
    return _HL7_TEMPLATE.format(ctrl_id=ctrl_id)


def _mllp_frame(message: str) -> bytes:
    """Wrap a string message in MLLP framing bytes."""
    return VT + message.encode("utf-8") + FS + CR


def _recv_mllp_frame(sock: socket.socket, timeout: float = 5.0) -> str:
    """Read one MLLP-framed response from a socket. Returns decoded message content."""
    sock.settimeout(timeout)
    data = b""
    while FS + CR not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Socket closed before FS+CR received")
        data += chunk
    # Strip VT prefix if present
    if data.startswith(VT):
        data = data[1:]
    # Strip FS+CR suffix
    idx = data.index(FS + CR)
    return data[:idx].decode("utf-8")


def _segments(msg: str):
    """Split HL7 message into segments."""
    return [s for s in msg.split("\r") if s.strip()]


def _field(segment: str, index: int) -> str:
    """Get a pipe-delimited field from a segment (0-based, MSH field encoding counts as index 1)."""
    return segment.split("|")[index]


def _make_handler(server_sock, template=None, template_name="TestAnalyzer", response_delay_ms=0):
    """Create an MLLPProtocolHandler from the server side of a socket pair."""
    addr = server_sock.getpeername()
    return MLLPProtocolHandler(
        conn=server_sock,
        addr=addr,
        template=template,
        template_name=template_name,
        response_delay_ms=response_delay_ms,
    )


def _socketpair_tcp():
    """Create a TCP socketpair via localhost listener (cross-platform)."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(("127.0.0.1", port))
    server, _ = listener.accept()
    listener.close()
    return client, server


class TestMLLPListener:
    """Tests for MLLPProtocolHandler."""

    def test_single_frame_roundtrip(self):
        """Send one MLLP-framed HL7 message, verify ACK structure (MSH-9=ACK, MSA-1=AA, MSA-2=original control ID)."""
        client, server = _socketpair_tcp()
        handler = _make_handler(server)
        t = threading.Thread(target=handler.handle, daemon=True)
        t.start()

        try:
            client.sendall(_mllp_frame(_make_hl7("MSG100")))
            ack_str = _recv_mllp_frame(client)
            segs = _segments(ack_str)

            # MSH segment
            msh = segs[0]
            assert msh.startswith("MSH|"), f"Expected MSH segment, got: {msh}"
            msh_9 = _field(msh, 8)
            assert msh_9.startswith("ACK"), f"MSH-9 should be ACK type, got: {msh_9}"

            # MSA segment
            msa = segs[1]
            assert msa.startswith("MSA|"), f"Expected MSA segment, got: {msa}"
            assert _field(msa, 1) == "AA", f"MSA-1 should be AA, got: {_field(msa, 1)}"
            assert _field(msa, 2) == "MSG100", f"MSA-2 should be original control ID, got: {_field(msa, 2)}"
        finally:
            client.close()
            t.join(timeout=3)

    def test_multiple_messages_persistent_connection(self):
        """Send 3 messages on the same TCP connection, verify 3 ACKs (persistent connection support)."""
        client, server = _socketpair_tcp()
        handler = _make_handler(server)
        t = threading.Thread(target=handler.handle, daemon=True)
        t.start()

        try:
            acks = []
            for i in range(3):
                ctrl_id = f"MULTI{i:03d}"
                client.sendall(_mllp_frame(_make_hl7(ctrl_id)))
                ack_str = _recv_mllp_frame(client)
                segs = _segments(ack_str)
                msa = segs[1]
                acks.append(_field(msa, 2))

            assert acks == ["MULTI000", "MULTI001", "MULTI002"], f"Expected 3 ACKs with matching control IDs, got: {acks}"
        finally:
            client.close()
            t.join(timeout=3)

    def test_framing_validation(self):
        """VT/FS/CR delimiters are correctly parsed; message content between delimiters is extracted."""
        client, server = _socketpair_tcp()
        handler = _make_handler(server)
        t = threading.Thread(target=handler.handle, daemon=True)
        t.start()

        try:
            msg = _make_hl7("FRAME01")
            raw_frame = _mllp_frame(msg)

            # Verify our frame has correct structure
            assert raw_frame[0:1] == VT
            assert raw_frame[-2:-1] == FS
            assert raw_frame[-1:] == CR

            client.sendall(raw_frame)
            ack_str = _recv_mllp_frame(client)

            # ACK should reference the control ID we sent, proving the message was correctly extracted
            segs = _segments(ack_str)
            msa = segs[1]
            assert _field(msa, 2) == "FRAME01"
        finally:
            client.close()
            t.join(timeout=3)

    def test_idle_timeout_keeps_connection(self):
        """After sending a message, wait briefly (< SOCKET_TIMEOUT), then send another -- connection stays alive (M3 fix)."""
        client, server = _socketpair_tcp()
        # Use a short socket timeout so the test doesn't take forever
        handler = _make_handler(server)
        t = threading.Thread(target=handler.handle, daemon=True)
        t.start()

        try:
            # First message
            client.sendall(_mllp_frame(_make_hl7("IDLE01")))
            ack1 = _recv_mllp_frame(client)
            segs1 = _segments(ack1)
            assert _field(segs1[1], 2) == "IDLE01"

            # Wait a bit (but less than SOCKET_TIMEOUT=30s)
            time.sleep(0.5)

            # Second message on same connection should still work
            client.sendall(_mllp_frame(_make_hl7("IDLE02")))
            ack2 = _recv_mllp_frame(client)
            segs2 = _segments(ack2)
            assert _field(segs2[1], 2) == "IDLE02"
        finally:
            client.close()
            t.join(timeout=3)

    def test_malformed_frame_no_vt(self):
        """Send data without VT start byte, verify connection is handled gracefully (no crash)."""
        client, server = _socketpair_tcp()
        handler = _make_handler(server)
        t = threading.Thread(target=handler.handle, daemon=True)
        t.start()

        try:
            # Send a message without VT start byte
            bad_frame = _make_hl7("BAD01").encode("utf-8") + FS + CR
            client.sendall(bad_frame)

            # Handler should exit gracefully -- the thread should finish
            t.join(timeout=5)
            assert not t.is_alive(), "Handler thread should have exited after malformed frame"
        finally:
            client.close()

    def test_pipelined_frames(self):
        """Send two back-to-back MLLP frames in a single TCP send, verify both get ACKs (M4 fix)."""
        client, server = _socketpair_tcp()
        handler = _make_handler(server)
        t = threading.Thread(target=handler.handle, daemon=True)
        t.start()

        try:
            # Build two frames concatenated
            frame1 = _mllp_frame(_make_hl7("PIPE01"))
            frame2 = _mllp_frame(_make_hl7("PIPE02"))
            combined = frame1 + frame2

            # Send both in a single TCP write
            client.sendall(combined)

            # Read two ACKs
            ack1 = _recv_mllp_frame(client)
            ack2 = _recv_mllp_frame(client)

            segs1 = _segments(ack1)
            segs2 = _segments(ack2)
            assert _field(segs1[1], 2) == "PIPE01", f"First ACK should reference PIPE01, got: {_field(segs1[1], 2)}"
            assert _field(segs2[1], 2) == "PIPE02", f"Second ACK should reference PIPE02, got: {_field(segs2[1], 2)}"
        finally:
            client.close()
            t.join(timeout=3)

    def test_connection_close_returns_none(self):
        """Close socket from client side, verify handler exits cleanly."""
        client, server = _socketpair_tcp()
        handler = _make_handler(server)
        t = threading.Thread(target=handler.handle, daemon=True)
        t.start()

        # Close client immediately
        client.close()

        # Handler should exit cleanly without exceptions
        t.join(timeout=5)
        assert not t.is_alive(), "Handler thread should have exited after client disconnect"

    def test_ack_contains_sending_facility(self):
        """ACK MSH segment includes the analyzer name as sending facility."""
        client, server = _socketpair_tcp()
        template = {"analyzer": {"name": "Mindray BS-200"}}
        handler = _make_handler(server, template=template, template_name="mindray-bs200")
        t = threading.Thread(target=handler.handle, daemon=True)
        t.start()

        try:
            client.sendall(_mllp_frame(_make_hl7("FAC01")))
            ack_str = _recv_mllp_frame(client)
            segs = _segments(ack_str)
            msh = segs[0]

            # MSH-4 is sending facility (index 3 in pipe-split)
            sending_facility = _field(msh, 3)
            assert sending_facility == "MINDRAY-BS-200", (
                f"MSH-4 sending facility should be analyzer name uppercased, got: {sending_facility}"
            )
        finally:
            client.close()
            t.join(timeout=3)
