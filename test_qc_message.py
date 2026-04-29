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


if __name__ == "__main__":
    unittest.main()
