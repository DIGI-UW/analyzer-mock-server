#!/usr/bin/env python3
"""
ASTM LIS2-A2 Mock Server for OpenELIS Analyzer Testing

This server simulates an ASTM-compatible laboratory analyzer for testing
the OpenELIS analyzer field mapping feature.

Reference Documents:
- specs/004-astm-analyzer-mapping/research.md Section 1 (ASTM Protocol)
- specs/004-astm-analyzer-mapping/spec.md FR-001, FR-002

ASTM LIS2-A2 Protocol Overview:
1. Client sends ENQ (0x05) to initiate communication
2. Server responds with ACK (0x06) if ready
3. Client sends data frames: <STX><FN><data><ETX/ETB><checksum><CR><LF>
4. Server ACKs each frame
5. Client sends EOT (0x04) to end transmission
6. Roles can reverse for bidirectional communication

Usage:
    python server.py [--port PORT] [--analyzer-type TYPE]

Environment Variables:
    ASTM_PORT: Server port (default: 5000)
    ANALYZER_TYPE: Analyzer type from fields.json (default: HEMATOLOGY)
    RESPONSE_DELAY_MS: Simulated response delay in milliseconds (default: 100)
"""

import socket
import threading
import json
import os
import sys
import time
import logging
import argparse
import urllib.request
import urllib.error
from typing import Optional, Dict, List, Any
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from protocols.astm_handler import generate_astm_message, ASTMHandler
from protocols.hl7_handler import HL7Handler, generate_oru_r01
from protocols.serial_handler import SerialHandler, send_astm_over_serial
from protocols.file_handler import FileHandler

# Optional: template loader for HL7 --hl7 push and /simulate/hl7 API (Abbott, etc.)
try:
    from template_loader import TemplateLoader
    HAS_HL7_SIM = True
except ImportError:
    TemplateLoader = None
    HAS_HL7_SIM = False

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ASTM LIS2-A2 Control Characters
ENQ = b'\x05'  # Enquiry - Start transmission
ACK = b'\x06'  # Acknowledge - Positive response
NAK = b'\x15'  # Negative Acknowledge
EOT = b'\x04'  # End of Transmission
STX = b'\x02'  # Start of Text (frame start)
ETX = b'\x03'  # End of Text (frame end with checksum)
ETB = b'\x17'  # End of Text Block (intermediate frame)
CR = b'\x0D'   # Carriage Return
LF = b'\x0A'   # Line Feed

# Server Configuration
DEFAULT_PORT = 5000
DEFAULT_ANALYZER_TYPE = 'HEMATOLOGY'
DEFAULT_RESPONSE_DELAY_MS = 100

# CLSI LIS1-A Timeout Requirements
ESTABLISHMENT_TIMEOUT = 15  # seconds - ENQ response timeout
FRAME_ACK_TIMEOUT = 15      # seconds - Frame ACK timeout  
RECEIVER_TIMEOUT = 30       # seconds - Receiver waiting for frame/EOT
SOCKET_TIMEOUT = 60         # seconds - Overall socket timeout (keep for safety)
MAX_CONNECTIONS = 10

# Restricted characters per CLSI LIS1-A 8.6
RESTRICTED_CHARS = [
    b'\x01',  # SOH
    b'\x02',  # STX
    b'\x03',  # ETX
    b'\x04',  # EOT
    b'\x05',  # ENQ
    b'\x06',  # ACK
    b'\x10',  # DLE
    b'\x15',  # NAK
    b'\x16',  # SYN
    b'\x17',  # ETB
    b'\x0A',  # LF (only allowed as last char of frame)
    b'\x11',  # DC1
    b'\x12',  # DC2
    b'\x13',  # DC3
    b'\x14',  # DC4
]


class ASTMProtocolHandler:
    """Handles ASTM LIS2-A2 protocol communication for a single client.

    Supports both legacy fields.json and template-driven ASTM generation.
    When astm_template is provided, field query responses and data generation
    use ASTMHandler.generate() for spec-compliant messages (GeneXpert, etc.).
    """

    def __init__(self, conn: socket.socket, addr: tuple, fields_config: Dict,
                 response_delay_ms: int = DEFAULT_RESPONSE_DELAY_MS,
                 astm_template: Optional[Dict] = None):
        self.conn = conn
        self.addr = addr
        self.fields_config = fields_config
        self.response_delay_ms = response_delay_ms
        self.astm_template = astm_template
        self.frame_number = 0
        self.last_accepted_frame = 0  # Track last accepted frame number per CLSI LIS1-A
        self.retransmit_count = 0  # Track retransmissions per CLSI LIS1-A
        self.received_data: List[bytes] = []
        self.running = True
        
    def handle(self):
        """Main handler loop for client connection."""
        logger.info(f"Client connected: {self.addr}")
        self.conn.settimeout(SOCKET_TIMEOUT)
        
        try:
            while self.running:
                data = self._receive_byte()
                if not data:
                    break
                    
                if data == ENQ:
                    self._handle_enq()
                elif data == EOT:
                    self._handle_eot()
                elif data == STX:
                    self._handle_frame()
                else:
                    logger.warning(f"Unexpected byte received: {data.hex()}")
                    
        except socket.timeout:
            logger.info(f"Client timeout: {self.addr}")
        except ConnectionResetError:
            logger.info(f"Client disconnected: {self.addr}")
        except Exception as e:
            logger.error(f"Error handling client {self.addr}: {e}")
        finally:
            self._cleanup()
            
    def _receive_byte(self) -> Optional[bytes]:
        """Receive a single byte from client."""
        try:
            return self.conn.recv(1)
        except socket.timeout:
            return None
            
    def _receive_until(self, terminator: bytes, max_length: int = 4096) -> bytes:
        """Receive data until terminator sequence."""
        data = b''
        while len(data) < max_length:
            byte = self._receive_byte()
            if not byte:
                break
            data += byte
            if data.endswith(terminator):
                break
        return data
        
    def _send(self, data: bytes):
        """Send data to client with optional delay."""
        if self.response_delay_ms > 0:
            time.sleep(self.response_delay_ms / 1000.0)
        self.conn.send(data)
        
    def _handle_enq(self):
        """Handle ENQ (enquiry) - client wants to send data."""
        logger.debug(f"Received ENQ from {self.addr}")
        # Per CLSI LIS1-A: Must respond within establishment timeout
        self._send(ACK)
        logger.debug(f"Sent ACK to {self.addr}")
        
    def _handle_eot(self):
        """Handle EOT (end of transmission)."""
        logger.debug(f"Received EOT from {self.addr}")
        # Process any received data
        if self.received_data:
            self._process_received_data()
        # Connection remains open for potential next transmission
        
    def _handle_frame(self):
        """Handle incoming ASTM data frame."""
        # Read frame content until ETX/ETB + checksum + CR + LF
        frame_data = self._receive_until(CR + LF)
        
        if not frame_data:
            logger.warning("Empty frame received")
            self._send(NAK)
            self.retransmit_count += 1
            if self.retransmit_count >= 6:
                logger.error("Aborting: Frame retransmitted 6 times without success")
                self._send(EOT)
                self.running = False
            return
            
        # Parse frame: <FN><data><ETX/ETB><checksum>
        if len(frame_data) < 4:  # Minimum: FN + ETX + checksum(2) + CRLF
            logger.warning(f"Frame too short: {len(frame_data)}")
            self._send(NAK)
            self.retransmit_count += 1
            if self.retransmit_count >= 6:
                logger.error("Aborting: Frame retransmitted 6 times without success")
                self._send(EOT)
                self.running = False
            return
            
        # Extract frame content (between STX and checksum)
        # Frame number is first byte after STX
        frame_num_bytes = frame_data[0:1]
        
        # Extract frame number (ASCII digit '1'-'7')
        try:
            frame_num_char = frame_num_bytes.decode('ascii', errors='strict')
            if not frame_num_char.isdigit():
                logger.warning(f"Invalid frame number: {frame_num_char}")
                self._send(NAK)
                self.retransmit_count += 1
                if self.retransmit_count >= 6:
                    logger.error("Aborting: Frame retransmitted 6 times without success")
                    self._send(EOT)
                    self.running = False
                return
            
            frame_num = int(frame_num_char)
        except (UnicodeDecodeError, ValueError) as e:
            logger.warning(f"Invalid frame number encoding: {e}")
            self._send(NAK)
            self.retransmit_count += 1
            if self.retransmit_count >= 6:
                logger.error("Aborting: Frame retransmitted 6 times without success")
                self._send(EOT)
                self.running = False
            return
        
        # Validate frame number per CLSI LIS1-A: must be same as last accepted OR one higher (modulo 8)
        if self.last_accepted_frame == 0:
            # First frame - accept any valid frame number (1-7)
            if frame_num < 1 or frame_num > 7:
                logger.warning(f"Invalid frame number range: {frame_num} (must be 1-7)")
                self._send(NAK)
                self.retransmit_count += 1
                if self.retransmit_count >= 6:
                    logger.error("Aborting: Frame retransmitted 6 times without success")
                    self._send(EOT)
                    self.running = False
                return
        else:
            # Subsequent frames: must be same as last accepted OR one higher (modulo 8)
            # Per CLSI LIS1-A: frame number must be same as last accepted OR one higher (modulo 8)
            # Frame numbers are 1-7, so "one higher" means: (last_accepted_frame % 7) + 1
            expected_frame = (self.last_accepted_frame % 7) + 1
            if frame_num != self.last_accepted_frame and frame_num != expected_frame:
                logger.warning(f"Frame number mismatch: expected {expected_frame} or {self.last_accepted_frame}, got {frame_num}")
                self._send(NAK)
                self.retransmit_count += 1
                if self.retransmit_count >= 6:
                    logger.error("Aborting: Frame retransmitted 6 times without success")
                    self._send(EOT)
                    self.running = False
                return
        
        # Find ETX or ETB position
        etx_pos = frame_data.find(ETX)
        etb_pos = frame_data.find(ETB)
        
        if etx_pos == -1 and etb_pos == -1:
            logger.warning("No frame terminator found")
            self._send(NAK)
            self.retransmit_count += 1
            if self.retransmit_count >= 6:
                logger.error("Aborting: Frame retransmitted 6 times without success")
                self._send(EOT)
                self.running = False
            return
            
        term_pos = etx_pos if etx_pos != -1 else etb_pos
        content = frame_data[1:term_pos]
        
        # Validate message characters per CLSI LIS1-A 8.6
        if not self._validate_message_chars(content):
            logger.warning("Restricted characters found in message text")
            self._send(NAK)
            self.retransmit_count += 1
            if self.retransmit_count >= 6:
                logger.error("Aborting: Frame retransmitted 6 times without success")
                self._send(EOT)
                self.running = False
            return
        
        # Extract checksum (2 hex digits after ETX/ETB)
        checksum_start = term_pos + 1
        if len(frame_data) < checksum_start + 2:
            logger.warning("Incomplete checksum in frame")
            self._send(NAK)
            self.retransmit_count += 1
            if self.retransmit_count >= 6:
                logger.error("Aborting: Frame retransmitted 6 times without success")
                self._send(EOT)
                self.running = False
            return
        
        checksum_str = frame_data[checksum_start:checksum_start+2].decode('ascii', errors='ignore')
        
        # Calculate expected checksum: sum of bytes from frame number to ETX, mod 256
        checksum_data = frame_data[0:term_pos+1]  # FN + content + ETX
        calculated_checksum = sum(checksum_data) % 256
        expected_checksum_str = f'{calculated_checksum:02X}'
        
        if checksum_str.upper() != expected_checksum_str:
            logger.warning(f"Checksum mismatch: expected {expected_checksum_str}, got {checksum_str}")
            self._send(NAK)
            self.retransmit_count += 1
            if self.retransmit_count >= 6:
                logger.error("Aborting: Frame retransmitted 6 times without success")
                self._send(EOT)
                self.running = False
            return
        
        # Frame is valid - reset retransmit counter and update last accepted frame
        self.retransmit_count = 0
        self.last_accepted_frame = frame_num
        
        logger.debug(f"Received frame {frame_num}: {content[:50]}...")
        self.received_data.append(content)
        self._send(ACK)
        
    def _process_received_data(self):
        """Process accumulated received data."""
        logger.info(f"[MESSAGE] Processing {len(self.received_data)} frames from {self.addr}")
        
        # Detect if this was a query request BEFORE processing/clearing data
        # Query is detected when we receive a complete message with header but no patient/order records
        is_query = self._is_field_query()
        logger.debug(f"[MESSAGE] Query detection: is_query={is_query}")
        
        for frame_idx, frame in enumerate(self.received_data, 1):
            try:
                decoded = frame.decode('utf-8', errors='replace')
                logger.debug(f"[MESSAGE] Frame {frame_idx}/{len(self.received_data)}: {decoded[:100]}")
                
                # Parse ASTM records
                if decoded.startswith('H|'):
                    self._process_header(decoded)
                elif decoded.startswith('P|'):
                    self._process_patient(decoded)
                elif decoded.startswith('O|'):
                    self._process_order(decoded)
                elif decoded.startswith('R|'):
                    self._process_result(decoded)
                elif decoded.startswith('Q|'):
                    self._process_qc(decoded)
                elif decoded.startswith('L|'):
                    self._process_terminator(decoded)
                    
            except Exception as e:
                logger.error(f"[MESSAGE] Error processing frame {frame_idx}: {e}", exc_info=True)
                
        # Clear received data
        self.received_data = []
        
        # Respond to query if detected
        if is_query:
            logger.info(f"[MESSAGE] Field query detected from {self.addr}, sending field list")
            self.send_field_query_response()
        else:
            logger.debug(f"[MESSAGE] Not a field query, no response needed")
        
    def _process_header(self, record: str):
        """Process H (Header) record."""
        logger.info(f"Header record received: {record[:60]}...")
        
    def _process_patient(self, record: str):
        """Process P (Patient) record."""
        parts = record.split('|')
        patient_id = parts[3] if len(parts) > 3 else 'Unknown'
        logger.info(f"Patient record: ID={patient_id}")
        
    def _process_order(self, record: str):
        """Process O (Order) record."""
        parts = record.split('|')
        sample_id = parts[2] if len(parts) > 2 else 'Unknown'
        logger.info(f"Order record: Sample={sample_id}")
        
    def _process_result(self, record: str):
        """Process R (Result) record."""
        parts = record.split('|')
        if len(parts) >= 4:
            test_code = parts[2] if len(parts) > 2 else 'Unknown'
            value = parts[3] if len(parts) > 3 else ''
            unit = parts[4] if len(parts) > 4 else ''
            logger.info(f"Result record: {test_code} = {value} {unit}")
            
    def _process_qc(self, record: str):
        """Process Q (QC/Quality Control) record."""
        parts = record.split('|')
        logger.info(f"QC record: {record[:60]}...")
        
    def _process_terminator(self, record: str):
        """Process L (Terminator) record."""
        logger.debug("Message terminator received")
    
    def _is_field_query(self) -> bool:
        """Detect if received message is a field query request.
        
        Query is detected when:
        - Header (H) record is present
        - No Patient (P) or Order (O) records follow
        - Only header + terminator received
        """
        if not self.received_data:
            return False
        
        has_header = False
        has_patient_or_order = False
        
        for frame in self.received_data:
            try:
                decoded = frame.decode('utf-8', errors='replace')
                if decoded.startswith('H|'):
                    has_header = True
                elif decoded.startswith('P|') or decoded.startswith('O|'):
                    has_patient_or_order = True
            except:
                pass
        
        return has_header and not has_patient_or_order
    
    def _validate_message_chars(self, content: bytes) -> bool:
        """Validate message text doesn't contain restricted characters per CLSI LIS1-A 8.6."""
        # LF is allowed as last character of frame, so check content without trailing LF
        content_to_check = content.rstrip(LF)
        for restricted in RESTRICTED_CHARS:
            if restricted in content_to_check:
                logger.warning(f"Restricted character found in message: {restricted.hex()}")
                return False
        return True
        
    def send_field_query_response(self):
        """Send available fields or a full template-based ASTM message in response to a query.

        When astm_template is set (via ASTM_TEMPLATE env var), generates a full
        spec-compliant ASTM message using ASTMHandler. This is the pull-based flow:
        bridge connects as client, mock responds with template-generated data.

        When no template is set, falls back to legacy field list response.
        """
        logger.info(f"[FIELD_QUERY] Sending response to {self.addr}")

        # Reset frame numbering for this new ASTM transmission.
        # CLSI LIS1-A expects frame numbers to restart for each ENQ→EOT sequence.
        self.frame_number = 0

        # Initiate transmission: send ENQ and wait for ACK
        logger.debug(f"[FIELD_QUERY] Sending ENQ to initiate response")
        self._send(ENQ)

        # _receive_byte() catches socket.timeout and returns None
        response = self._receive_byte()
        if response is None:
            logger.warning("[FIELD_QUERY] Timeout waiting for ACK")
            return
        if response != ACK:
            logger.warning(f"[FIELD_QUERY] Did not receive ACK, got: {response.hex()}")
            return
        logger.debug(f"[FIELD_QUERY] Received ACK, proceeding")

        if self.astm_template:
            # Template mode: generate full ASTM message and send as framed records
            logger.info(f"[FIELD_QUERY] Using template: {self.astm_template.get('analyzer', {}).get('name', 'unknown')}")
            try:
                message = ASTMHandler().generate(self.astm_template, use_seed=True)
                records = [r for r in message.strip().split('\n') if r.strip()]
                for i, record in enumerate(records):
                    if not self._send_frame(record.strip()):
                        logger.warning(f"[FIELD_QUERY] Send failed at record {i+1}/{len(records)}")
                        break
                logger.info(f"[FIELD_QUERY] Sent {len(records)} template records to {self.addr}")
            except Exception as e:
                logger.error(f"[FIELD_QUERY] Template generation failed: {e}", exc_info=True)
                # Fall through to EOT
        else:
            # Legacy mode: send field list from fields.json
            header_record = f"H|\\^&|||MockAnalyzer^ASTM-Mock^1.0|||||||LIS2-A2"
            self._send_frame(header_record)

            frame_seq = 1
            analyzer_type = os.getenv('ANALYZER_TYPE', DEFAULT_ANALYZER_TYPE)
            fields = self.fields_config.get(analyzer_type, [])

            if not fields and self.fields_config:
                analyzer_type = list(self.fields_config.keys())[0]
                fields = self.fields_config[analyzer_type]

            logger.info(f"[FIELD_QUERY] Sending {len(fields)} fields for {analyzer_type}")

            for field in fields:
                field_name = field.get('name', 'Unknown')
                display_name = field.get('displayName', field_name)
                field_type = field.get('type', 'NUMERIC')
                unit = field.get('unit', '')
                astm_ref = field.get('astmRef', f'^^^{field_name}')

                if display_name != field_name and '^' not in astm_ref:
                    test_id = f"{astm_ref}^{display_name}"
                else:
                    test_id = astm_ref

                record = f"R|{frame_seq}|{test_id}||{unit}|||{field_type}"
                if not self._send_frame(record):
                    logger.warning(f"[FIELD_QUERY] Send failed at frame {frame_seq}")
                    break
                frame_seq += 1

            if not self._send_frame("L|1|N"):
                logger.warning("[FIELD_QUERY] Terminator frame send failed")

        # End transmission
        self._send(EOT)
        logger.info(f"[FIELD_QUERY] Response complete for {self.addr}")
        
    def _send_frame(self, content: str):
        """Send an ASTM frame with proper framing."""
        # Per CLSI LIS1-A: Frame numbers are 1-7, then wrap to 1.
        # Since frame_number starts at 0, we use (frame_number % 7) + 1 to get 1-7
        self.frame_number = (self.frame_number % 7) + 1
        
        # Build frame: <STX><FN><content><ETX><checksum><CR><LF>
        frame_num = str(self.frame_number).encode()
        content_bytes = content.encode('utf-8')
        
        # Calculate checksum (sum of bytes from frame num to ETX, mod 256)
        checksum_data = frame_num + content_bytes + ETX
        checksum = sum(checksum_data) % 256
        checksum_str = f'{checksum:02X}'.encode()
        
        frame = STX + frame_num + content_bytes + ETX + checksum_str + CR + LF
        self.conn.send(frame)
        
        # Wait for ACK with proper timeout per CLSI LIS1-A
        self.conn.settimeout(FRAME_ACK_TIMEOUT)
        try:
            response = self.conn.recv(1)
            if response == EOT:
                # Receiver interrupt request per CLSI LIS1-A 8.3.5
                logger.info("Receiver interrupt requested (EOT received)")
                return False  # Signal to stop sending
            elif response != ACK:
                logger.warning(f"Frame not ACKed: {response.hex() if response else 'none'}")
                # Per CLSI LIS1-A: Should retransmit on NAK, abort after 6 failures
                return False
        except socket.timeout:
            logger.warning("Timeout waiting for frame ACK (15s limit per CLSI LIS1-A)")
            return False
        
        return True
            
    def _cleanup(self):
        """Clean up connection resources."""
        try:
            self.conn.close()
        except:
            pass
        logger.info(f"Client disconnected: {self.addr}")


class ASTMMockServer:
    """ASTM LIS2-A2 Mock Server for analyzer testing.

    Supports template-driven mode via ASTM_TEMPLATE env var. When set, the
    server responds to client connections with spec-compliant ASTM messages
    generated from the specified template (e.g., genexpert_astm).
    """

    def __init__(self, port: int = DEFAULT_PORT,
                 analyzer_type: str = DEFAULT_ANALYZER_TYPE,
                 response_delay_ms: int = DEFAULT_RESPONSE_DELAY_MS):
        self.port = port
        self.analyzer_type = analyzer_type
        self.response_delay_ms = response_delay_ms
        self.fields_config = self._load_fields_config()
        self.astm_template = self._load_astm_template()
        self.running = False
        self.server_socket: Optional[socket.socket] = None
        self.client_threads: List[threading.Thread] = []

    def _load_astm_template(self) -> Optional[Dict]:
        """Load ASTM template from ASTM_TEMPLATE env var if set."""
        template_name = os.environ.get('ASTM_TEMPLATE')
        if not template_name:
            return None
        template = _load_template(template_name)
        if template:
            proto = template.get('protocol', {}).get('type', '')
            name = template.get('analyzer', {}).get('name', template_name)
            if proto != 'ASTM':
                logger.warning(f"ASTM_TEMPLATE={template_name} is not ASTM protocol ({proto}), ignoring")
                return None
            logger.info(f"Loaded ASTM template: {name} ({template_name})")
        else:
            logger.warning(f"ASTM_TEMPLATE={template_name} not found in templates/")
        return template
        
    def _load_fields_config(self) -> Dict:
        """Load analyzer field configuration from JSON file."""
        config_path = os.path.join(os.path.dirname(__file__), 'fields.json')
        
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading fields.json: {e}")
                
        # Return default configuration if file not found
        return self._get_default_fields()
        
    def _get_default_fields(self) -> Dict:
        """Return default field configuration."""
        return {
            "HEMATOLOGY": [
                {"name": "WBC", "astmRef": "R|1|^^^WBC", "type": "NUMERIC", "unit": "10^3/μL"},
                {"name": "RBC", "astmRef": "R|1|^^^RBC", "type": "NUMERIC", "unit": "10^6/μL"},
                {"name": "HGB", "astmRef": "R|1|^^^HGB", "type": "NUMERIC", "unit": "g/dL"},
                {"name": "HCT", "astmRef": "R|1|^^^HCT", "type": "NUMERIC", "unit": "%"},
                {"name": "PLT", "astmRef": "R|1|^^^PLT", "type": "NUMERIC", "unit": "10^3/μL"}
            ],
            "CHEMISTRY": [
                {"name": "Glucose", "astmRef": "R|1|^^^GLUCOSE", "type": "NUMERIC", "unit": "mg/dL"},
                {"name": "Creatinine", "astmRef": "R|1|^^^CREATININE", "type": "NUMERIC", "unit": "mg/dL"}
            ]
        }
        
    def start(self):
        """Start the mock server."""
        self.running = True
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        try:
            self.server_socket.bind(('0.0.0.0', self.port))
            self.server_socket.listen(MAX_CONNECTIONS)
            logger.info(f"ASTM Mock Server started on port {self.port}")
            logger.info(f"Analyzer type: {self.analyzer_type}")
            logger.info(f"Response delay: {self.response_delay_ms}ms")
            
            while self.running:
                try:
                    self.server_socket.settimeout(1.0)
                    conn, addr = self.server_socket.accept()
                    
                    # Handle client in separate thread
                    handler = ASTMProtocolHandler(
                        conn, addr, self.fields_config, self.response_delay_ms,
                        astm_template=self.astm_template
                    )
                    thread = threading.Thread(target=handler.handle, daemon=True)
                    thread.start()
                    self.client_threads.append(thread)
                    
                except socket.timeout:
                    # Check if we should stop
                    continue
                    
        except KeyboardInterrupt:
            logger.info("Server shutdown requested")
        except Exception as e:
            logger.error(f"Server error: {e}")
        finally:
            self.stop()
            
    def stop(self):
        """Stop the mock server."""
        self.running = False
        if self.server_socket:
            try:
                self.server_socket.close()
            except:
                pass
        logger.info("ASTM Mock Server stopped")


class PushAPIHandler(BaseHTTPRequestHandler):
    """HTTP API handler for triggering pushes."""

    fields_config = {}
    openelis_url = None
    template_loader = None

    def do_POST(self):
        """Handle POST requests to trigger pushes."""
        if self.path == '/push' or self.path.startswith('/push?'):
            self.handle_push_request()
        elif HAS_HL7_SIM and self.path.startswith('/simulate/hl7/'):
            self.handle_simulate_hl7_post()
        elif self.path.startswith('/simulate/astm/'):
            self.handle_simulate_astm_post()
        else:
            self.send_error(404, "Not Found")

    def do_GET(self):
        """Handle GET requests for health check and info."""
        if self.path == '/health' or self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            endpoints = {
                "POST /push": "Trigger a push to OpenELIS (query: template, count)",
                "GET /health": "Health check",
                "GET /simulate/astm/<template>": "Generate ASTM message from template",
                "POST /simulate/astm/<template>": "Generate + push ASTM (body: destination, count)",
            }
            if HAS_HL7_SIM:
                endpoints["GET /simulate/hl7/<template>"] = "Generate HL7 ORU^R01"
                endpoints["POST /simulate/hl7/<template>"] = "Generate + push HL7 (body: count, destination)"
            response = {
                "status": "ok",
                "service": "ASTM Mock Server Push API",
                "endpoints": endpoints
            }
            self.wfile.write(json.dumps(response, indent=2).encode('utf-8'))
        elif HAS_HL7_SIM and self.path.startswith('/simulate/hl7/'):
            self.handle_simulate_hl7_get()
        elif self.path.startswith('/simulate/astm/'):
            self.handle_simulate_astm_get()
        else:
            self.send_error(404, "Not Found")
    
    def handle_push_request(self):
        """Handle push trigger request.

        Supports both legacy (analyzer_type from fields.json) and template-driven
        modes. When a `template` parameter is provided, uses ASTMHandler.generate()
        for spec-compliant ASTM message generation.

        Query/body params:
            template: ASTM template name (e.g., genexpert_astm) — uses template mode
            analyzer_type: Legacy analyzer type from fields.json (default: HEMATOLOGY)
            count: Number of messages to push (default: 1)
            destination: Override push URL (supports tcp:// for bridge routing)
        """
        client_addr = self.address_string()
        try:
            logger.info(f"[API] Push request received from {client_addr}: {self.path}")

            # Parse query parameters
            parsed_path = urlparse(self.path)
            query_params = parse_qs(parsed_path.query)

            # Get parameters from query string
            template_name = query_params.get('template', [None])[0]
            analyzer_type = query_params.get('analyzer_type', ['HEMATOLOGY'])[0].upper()
            try:
                count = int(query_params.get('count', ['1'])[0])
            except (TypeError, ValueError):
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": "Parameter 'count' must be an integer"}).encode('utf-8'))
                return
            destination = query_params.get('destination', [None])[0]

            # Read request body if present (JSON) — overrides query params
            content_length = int(self.headers.get('Content-Length', 0))
            request_body = {}
            if content_length > 0:
                body_data = self.rfile.read(content_length)
                try:
                    request_body = json.loads(body_data.decode('utf-8'))
                    logger.debug(f"[API] Request body: {json.dumps(request_body)}")
                    template_name = request_body.get('template', template_name)
                    analyzer_type = request_body.get('analyzer_type', analyzer_type).upper()
                    try:
                        count = int(request_body.get('count', count))
                    except (TypeError, ValueError):
                        self.send_response(400)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps({"status": "error", "message": "Parameter 'count' must be an integer"}).encode('utf-8'))
                        return
                    destination = request_body.get('destination', destination)
                except json.JSONDecodeError as e:
                    logger.warning(f"[API] Failed to parse JSON body: {e}")

            push_target = destination or self.openelis_url

            # Generate and push messages
            results = []
            success_count = 0

            if template_name:
                # Template-driven mode — uses ASTMHandler for spec-compliant messages
                logger.info(f"[API] Template mode: {template_name}, count={count}")
                if not self.template_loader:
                    self.send_response(500)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "error", "message": "Template loader not available"}).encode('utf-8'))
                    return
                try:
                    template = self.template_loader.load_template(template_name)
                except FileNotFoundError:
                    self.send_response(404)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "error", "message": f"Template not found: {template_name}"}).encode('utf-8'))
                    return
                if template.get('protocol', {}).get('type') != 'ASTM':
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "error", "message": f"Template '{template_name}' is not ASTM protocol"}).encode('utf-8'))
                    return
                handler = ASTMHandler()
                for i in range(count):
                    logger.debug(f"[API] Generating template message {i+1}/{count}")
                    message = handler.generate(template, use_seed=True)
                    if message:
                        logger.debug(f"[API] Pushing message {i+1}/{count} to {push_target}")
                        success = _push_astm_to_destination(push_target, message)
                        results.append({
                            "message_number": i + 1,
                            "success": success,
                            "template": template_name
                        })
                        if success:
                            success_count += 1
                    else:
                        results.append({"message_number": i + 1, "success": False, "error": "Generation failed"})
            else:
                # Legacy mode — uses fields.json
                logger.info(f"[API] Legacy mode: analyzer_type={analyzer_type}, count={count}")
                for i in range(count):
                    logger.debug(f"[API] Generating message {i+1}/{count}")
                    message = generate_astm_message(
                        analyzer_type=analyzer_type,
                        fields_config=self.fields_config
                    )

                    if message:
                        logger.debug(f"[API] Pushing message {i+1}/{count} to {push_target}")
                        success = _push_astm_to_destination(push_target, message)
                        results.append({
                            "message_number": i + 1,
                            "success": success,
                            "analyzer_type": analyzer_type
                        })
                        if success:
                            success_count += 1
                    else:
                        logger.error(f"[API] Failed to generate message {i+1}/{count}")
                        results.append({
                            "message_number": i + 1,
                            "success": False,
                            "error": "Failed to generate message"
                        })
            
            # Send response
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            
            response = {
                "status": "completed",
                "total": count,
                "successful": success_count,
                "failed": count - success_count,
                "results": results
            }
            
            logger.info(f"[API] Push request completed: {success_count}/{count} successful")
            self.wfile.write(json.dumps(response, indent=2).encode('utf-8'))
            
        except Exception as e:
            logger.error(f"[API] Error handling push request from {client_addr}: {e}", exc_info=True)
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            error_response = {
                "status": "error",
                "message": str(e)
            }
            self.wfile.write(json.dumps(error_response, indent=2).encode('utf-8'))
    
    def _get_hl7_analyzer_from_path(self) -> Optional[str]:
        """Extract analyzer template name from path /simulate/hl7/<analyzer>."""
        prefix = '/simulate/hl7/'
        if not self.path.startswith(prefix):
            return None
        name = self.path[len(prefix):].split('?')[0].strip('/')
        return name if name else None

    def handle_simulate_hl7_get(self):
        """GET /simulate/hl7/<analyzer>: generate one HL7 message and return as text/plain."""
        analyzer = self._get_hl7_analyzer_from_path()
        if not analyzer or not self.template_loader:
            self.send_error(404, "Not Found")
            return
        try:
            template = self.template_loader.load_template(analyzer)
            if template.get('protocol', {}).get('type') != 'HL7':
                self.send_error(400, "Template is not HL7 protocol")
                return
            message = generate_oru_r01(template, deterministic=True)
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(message.encode('utf-8'))
        except FileNotFoundError:
            self.send_error(404, f"Template not found: {analyzer}")
        except Exception as e:
            logger.exception("HL7 simulate GET failed")
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))

    def handle_simulate_hl7_post(self):
        """POST /simulate/hl7/<analyzer>: generate HL7 message(s), optionally push to destination."""
        analyzer = self._get_hl7_analyzer_from_path()
        if not analyzer or not self.template_loader:
            self.send_error(404, "Not Found")
            return
        try:
            template = self.template_loader.load_template(analyzer)
            if template.get('protocol', {}).get('type') != 'HL7':
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": "Template is not HL7 protocol"}).encode('utf-8'))
                return
            count = 1
            destination = None
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                try:
                    body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                except json.JSONDecodeError:
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "error", "message": "Invalid JSON body"}).encode('utf-8'))
                    return
                raw_count = body.get('count', 1)
                try:
                    count = int(raw_count)
                except (TypeError, ValueError):
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "error", "message": "Parameter 'count' must be an integer"}).encode('utf-8'))
                    return
                if count < 1 or count > 1000:
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "error", "message": "Parameter 'count' must be between 1 and 1000"}).encode('utf-8'))
                    return
                destination = body.get('destination')
            results = []
            success_count = 0
            for i in range(count):
                message = generate_oru_r01(template, deterministic=True)
                pushed = False
                if destination:
                    pushed = push_hl7_to_openelis(destination, message)
                    if pushed:
                        success_count += 1
                results.append({
                    "message_number": i + 1,
                    "pushed": pushed,
                    "preview": message.split('\r')[0][:80] + "..."
                })
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "completed",
                "analyzer": analyzer,
                "count": count,
                "pushed": success_count if destination else None,
                "results": results
            }, indent=2).encode('utf-8'))
        except FileNotFoundError:
            self.send_error(404, f"Template not found: {analyzer}")
        except Exception as e:
            logger.exception("HL7 simulate POST failed")
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))

    def _get_astm_template_from_path(self) -> Optional[str]:
        """Extract ASTM template name from path /simulate/astm/<template>."""
        prefix = '/simulate/astm/'
        if not self.path.startswith(prefix):
            return None
        name = self.path[len(prefix):].split('?')[0].strip('/')
        return name if name else None

    def handle_simulate_astm_get(self):
        """GET /simulate/astm/<template>: generate one ASTM message and return as text/plain."""
        template_name = self._get_astm_template_from_path()
        if not template_name or not self.template_loader:
            self.send_error(404, "Not Found")
            return
        try:
            template = self.template_loader.load_template(template_name)
            if template.get('protocol', {}).get('type') != 'ASTM':
                self.send_error(400, "Template is not ASTM protocol")
                return
            message = ASTMHandler().generate(template, use_seed=True)
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(message.encode('utf-8'))
        except FileNotFoundError:
            self.send_error(404, f"Template not found: {template_name}")
        except Exception as e:
            logger.exception("ASTM simulate GET failed")
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))

    def handle_simulate_astm_post(self):
        """POST /simulate/astm/<template>: generate ASTM message(s), optionally push.

        Body JSON:
            destination: URL (http/https) or TCP address (tcp://host:port)
            count: Number of messages (1-100, default 1)
        """
        template_name = self._get_astm_template_from_path()
        if not template_name or not self.template_loader:
            self.send_error(404, "Not Found")
            return
        try:
            template = self.template_loader.load_template(template_name)
            if template.get('protocol', {}).get('type') != 'ASTM':
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": "Template is not ASTM protocol"}).encode('utf-8'))
                return

            count = 1
            destination = None
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                try:
                    body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                except json.JSONDecodeError:
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "error", "message": "Invalid JSON body"}).encode('utf-8'))
                    return
                count = min(max(int(body.get('count', 1)), 1), 100)
                destination = body.get('destination')

            results = []
            success_count = 0
            handler = ASTMHandler()
            for i in range(count):
                message = handler.generate(template, use_seed=True)
                pushed = False
                if destination:
                    pushed = _push_astm_to_destination(destination, message)
                    if pushed:
                        success_count += 1
                results.append({
                    "message_number": i + 1,
                    "pushed": pushed,
                    "preview": message.split('\n')[0][:80] + "..."
                })

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "completed",
                "template": template_name,
                "count": count,
                "pushed": success_count if destination else None,
                "destination": destination,
                "results": results
            }, indent=2).encode('utf-8'))
        except FileNotFoundError:
            self.send_error(404, f"Template not found: {template_name}")
        except Exception as e:
            logger.exception("ASTM simulate POST failed")
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))

    def log_message(self, format, *args):
        """Override to use our logger instead of default."""
        logger.info(f"{self.address_string()} - {format % args}")


def _load_template(analyzer: str) -> Optional[Dict]:
    """Load analyzer template from templates/<analyzer>.json."""
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "templates", f"{analyzer}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to load template %s: %s", path, e)
        return None


class SimulateAPIHandler(BaseHTTPRequestHandler):
    """HTTP API for /simulate/{protocol}/{template} (M4 CI/CD).

    Supports both HL7 and ASTM template-driven message generation and push.
    """

    def do_GET(self):
        if self.path == "/health" or self.path == "/":
            self._send_json(200, {
                "status": "ok",
                "service": "Multi-Protocol Analyzer Simulator",
                "endpoints": {
                    "GET /health": "Health check",
                    "GET /simulate/hl7/{template}": "Generate HL7 ORU^R01",
                    "POST /simulate/hl7/{template}": "Generate HL7 ORU^R01 (JSON body: patientId, sampleId)",
                    "GET /simulate/astm/{template}": "Generate ASTM message from template",
                    "POST /simulate/astm/{template}": "Generate + push ASTM (body: destination, count)",
                },
            })
            return
        if self.path.startswith("/simulate/hl7/"):
            analyzer = self._extract_name("/simulate/hl7/")
            if not analyzer:
                self._send_json(400, {"status": "error", "message": "Missing analyzer"})
                return
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            params = {
                "patient_id": (qs.get("patientId") or qs.get("patient_id") or [None])[0],
                "sample_id": (qs.get("sampleId") or qs.get("sample_id") or [None])[0],
            }
            self._handle_simulate_hl7(analyzer, params)
            return
        if self.path.startswith("/simulate/astm/"):
            template_name = self._extract_name("/simulate/astm/")
            if not template_name:
                self._send_json(400, {"status": "error", "message": "Missing template name"})
                return
            self._handle_simulate_astm_get(template_name)
            return
        self.send_error(404, "Not Found")

    def do_POST(self):
        if self.path.startswith("/simulate/hl7/"):
            analyzer = self._extract_name("/simulate/hl7/")
            if not analyzer:
                self._send_json(400, {"status": "error", "message": "Missing analyzer"})
                return
            length = int(self.headers.get("Content-Length", 0))
            params = {}
            if length > 0:
                try:
                    params = json.loads(self.rfile.read(length).decode("utf-8"))
                except json.JSONDecodeError:
                    pass
            kwargs = {
                "patient_id": params.get("patientId") or params.get("patient_id"),
                "sample_id": params.get("sampleId") or params.get("sample_id"),
                "tests": params.get("tests"),
            }
            self._handle_simulate_hl7(analyzer, kwargs)
            return
        if self.path.startswith("/simulate/astm/"):
            template_name = self._extract_name("/simulate/astm/")
            if not template_name:
                self._send_json(400, {"status": "error", "message": "Missing template name"})
                return
            self._handle_simulate_astm_post(template_name)
            return
        self.send_error(404, "Not Found")

    def _extract_name(self, prefix: str) -> Optional[str]:
        """Extract template/analyzer name from URL path after prefix."""
        name = self.path.split(prefix)[-1].split("?")[0].strip("/")
        return name if name else None

    def _handle_simulate_hl7(self, analyzer: str, kwargs: Dict):
        template = _load_template(analyzer)
        if not template:
            self._send_json(404, {"status": "error", "message": f"Template not found: {analyzer}"})
            return
        try:
            msg = HL7Handler().generate(template, **{k: v for k, v in kwargs.items() if v is not None})
            msg_id = "MSG-" + msg.split("ORU^R01|")[-1].split("|")[0] if "ORU^R01|" in msg else "MSG-UNK"
            self._send_json(200, {"status": "sent", "messageId": msg_id, "message": msg})
        except Exception as e:
            logger.exception("HL7 simulate failed for %s", analyzer)
            self._send_json(500, {"status": "error", "message": str(e)})

    def _handle_simulate_astm_get(self, template_name: str):
        """GET /simulate/astm/{template}: generate one ASTM message."""
        template = _load_template(template_name)
        if not template:
            self._send_json(404, {"status": "error", "message": f"Template not found: {template_name}"})
            return
        if template.get('protocol', {}).get('type') != 'ASTM':
            self._send_json(400, {"status": "error", "message": "Template is not ASTM protocol"})
            return
        try:
            msg = ASTMHandler().generate(template, use_seed=True)
            self._send_json(200, {"status": "generated", "template": template_name, "message": msg})
        except Exception as e:
            logger.exception("ASTM simulate GET failed for %s", template_name)
            self._send_json(500, {"status": "error", "message": str(e)})

    def _handle_simulate_astm_post(self, template_name: str):
        """POST /simulate/astm/{template}: generate + optionally push ASTM messages.

        Body JSON:
            destination: URL (http/https) or TCP address (tcp://host:port) to push to
            count: Number of messages to generate (1-100, default 1)
        """
        template = _load_template(template_name)
        if not template:
            self._send_json(404, {"status": "error", "message": f"Template not found: {template_name}"})
            return
        if template.get('protocol', {}).get('type') != 'ASTM':
            self._send_json(400, {"status": "error", "message": "Template is not ASTM protocol"})
            return

        # Parse body
        length = int(self.headers.get("Content-Length", 0))
        params = {}
        if length > 0:
            try:
                params = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                self._send_json(400, {"status": "error", "message": "Invalid JSON body"})
                return

        count = min(max(int(params.get("count", 1)), 1), 100)
        destination = params.get("destination")

        results = []
        success_count = 0
        handler = ASTMHandler()
        for i in range(count):
            msg = handler.generate(template, use_seed=True)
            pushed = False
            if destination:
                pushed = _push_astm_to_destination(destination, msg)
                if pushed:
                    success_count += 1
            results.append({
                "message_number": i + 1,
                "pushed": pushed,
                "preview": msg.split('\n')[0][:80] + "..." if msg else ""
            })

        self._send_json(200, {
            "status": "completed",
            "template": template_name,
            "count": count,
            "pushed": success_count if destination else None,
            "destination": destination,
            "results": results
        })

    def _send_json(self, code: int, obj: Dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj, indent=2).encode("utf-8"))

    def log_message(self, format, *args):
        logger.info("%s - %s", self.address_string(), format % args)


def _push_astm_to_destination(destination: str, astm_message: str) -> bool:
    """Route an ASTM push to the correct transport based on destination URL.

    Supports:
        tcp://host:port  — Raw ASTM TCP with ENQ/ACK framing (bridge integration)
        http(s)://...    — HTTP POST to OpenELIS /analyzer/astm endpoint
    """
    if destination.startswith("tcp://"):
        # Parse tcp://host:port
        addr = destination[len("tcp://"):]
        if ":" not in addr:
            logger.error(f"[PUSH] Invalid TCP destination (missing port): {destination}")
            return False
        host, port_str = addr.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            logger.error(f"[PUSH] Invalid TCP port: {port_str}")
            return False
        return push_astm_tcp(host, port, astm_message)
    else:
        # HTTP push — allow both base URLs and full endpoint URLs.
        # push_to_openelis() appends /api/OpenELIS-Global/analyzer/astm, so strip
        # it if the caller already included it (mirrors push_hl7_to_openelis logic).
        astm_suffix = "/api/OpenELIS-Global/analyzer/astm"
        clean_dest = destination.rstrip("/")
        if clean_dest.endswith(astm_suffix):
            clean_dest = clean_dest[:-len(astm_suffix)]
        return push_to_openelis(clean_dest, astm_message)


def start_simulate_api_server(port: int):
    """Start HTTP API for /simulate/{protocol}/{template} (CI/CD)."""
    server = HTTPServer(("0.0.0.0", port), SimulateAPIHandler)
    logger.info("Simulate API server started on port %s", port)
    logger.info("  GET  /simulate/hl7/{template} - Generate HL7 ORU^R01")
    logger.info("  POST /simulate/hl7/{template} - Generate HL7 ORU^R01 (no push)")
    logger.info("  GET  /simulate/astm/{template} - Generate ASTM message")
    logger.info("  POST /simulate/astm/{template} - Generate + push ASTM")
    logger.info("  GET  /health - Health check")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Simulate API stopped")
        server.shutdown()


def start_push_api_server(api_port: int, openelis_url: str, fields_config: Dict):
    """Start HTTP API server for triggering pushes."""
    PushAPIHandler.fields_config = fields_config
    PushAPIHandler.openelis_url = openelis_url
    # Always init template loader — needed for both HL7 and ASTM template simulation
    try:
        PushAPIHandler.template_loader = TemplateLoader()
    except Exception as e:
        logger.warning("Template simulation disabled: could not init TemplateLoader: %s", e)
        PushAPIHandler.template_loader = None

    server = HTTPServer(('0.0.0.0', api_port), PushAPIHandler)
    logger.info(f"Push API server started on port {api_port}")
    logger.info(f"  POST /push - Trigger push (query: template, count, destination)")
    logger.info(f"  GET/POST /simulate/astm/<template> - ASTM template simulation")
    if HAS_HL7_SIM:
        logger.info(f"  GET/POST /simulate/hl7/<template> - HL7 template simulation")
    logger.info(f"  GET /health - Health check")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Push API server stopped")
        server.shutdown()


def push_to_openelis(openelis_url: str, astm_message: str, timeout: int = 30) -> bool:
    """
    Push an ASTM message to OpenELIS via HTTP POST.
    
    Args:
        openelis_url: Base URL for OpenELIS (e.g., "https://localhost:8443")
        astm_message: Complete ASTM message as string
        timeout: Request timeout in seconds
    
    Returns:
        True if push succeeded, False otherwise
    """
    endpoint = f"{openelis_url}/api/OpenELIS-Global/analyzer/astm"
    
    try:
        message_lines = astm_message.strip().split('\n')
        message_type = "unknown"
        if message_lines:
            first_line = message_lines[0]
            if first_line.startswith('H|'):
                # Try to extract analyzer info from header
                parts = first_line.split('|')
                if len(parts) >= 5:
                    analyzer_info = parts[4] if parts[4] else "Unknown"
                    message_type = f"ASTM message from {analyzer_info}"
        
        logger.info(f"[PUSH] Pushing ASTM message to {endpoint}")
        logger.info(f"[PUSH] Message type: {message_type}, size: {len(astm_message)} bytes, lines: {len(message_lines)}")
        logger.debug(f"[PUSH] Message preview (first 300 chars): {astm_message[:300]}...")
        
        # Create HTTP request
        req = urllib.request.Request(
            endpoint,
            data=astm_message.encode('utf-8'),
            headers={
                'Content-Type': 'text/plain; charset=utf-8',
            },
            method='POST'
        )
        
        # Disable SSL verification for development (OpenELIS uses self-signed certs)
        import ssl
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        
        # Send request
        start_time = time.time()
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as response:
            elapsed_time = time.time() - start_time
            status_code = response.getcode()
            response_body = response.read().decode('utf-8')
            
            if status_code == 200:
                logger.info(f"[PUSH] ✓ Push successful (HTTP {status_code}) in {elapsed_time:.2f}s")
                if response_body:
                    logger.debug(f"[PUSH] Response body: {response_body[:200]}")
                return True
            else:
                logger.error(f"[PUSH] ✗ Push failed: HTTP {status_code}")
                if response_body:
                    logger.error(f"[PUSH] Response: {response_body[:500]}")
                return False
                
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else "No error details"
        logger.error(f"[PUSH] ✗ HTTP error {e.code}: {e.reason}")
        logger.error(f"[PUSH] Error response: {error_body[:500]}")
        return False
    except urllib.error.URLError as e:
        logger.error(f"[PUSH] ✗ URL error: {e.reason}")
        return False
    except Exception as e:
        logger.error(f"[PUSH] ✗ Push failed with exception: {e}", exc_info=True)
        return False


def push_hl7_to_openelis(openelis_url: str, hl7_message: str, timeout: int = 30) -> bool:
    """
    Push an HL7 ORU^R01 message to OpenELIS.

    Args:
        openelis_url: Base URL for OpenELIS (e.g. https://localhost:8443) or full HL7 endpoint URL.
        hl7_message: Complete HL7 message string (segment terminator \\r).
        timeout: Request timeout in seconds.

    Returns:
        True if push succeeded, False otherwise.
    """
    if openelis_url.rstrip('/').endswith('/hl7') or '/analyzer/' in openelis_url:
        endpoint = openelis_url if openelis_url.startswith('http') else f"https://{openelis_url}"
    else:
        endpoint = f"{openelis_url.rstrip('/')}/api/OpenELIS-Global/analyzer/hl7"

    try:
        logger.info(f"[PUSH-HL7] Pushing ORU^R01 to {endpoint}")
        req = urllib.request.Request(
            endpoint,
            data=hl7_message.encode('utf-8'),
            headers={'Content-Type': 'text/plain; charset=utf-8'},
            method='POST'
        )
        import ssl
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as response:
            if response.getcode() == 200:
                logger.info("[PUSH-HL7] Push successful")
                return True
            logger.error(f"[PUSH-HL7] Push failed: HTTP {response.getcode()}")
            return False
    except Exception as e:
        logger.error(f"[PUSH-HL7] Push failed: {e}", exc_info=True)
        return False


def push_astm_tcp(host: str, port: int, astm_message: str, timeout: int = 30) -> bool:
    """
    Push an ASTM message via raw ASTM TCP (ENQ/ACK framing) to a bridge or LIS.

    Simulates a real analyzer sending results: connects via TCP, performs
    CLSI LIS1-A framing (ENQ → ACK → STX frames → EOT), then disconnects.

    Args:
        host: Target hostname or IP (e.g., "openelis-analyzer-bridge")
        port: Target ASTM listen port (e.g., 12001)
        astm_message: Complete ASTM message as string (H|...\nP|...\nR|...\nL|...)
        timeout: Socket timeout in seconds

    Returns:
        True if all frames were ACKed, False otherwise.
    """
    logger.info(f"[PUSH-TCP] Connecting to {host}:{port} for ASTM TCP push")
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        logger.info(f"[PUSH-TCP] Connected to {host}:{port}")

        # Step 1: Send ENQ to establish link
        sock.sendall(ENQ)
        response = sock.recv(1)
        if response != ACK:
            logger.error(f"[PUSH-TCP] ENQ not ACKed, got: {response.hex() if response else 'none'}")
            return False
        logger.debug("[PUSH-TCP] ENQ acknowledged, sending frames")

        # Step 2: Split message into records and send as ASTM frames
        records = [r for r in astm_message.strip().split('\n') if r.strip()]
        frame_number = 0
        for i, record in enumerate(records):
            frame_number = (frame_number % 7) + 1
            content_bytes = record.strip().encode('utf-8')
            frame_num_bytes = str(frame_number).encode()

            # Checksum: sum of bytes from frame_num through ETX, mod 256
            checksum_data = frame_num_bytes + content_bytes + ETX
            checksum = sum(checksum_data) % 256
            checksum_str = f'{checksum:02X}'.encode()

            frame = STX + frame_num_bytes + content_bytes + ETX + checksum_str + CR + LF
            sock.sendall(frame)

            # Wait for frame ACK
            ack = sock.recv(1)
            if ack == EOT:
                logger.info("[PUSH-TCP] Receiver sent EOT (interrupt), stopping")
                return False
            if ack != ACK:
                logger.warning(f"[PUSH-TCP] Frame {i+1} not ACKed: {ack.hex() if ack else 'none'}")
                return False
            logger.debug(f"[PUSH-TCP] Frame {i+1}/{len(records)} ACKed")

        # Step 3: Send EOT to end transmission
        sock.sendall(EOT)
        logger.info(f"[PUSH-TCP] Successfully sent {len(records)} frames to {host}:{port}")
        return True

    except socket.timeout:
        logger.error(f"[PUSH-TCP] Timeout connecting to {host}:{port}")
        return False
    except ConnectionRefusedError:
        logger.error(f"[PUSH-TCP] Connection refused by {host}:{port}")
        return False
    except Exception as e:
        logger.error(f"[PUSH-TCP] Push failed: {e}", exc_info=True)
        return False
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='ASTM LIS2-A2 Mock Server for OpenELIS Analyzer Testing'
    )
    parser.add_argument(
        '--port', '-p',
        type=int,
        default=int(os.environ.get('ASTM_PORT', DEFAULT_PORT)),
        help=f'Server port (default: {DEFAULT_PORT})'
    )
    parser.add_argument(
        '--analyzer-type', '-t',
        type=str,
        default=os.environ.get('ANALYZER_TYPE', DEFAULT_ANALYZER_TYPE),
        help=f'Analyzer type (default: {DEFAULT_ANALYZER_TYPE})'
    )
    parser.add_argument(
        '--response-delay', '-d',
        type=int,
        default=int(os.environ.get('RESPONSE_DELAY_MS', DEFAULT_RESPONSE_DELAY_MS)),
        help=f'Response delay in ms (default: {DEFAULT_RESPONSE_DELAY_MS})'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable verbose logging'
    )
    parser.add_argument(
        '--push', '-P',
        type=str,
        metavar='URL',
        help='Push mode: Send ASTM message to OpenELIS at URL (e.g., https://localhost:8443)'
    )
    parser.add_argument(
        '--push-count', '-c',
        type=int,
        default=1,
        help='Number of messages to push (default: 1)'
    )
    parser.add_argument(
        '--push-interval', '-i',
        type=int,
        default=1,
        help='Interval between pushes in seconds (default: 1)'
    )
    parser.add_argument(
        '--push-continuous', '-C',
        action='store_true',
        help='Continuous push mode: Push messages indefinitely at specified interval (simulates real analyzer behavior)'
    )
    parser.add_argument(
        '--api-port', '-a',
        type=int,
        metavar='PORT',
        help='Start HTTP API server on specified port for triggering pushes (e.g., 8080)'
    )
    parser.add_argument(
        '--simulate-api-port',
        type=int,
        metavar='PORT',
        help='M4: Start simulate API (GET/POST /simulate/hl7/{analyzer}) for CI/CD (e.g., 8081)'
    )
    parser.add_argument(
        '--serial-port',
        type=str,
        metavar='PATH',
        help='M4: Serial simulation mode: send ASTM over port (e.g. /dev/pts/X via socat)'
    )
    parser.add_argument(
        '--serial-analyzer',
        type=str,
        default='horiba_pentra60',
        help='M4: Template name for --serial-port (default: horiba_pentra60)'
    )
    parser.add_argument(
        '--generate-files',
        type=str,
        metavar='DIR',
        help='M4: File generation mode: write CSV to DIR (use with --generate-files-analyzer)'
    )
    parser.add_argument(
        '--generate-files-analyzer',
        type=str,
        default='quantstudio7',
        help='M4: Template for --generate-files (default: quantstudio7)'
    )
    parser.add_argument(
        '--hl7',
        action='store_true',
        help='HL7 mode: use HL7 template and push ORU^R01 to destination (use with --push)'
    )
    parser.add_argument(
        '--hl7-template',
        type=str,
        metavar='NAME',
        default=os.environ.get('HL7_TEMPLATE', 'abbott_architect_hl7'),
        help='HL7 template name (default: abbott_architect_hl7 or HL7_TEMPLATE env)'
    )
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # M4: File generation mode
    if getattr(args, 'generate_files', None):
        out_dir = args.generate_files
        analyzer = getattr(args, 'generate_files_analyzer', None) or 'quantstudio7'
        template = _load_template(analyzer)
        if not template:
            logger.error("Template not found: %s", analyzer)
            return 1
        fcfg = template.get("file_config") or {}
        pat = (template.get("identification") or {}).get("file_pattern", "sim_%s.csv")
        import uuid
        fname = (pat % uuid.uuid4().hex[:8]) if "%" in pat else f"sim_{analyzer}_{uuid.uuid4().hex[:8]}.csv"
        path = os.path.join(out_dir, fname)
        ok = FileHandler().write_to_file(template, path, sample_count=1)
        if ok:
            print("Wrote %s" % ok)
            return 0
        return 1

    # M4: Serial simulation mode
    if getattr(args, 'serial_port', None):
        port_path = args.serial_port
        analyzer = getattr(args, 'serial_analyzer', None) or 'horiba_pentra60'
        template = _load_template(analyzer)
        if not template:
            logger.error("Template not found: %s", analyzer)
            return 1
        cfg = template.get("serial_config") or {}
        baud = int(cfg.get("baud_rate", 9600))
        msg = SerialHandler().generate(template)
        print("=" * 60)
        print("  Multi-Protocol Simulator - Serial Mode")
        print("=" * 60)
        print("  Port: %s  Analyzer: %s  Baud: %s" % (port_path, analyzer, baud))
        print("=" * 60)
        ok = send_astm_over_serial(port_path, msg, baud=baud)
        return 0 if ok else 1

    # M4: Simulate API (HL7 /simulate/hl7/{analyzer}) for CI/CD
    if getattr(args, 'simulate_api_port', None):
        port = args.simulate_api_port
        print("=" * 60)
        print("  Multi-Protocol Simulator - HL7 Simulate API")
        print("=" * 60)
        print(f"  Port: {port}")
        print("  GET  /simulate/hl7/{{analyzer}}  POST /simulate/hl7/{{analyzer}}")
        print("  GET  /health")
        print("=" * 60)
        print()
        start_simulate_api_server(port)
        return 0

    # HL7 push mode: Send ORU^R01 to OpenELIS (template-driven, e.g. Abbott)
    if args.hl7 and args.push:
        if not HAS_HL7_SIM:
            logger.error("HL7 simulation not available (template_loader or hl7_handler missing)")
            return 1
        try:
            loader = TemplateLoader()
            template = loader.load_template(args.hl7_template)
        except FileNotFoundError as e:
            logger.error("HL7 template not found: %s", e)
            return 1
        except Exception as e:
            logger.error("Failed to load HL7 template: %s", e)
            return 1
        if template.get('protocol', {}).get('type') != 'HL7':
            logger.error("Template %s is not an HL7 template", args.hl7_template)
            return 1
        print("=" * 60)
        print("  ASTM Mock Server - HL7 Push Mode")
        print("=" * 60)
        print(f"  OpenELIS URL: {args.push}")
        print(f"  HL7 Template: {args.hl7_template}")
        print(f"  Message Count: {args.push_count}")
        print(f"  Interval: {args.push_interval}s")
        print("=" * 60)
        print()
        success_count = 0
        total_sent = 0
        try:
            if args.push_continuous:
                while True:
                    total_sent += 1
                    msg = generate_oru_r01(template, deterministic=True)
                    if push_hl7_to_openelis(args.push, msg):
                        success_count += 1
                    time.sleep(args.push_interval)
            else:
                for i in range(args.push_count):
                    msg = generate_oru_r01(template, deterministic=True)
                    if push_hl7_to_openelis(args.push, msg):
                        success_count += 1
                    total_sent += 1
                    if i < args.push_count - 1:
                        time.sleep(args.push_interval)
        except KeyboardInterrupt:
            # Allow user-initiated interruption and proceed to summary output
            pass
        print()
        print("=" * 60)
        print(f"  HL7 Push Complete: {success_count}/{total_sent} successful")
        print("=" * 60)
        return 0 if (not args.push_continuous and success_count == args.push_count) or (args.push_continuous and total_sent > 0) else 1

    # Push mode: Send ASTM messages to OpenELIS
    if args.push:
        print("=" * 60)
        print("  ASTM Mock Server - Push Mode")
        print("=" * 60)
        print(f"  OpenELIS URL: {args.push}")
        print(f"  Analyzer Type: {args.analyzer_type}")
        print(f"  Message Count: {args.push_count}")
        print(f"  Interval: {args.push_interval}s")
        print("=" * 60)
        print()
        
        # Load fields configuration
        fields_config = {}
        fields_file = os.path.join(os.path.dirname(__file__), 'fields.json')
        if os.path.exists(fields_file):
            try:
                with open(fields_file, 'r') as f:
                    fields_config = json.load(f)
            except Exception as e:
                logger.error(f"Error loading fields.json: {e}")
                return 1
        
        if not fields_config:
            logger.error("No fields configuration available")
            return 1
        
        # Push messages
        success_count = 0
        total_pushed = 0
        
        if args.push_continuous:
            # Continuous mode: Push indefinitely (simulates real analyzer behavior)
            logger.info("Starting continuous push mode (press Ctrl+C to stop)")
            try:
                while True:
                    total_pushed += 1
                    logger.info(f"Generating and pushing message #{total_pushed}")
                    
                    # Generate ASTM message
                    message = generate_astm_message(
                        analyzer_type=args.analyzer_type,
                        fields_config=fields_config
                    )
                    
                    if not message:
                        logger.error("Failed to generate message")
                        time.sleep(args.push_interval)
                        continue
                    
                    # Push to OpenELIS
                    if push_to_openelis(args.push, message):
                        success_count += 1
                    else:
                        logger.warning(f"Push #{total_pushed} failed")
                    
                    # Wait before next push
                    time.sleep(args.push_interval)
            except KeyboardInterrupt:
                logger.info("Continuous push mode stopped by user")
        else:
            # Batch mode: Push specified number of messages
            for i in range(args.push_count):
                total_pushed += 1
                logger.info(f"Generating and pushing message {i+1}/{args.push_count}")
                
                # Generate ASTM message
                message = generate_astm_message(
                    analyzer_type=args.analyzer_type,
                    fields_config=fields_config
                )
                
                if not message:
                    logger.error("Failed to generate message")
                    continue
                
                # Push to OpenELIS
                if push_to_openelis(args.push, message):
                    success_count += 1
                else:
                    logger.warning(f"Push {i+1} failed")
                
                # Wait before next push (except for last one)
                if i < args.push_count - 1:
                    time.sleep(args.push_interval)
        
        print()
        print("=" * 60)
        if args.push_continuous:
            print(f"  Push Complete: {success_count}/{total_pushed} successful")
        else:
            print(f"  Push Complete: {success_count}/{args.push_count} successful")
        print("=" * 60)
        return 0 if success_count == total_pushed else 1
    
    # API server mode: Start HTTP API for triggering pushes
    if args.api_port:
        if not args.push:
            logger.error("--api-port requires --push URL to be specified")
            return 1
        
        # Load fields configuration
        fields_config = {}
        fields_file = os.path.join(os.path.dirname(__file__), 'fields.json')
        if os.path.exists(fields_file):
            try:
                with open(fields_file, 'r') as f:
                    fields_config = json.load(f)
            except Exception as e:
                logger.error(f"Error loading fields.json: {e}")
                return 1
        
        if not fields_config:
            logger.error("No fields configuration available")
            return 1
        
        print("=" * 60)
        print("  ASTM Mock Server - API Mode")
        print("=" * 60)
        print(f"  API Port: {args.api_port}")
        print(f"  OpenELIS URL: {args.push}")
        print(f"  Default Analyzer Type: {args.analyzer_type}")
        print("=" * 60)
        print("  Endpoints:")
        print(f"    POST http://localhost:{args.api_port}/push")
        print(f"    GET  http://localhost:{args.api_port}/health")
        print("=" * 60)
        print("  Press Ctrl+C to stop")
        print("=" * 60)
        print()
        
        start_push_api_server(args.api_port, args.push, fields_config)
        return 0
    
    # Server mode: Listen for connections
    print("=" * 60)
    print("  ASTM LIS2-A2 Mock Server for OpenELIS")
    print("=" * 60)
    print(f"  Port: {args.port}")
    print(f"  Analyzer Type: {args.analyzer_type}")
    print(f"  Response Delay: {args.response_delay}ms")
    print("=" * 60)
    print("  Press Ctrl+C to stop")
    print("=" * 60)
    print()
    
    server = ASTMMockServer(
        port=args.port,
        analyzer_type=args.analyzer_type,
        response_delay_ms=args.response_delay
    )
    server.start()


if __name__ == '__main__':
    main()


