"""
MLLP (Minimal Lower Layer Protocol) server-side listener for HL7 analyzers.

Accepts inbound MLLP connections and responds with HL7 ACK — simulating
a real HL7 analyzer that listens for LIS-initiated messages (ORM^O01
worklist download, QRY queries, etc.).

MLLP framing (per HL7 v2.x MLLP conventions):
  Inbound:  <VT> hl7_message <FS><CR>
  Response: <VT> ack_message <FS><CR>

Where:
  VT  = 0x0B (Vertical Tab, Start Block)
  FS  = 0x1C (File Separator, End Block)
  CR  = 0x0D (Carriage Return)

Reference: specs/013-hjra-hl7-stream-alignment
"""

import logging
import socket
import time
from datetime import datetime
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# MLLP framing bytes
VT = b"\x0B"  # Start Block
FS = b"\x1C"  # End Block
CR = b"\x0D"  # Carriage Return

SOCKET_TIMEOUT = 30.0
RECV_BUFFER = 4096


class MLLPProtocolHandler:
    """Handles MLLP protocol for a single inbound connection.

    Mirrors ASTMProtocolHandler's lifecycle pattern:
    - Constructed with a connected socket + template context
    - handle() runs the receive/respond loop
    - Thread-safe for use with per-connection threading
    """

    def __init__(
        self,
        conn: socket.socket,
        addr: Tuple,
        template: Optional[Dict] = None,
        template_name: Optional[str] = None,
        response_delay_ms: int = 0,
    ):
        self.conn = conn
        self.addr = addr
        self.template = template
        self.template_name = template_name or "unknown"
        self.response_delay_ms = response_delay_ms
        self.running = True

        analyzer_name = ""
        if template:
            analyzer_name = template.get("analyzer", {}).get("name", "")
        self.analyzer_name = analyzer_name or self.template_name

    def handle(self):
        """Main handler loop — read MLLP frames, respond with ACK."""
        logger.info(
            "[MLLP-LISTEN] Connection from %s (analyzer: %s, port: %s)",
            self.addr,
            self.analyzer_name,
            self.conn.getsockname()[1],
        )
        self.conn.settimeout(SOCKET_TIMEOUT)

        try:
            while self.running:
                message = self._receive_mllp_frame()
                if message is None:
                    break

                logger.info(
                    "[MLLP-LISTEN] Received HL7 message from %s (%d bytes): %s",
                    self.addr,
                    len(message),
                    message[:120].replace("\r", "\\r"),
                )

                # Parse MSH-10 (message control ID) for ACK
                control_id = self._extract_msh10(message)
                msg_type = self._extract_msh9(message)

                # Simulate response delay
                if self.response_delay_ms > 0:
                    time.sleep(self.response_delay_ms / 1000.0)

                # Generate and send ACK
                ack = self._build_ack(control_id, msg_type)
                self._send_mllp_frame(ack)

                logger.info(
                    "[MLLP-LISTEN] Sent ACK to %s for control ID %s (msg type: %s)",
                    self.addr,
                    control_id,
                    msg_type,
                )

        except socket.timeout:
            logger.debug("[MLLP-LISTEN] Connection timeout from %s", self.addr)
        except ConnectionResetError:
            logger.info("[MLLP-LISTEN] Connection reset by %s", self.addr)
        except Exception as e:
            logger.error("[MLLP-LISTEN] Error handling connection from %s: %s", self.addr, e)
        finally:
            try:
                self.conn.close()
            except Exception:
                pass
            logger.info("[MLLP-LISTEN] Connection closed from %s", self.addr)

    def __init_leftover(self):
        """Lazy-init the leftover buffer for pipelined frames."""
        if not hasattr(self, "_leftover"):
            self._leftover = bytearray()

    def _receive_mllp_frame(self) -> Optional[str]:
        """Read one MLLP-framed message. Returns message content or None on disconnect.

        Supports persistent connections: loops on idle timeouts waiting for VT,
        only returns None on actual disconnect. Handles pipelined frames by
        preserving leftover bytes after FS+CR for the next call.
        """
        self.__init_leftover()

        # Wait for VT start byte — loop on timeout to keep persistent connections alive
        while self.running:
            # Check leftover buffer first (from pipelined frames)
            if self._leftover:
                if self._leftover[0:1] == VT:
                    self._leftover = self._leftover[1:]
                    break
                else:
                    logger.warning(
                        "[MLLP-LISTEN] Expected VT (0x0B) in leftover, got 0x%s from %s",
                        self._leftover[0:1].hex(),
                        self.addr,
                    )
                    self._leftover.clear()
                    return None

            try:
                start = self.conn.recv(1)
            except socket.timeout:
                # Idle timeout — keep connection alive for persistent analyzers
                continue

            if not start:
                return None  # Connection closed

            if start == VT:
                break

            logger.warning(
                "[MLLP-LISTEN] Expected VT (0x0B), got 0x%s from %s",
                start.hex(),
                self.addr,
            )
            return None

        if not self.running:
            return None

        # Read until FS+CR end marker
        buffer = bytearray(self._leftover)
        self._leftover.clear()

        while True:
            # Check for FS+CR terminator anywhere in buffer (handles pipelined frames)
            term_pos = buffer.find(FS + CR)
            if term_pos >= 0:
                message = buffer[:term_pos].decode("utf-8", errors="replace")
                # Preserve any bytes after FS+CR for next frame
                self._leftover = bytearray(buffer[term_pos + 2:])
                return message

            if len(buffer) > 1_000_000:
                logger.error("[MLLP-LISTEN] Message too large (>1MB) from %s", self.addr)
                return None

            try:
                chunk = self.conn.recv(RECV_BUFFER)
            except socket.timeout:
                logger.warning("[MLLP-LISTEN] Timeout reading message body from %s", self.addr)
                return None

            if not chunk:
                return None  # Connection closed mid-message

            buffer.extend(chunk)

    def _send_mllp_frame(self, message: str):
        """Send an MLLP-framed message."""
        frame = VT + message.encode("utf-8") + FS + CR
        self.conn.sendall(frame)

    def _extract_msh10(self, message: str) -> str:
        """Extract MSH-10 (Message Control ID) from HL7 message."""
        for segment in message.split("\r"):
            if segment.startswith("MSH"):
                fields = segment.split("|")
                if len(fields) > 9:
                    return fields[9]  # MSH-10 is field index 9 (0-based after MSH|^~\\&)
        return "UNKNOWN"

    def _extract_msh9(self, message: str) -> str:
        """Extract MSH-9 (Message Type) from HL7 message."""
        for segment in message.split("\r"):
            if segment.startswith("MSH"):
                fields = segment.split("|")
                if len(fields) > 8:
                    return fields[8]  # MSH-9
        return "UNKNOWN"

    def _build_ack(self, control_id: str, msg_type: str) -> str:
        """Build HL7 ACK message matching the inbound message's control ID."""
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        ack_control_id = f"ACK{ts}"

        # Derive ACK message type from inbound (e.g., ORU^R01 → ACK^R01)
        ack_type = "ACK"
        if "^" in msg_type:
            ack_type = f"ACK^{msg_type.split('^')[1]}"

        sending_app = "SIMULATOR"
        sending_facility = self.analyzer_name.upper().replace(" ", "-")

        segments = [
            f"MSH|^~\\&|{sending_app}|{sending_facility}|OpenELIS|LAB|{ts}||{ack_type}|{ack_control_id}|P|2.3.1",
            f"MSA|AA|{control_id}|Message accepted",
        ]

        return "\r".join(segments) + "\r"
