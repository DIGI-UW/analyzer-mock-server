"""
Transport push functions for the analyzer mock server.

Handles pushing messages to destinations via various protocols:
- ASTM over TCP (ENQ/ACK framing)
- HL7 over MLLP (VT/FS framing)
- HTTP POST to OpenELIS
"""

import logging
import socket
import ssl
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# ASTM control characters
ENQ = b'\x05'
ACK = b'\x06'
NAK = b'\x15'
EOT = b'\x04'
STX = b'\x02'
ETX = b'\x03'
ETB = b'\x17'
CR = b'\x0D'
LF = b'\x0A'

# MLLP framing
VT = b'\x0B'
FS = b'\x1C'


def push_hl7_to_destination(destination: str, hl7_message: str) -> bool:
    """Route HL7 push by destination scheme: mllp:// or http(s)://."""
    if destination.startswith("mllp://"):
        addr = destination[len("mllp://"):]
        if ":" not in addr:
            logger.error("[PUSH-HL7] Invalid MLLP destination (missing port): %s", destination)
            return False
        host, port_str = addr.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            logger.error("[PUSH-HL7] Invalid MLLP port: %s", port_str)
            return False
        return push_hl7_mllp(host, port, hl7_message)
    return push_hl7_http(destination, hl7_message)


def push_astm_to_destination(destination: str, astm_message: str) -> bool:
    """Route ASTM push by destination scheme: tcp:// or http(s)://."""
    if destination.startswith("tcp://"):
        addr = destination[len("tcp://"):]
        if ":" not in addr:
            logger.error("[PUSH] Invalid TCP destination (missing port): %s", destination)
            return False
        host, port_str = addr.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            logger.error("[PUSH] Invalid TCP port: %s", port_str)
            return False
        return push_astm_tcp(host, port, astm_message)
    return push_astm_http(destination, astm_message)


def push_hl7_mllp(host: str, port: int, hl7_message: str, timeout: int = 30) -> bool:
    """Push an HL7 message over MLLP and require positive application ACK."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        payload = VT + hl7_message.encode("utf-8") + FS + CR
        sock.sendall(payload)

        ack = b""
        while not ack.endswith(FS + CR):
            chunk = sock.recv(4096)
            if not chunk:
                logger.error("[PUSH-HL7-MLLP] Peer closed before MLLP terminator from %s:%s", host, port)
                return False
            ack += chunk
        ack_text = ack.replace(VT, b"").replace(FS + CR, b"").decode("utf-8", errors="replace")
        if "MSA|AA|" in ack_text:
            logger.info("[PUSH-HL7-MLLP] Positive ACK received from %s:%s", host, port)
            return True
        logger.error("[PUSH-HL7-MLLP] Negative/invalid ACK from %s:%s: %s", host, port, ack_text[:200])
        return False
    except Exception as e:
        logger.error("[PUSH-HL7-MLLP] Push failed: %s", e, exc_info=True)
        return False
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


def push_hl7_http(openelis_url: str, hl7_message: str, timeout: int = 30) -> bool:
    """Push an HL7 ORU^R01 message to OpenELIS via HTTP POST."""
    if openelis_url.rstrip('/').endswith('/hl7') or '/analyzer/' in openelis_url:
        endpoint = openelis_url if openelis_url.startswith('http') else f"https://{openelis_url}"
    else:
        endpoint = f"{openelis_url.rstrip('/')}/api/OpenELIS-Global/analyzer/hl7"
    try:
        logger.info("[PUSH-HL7] Pushing ORU^R01 to %s", endpoint)
        req = urllib.request.Request(
            endpoint, data=hl7_message.encode('utf-8'),
            headers={'Content-Type': 'text/plain; charset=utf-8'}, method='POST')
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as response:
            if response.getcode() == 200:
                logger.info("[PUSH-HL7] Push successful")
                return True
            logger.error("[PUSH-HL7] Push failed: HTTP %s", response.getcode())
            return False
    except Exception as e:
        logger.error("[PUSH-HL7] Push failed: %s", e, exc_info=True)
        return False


def push_astm_http(openelis_url: str, astm_message: str, timeout: int = 30) -> bool:
    """Push an ASTM message to OpenELIS via HTTP POST."""
    endpoint = f"{openelis_url}/api/OpenELIS-Global/analyzer/astm"
    try:
        logger.info("[PUSH] Pushing ASTM message to %s", endpoint)
        req = urllib.request.Request(
            endpoint, data=astm_message.encode('utf-8'),
            headers={'Content-Type': 'text/plain; charset=utf-8'}, method='POST')
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as response:
            if response.getcode() == 200:
                logger.info("[PUSH] Push successful (HTTP %s)", response.getcode())
                return True
            logger.error("[PUSH] Push failed: HTTP %s", response.getcode())
            return False
    except urllib.error.HTTPError as e:
        logger.error("[PUSH] HTTP error %s: %s", e.code, e.reason)
        return False
    except Exception as e:
        logger.error("[PUSH] Push failed: %s", e, exc_info=True)
        return False


def send_astm_session(sock, records: list, session_label: str = "") -> bool:
    """Send one ASTM session (ENQ → framed records → EOT) over an open socket."""
    tag = f"[PUSH-TCP{' ' + session_label if session_label else ''}]"

    sock.sendall(ENQ)
    response = sock.recv(1)
    if response != ACK:
        logger.error("%s ENQ not ACKed, got: %s", tag, response.hex() if response else 'none')
        return False
    logger.debug("%s ENQ acknowledged, sending %d frames", tag, len(records))

    for i, record in enumerate(records):
        frame_number = (i + 1) % 8
        content_bytes = (record.strip() + '\r').encode('ascii', errors='replace')
        frame_num_bytes = str(frame_number).encode()
        is_last = (i == len(records) - 1)
        terminator = ETX if is_last else ETB

        checksum_data = frame_num_bytes + content_bytes + terminator
        checksum = sum(checksum_data) % 256
        checksum_str = f'{checksum:02X}'.encode()

        frame = STX + frame_num_bytes + content_bytes + terminator + checksum_str + CR + LF
        sock.sendall(frame)

        ack = sock.recv(1)
        if ack == EOT:
            logger.info("%s Receiver sent EOT (interrupt) at frame %d", tag, i + 1)
            return False
        if ack != ACK:
            logger.warning("%s Frame %d/%d NAKed: %s", tag, i + 1, len(records), ack.hex() if ack else 'none')
            return False
        logger.debug("%s Frame %d/%d ACKed (FN=%d)", tag, i + 1, len(records), frame_number)

    sock.sendall(EOT)
    logger.info("%s Session complete — %d frames sent", tag, len(records))
    return True


def push_astm_tcp(host: str, port: int, astm_message: str, timeout: int = 30) -> bool:
    """Push ASTM message via raw TCP with ENQ/ACK framing."""
    logger.info("[PUSH-TCP] Connecting to %s:%s for ASTM TCP push", host, port)

    all_records = [r for r in astm_message.strip().split('\n') if r.strip()]
    sessions = []
    current = []
    for record in all_records:
        if record.strip().startswith('H|') and current:
            sessions.append(current)
            current = []
        current.append(record)
    if current:
        sessions.append(current)

    logger.info("[PUSH-TCP] Message has %d session(s), %d total records", len(sessions), len(all_records))

    for idx, session_records in enumerate(sessions):
        label = f"S{idx + 1}/{len(sessions)}" if len(sessions) > 1 else ""
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(timeout)
            sock.connect((host, port))
            if not send_astm_session(sock, session_records, label):
                return False
        except socket.timeout:
            logger.error("[PUSH-TCP] Timeout connecting to %s:%s", host, port)
            return False
        except ConnectionRefusedError:
            logger.error("[PUSH-TCP] Connection refused by %s:%s", host, port)
            return False
        except Exception as e:
            logger.error("[PUSH-TCP] Push failed: %s", e, exc_info=True)
            return False
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
        if idx < len(sessions) - 1:
            time.sleep(0.5)

    logger.info("[PUSH-TCP] All %d session(s) sent to %s:%s", len(sessions), host, port)
    return True
