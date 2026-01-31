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


if __name__ == "__main__":
    unittest.main()
