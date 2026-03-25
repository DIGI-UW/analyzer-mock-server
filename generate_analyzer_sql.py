#!/usr/bin/env python3
"""
Generate SQL to seed OpenELIS analyzer table from mock server templates.

Usage:
    python3 generate_analyzer_sql.py [--output FILE] [--templates-dir DIR]

Output:
    SQL file with a PL/pgSQL DO block that seeds:
      1. clinlims.analyzer           (ASTM templates only)
      2. clinlims.test               (fields missing from OE catalog)
      3. clinlims.analyzer_test_map  (field code → OE test)
      4. clinlims.analyzer_field     (NUMERIC + CONTROL_TEST per QC field)
      5. clinlims.analyzer_field_mapping (field → test, lot → control_lot)
      6. clinlims.qc_control_lot     (one per QC field with manufacturer mean/SD)
      7. clinlims.qc_statistics      (pre-seeded so z-scores work immediately)

Rules:
  - ASTM + astm_header  → ACTIVE, identifier_pattern = escaped exact match
  - ASTM + no header    → SETUP,  identifier_pattern = NULL (won't match spuriously)
  - HL7 / FILE          → skipped (different integration path, not ASTM TCP)
"""

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_FILE = "schema.json"

# analyzer.category → test_section_id in clinlims.test_section
CATEGORY_TO_SECTION_ID: dict[str, int] = {
    "HEMATOLOGY":   36,
    "CHEMISTRY":    56,
    "IMMUNOLOGY":   59,
    "MICROBIOLOGY": 57,
    "MOLECULAR":    136,
    "COAGULATION":  56,
}

# QC control level mapping: template short code → OE control_level value
LEVEL_MAP: dict[str, str] = {
    "N": "NORMAL",
    "L": "LOW",
    "H": "HIGH",
}

# Maps common analyzer field codes → OE test name (clinlims.test.name).
CODE_TO_TEST_NAME: dict[str, str] = {
    # Hematology
    "WBC":   "White Blood Cells Count (WBC)",
    "RBC":   "Red Blood Cells Count (RBC)",
    "HGB":   "Hemoglobin",
    "HCT":   "Hematocrit",
    "MCV":   "Medium corpuscular volum",
    "MCH":   "TMCH",
    "MCHC":  "CMCH",
    "PLT":   "Platelets",
    "NEU%":  "Neutrophiles (%)",
    "NEU#":  "Neutrophiles",
    "EOS%":  "Eosinophiles (%)",
    "EOS#":  "Eosinophiles",
    "BAS%":  "Basophiles (%)",
    "BAS#":  "Basophiles",
    "LYM%":  "Lymphocytes (%)",
    "LYM#":  "Lymphocytes (Abs)",
    "MON%":  "Monocytes (%)",
    "MON#":  "Monocytes (Abs)",
    # Chemistry
    "CREA":  "Creatinine",
    "ALT":   "GPT/ALAT",
    "AST":   "GOT/ASAT",
    # Immunology
    "HBSAG": "HBsAg (Hepatitis B surface antigen)",
    "HIV":   "HIV rapid test HIV",
    # Molecular
    "COVID19": "COVID-19 PCR",
    "HIV-VL":  "HIV VIRAL LOAD",
}

# Maps LOINC codes → OE test name.
LOINC_TO_TEST_NAME: dict[str, str] = {
    "6690-2": "White Blood Cells Count (WBC)",
    "789-8":  "Red Blood Cells Count (RBC)",
    "718-7":  "Hemoglobin",
    "4544-3": "Hematocrit",
    "777-3":  "Platelets",
    "787-2":  "Medium corpuscular volum",
    "785-6":  "TMCH",
    "786-4":  "CMCH",
}

# Standard Westgard rules: (rule_code, severity, requires_corrective_action)
# Per reference: 1₂ₛ/3₁ₛ/4₁ₛ/7ₜ = WARNING, 1₃ₛ/2₂ₛ/R₄ₛ/10ₓ = REJECTION
WESTGARD_RULES: list[tuple[str, str, bool]] = [
    ("1₂ₛ", "WARNING",   False),
    ("1₃ₛ", "REJECTION", True),
    ("2₂ₛ", "REJECTION", True),
    ("R₄ₛ", "REJECTION", True),
    ("3₁ₛ", "WARNING",   False),
    ("4₁ₛ", "WARNING",   False),
    ("7ₜ",  "WARNING",   False),
    ("10ₓ", "REJECTION", True),
]

# Reasonable SD estimates as percentage of mean, by category
DEFAULT_SD_PCT: dict[str, float] = {
    "HEMATOLOGY": 0.05,
    "CHEMISTRY":  0.05,
    "MOLECULAR":  0.10,
    "COAGULATION": 0.08,
    "IMMUNOLOGY":  0.10,
}


def esc(value: str) -> str:
    """Escape single quotes for SQL string literals."""
    return value.replace("'", "''")


def load_templates(templates_dir: Path) -> list[tuple[str, dict]]:
    templates = []
    for path in sorted(templates_dir.glob("*.json")):
        if path.name == SCHEMA_FILE:
            continue
        try:
            data = json.loads(path.read_text())
            analyzer = data.get("analyzer", {})
            if not analyzer.get("name"):
                continue
            templates.append((path.stem, data))
        except json.JSONDecodeError as e:
            print(f"WARNING: skipping {path.name}: {e}", file=sys.stderr)
    return templates


def resolve_test_name(code: str, loinc: str | None) -> str | None:
    """Return an existing OE test name for this code/loinc, or None if unknown."""
    return CODE_TO_TEST_NAME.get(code) or LOINC_TO_TEST_NAME.get(loinc or code)


def test_name_sql(code: str, loinc: str | None, seeded: dict[str, str]) -> str:
    """Return the OE test name for a field code (existing or newly seeded)."""
    existing = resolve_test_name(code, loinc)
    if existing:
        return existing
    return seeded.get(code, code)


def generate_sql(templates: list[tuple[str, dict]], now: str) -> tuple[str, int, int]:
    """Returns (sql_string, new_test_count, qc_lot_count)."""

    # Collect ASTM-only templates
    astm_templates = []
    for stem, data in templates:
        if data.get("protocol", {}).get("type") == "ASTM":
            astm_templates.append((stem, data))

    # First pass: collect fields needing new test rows (deduplicated by code)
    seeded_tests: dict[str, str] = {}
    new_test_entries: list[dict] = []

    for _stem, data in astm_templates:
        category = data.get("analyzer", {}).get("category") or ""
        section_id = CATEGORY_TO_SECTION_ID.get(category)

        for field in data.get("fields", []):
            code = field.get("code", "")
            loinc = field.get("loinc")
            if not code or resolve_test_name(code, loinc) or code in seeded_tests:
                continue

            test_name = field.get("name", code)
            seeded_tests[code] = test_name
            new_test_entries.append({
                "code": code,
                "name": test_name,
                "loinc": loinc,
                "section_id": section_id,
            })

    # Build PL/pgSQL block
    lines: list[str] = [
        "-- ==========================================================",
        "-- OpenELIS Analyzer Seed SQL",
        f"-- Generated: {now}",
        "-- Source: analyzer-mock-server templates",
        "-- ==========================================================",
        "",
        "DO $$",
        "DECLARE",
        "    v_sys_user_id INTEGER := 1;",
        "    v_generic_astm_type_id INTEGER;",
        "    v_analyzer_id INTEGER;",
        "    v_test_id INTEGER;",
        "    v_field_id TEXT;",
        "    v_lot_field_id TEXT;",
        "    v_lot_id TEXT;",
        "    v_name_loc_id NUMERIC;",
        "    v_report_loc_id NUMERIC;",
        "BEGIN",
        "",
        "    -- Resolve Generic ASTM analyzer type",
        "    SELECT id INTO v_generic_astm_type_id",
        "    FROM clinlims.analyzer_type",
        "    WHERE plugin_class_name = "
        "'org.openelisglobal.plugins.analyzer.genericastm.GenericASTMAnalyzer'",
        "    LIMIT 1;",
        "",
        "    IF v_generic_astm_type_id IS NULL THEN",
        "        RAISE EXCEPTION 'Generic ASTM analyzer type not found. "
        "Ensure GenericASTM plugin is loaded.';",
        "    END IF;",
        "",
    ]

    # Section 1: New tests (with localization)
    if new_test_entries:
        lines.append(f"    -- 1. New tests ({len(new_test_entries)} missing from OE catalog)")
        lines.append("    -- --------------------------------------------------------")

        for entry in new_test_entries:
            test_name = esc(entry["name"])
            code = esc(entry["code"])
            loinc = entry["loinc"]
            section_id = entry["section_id"]
            section_sql = str(section_id) if section_id else "NULL"
            loinc_sql = f"'{esc(loinc)}'" if loinc else "NULL"
            description = esc(f"{entry['name']}({entry['code']})")

            lines.append(f"    -- Test: {entry['name']} ({entry['code']})")

            # Skip if test already exists
            lines.append(
                f"    IF NOT EXISTS (SELECT 1 FROM clinlims.test "
                f"WHERE description = '{description}') THEN"
            )

            # Localization for test name
            lines.append("        v_name_loc_id := nextval('clinlims.localization_seq');")
            lines.append(
                "        INSERT INTO clinlims.localization (id, description, lastupdated)"
                f" VALUES (v_name_loc_id, 'test name', NOW());"
            )
            lines.append(
                "        INSERT INTO clinlims.localization_value "
                "(id, localization_id, locale, value, last_updated)"
                f" VALUES (nextval('clinlims.localization_value_seq'), "
                f"v_name_loc_id, 'en', '{test_name}', NOW());"
            )

            # Localization for reporting name
            lines.append("        v_report_loc_id := nextval('clinlims.localization_seq');")
            lines.append(
                "        INSERT INTO clinlims.localization (id, description, lastupdated)"
                f" VALUES (v_report_loc_id, 'test report name', NOW());"
            )
            lines.append(
                "        INSERT INTO clinlims.localization_value "
                "(id, localization_id, locale, value, last_updated)"
                f" VALUES (nextval('clinlims.localization_value_seq'), "
                f"v_report_loc_id, 'en', '{test_name}', NOW());"
            )

            # Test with localization FKs
            lines.append(
                "        INSERT INTO clinlims.test "
                "(id, name, description, loinc, is_active, is_reportable, "
                "orderable, test_section_id, lastupdated, guid, "
                "name_localization_id, reporting_name_localization_id)"
                " VALUES ("
                f"nextval('clinlims.test_seq'), "
                f"'{test_name}', '{description}', {loinc_sql}, "
                f"'Y', 'Y', TRUE, {section_sql}, '{now}', '{uuid.uuid4()}', "
                f"v_name_loc_id, v_report_loc_id"
                ");"
            )
            lines.append("    END IF;")
            lines.append("")

        lines.append("")

    # Section 2-6: Per-analyzer
    qc_lot_count = 0

    for _stem, data in astm_templates:
        analyzer_meta = data.get("analyzer", {})
        identification = data.get("identification", {})
        fields = data.get("fields", [])
        qc_controls = data.get("qc_controls", [])
        category = analyzer_meta.get("category") or ""

        name = esc(analyzer_meta.get("name", ""))
        manufacturer = esc(analyzer_meta.get("manufacturer", ""))
        model = esc(analyzer_meta.get("model", ""))
        raw_header = identification.get("astm_header", "")
        astm_header = esc(raw_header[:20])
        description = esc(f"{manufacturer} {model}".strip())

        if raw_header:
            identifier_pattern = f"'{esc(re.escape(raw_header))}'"
            status = "ACTIVE"
        else:
            identifier_pattern = "NULL"
            status = "SETUP"

        # Build qc_controls lookup by field_code
        qc_by_code: dict[str, dict] = {}
        for qc in qc_controls:
            qc_by_code[qc["field_code"]] = qc

        lines.append(f"    -- ========== {analyzer_meta.get('name')} [{status}] ==========")
        lines.append("")

        # 2. Analyzer (skip if already exists by name)
        lines.append("    -- 2. Analyzer")
        lines.append(
            f"    SELECT id INTO v_analyzer_id FROM clinlims.analyzer "
            f"WHERE name = '{name}' LIMIT 1;"
        )
        lines.append("    IF v_analyzer_id IS NULL THEN")
        lines.append(
            "        INSERT INTO clinlims.analyzer "
            "(id, name, machine_id, description, analyzer_type, is_active, last_updated, "
            "has_setup_page, analyzer_type_id, identifier_pattern, status, "
            "protocol_version, last_activated_date)"
            " VALUES ("
            f"nextval('clinlims.analyzer_seq'), "
            f"'{name}', '{astm_header}', '{description}', '{esc(category)}', "
            f"TRUE, NOW(), FALSE, v_generic_astm_type_id, "
            f"{identifier_pattern}, '{status}', 'ASTM_LIS2_A2', "
            f"{'NOW()' if status == 'ACTIVE' else 'NULL'}"
            ") RETURNING id INTO v_analyzer_id;"
        )
        lines.append(
            f"        RAISE NOTICE 'Created analyzer: {name} (id=%)', v_analyzer_id;"
        )
        lines.append("    ELSE")
        lines.append(
            f"        RAISE NOTICE 'Reusing existing analyzer: {name} (id=%)', v_analyzer_id;"
        )
        lines.append("    END IF;")
        lines.append("")

        # 3. Analyzer test map
        lines.append("    -- 3. Analyzer → test mappings")
        for field in fields:
            code = field.get("code", "")
            loinc = field.get("loinc")
            if not code:
                continue
            tn = test_name_sql(code, loinc, seeded_tests)
            lines.append(
                "    INSERT INTO clinlims.analyzer_test_map "
                "(analyzer_id, analyzer_type_id, analyzer_test_name, test_id, last_updated)"
                " VALUES ("
                f"v_analyzer_id, v_generic_astm_type_id, '{esc(code)}', "
                f"(SELECT id FROM clinlims.test WHERE name = '{esc(tn)}' LIMIT 1), "
                f"'{now}'"
                ") ON CONFLICT DO NOTHING;"
            )
        lines.append("")

        # 4-6. Analyzer fields, field mappings, QC control lots, statistics
        if qc_by_code:
            lines.append("    -- 4. Analyzer fields (NUMERIC + CONTROL_TEST)")
            lines.append("    -- 5. Field mappings (TEST + QC)")
            lines.append("    -- 6. QC control lots + statistics")

            for field in fields:
                code = field.get("code", "")
                if not code or code not in qc_by_code:
                    continue

                qc = qc_by_code[code]
                loinc = field.get("loinc")
                tn = test_name_sql(code, loinc, seeded_tests)
                lot_number = esc(qc.get("lot_number", f"LOT-{code}-N"))
                level = qc.get("level", "N")
                control_level = LEVEL_MAP.get(level, "NORMAL")
                target = qc.get("target", 0)
                sd_pct = DEFAULT_SD_PCT.get(category, 0.05)
                std_dev = round(target * sd_pct, 5)
                unit = esc(field.get("unit", ""))
                product_name = esc(f"{analyzer_meta.get('name', '')} {code} Control {qc.get('level_name', 'Normal')}")

                lines.append("")
                lines.append(f"    -- {code}: lot={lot_number}, target={target}, SD={std_dev}")

                # Skip if this field already exists for this analyzer
                lines.append(
                    f"    IF NOT EXISTS (SELECT 1 FROM clinlims.analyzer_field "
                    f"WHERE analyzer_id = v_analyzer_id AND field_name = '{esc(code)}') THEN"
                )

                # Resolve test_id for this field
                lines.append(
                    f"        SELECT id INTO v_test_id FROM clinlims.test "
                    f"WHERE name = '{esc(tn)}' LIMIT 1;"
                )

                # analyzer_field: NUMERIC (the test code)
                lines.append(f"        v_field_id := gen_random_uuid()::text;")
                lines.append(
                    "        INSERT INTO clinlims.analyzer_field "
                    "(id, analyzer_id, field_name, field_type, unit, is_active, "
                    "last_updated, sys_user_id)"
                    " VALUES ("
                    f"v_field_id, v_analyzer_id, '{esc(code)}', 'NUMERIC', "
                    f"'{unit}', true, NOW(), v_sys_user_id::text"
                    ");"
                )

                # analyzer_field_mapping: NUMERIC → TEST
                lines.append(
                    "        INSERT INTO clinlims.analyzer_field_mapping "
                    "(id, analyzer_field_id, analyzer_id, openelis_field_id, "
                    "openelis_field_type, mapping_type, is_active, is_required, "
                    "version, last_updated, sys_user_id)"
                    " VALUES ("
                    "gen_random_uuid()::text, v_field_id, v_analyzer_id, "
                    "v_test_id::text, 'TEST', 'TEST_LEVEL', true, false, "
                    "0, NOW(), v_sys_user_id::text"
                    ");"
                )

                # analyzer_field: CONTROL_TEST (the lot code)
                lines.append(f"        v_lot_field_id := gen_random_uuid()::text;")
                lines.append(
                    "        INSERT INTO clinlims.analyzer_field "
                    "(id, analyzer_id, field_name, field_type, is_active, "
                    "last_updated, sys_user_id)"
                    " VALUES ("
                    f"v_lot_field_id, v_analyzer_id, '{lot_number}', 'CONTROL_TEST', "
                    f"true, NOW(), v_sys_user_id::text"
                    ");"
                )

                # qc_control_lot
                lines.append(f"        v_lot_id := gen_random_uuid()::text;")
                lines.append(
                    "        INSERT INTO clinlims.qc_control_lot "
                    "(id, fhir_uuid, product_name, lot_number, manufacturer, "
                    "control_level, test_id, instrument_id, calculation_method, "
                    "initial_runs_count, manufacturer_mean, manufacturer_std_dev, "
                    "activation_date, status, sys_user_id, last_updated)"
                    " VALUES ("
                    f"v_lot_id, gen_random_uuid(), '{product_name}', '{lot_number}', "
                    f"'{esc(analyzer_meta.get('manufacturer', 'Mock'))}', "
                    f"'{control_level}', v_test_id, v_analyzer_id, "
                    f"'MANUFACTURER_FIXED', 20, {target}, {std_dev}, "
                    f"NOW(), 'ACTIVE', v_sys_user_id, NOW()"
                    ");"
                )

                # analyzer_field_mapping: CONTROL_TEST → QC control lot
                lines.append(
                    "        INSERT INTO clinlims.analyzer_field_mapping "
                    "(id, analyzer_field_id, analyzer_id, openelis_field_id, "
                    "openelis_field_type, mapping_type, is_active, is_required, "
                    "version, last_updated, sys_user_id)"
                    " VALUES ("
                    "gen_random_uuid()::text, v_lot_field_id, v_analyzer_id, "
                    "v_lot_id, 'QC', 'TEST_LEVEL', true, false, "
                    "0, NOW(), v_sys_user_id::text"
                    ");"
                )

                # qc_statistics (pre-seeded so z-scores work immediately)
                lines.append(
                    "        INSERT INTO clinlims.qc_statistics "
                    "(id, control_lot_id, calculation_date, mean, standard_deviation, "
                    "num_values, calculation_method, validity_start, "
                    "sys_user_id, last_updated)"
                    " VALUES ("
                    f"gen_random_uuid()::text, v_lot_id, NOW(), {target}, {std_dev}, "
                    f"20, 'MANUFACTURER_FIXED', NOW(), v_sys_user_id, NOW()"
                    ");"
                )

                lines.append("    END IF;")

                # Westgard rules (8 per test/instrument, idempotent via ON CONFLICT)
                lines.append(
                    f"    SELECT id INTO v_test_id FROM clinlims.test "
                    f"WHERE name = '{esc(tn)}' LIMIT 1;"
                )
                for rule_code, severity, corrective in WESTGARD_RULES:
                    lines.append(
                        "    INSERT INTO clinlims.westgard_rule_config "
                        "(id, test_id, instrument_id, rule_code, enabled, severity, "
                        "requires_corrective_action, sys_user_id, last_updated)"
                        " VALUES ("
                        f"gen_random_uuid()::text, v_test_id, v_analyzer_id, "
                        f"'{rule_code}', true, '{severity}', "
                        f"{'true' if corrective else 'false'}, v_sys_user_id, NOW()"
                        ") ON CONFLICT (test_id, instrument_id, rule_code) DO NOTHING;"
                    )

                qc_lot_count += 1

        lines.append("")

    # Summary
    lines.append("    RAISE NOTICE 'Seed complete: % analyzers, % QC control lots',")
    lines.append(f"        {len(astm_templates)}, {qc_lot_count};")
    lines.append("")
    lines.append("END $$;")
    lines.append("")

    return "\n".join(lines), len(new_test_entries), qc_lot_count


def main():
    parser = argparse.ArgumentParser(
        description="Generate OpenELIS analyzer seed SQL from mock server templates"
    )
    parser.add_argument(
        "--templates-dir",
        default="templates",
        metavar="DIR",
        help="Templates directory (default: templates)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="seed_analyzers.sql",
        metavar="FILE",
        help="Output SQL file (default: seed_analyzers.sql)",
    )
    args = parser.parse_args()

    templates_dir = Path(args.templates_dir)
    if not templates_dir.is_dir():
        print(f"ERROR: templates dir not found: {templates_dir}", file=sys.stderr)
        sys.exit(1)

    templates = load_templates(templates_dir)
    if not templates:
        print("ERROR: no templates found", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00")
    sql, new_test_count, qc_lot_count = generate_sql(templates, now)

    output = Path(args.output)
    output.write_text(sql)
    astm = [(s, d) for s, d in templates if d.get("protocol", {}).get("type") == "ASTM"]
    non_astm = [(s, d) for s, d in templates if d.get("protocol", {}).get("type") != "ASTM"]
    print(f"Generated {output} ({len(astm)} ASTM analyzers, {new_test_count} new tests, {qc_lot_count} QC lots, {len(non_astm)} skipped)")
    for _stem, data in astm:
        name = data["analyzer"]["name"]
        header = data.get("identification", {}).get("astm_header", "")
        status = "ACTIVE" if header else "SETUP "
        field_count = len(data.get("fields", []))
        qc_count = len(data.get("qc_controls", []))
        qc_str = f", {qc_count} QC lots" if qc_count else ""
        print(f"  [{status}] {name:<38}  {field_count} fields{qc_str}")
    if non_astm:
        print("Skipped (not ASTM):")
        for _stem, data in non_astm:
            proto = data.get("protocol", {}).get("type", "?")
            print(f"  [{proto:<4}] {data['analyzer']['name']}")


if __name__ == "__main__":
    main()
