#!/usr/bin/env python3
"""
Unit tests for HL7 ORU^R01 generation from analyzer templates.

Validates that Mindray BC-5380 and BS-360E templates produce HL7 messages
with correct MSH, PID, ORC, OBR, OBX segments and that OBX values/units
match template seedValues.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from template_loader import TemplateLoader
from hl7_generator import generate_oru_r01


class TestHL7Generator(unittest.TestCase):
    """Tests for HL7 message generation from templates."""

    @classmethod
    def setUpClass(cls):
        cls.loader = TemplateLoader()
        cls.templates_dir = os.path.join(os.path.dirname(__file__), 'templates')

    def test_load_mindray_bc5380_and_generate_hl7(self):
        """Generate HL7 from mindray_bc5380 template; assert segments and values."""
        template = self.loader.load_template('mindray_bc5380')
        message = generate_oru_r01(template, deterministic=True)
        lines = message.strip().split('\n')

        self.assertGreaterEqual(len(lines), 5, "Expected at least MSH, PID, ORC, OBR, OBX lines")
        self.assertTrue(lines[0].startswith('MSH|'), "First segment must be MSH")
        self.assertIn('|^~\\&|', lines[0], "MSH must contain standard delimiters")
        self.assertIn('MINDRAY|LAB', lines[0], "MSH must have sending app and facility")
        self.assertIn('ORU^R01', lines[0], "MSH must be ORU^R01")

        self.assertTrue(lines[1].startswith('PID|'), "Second segment must be PID")
        self.assertTrue(lines[2].startswith('ORC|'), "Third segment must be ORC")
        self.assertTrue(lines[3].startswith('OBR|'), "Fourth segment must be OBR")

        obx_lines = [l for l in lines if l.startswith('OBX|')]
        self.assertEqual(len(obx_lines), 4, "BC-5380 template has 4 fields (WBC, RBC, HGB, HCT)")

        # OBX format: OBX|seq|NM|^^^CODE^NAME||value|unit|... (value may be 7.50 for 7.5)
        self.assertIn('OBX|1|NM|^^^WBC^', obx_lines[0])
        self.assertIn('7.5', obx_lines[0])
        self.assertIn('10*3/uL', obx_lines[0])
        self.assertIn('OBX|2|NM|^^^RBC^', obx_lines[1])
        self.assertIn('4.82', obx_lines[1])
        self.assertIn('10*6/uL', obx_lines[1])
        self.assertIn('OBX|3|NM|^^^HGB^', obx_lines[2])
        self.assertIn('14.2', obx_lines[2])
        self.assertIn('g/dL', obx_lines[2])
        self.assertIn('OBX|4|NM|^^^HCT^', obx_lines[3])
        self.assertTrue('42' in obx_lines[3], "HCT value 42 or 42.0")
        self.assertIn('%', obx_lines[3])

    def test_load_mindray_bs360e_and_generate_hl7(self):
        """Generate HL7 from mindray_bs360e template; assert segments and values."""
        template = self.loader.load_template('mindray_bs360e')
        message = generate_oru_r01(template, deterministic=True)
        lines = message.strip().split('\n')

        self.assertTrue(lines[0].startswith('MSH|'))
        self.assertIn('MINDRAY|LAB', lines[0])
        obx_lines = [l for l in lines if l.startswith('OBX|')]
        self.assertEqual(len(obx_lines), 3, "BS-360E template has 3 fields (CREA, ALT, AST)")

        self.assertIn('OBX|1|NM|^^^CREA^', obx_lines[0])
        self.assertIn('0.9', obx_lines[0])
        self.assertIn('mg/dL', obx_lines[0])
        self.assertIn('OBX|2|NM|^^^ALT^', obx_lines[1])
        self.assertIn('25', obx_lines[1])
        self.assertIn('U/L', obx_lines[1])
        self.assertIn('OBX|3|NM|^^^AST^', obx_lines[2])
        self.assertIn('22', obx_lines[2])
        self.assertIn('U/L', obx_lines[2])

    def test_deterministic_produces_same_values(self):
        """Deterministic mode uses seedValues; two messages have same OBX values."""
        template = self.loader.load_template('mindray_bc5380')
        msg1 = generate_oru_r01(template, deterministic=True)
        msg2 = generate_oru_r01(template, deterministic=True)
        obx1 = [l for l in msg1.strip().split('\n') if l.startswith('OBX|')]
        obx2 = [l for l in msg2.strip().split('\n') if l.startswith('OBX|')]
        self.assertEqual(len(obx1), len(obx2))
        for a, b in zip(obx1, obx2):
            # Same field code and value (ignore message control ID in MSH)
            self.assertEqual(a.split('|')[3], b.split('|')[3], "OBX observation ID should match")
            self.assertEqual(a.split('|')[5], b.split('|')[5], "OBX value should match in deterministic mode")

    def test_message_ends_with_newline(self):
        """Generated message ends with a single newline for parser compatibility."""
        template = self.loader.load_template('mindray_bc5380')
        message = generate_oru_r01(template, deterministic=True)
        self.assertTrue(message.endswith('\n'), "Message must end with newline")
        self.assertFalse(message.endswith('\n\n'), "Message must not end with double newline")


if __name__ == '__main__':
    unittest.main()
