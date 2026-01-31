"""
RS232 / virtual serial protocol handler (M4). Uses socat for virtual ports.

Reference: specs/011-madagascar-analyzer-integration, tasks T079–T082.
"""

import logging
import time
from typing import Any, Dict, Optional

from .base_handler import BaseHandler
from .astm_handler import ASTMHandler, build_astm_frames

logger = logging.getLogger(__name__)

ENQ = b"\x05"
ACK = b"\x06"
EOT = b"\x04"


def send_astm_over_serial(
    port_path: str,
    astm_message: str,
    baud: int = 9600,
    timeout: float = 2.0,
) -> bool:
    """
    Send ASTM message over serial (ENQ/ACK, framed segments, EOT).
    Use socat to create a pair: socat -d -d pty,raw,echo=0 pty,raw,echo=0
    """
    try:
        import serial
    except ImportError:
        logger.error("pyserial not installed. pip install pyserial")
        return False

    frames = build_astm_frames(astm_message)
    if not frames:
        logger.warning("No ASTM frames to send")
        return False

    try:
        ser = serial.Serial(port_path, baudrate=baud, timeout=timeout)
    except Exception as e:
        logger.error("Failed to open %s: %s", port_path, e)
        return False

    try:
        ser.write(ENQ)
        ser.flush()
        r = ser.read(1)
        if r != ACK:
            logger.warning("No ACK after ENQ (got %r), continuing anyway", r)
        for i, frame in enumerate(frames):
            ser.write(frame)
            ser.flush()
            r = ser.read(1)
            if r != ACK:
                logger.warning("No ACK after frame %s (got %r)", i + 1, r)
        ser.write(EOT)
        ser.flush()
        logger.info("Sent ASTM over %s (%s frames)", port_path, len(frames))
        return True
    except Exception as e:
        logger.exception("Serial send failed: %s", e)
        return False
    finally:
        try:
            ser.close()
        except Exception:
            pass


class SerialHandler(BaseHandler):
    """
    RS232 protocol handler. Generates ASTM from template (Pentra/Micros);
    transport via send_astm_over_serial(port, message).
    """

    protocol_type = "RS232"

    def generate(self, template: Dict[str, Any], **kwargs) -> str:
        if not self.validate_template(template):
            raise ValueError("Invalid template: missing analyzer or fields")
        return ASTMHandler().generate(template, **kwargs)
