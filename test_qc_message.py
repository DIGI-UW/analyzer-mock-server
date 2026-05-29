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
    # Use the production loader so assay fields are derived from the canonical
    # profile (single source of truth), not a raw template read that would miss
    # profile-backed analyzers' fields.
    from server import _load_template as _load_production_template
    return _load_production_template(name)


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
      - SPECIMEN_ID_PREFIX operand=LPC|HPC|CNEG|CPOS|NTC|PTC → row Sample Name
        must start with one of those level prefixes so the bridge classifies
        as QC AND propagates controlLevel to OE's Tier 2 lot resolver.
      - FIELD_EQUALS targetField=QC_TASK operand=STANDARD → Task column must
        equal STANDARD.

    Each test runs the same content contract against all three on-the-wire
    formats (CSV / TSV / XLSX) so the mock guarantees parity — the bridge's
    extension-based dispatch picks the parser, but the rows must look the
    same regardless of format. Native QuantStudio output is .xls/.xlsx; the
    CSV/TSV paths exist for analyzers whose native format is delimited text
    and for diagnostic-friendly tooling.
    """

    @classmethod
    def setUpClass(cls):
        from protocols.file_handler import FileHandler
        cls.handler = FileHandler()
        cls.base_template = _load_template("quantstudio5")

    def _template(self, fmt: str):
        """Clone the live template with a different file_config.format so the
        same QC controls flow through every emitter."""
        import copy
        t = copy.deepcopy(self.base_template)
        t.setdefault("file_config", {})["format"] = fmt
        return t

    def _rows(self, content, fmt: str):
        """Parse generate_qc bytes into [{header: value, ...}] rows."""
        if fmt == "XLSX":
            from openpyxl import load_workbook
            import io
            wb = load_workbook(io.BytesIO(content))
            ws = wb.active
            data = list(ws.values)
            header = [str(h) if h is not None else "" for h in data[0]]
            rows = []
            for raw in data[1:]:
                if all(c is None or str(c).strip() == "" for c in raw):
                    continue
                rows.append({h: ("" if v is None else str(v)) for h, v in zip(header, raw)})
            return header, rows
        text = content.decode("utf-8") if isinstance(content, (bytes, bytearray)) else content
        delim = "\t" if fmt == "TSV" else ","
        lines = [r for r in text.split("\n") if r.strip()]
        header = lines[0].split(delim)
        return header, [dict(zip(header, line.split(delim))) for line in lines[1:]]

    def _check_format(self, fmt: str, deviation: float, expected_lpc_value: float):
        template = self._template(fmt)
        content = self.handler.generate_qc(template, deviation=deviation)

        # Bytes contract — generate_qc always returns bytes regardless of format.
        self.assertIsInstance(content, (bytes, bytearray),
                              f"generate_qc must return bytes for {fmt}")

        _, rows = self._rows(content, fmt)
        self.assertGreaterEqual(len(rows), 2, f"[{fmt}] expected LPC + HPC rows")

        for row in rows:
            sample_name = row.get("Sample Name", "")
            self.assertTrue(
                sample_name.startswith("LPC-") or sample_name.startswith("HPC-"),
                f"[{fmt}] expected LPC-/HPC- prefix per QuantStudio qcRules, got: {sample_name}")
            self.assertIn("LOT-", sample_name,
                          f"[{fmt}] expected lot string embedded in sample name, got: {sample_name}")
            self.assertEqual(row.get("Task"), "STANDARD",
                             f"[{fmt}] expected Task=STANDARD, got: {row.get('Task')}")

        lpc = next(r for r in rows if r["Sample Name"].startswith("LPC-"))
        self.assertAlmostEqual(
            float(lpc["Quantity Mean"]), expected_lpc_value, places=1,
            msg=f"[{fmt}] LPC value mismatch")

    def test_csv_format_contract(self):
        # LPC target=32.0 sd=0.5 → at deviation=0, value should be 32.0.
        self._check_format("CSV", deviation=0, expected_lpc_value=32.0)

    def test_tsv_format_contract(self):
        self._check_format("TSV", deviation=0, expected_lpc_value=32.0)

    def test_xlsx_format_contract(self):
        # Native QuantStudio output format — what production sees.
        self._check_format("XLSX", deviation=0, expected_lpc_value=32.0)

    def test_deviation_3_emits_target_plus_3_sd_across_formats(self):
        # LPC target=32.0 sd=0.5 → deviation=3.0 → 33.5; same value in every emitter.
        for fmt in ("CSV", "TSV", "XLSX"):
            with self.subTest(format=fmt):
                self._check_format(fmt, deviation=3.0, expected_lpc_value=33.5)

    def test_qc_extension_resolution(self):
        from protocols.file_handler import FileHandler
        self.assertEqual(FileHandler.qc_extension(self._template("CSV")), ".csv")
        self.assertEqual(FileHandler.qc_extension(self._template("TSV")), ".tsv")
        self.assertEqual(FileHandler.qc_extension(self._template("XLSX")), ".xlsx")
        self.assertEqual(FileHandler.qc_extension(self._template("EXCEL")), ".xlsx")
        self.assertEqual(FileHandler.qc_extension(self._template("XLS")), ".xlsx")

    def test_unsupported_pattern_placeholder_raises(self):
        # Defensive: bad qc_sample_id_pattern should raise ValueError up
        # front (clear message), not KeyError as a 500.
        import copy
        t = copy.deepcopy(self.base_template)
        t["qc_sample_id_pattern"] = "{nonsense}-{lot}"
        with self.assertRaises(ValueError) as ctx:
            self.handler.generate_qc(t, deviation=0)
        self.assertIn("nonsense", str(ctx.exception))

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

    def test_obr_25_is_control_result_status(self):
        # OBR-25 = "C" (Result Status: control). Pin the field position so an
        # off-by-one in the OBR layout (extra/missing pipe) is caught here.
        msg = self.handler.generate_qc(self.template, deviation=0)
        obr = next(s for s in self._segments(msg) if s.startswith("OBR|"))
        fields = obr.split("|")
        self.assertGreaterEqual(len(fields), 26,
                                f"OBR has only {len(fields)} fields, need >=26")
        self.assertEqual(fields[25], "C",
                         f"Expected OBR-25=C, got: {fields[25]!r} in {obr}")

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
