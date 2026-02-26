"""
Unit tests for protocol handlers (M4).

Reference: specs/011-madagascar-analyzer-integration, tasks T091–T095.
"""

import json
import os
import unittest

from protocols.astm_handler import ASTMHandler
from protocols.hl7_handler import HL7Handler
from protocols.serial_handler import SerialHandler
from protocols.file_handler import FileHandler


def _load_template(name: str):
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "templates", f"{name}.json")
    with open(path, "r") as f:
        return json.load(f)


class TestASTMHandler(unittest.TestCase):
    def test_generate_mindray_bc5380(self):
        t = _load_template("mindray_bc5380")
        msg = ASTMHandler().generate(t, patient_id="P001", sample_id="S001")
        self.assertIn("H|", msg)
        self.assertIn("P|", msg)
        self.assertIn("O|", msg)
        self.assertIn("R|", msg)
        self.assertIn("L|", msg)
        self.assertIn("P001", msg)
        self.assertIn("S001", msg)

    def test_generate_horiba_pentra60(self):
        t = _load_template("horiba_pentra60")
        msg = ASTMHandler().generate(t)
        self.assertIn("H|", msg)
        self.assertIn("PENTRA", msg.upper())


class TestASTMGeneXpert(unittest.TestCase):
    """Tests for GeneXpert ASTM template features (PR #13 review comments)."""

    @classmethod
    def setUpClass(cls):
        cls.template = _load_template("genexpert_astm")
        cls.handler = ASTMHandler()

    def _generate(self, **kwargs):
        return self.handler.generate(self.template, use_seed=True, **kwargs)

    def _segments(self, msg, prefix):
        return [l for l in msg.strip().split("\n") if l.startswith(prefix)]

    # --- H-record ---

    def test_h_record_has_message_id(self):
        msg = self._generate()
        h = self._segments(msg, "H")[0]
        fields = h.split("|")
        # H.3 should be a non-empty message ID (MSG-...)
        self.assertTrue(fields[2].startswith("MSG-"), f"H.3 should be MSG-..., got: {fields[2]}")

    def test_h_record_receiver_id(self):
        msg = self._generate()
        h = self._segments(msg, "H")[0]
        fields = h.split("|")
        # H.10 = Receiver ID = "LIS"
        self.assertEqual(fields[9], "LIS")

    def test_h_record_processing_id(self):
        msg = self._generate()
        h = self._segments(msg, "H")[0]
        fields = h.split("|")
        # H.12 = Processing ID = "P"
        self.assertEqual(fields[11], "P")

    def test_h_record_version_number(self):
        msg = self._generate()
        h = self._segments(msg, "H")[0]
        fields = h.split("|")
        # H.13 = Version = "1394-97" (not "LIS2-A2")
        self.assertEqual(fields[12], "1394-97")

    def test_h_record_analyzer_name(self):
        msg = self._generate()
        h = self._segments(msg, "H")[0]
        self.assertIn("GENEXPERT^GeneXpert^4.6.0", h)

    # --- O-record ---

    def test_o_record_action_code_empty_for_patient(self):
        msg = self._generate()
        o = self._segments(msg, "O")[0]
        fields = o.split("|")
        # O.12 (index 11) should be empty for patient samples
        self.assertEqual(fields[11], "")

    def test_o_record_specimen_descriptor(self):
        msg = self._generate()
        o = self._segments(msg, "O")[0]
        fields = o.split("|")
        # O.16 (index 15) = "ORH"
        self.assertEqual(fields[15], "ORH")

    def test_o_record_report_type(self):
        msg = self._generate()
        o = self._segments(msg, "O")[0]
        fields = o.split("|")
        # O.26 (index 25) = "F"
        self.assertEqual(fields[25], "F")

    def test_o_record_universal_test_id(self):
        msg = self._generate()
        o = self._segments(msg, "O")[0]
        fields = o.split("|")
        # O.5 (index 4) = ^^^MTB-RIF
        self.assertEqual(fields[4], "^^^MTB-RIF")

    def test_o_record_has_26_fields(self):
        msg = self._generate()
        o = self._segments(msg, "O")[0]
        fields = o.split("|")
        self.assertEqual(len(fields), 26)

    # --- R-record: 7-component test ID ---

    def test_r_record_8_component_test_id(self):
        msg = self._generate()
        r_lines = self._segments(msg, "R")
        # First R-record should have MTB-RIF with 8 components
        r_fields = r_lines[0].split("|")
        test_id = r_fields[2]
        components = test_id.split("^")
        # ^^^MTB-RIF^Xpert MTB/RIF^2.1^^ = 8 components
        # (3 empty + code + name + version + analyte(empty) + complementary(empty))
        self.assertEqual(len(components), 8, f"Expected 8 components, got {len(components)}: {components}")
        self.assertEqual(components[3], "MTB-RIF")
        self.assertEqual(components[4], "Xpert MTB/RIF")
        self.assertEqual(components[5], "2.1")
        self.assertEqual(components[6], "")  # analyte name (empty for main result)
        self.assertEqual(components[7], "")  # complementary name (empty for main result)

    # --- R-record: complementary results (8 components) ---

    def test_complementary_result_8_component_test_id(self):
        msg = self._generate()
        r_lines = self._segments(msg, "R")
        # Find the Conc/LOG complementary result (HIV-VL has one)
        comp_lines = [r for r in r_lines if "Conc/LOG" in r]
        self.assertTrue(len(comp_lines) > 0, "Should have a Conc/LOG complementary result")
        r_fields = comp_lines[0].split("|")
        test_id = r_fields[2]
        components = test_id.split("^")
        # ^^^HIV-VL^...^...^^Conc/LOG = 8 components
        self.assertEqual(len(components), 8, f"Expected 8 components, got {len(components)}: {components}")
        self.assertEqual(components[7], "Conc/LOG")

    def test_complementary_result_seed_value(self):
        msg = self._generate()
        r_lines = self._segments(msg, "R")
        comp_lines = [r for r in r_lines if "Conc/LOG" in r]
        self.assertTrue(len(comp_lines) > 0)
        r_fields = comp_lines[0].split("|")
        # Value field (R.4) should contain ^3.1 (seed value)
        self.assertIn("3.1", r_fields[3])

    # --- R-record: qualitative values ---

    def test_qualitative_seed_value(self):
        msg = self._generate()
        r_lines = self._segments(msg, "R")
        # First R-record (MTB-RIF) should have NEGATIVE seed
        r_fields = r_lines[0].split("|")
        self.assertIn("NEGATIVE", r_fields[3])

    # --- QC message generation ---

    def test_qc_message_generated(self):
        msg = self._generate()
        h_records = self._segments(msg, "H")
        # Should have 2 H-records: one for patient, one for QC
        self.assertEqual(len(h_records), 2, "Should generate patient + QC messages")

    def test_qc_action_code(self):
        msg = self._generate()
        o_records = self._segments(msg, "O")
        # Second O-record (QC) should have Action Code "Q" at O.12
        self.assertTrue(len(o_records) >= 2, "Should have at least 2 O-records")
        qc_o_fields = o_records[1].split("|")
        self.assertEqual(qc_o_fields[11], "Q")

    def test_qc_specimen_id(self):
        msg = self._generate()
        o_records = self._segments(msg, "O")
        qc_o_fields = o_records[1].split("|")
        # O.3 should contain QC specimen ID
        self.assertIn("QC-MTB-CTRL-001", qc_o_fields[2])

    def test_qc_includes_all_template_fields(self):
        msg = self._generate()
        # Split into patient msg and QC msg (second H starts QC)
        lines = msg.strip().split("\n")
        h_indices = [i for i, l in enumerate(lines) if l.startswith("H")]
        self.assertEqual(len(h_indices), 2)
        qc_lines = lines[h_indices[1]:]
        qc_r_lines = [l for l in qc_lines if l.startswith("R")]
        # Template has 4 fields + 1 complementary = 5 R-records minimum
        self.assertGreaterEqual(len(qc_r_lines), 4,
                                f"QC should include all template fields, got {len(qc_r_lines)} R-records")

    # --- use_seed determinism ---

    def test_seed_produces_deterministic_output(self):
        msg1 = self._generate()
        msg2 = self._generate()
        # With use_seed=True, qualitative values should be identical
        r1 = [l for l in msg1.strip().split("\n") if l.startswith("R")]
        r2 = [l for l in msg2.strip().split("\n") if l.startswith("R")]
        # Compare R.4 (value field) for qualitative results
        for line1, line2 in zip(r1, r2):
            val1 = line1.split("|")[3]
            val2 = line2.split("|")[3]
            self.assertEqual(val1, val2, f"Seed values should be deterministic: {val1} != {val2}")


class TestHL7Handler(unittest.TestCase):
    def test_generate_mindray_bc5380(self):
        t = _load_template("mindray_bc5380")
        msg = HL7Handler().generate(t, patient_id="P001", sample_id="S001")
        self.assertIn("MSH|", msg)
        self.assertIn("ORU^R01", msg)
        self.assertIn("PID|", msg)
        self.assertIn("OBR|", msg)
        self.assertIn("OBX|", msg)
        self.assertIn("MINDRAY", msg)
        self.assertIn("P001", msg)
        self.assertIn("S001", msg)

    def test_generate_sysmex_xn(self):
        t = _load_template("sysmex_xn")
        msg = HL7Handler().generate(t)
        self.assertIn("MSH|", msg)
        self.assertIn("SYSMEX", msg)


class TestSerialHandler(unittest.TestCase):
    def test_generate_horiba_pentra60(self):
        t = _load_template("horiba_pentra60")
        msg = SerialHandler().generate(t)
        self.assertIn("H|", msg)
        self.assertIn("PENTRA", msg.upper())


class TestFileHandler(unittest.TestCase):
    def test_generate_quantstudio7(self):
        t = _load_template("quantstudio7")
        csv = FileHandler().generate(t, sample_id="S001")
        self.assertIn("Sample Name", csv)
        self.assertIn("Target", csv)
        self.assertIn("Ct", csv)
        self.assertIn("S001", csv)

    def test_generate_hain_fluorocycler(self):
        t = _load_template("hain_fluorocycler")
        csv = FileHandler().generate(t)
        self.assertIn("Sample ID", csv)
        self.assertIn("Assay", csv)
        self.assertIn("Result", csv)


class TestASTMTcpPush(unittest.TestCase):
    """Tests for push_astm_tcp() and _push_astm_to_destination() routing."""

    # ASTM control characters (must match server.py constants)
    ENQ = b'\x05'
    ACK = b'\x06'
    NAK = b'\x15'
    EOT = b'\x04'
    STX = b'\x02'
    ETX = b'\x03'
    CR = b'\x0D'
    LF = b'\x0A'

    def _make_message(self, record_count=3):
        """Build a minimal ASTM message with the given number of records."""
        records = [f"H|\\^&|||TEST|||||||LIS2-A2"]
        for i in range(record_count - 2):
            records.append(f"R|{i+1}|^^^TEST{i+1}|{i*10}|mg/dL")
        records.append("L|1|N")
        return "\n".join(records)

    def test_push_astm_tcp_uses_sendall(self):
        """push_astm_tcp must use sendall() not send() to prevent partial writes."""
        from unittest.mock import patch, MagicMock
        from server import push_astm_tcp

        mock_sock = MagicMock()
        mock_sock.recv.return_value = self.ACK  # ACK everything

        with patch('socket.socket', return_value=mock_sock):
            result = push_astm_tcp("host", 9600, self._make_message())

        # Verify sendall was used (not send)
        self.assertTrue(mock_sock.sendall.called, "Must use sendall() not send()")
        self.assertFalse(mock_sock.send.called, "send() should not be called — use sendall()")

    def test_push_astm_tcp_enq_ack_flow(self):
        """Verify ENQ is sent first, frames follow, EOT terminates."""
        from unittest.mock import patch, MagicMock, call
        from server import push_astm_tcp

        mock_sock = MagicMock()
        mock_sock.recv.return_value = self.ACK

        with patch('socket.socket', return_value=mock_sock):
            result = push_astm_tcp("host", 9600, self._make_message(3))

        self.assertTrue(result)
        calls = mock_sock.sendall.call_args_list
        # First call: ENQ
        self.assertEqual(calls[0], call(self.ENQ))
        # Last call: EOT
        self.assertEqual(calls[-1], call(self.EOT))
        # Middle calls: frames (3 records = 3 frames)
        frame_calls = calls[1:-1]
        self.assertEqual(len(frame_calls), 3)

    def test_push_astm_tcp_frame_checksum(self):
        """Verify frame format: STX + FN + content + ETX + checksum(2hex) + CR + LF."""
        from unittest.mock import patch, MagicMock
        from server import push_astm_tcp

        mock_sock = MagicMock()
        mock_sock.recv.return_value = self.ACK

        msg = "H|\\^&|||TEST\nL|1|N"
        with patch('socket.socket', return_value=mock_sock):
            push_astm_tcp("host", 9600, msg)

        # Get first frame (skip ENQ at index 0)
        frame = mock_sock.sendall.call_args_list[1][0][0]
        # Must start with STX
        self.assertEqual(frame[0:1], self.STX)
        # Must end with CR+LF
        self.assertEqual(frame[-2:], self.CR + self.LF)
        # Frame number is ASCII digit after STX
        fn = frame[1:2]
        self.assertTrue(fn.isdigit(), f"Frame number must be ASCII digit, got {fn}")
        # ETX before checksum (2 hex bytes before CR+LF)
        self.assertIn(self.ETX, frame)

    def test_push_astm_tcp_frame_numbering_wraps_at_7(self):
        """Frame numbers must cycle 1-7 per CLSI LIS1-A."""
        from unittest.mock import patch, MagicMock
        from server import push_astm_tcp

        mock_sock = MagicMock()
        mock_sock.recv.return_value = self.ACK

        # 8 records → frame numbers should be 1,2,3,4,5,6,7,1
        msg = self._make_message(8)
        with patch('socket.socket', return_value=mock_sock):
            push_astm_tcp("host", 9600, msg)

        frame_calls = mock_sock.sendall.call_args_list[1:-1]  # skip ENQ and EOT
        frame_numbers = [c[0][0][1:2].decode() for c in frame_calls]
        self.assertEqual(frame_numbers, ['1', '2', '3', '4', '5', '6', '7', '1'])

    def test_push_astm_tcp_enq_nak_returns_false(self):
        """If ENQ gets NAK, push should fail."""
        from unittest.mock import patch, MagicMock
        from server import push_astm_tcp

        mock_sock = MagicMock()
        mock_sock.recv.return_value = self.NAK

        with patch('socket.socket', return_value=mock_sock):
            result = push_astm_tcp("host", 9600, self._make_message())

        self.assertFalse(result)

    def test_push_astm_tcp_frame_nak_returns_false(self):
        """If a frame gets NAK, push should fail."""
        from unittest.mock import patch, MagicMock
        from server import push_astm_tcp

        mock_sock = MagicMock()
        # ACK the ENQ, NAK the first frame
        mock_sock.recv.side_effect = [self.ACK, self.NAK]

        with patch('socket.socket', return_value=mock_sock):
            result = push_astm_tcp("host", 9600, self._make_message())

        self.assertFalse(result)

    def test_push_astm_tcp_connection_refused(self):
        """ConnectionRefusedError should return False, not raise."""
        from unittest.mock import patch, MagicMock
        from server import push_astm_tcp

        mock_sock = MagicMock()
        mock_sock.connect.side_effect = ConnectionRefusedError("refused")

        with patch('socket.socket', return_value=mock_sock):
            result = push_astm_tcp("host", 9600, self._make_message())

        self.assertFalse(result)

    def test_destination_tcp_routes_to_push_astm_tcp(self):
        """tcp:// destinations should call push_astm_tcp."""
        from unittest.mock import patch
        from server import _push_astm_to_destination

        with patch('server.push_astm_tcp', return_value=True) as mock_tcp:
            result = _push_astm_to_destination("tcp://bridge:12001", "H|test")

        self.assertTrue(result)
        mock_tcp.assert_called_once_with("bridge", 12001, "H|test")

    def test_destination_http_routes_to_push_to_openelis(self):
        """http(s):// destinations should call push_to_openelis."""
        from unittest.mock import patch
        from server import _push_astm_to_destination

        with patch('server.push_to_openelis', return_value=True) as mock_http:
            result = _push_astm_to_destination("https://oe:8443", "H|test")

        self.assertTrue(result)
        mock_http.assert_called_once()

    def test_destination_tcp_invalid_port_returns_false(self):
        """tcp:// with non-numeric port should return False."""
        from server import _push_astm_to_destination

        result = _push_astm_to_destination("tcp://host:abc", "H|test")
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
