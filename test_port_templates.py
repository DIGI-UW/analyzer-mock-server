"""
Unit and integration tests for port-to-template mapping and multi-port mock server.

Tests _load_port_templates resolution and that connections on different ports
receive template-specific responses (e.g. Mindray H-segment on port 9601).
"""

import json
import os
import socket
import threading
import time
import unittest

# Import server module; _load_port_templates is used for port->template resolution
import server as server_module

ENQ = b'\x05'
ACK = b'\x06'
EOT = b'\x04'
STX = b'\x02'
ETX = b'\x03'
CR = b'\x0D'
LF = b'\x0A'


def _send_frame(sock, frame_num: int, data: str) -> bool:
    data_bytes = data.encode('utf-8')
    checksum_data = str(frame_num).encode() + data_bytes + ETX
    checksum = sum(checksum_data) % 256
    frame = STX + str(frame_num).encode() + data_bytes + ETX + f'{checksum:02X}'.encode() + CR + LF
    sock.send(frame)
    return sock.recv(1) == ACK


class TestLoadPortTemplates(unittest.TestCase):
    """Unit tests for _load_port_templates()."""

    def setUp(self):
        self._saved_env = {}
        for key in ('ASTM_PORT_TEMPLATES', 'ASTM_TEMPLATE', 'ASTM_PORT'):
            if key in os.environ:
                self._saved_env[key] = os.environ[key]

    def tearDown(self):
        for key in list(os.environ.keys()):
            if key in self._saved_env:
                os.environ[key] = self._saved_env[key]
            elif key in ('ASTM_PORT_TEMPLATES', 'ASTM_TEMPLATE', 'ASTM_PORT'):
                os.environ.pop(key, None)

    def test_env_override(self):
        os.environ['ASTM_PORT_TEMPLATES'] = '{"9600": "genexpert_astm", "9601": "mindray_ba88a"}'
        result = server_module._load_port_templates(5000)
        self.assertIn(9600, result)
        self.assertIn(9601, result)
        self.assertEqual(result[9600], 'genexpert_astm')
        self.assertEqual(result[9601], 'mindray_ba88a')

    def test_fallback_single_port(self):
        os.environ['ASTM_PORT_TEMPLATES'] = '{}'
        os.environ['ASTM_TEMPLATE'] = 'genexpert_astm'
        result = server_module._load_port_templates(9600)
        self.assertEqual(result, {9600: 'genexpert_astm'})

    def test_config_file(self):
        base = os.path.dirname(os.path.abspath(server_module.__file__))
        config_path = os.path.join(base, 'config', 'port_templates.json')
        if not os.path.exists(config_path):
            self.skipTest('config/port_templates.json not present')
        os.environ.pop('ASTM_PORT_TEMPLATES', None)
        os.environ.pop('ASTM_TEMPLATE', None)
        result = server_module._load_port_templates(5000)
        self.assertGreater(len(result), 0)
        self.assertIn(9600, result)
        self.assertEqual(result[9600], 'genexpert_astm')


class TestMultiPortResponse(unittest.TestCase):
    """Integration test: connect to a port and assert response uses that port's template."""

    def test_port_9601_returns_mindray_h_segment(self):
        port = 19601
        port_to_template = {port: 'mindray_ba88a'}
        srv = server_module.ASTMMockServer(
            port=port,
            response_delay_ms=0,
            port_to_template=port_to_template
        )
        started = threading.Event()
        def run():
            started.set()
            srv.start()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.assertTrue(started.wait(timeout=2), 'Server should start')
        time.sleep(0.3)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(('127.0.0.1', port))
            sock.send(ENQ)
            self.assertEqual(sock.recv(1), ACK)
            _send_frame(sock, 1, 'H|\\^&|||LIS^OE^1|||||||||||')
            _send_frame(sock, 2, 'L|1|N')
            sock.send(EOT)
            first = sock.recv(1)
            self.assertEqual(first, ENQ, 'Server sends ENQ to start response')
            sock.send(ACK)
            data = b''
            sock.settimeout(2)
            while True:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                data += chunk
                i = 0
                while i < len(data):
                    if data[i:i+1] == STX:
                        end = data.find(CR + LF, i)
                        if end != -1:
                            sock.send(ACK)
                            i = end + 2
                            continue
                    if data[i:i+1] == EOT:
                        i += 1
                        break
                    i += 1
                if b'MINDRAY' in data or EOT in data:
                    break
            sock.close()
            text = data.decode('utf-8', errors='replace')
            self.assertIn('MINDRAY', text, 'Response should contain Mindray H-segment')
        finally:
            srv.stop()
            t.join(timeout=1)


if __name__ == '__main__':
    unittest.main()
