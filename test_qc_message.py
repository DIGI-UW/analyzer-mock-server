"""QC message contract tests for the analyzer mock server.

These tests pin down the wire-format contract that the mock --qc CLI
produces for ASTM QC pushes. They serve two purposes:

  1. Regression coverage: the mock's QC output is the contract the bridge
     consumes. If we accidentally change the field layout or action code,
     the bridge's QC-detection breaks silently. Test A catches that.
  2. Documentation by example: the bridge's expected input format is
     literally these tests' assertions.

Reference: ASTM LIS2-A2, OE GenericASTMLineInserter conventions.
"""

import json
import os
import unittest

from protocols.astm_handler import ASTMHandler


TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")


def _load_template(name: str) -> dict:
    with open(os.path.join(TEMPLATE_DIR, f"{name}.json")) as handle:
        return json.load(handle)


class QCMessageContract(unittest.TestCase):
    """Test A — Mock --qc emission contract."""

    @classmethod
    def setUpClass(cls):
        cls.template = _load_template("genexpert_astm")

    def _generate(self, deviation=None) -> str:
        return ASTMHandler().generate_qc(self.template, deviation=deviation)

    def _records(self, msg: str) -> list:
        return [r for r in msg.split("\n") if r.strip()]

    def test_o_record_has_action_code_q(self):
        msg = self._generate(deviation=0)
        o_records = [r for r in self._records(msg) if r.startswith("O|")]
        self.assertEqual(
            len(o_records), 1,
            f"Expected exactly one O-record; got {len(o_records)}: {o_records}",
        )
        fields = o_records[0].split("|")
        # ASTM O.12 = Action Code per LIS2-A2.
        # Convention: ASTM 1-indexed field 12 is 0-indexed array index 11.
        # The bridge's O_ACTION_CODE_FIELD constant should match this.
        self.assertEqual(
            fields[11].strip(), "Q",
            f"Expected O.12 (idx 11) = 'Q', got fields={fields}",
        )

    def test_q_segment_carries_lot_number(self):
        msg = self._generate(deviation=0)
        q_records = [r for r in self._records(msg) if r.startswith("Q|")]
        self.assertGreaterEqual(
            len(q_records), 1,
            "Expected at least one Q-record carrying lot/level/target",
        )
        # Q|seq|field_code^lot_number^level|value|unit|timestamp
        components = q_records[0].split("|")[2].split("^")
        self.assertEqual(
            len(components), 3,
            f"Q.2 should be field^lot^level, got components={components}",
        )
        self.assertTrue(
            components[1].startswith("LOT-"),
            f"Expected lot number prefix 'LOT-', got: {components[1]}",
        )

    def test_deviation_zero_emits_target_value(self):
        msg = self._generate(deviation=0)
        r_records = [r for r in self._records(msg) if r.startswith("R|")]
        self.assertEqual(len(r_records), 1, f"Expected one R-record, got: {r_records}")
        value = float(r_records[0].split("|")[3])
        # genexpert_astm.json qc_controls[0].target = 1250.0
        self.assertAlmostEqual(value, 1250.0, places=1)

    def test_deviation_3_5_emits_target_plus_3_5_sd(self):
        # MOLECULAR SD = 10% of target (astm_handler.py:367) → 125.0
        # Expected: 1250 + 3.5 * 125 = 1687.5 (1₃ₛ rejection territory)
        msg = self._generate(deviation=3.5)
        r_records = [r for r in self._records(msg) if r.startswith("R|")]
        value = float(r_records[0].split("|")[3])
        self.assertAlmostEqual(value, 1687.5, places=1)

    def test_deviation_minus_2_5_emits_target_minus_2_5_sd(self):
        # 1250 - 2.5 * 125 = 937.5 (1₂ₛ warning territory)
        msg = self._generate(deviation=-2.5)
        r_records = [r for r in self._records(msg) if r.startswith("R|")]
        value = float(r_records[0].split("|")[3])
        self.assertAlmostEqual(value, 937.5, places=1)

    def test_h_record_carries_genexpert_identifier(self):
        # Bridge identifies analyzer via H.5 (sender identifier). For
        # genexpert template the H-record should include "GENEXPERT" or
        # "GeneXpert" so the bridge's protocolHint matches the registered
        # analyzer name.
        msg = self._generate(deviation=0)
        h_records = [r for r in self._records(msg) if r.startswith("H|")]
        self.assertEqual(len(h_records), 1)
        self.assertIn("eneXpert", h_records[0],
                      f"Expected GeneXpert identifier in H-record, got: {h_records[0]}")


class FileQCMessageContract(unittest.TestCase):
    """Test B — FILE (QuantStudio) generate_qc emission contract.

    QuantStudio FILE profile qcRules (OE side):
      - SPECIMEN_ID_PREFIX operand=QC → row Sample Name must start with "QC-"
      - FIELD_EQUALS targetField=QC_TASK operand=STANDARD → Task column must equal STANDARD
    """

    @classmethod
    def setUpClass(cls):
        from protocols.file_handler import FileHandler
        cls.handler = FileHandler()
        cls.template = _load_template("quantstudio5")

    def _rows(self, content: str):
        lines = [r for r in content.split("\n") if r.strip()]
        header = lines[0].split(",")
        return header, [dict(zip(header, line.split(","))) for line in lines[1:]]

    def test_qc_rows_have_qc_sample_id_prefix(self):
        content = self.handler.generate_qc(self.template, deviation=0)
        _, rows = self._rows(content)
        self.assertGreaterEqual(len(rows), 2, "Expected LPC + HPC rows")
        for row in rows:
            sample_name = row.get("Sample Name", "")
            self.assertTrue(sample_name.startswith("QC-"),
                            f"Expected QC- prefix, got: {sample_name}")

    def test_qc_task_column_equals_standard(self):
        content = self.handler.generate_qc(self.template, deviation=0)
        _, rows = self._rows(content)
        for row in rows:
            self.assertEqual(row.get("Task"), "STANDARD",
                             f"Expected Task=STANDARD, got: {row.get('Task')}")

    def test_deviation_zero_emits_target_value(self):
        # LPC target=32.0 sd=0.5 → at deviation=0, value should be 32.0.
        content = self.handler.generate_qc(self.template, deviation=0)
        _, rows = self._rows(content)
        lpc = next(r for r in rows if "LPC" in r["Sample Name"])
        self.assertAlmostEqual(float(lpc["Quantity Mean"]), 32.0, places=1)

    def test_deviation_3_emits_target_plus_3_sd(self):
        # LPC target=32.0 sd=0.5 → deviation=3.0 → 33.5 (per fixture README math)
        content = self.handler.generate_qc(self.template, deviation=3.0)
        _, rows = self._rows(content)
        lpc = next(r for r in rows if "LPC" in r["Sample Name"])
        self.assertAlmostEqual(float(lpc["Quantity Mean"]), 33.5, places=1)

    def test_no_qc_controls_raises(self):
        bad_template = {"analyzer": {"name": "X"}, "fields": [{"name": "f"}]}
        with self.assertRaises(ValueError):
            self.handler.generate_qc(bad_template, deviation=0)


class HL7QCMessageContract(unittest.TestCase):
    """Test C — HL7 (Mindray BS-200) generate_qc emission contract.

    Mindray HL7 profile qcRule (OE side):
      - SPECIMEN_ID_PREFIX operand=QC → OBR-3 (Filler/Specimen ID) must start with "QC-"
    """

    @classmethod
    def setUpClass(cls):
        from protocols.hl7_handler import HL7Handler
        cls.handler = HL7Handler()
        cls.template = _load_template("mindray_bs200")

    def _segments(self, msg: str):
        return [s for s in msg.split("\r") if s.strip()]

    def test_obr_3_starts_with_qc_prefix(self):
        msg = self.handler.generate_qc(self.template, deviation=0)
        obr = next(s for s in self._segments(msg) if s.startswith("OBR|"))
        fields = obr.split("|")
        self.assertTrue(fields[3].startswith("QC-"),
                        f"Expected OBR-3 to start with QC-, got: {fields[3]}")

    def test_obx_11_is_control_status(self):
        msg = self.handler.generate_qc(self.template, deviation=0)
        obx_records = [s for s in self._segments(msg) if s.startswith("OBX|")]
        self.assertGreaterEqual(len(obx_records), 1)
        # OBX-11 is 1-indexed field 11 = 0-indexed array index 11 (after split on |).
        for obx in obx_records:
            fields = obx.split("|")
            self.assertEqual(fields[11], "C",
                             f"Expected OBX-11=C, got: {fields[11]} in {obx}")

    def test_obx_3_components_carry_field_lot_level(self):
        msg = self.handler.generate_qc(self.template, deviation=0)
        obx = next(s for s in self._segments(msg) if s.startswith("OBX|"))
        fields = obx.split("|")
        # OBX-3 format: ^^^code^lot^level — components 4, 5, 6 of the ^-split.
        components = fields[3].split("^")
        self.assertEqual(components[3], "GLU", f"Expected field code GLU, got: {components}")
        self.assertEqual(components[4], "LOT-GLU-N", f"Expected lot LOT-GLU-N, got: {components}")
        self.assertEqual(components[5], "NORMAL", f"Expected level NORMAL, got: {components}")

    def test_deviation_zero_emits_target_value(self):
        # GLU target=100, sd=5 → at deviation=0, value should be 100.0.
        msg = self.handler.generate_qc(self.template, deviation=0)
        obx = next(s for s in self._segments(msg) if s.startswith("OBX|"))
        fields = obx.split("|")
        self.assertAlmostEqual(float(fields[5]), 100.0, places=1)

    def test_deviation_3_5_emits_target_plus_3_5_sd(self):
        # GLU target=100, sd=5 → deviation=3.5 → 117.5 (1₃ₛ rejection territory)
        msg = self.handler.generate_qc(self.template, deviation=3.5)
        obx = next(s for s in self._segments(msg) if s.startswith("OBX|"))
        fields = obx.split("|")
        self.assertAlmostEqual(float(fields[5]), 117.5, places=1)

    def test_no_qc_controls_raises(self):
        bad_template = {"protocol": {"type": "HL7"}, "analyzer": {"name": "X"}}
        with self.assertRaises(ValueError):
            self.handler.generate_qc(bad_template, deviation=0)


if __name__ == "__main__":
    unittest.main()
