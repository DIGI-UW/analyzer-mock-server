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
import os
import socket
import time
from datetime import datetime
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Default destination for ORU^R01 result push after an inbound ORM^O01 order.
# In the harness docker-compose, both vars resolve to the bridge's MLLP listener;
# overrideable per environment (or set to empty to disable the push entirely).
DEFAULT_ORDER_RESULT_PUSH_HOST = "openelis-analyzer-bridge"
DEFAULT_ORDER_RESULT_PUSH_PORT = 2575

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

                # LIS-initiated order: push a matching ORU^R01 result back to the
                # bridge's MLLP listener so OE2's inbound import sees it. Echoes
                # ORC-2 (placer) and OBR-3 (filler / accession) for correlation.
                if msg_type.startswith("ORM") and self.template:
                    self._push_order_result(message)

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

    def _extract_order_correlation(self, order_message: str) -> Tuple[Optional[str], Optional[str]]:
        """Parse ORC-2/OBR-2 (placer) and OBR-3/ORC-3 (filler) from an inbound
        ORM^O01. The bridge embeds the OE2 accession in both placer and filler;
        we echo whatever was sent. Returns (placer, filler), either may be None
        if absent."""
        placer: Optional[str] = None
        filler: Optional[str] = None
        for segment in order_message.split("\r"):
            if segment.startswith("OBR|"):
                fields = segment.split("|")
                if len(fields) > 2 and fields[2].strip():
                    placer = placer or fields[2].strip()
                if len(fields) > 3 and fields[3].strip():
                    filler = fields[3].strip()
            elif segment.startswith("ORC|"):
                fields = segment.split("|")
                if len(fields) > 2 and fields[2].strip() and placer is None:
                    placer = fields[2].strip()
                if len(fields) > 3 and fields[3].strip() and filler is None:
                    filler = fields[3].strip()
        return placer, filler

    def _push_order_result(self, order_message: str) -> None:
        """After ACK'ing an ORM^O01, push a matching ORU^R01 to the LIS via a
        fresh MLLP connection to the configured destination (the bridge's MLLP
        listener in compose). Result OBR-3 echoes the inbound order's filler so
        OE2's existing accession-keyed inbound result import picks it up.

        Failures are logged, not raised — the order-receipt path stays
        successful even if the result push can't reach the bridge.
        """
        # Lazy import to avoid cross-package import cycle at module load
        from push import push_hl7_mllp
        from protocols.hl7_handler import generate_oru_r01

        host = os.environ.get("ORDER_RESULT_PUSH_HOST", DEFAULT_ORDER_RESULT_PUSH_HOST)
        port_raw = os.environ.get("ORDER_RESULT_PUSH_PORT", str(DEFAULT_ORDER_RESULT_PUSH_PORT))
        if not host:
            logger.info("[ORDER_IN] ORDER_RESULT_PUSH_HOST empty; result push disabled")
            return
        try:
            port = int(port_raw)
        except (TypeError, ValueError):
            logger.warning("[ORDER_IN] ORDER_RESULT_PUSH_PORT=%r invalid; skipping push", port_raw)
            return

        placer, filler = self._extract_order_correlation(order_message)
        if not (placer or filler):
            logger.warning(
                "[ORDER_IN] No ORC/OBR placer or filler in order; cannot correlate result for %s",
                self.addr,
            )
            return

        try:
            oru = generate_oru_r01(
                self.template,
                deterministic=True,
                placer_order_id=placer or filler,
                filler_order_id=filler or placer,
            )
        except Exception as e:
            logger.error("[ORDER_IN] Failed to generate ORU^R01: %s", e, exc_info=True)
            return

        try:
            ok = push_hl7_mllp(host, port, oru, timeout=10)
            if ok:
                logger.info(
                    "[ORDER_IN] Pushed ORU^R01 to %s:%s (placer=%s, filler=%s)",
                    host, port, placer, filler,
                )
            else:
                logger.warning(
                    "[ORDER_IN] push_hl7_mllp returned false for %s:%s (placer=%s, filler=%s)",
                    host, port, placer, filler,
                )
        except Exception as e:
            logger.error(
                "[ORDER_IN] Exception pushing ORU^R01 to %s:%s: %s",
                host, port, e, exc_info=True,
            )

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
