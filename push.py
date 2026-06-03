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
from typing import Optional

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

# How long a push waits for an analyzer's per-network source IP to become
# bindable. POST /analyzers attaches the mock container to the new Docker
# network in a background thread (so the create response isn't blocked by the
# attach), so a push fired immediately after create can race ahead of the
# attach. We poll for the IP rather than fail the push on that timing window.
SOURCE_IP_READY_TIMEOUT_S = 12.0
SOURCE_IP_READY_INTERVAL_S = 0.25

# Result type: (delivered, error_reason). error_reason is None on success and a
# human-readable string on failure so callers never see a silent False — the
# whole point of pushing results through the mock is to know WHY one didn't land.
PushResult = "tuple[bool, Optional[str]]"


def _wait_source_ip_bindable(source_ip: str,
                             timeout_s: float = SOURCE_IP_READY_TIMEOUT_S,
                             interval_s: float = SOURCE_IP_READY_INTERVAL_S) -> bool:
    """Poll until `source_ip` is a bindable local address on this container.

    The mock binds the outgoing socket to the analyzer's per-network IP so the
    bridge can attribute the message to the right analyzer. That IP only exists
    once the mock container has been attached to the analyzer's Docker network,
    which POST /analyzers does asynchronously. Until then `bind()` raises
    "Cannot assign requested address". A throwaway bind-test is the cheapest
    readiness probe.
    """
    deadline = time.monotonic() + timeout_s
    last_err: Optional[Exception] = None
    while True:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((source_ip, 0))
            return True
        except OSError as err:
            last_err = err
        finally:
            probe.close()
        if time.monotonic() >= deadline:
            logger.error("[PUSH] source IP %s not bindable after %.1fs: %s",
                         source_ip, timeout_s, last_err)
            return False
        time.sleep(interval_s)


def push_hl7_to_destination(destination: str, hl7_message: str,
                             source_ip: Optional[str] = None):
    """Route HL7 push by destination scheme: mllp:// or http(s)://.

    Returns (delivered, error_reason). source_ip is forwarded to push_hl7_mllp;
    ignored for HTTP destinations.
    """
    if destination.startswith("mllp://"):
        addr = destination[len("mllp://"):]
        if ":" not in addr:
            return False, f"invalid MLLP destination (missing port): {destination}"
        host, port_str = addr.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            return False, f"invalid MLLP port: {port_str}"
        return push_hl7_mllp(host, port, hl7_message, source_ip=source_ip)
    return push_hl7_http(destination, hl7_message), None


def push_astm_to_destination(destination: str, astm_message: str,
                              source_ip: Optional[str] = None):
    """Route ASTM push by destination scheme: tcp:// or http(s)://.

    Returns (delivered, error_reason). source_ip is forwarded to push_astm_tcp;
    ignored for HTTP destinations.
    """
    if destination.startswith("tcp://"):
        addr = destination[len("tcp://"):]
        if ":" not in addr:
            return False, f"invalid TCP destination (missing port): {destination}"
        host, port_str = addr.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            return False, f"invalid TCP port: {port_str}"
        return push_astm_tcp(host, port, astm_message, source_ip=source_ip)
    return push_astm_http(destination, astm_message), None


def push_hl7_mllp(host: str, port: int, hl7_message: str, timeout: int = 30,
                  source_ip: Optional[str] = None):
    """Push an HL7 message over MLLP and require positive application ACK.

    Returns (delivered, error_reason) — error_reason is None on a positive ACK,
    else a string explaining the failure (never a silent False).

    source_ip: optional local IP to bind the outgoing socket to. The mock has one
    Docker network interface per registered analyzer; the bridge identifies the
    source analyzer by the connection's source IP, so the push (a QC message or an
    ORU result) must come from the ordering analyzer's interface. Without binding,
    the kernel picks the route arbitrarily and the message is attributed to the
    wrong analyzer.
    """
    if source_ip and not _wait_source_ip_bindable(source_ip):
        return False, (f"source IP {source_ip} not configured on the mock container "
                       f"after {SOURCE_IP_READY_TIMEOUT_S:.0f}s — analyzer network attach "
                       "incomplete (POST /analyzers connects asynchronously)")
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        if source_ip:
            sock.bind((source_ip, 0))
        sock.connect((host, port))
        payload = VT + hl7_message.encode("utf-8") + FS + CR
        sock.sendall(payload)

        ack = b""
        while not ack.endswith(FS + CR):
            chunk = sock.recv(4096)
            if not chunk:
                logger.error("[PUSH-HL7-MLLP] Peer closed before MLLP terminator from %s:%s", host, port)
                return False, f"peer {host}:{port} closed before MLLP terminator"
            ack += chunk
        ack_text = ack.replace(VT, b"").replace(FS + CR, b"").decode("utf-8", errors="replace")
        if "MSA|AA|" in ack_text:
            logger.info("[PUSH-HL7-MLLP] Positive ACK received from %s:%s", host, port)
            return True, None
        logger.error("[PUSH-HL7-MLLP] Negative/invalid ACK from %s:%s: %s", host, port, ack_text[:200])
        return False, f"negative/invalid ACK from {host}:{port}: {ack_text[:120]}"
    except Exception as e:
        logger.error("[PUSH-HL7-MLLP] Push failed: %s", e, exc_info=True)
        return False, f"MLLP push to {host}:{port} failed: {e}"
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


def push_astm_tcp(host: str, port: int, astm_message: str, timeout: int = 30,
                   source_ip: Optional[str] = None):
    """Push ASTM message via raw TCP with ENQ/ACK framing.

    Returns (delivered, error_reason) — error_reason is None on success, else a
    string explaining the failure (never a silent False).

    source_ip: optional local IP to bind the outgoing socket to. The mock
    container has multiple Docker network interfaces (one per registered
    analyzer); the bridge identifies analyzers by source IP, so QC pushes
    must come from the right network's address. Without this binding,
    Linux picks the route arbitrarily and QC ends up associated with the
    wrong analyzer.
    """
    if source_ip and not _wait_source_ip_bindable(source_ip):
        return False, (f"source IP {source_ip} not configured on the mock container "
                       f"after {SOURCE_IP_READY_TIMEOUT_S:.0f}s — analyzer network attach "
                       "incomplete (POST /analyzers connects asynchronously)")
    if source_ip:
        logger.info("[PUSH-TCP] Connecting to %s:%s (source=%s) for ASTM TCP push", host, port, source_ip)
    else:
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
            if source_ip:
                sock.bind((source_ip, 0))
            sock.connect((host, port))
            if not send_astm_session(sock, session_records, label):
                return False, f"ASTM session {label or '1/1'} not accepted by {host}:{port} (ENQ/frame NAK or EOT)"
        except socket.timeout:
            logger.error("[PUSH-TCP] Timeout connecting to %s:%s", host, port)
            return False, f"timeout connecting to {host}:{port}"
        except ConnectionRefusedError:
            logger.error("[PUSH-TCP] Connection refused by %s:%s", host, port)
            return False, f"connection refused by {host}:{port}"
        except Exception as e:
            logger.error("[PUSH-TCP] Push failed: %s", e, exc_info=True)
            return False, f"ASTM TCP push to {host}:{port} failed: {e}"
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
        if idx < len(sessions) - 1:
            time.sleep(0.5)

    logger.info("[PUSH-TCP] All %d session(s) sent to %s:%s", len(sessions), host, port)
    return True, None
