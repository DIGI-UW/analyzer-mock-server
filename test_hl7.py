#!/usr/bin/env python3
"""
HL7 ORU^R01 and template tests for the analyzer simulator.

Tests:
- Schema validation loads templates
- HL7 handler generates syntactically valid ORU^R01 (segment presence, delimiters)
- Abbott template produces MSH-3=ARCHITECT and OBX segments with expected codes/values
- Golden semantics: key extracted fields match expected (Abbott plugin routing)
"""

import os
import sys
import json

import pytest

# Run from repo root so imports resolve
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Optional: jsonschema for schema validation tests
try:
    import jsonschema
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False


def _segments(msg: str):
    """Split HL7 message into segments (\\r or \\n)."""
    return [s.strip() for s in msg.replace("\r", "\n").split("\n") if s.strip()]


def _segment_map(msg: str):
    """Return dict of segment_type -> list of segment rows (first segment type only for multi)."""
    segs = _segments(msg)
    out = {}
    for s in segs:
        if "|" not in s:
            continue
        seg_type = s.split("|", 1)[0]
        if seg_type not in out:
            out[seg_type] = []
        out[seg_type].append(s)
    return out


def _msh_fields(msh_line: str):
    """Extract MSH-3, MSH-4, MSH-9 from MSH segment."""
    parts = msh_line.split("|")
    return {
        "sending_app": parts[2] if len(parts) > 2 else "",
        "sending_facility": parts[3] if len(parts) > 3 else "",
        "message_type": parts[8] if len(parts) > 8 else "",
        "version": parts[11] if len(parts) > 11 else "",
    }


def _obx_fields(obx_line: str):
    """Extract OBX identifier (code/name) and value from OBX segment."""
    parts = obx_line.split("|")
    # OBX-3 is ^^^code^name, OBX-5 is value
    obx3 = parts[3] if len(parts) > 3 else ""
    value = parts[5] if len(parts) > 5 else ""
    code = ""
    if "^" in obx3:
        sub = obx3.split("^")
        if len(sub) >= 4:
            code = sub[3]
        elif len(sub) >= 1:
            code = sub[-1]
    else:
        code = obx3
    return {"code": code, "value": value}


def _pid_fields(pid_line: str):
    """Extract PID patient id and name (PID-3, PID-5)."""
    parts = pid_line.split("|")
    pid3 = parts[3] if len(parts) > 3 else ""
    pid5 = parts[5] if len(parts) > 5 else ""
    return {"patient_id": pid3, "name": pid5}


def _orc_obr_placer_filler(orc_line: str):
    """ORC-2 placer, ORC-3 filler."""
    parts = orc_line.split("|")
    return {
        "placer_order_id": parts[2] if len(parts) > 2 else "",
        "filler_order_id": parts[3] if len(parts) > 3 else "",
    }


# --- Schema validation ---


@pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
def test_schema_validation_loads_abbott_template():
    """Template loader validates abbott_architect_hl7 against schema."""
    from template_loader import TemplateLoader

    loader = TemplateLoader()
    template = loader.load_template("abbott_architect_hl7")
    assert template is not None
    assert template.get("protocol", {}).get("type") == "HL7"
    assert template.get("identification", {}).get("hl7_sending_app") == "ARCHITECT"
    assert len(template.get("fields", [])) >= 2


@pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
def test_schema_validation_rejects_invalid_template():
    """Invalid template (missing required) fails validation."""
    import jsonschema
    from template_loader import TemplateLoader

    loader = TemplateLoader()
    if not loader.schema:
        pytest.skip("schema file not found")
    invalid = {"analyzer": {"name": "X"}, "protocol": {"type": "HL7"}}
    # Missing required: identification, fields
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, loader.schema)


# --- HL7 handler syntax ---


def test_hl7_handler_generates_oru_r01_structure():
    """HL7 handler produces message with MSH, PID, ORC, OBR, OBX and \\r delimiter."""
    from template_loader import TemplateLoader
    from protocols.hl7_handler import generate_oru_r01

    loader = TemplateLoader()
    template = loader.load_template("abbott_architect_hl7")
    msg = generate_oru_r01(template, deterministic=True)

    assert "\r" in msg or msg.endswith("\r")
    segs = _segment_map(msg)
    assert "MSH" in segs and len(segs["MSH"]) == 1
    assert "PID" in segs and len(segs["PID"]) >= 1
    assert "ORC" in segs and len(segs["ORC"]) >= 1
    assert "OBR" in segs and len(segs["OBR"]) >= 1
    assert "OBX" in segs and len(segs["OBX"]) >= 1

    msh = _msh_fields(segs["MSH"][0])
    assert msh["message_type"] == "ORU^R01"


def test_hl7_handler_delimiter_sanity():
    """Segment delimiter is pipe; segments separated by \\r."""
    from template_loader import TemplateLoader
    from protocols.hl7_handler import generate_oru_r01

    loader = TemplateLoader()
    template = loader.load_template("abbott_architect_hl7")
    msg = generate_oru_r01(template, deterministic=True)

    lines = msg.strip().split("\r")
    for line in lines:
        if not line.strip():
            continue
        assert line.startswith("MSH") or line.startswith("PID") or line.startswith("ORC") or line.startswith("OBR") or line.startswith("OBX")
        assert "|" in line


# --- Abbott template semantics ---


def test_abbott_template_msh_sender():
    """Abbott template produces MSH-3=ARCHITECT, MSH-4=LAB for OpenELIS routing."""
    from template_loader import TemplateLoader
    from protocols.hl7_handler import generate_oru_r01

    loader = TemplateLoader()
    template = loader.load_template("abbott_architect_hl7")
    msg = generate_oru_r01(template, deterministic=True)

    segs = _segment_map(msg)
    msh = _msh_fields(segs["MSH"][0])
    assert msh["sending_app"] == "ARCHITECT"
    assert msh["sending_facility"] == "LAB"


@pytest.mark.parametrize(
    "template_name,expected_facility",
    [
        ("mindray_bc5380", "BC-5380"),
        ("mindray_bc2000", "BC2000"),
        ("mindray_bs200", "BS-200"),
        ("mindray_bs300", "BS-300"),
    ],
)
def test_mindray_templates_emit_sender_identity_and_version(template_name, expected_facility):
    """Mindray HL7 templates emit MSH-3/MSH-4 identity and the configured HL7 version."""
    from server import _load_template
    from protocols.hl7_handler import generate_oru_r01

    template = _load_template(template_name)
    msg = generate_oru_r01(template, deterministic=True)

    segs = _segment_map(msg)
    msh = _msh_fields(segs["MSH"][0])
    assert msh["sending_app"] == "MINDRAY"
    assert msh["sending_facility"] == expected_facility
    assert msh["message_type"] == "ORU^R01"
    assert msh["version"] == "2.3.1"


def test_abbott_template_obx_codes_and_values():
    """Abbott template produces OBX with HIV=NEGATIVE and HBSAG=POSITIVE."""
    from template_loader import TemplateLoader
    from protocols.hl7_handler import generate_oru_r01

    loader = TemplateLoader()
    template = loader.load_template("abbott_architect_hl7")
    msg = generate_oru_r01(template, deterministic=True)

    segs = _segment_map(msg)
    obx_list = segs.get("OBX", [])
    codes_values = {_obx_fields(o)["code"]: _obx_fields(o)["value"] for o in obx_list}

    assert "HIV" in codes_values
    assert codes_values["HIV"] == "NEGATIVE"
    assert "HBSAG" in codes_values
    assert codes_values["HBSAG"] == "POSITIVE"


def test_abbott_template_patient_and_sample_ids():
    """Abbott template uses testPatient/testSample IDs in PID and ORC."""
    from template_loader import TemplateLoader
    from protocols.hl7_handler import generate_oru_r01

    loader = TemplateLoader()
    template = loader.load_template("abbott_architect_hl7")
    msg = generate_oru_r01(template, deterministic=True)

    segs = _segment_map(msg)
    pid = _pid_fields(segs["PID"][0])
    orc = _orc_obr_placer_filler(segs["ORC"][0])

    assert "PAT003" in pid["patient_id"]
    assert "RAKOTO" in pid["name"] or "JAO" in pid["name"]
    # Sample IDs are generated sequentially by the mock (like a real analyzer)
    assert orc["placer_order_id"], "placer_order_id should not be empty"
    assert orc["filler_order_id"] == orc["placer_order_id"], "filler should match placer"


# --- Golden fixture semantics (key fields only) ---

# Expected key fields from abbott-architect-result.hl7 semantics (OpenELIS Abbott plugin)
ABBOTT_GOLDEN_SEMANTICS = {
    "MSH": {"sending_app": "ARCHITECT", "sending_facility": "LAB", "message_type": "ORU^R01"},
    "OBX": [{"code": "HIV", "value": "NEGATIVE"}, {"code": "HBSAG", "value": "POSITIVE"}],
    "PID": {"patient_id_contains": "PAT003", "name_contains": "RAKOTO"},
    "ORC": {"placer_prefix": "DEV01264300", "filler_equals_placer": True},
}


def test_generated_abbott_message_matches_golden_semantics():
    """Generated Abbott HL7 message matches expected key fields (semantic comparison)."""
    from template_loader import TemplateLoader
    from protocols.hl7_handler import generate_oru_r01

    loader = TemplateLoader()
    template = loader.load_template("abbott_architect_hl7")
    msg = generate_oru_r01(template, deterministic=True)

    segs = _segment_map(msg)
    msh = _msh_fields(segs["MSH"][0])
    assert msh["sending_app"] == ABBOTT_GOLDEN_SEMANTICS["MSH"]["sending_app"]
    assert msh["sending_facility"] == ABBOTT_GOLDEN_SEMANTICS["MSH"]["sending_facility"]
    assert msh["message_type"] == ABBOTT_GOLDEN_SEMANTICS["MSH"]["message_type"]

    obx_list = segs.get("OBX", [])
    codes_values = [_obx_fields(o) for o in obx_list]
    for expected in ABBOTT_GOLDEN_SEMANTICS["OBX"]:
        match = next((x for x in codes_values if x["code"] == expected["code"]), None)
        assert match is not None, f"OBX with code {expected['code']} not found"
        assert match["value"] == expected["value"]

    pid = _pid_fields(segs["PID"][0])
    assert ABBOTT_GOLDEN_SEMANTICS["PID"]["patient_id_contains"] in pid["patient_id"]
    assert ABBOTT_GOLDEN_SEMANTICS["PID"]["name_contains"] in pid["name"]

    orc = _orc_obr_placer_filler(segs["ORC"][0])
    assert orc["placer_order_id"].startswith(ABBOTT_GOLDEN_SEMANTICS["ORC"]["placer_prefix"])
    assert orc["filler_order_id"] == orc["placer_order_id"]  # filler = placer


def test_non_hl7_template_raises():
    """Passing a non-HL7 template to generate_oru_r01 raises ValueError."""
    from protocols.hl7_handler import generate_oru_r01

    non_hl7 = {
        "analyzer": {"name": "X", "manufacturer": "Y"},
        "protocol": {"type": "ASTM"},
        "identification": {},
        "fields": [{"code": "WBC", "name": "WBC", "unit": "", "type": "NUMERIC"}],
    }
    with pytest.raises(ValueError, match="protocol type must be HL7"):
        generate_oru_r01(non_hl7, deterministic=True)


def test_strict_013_profiles_have_adapter_targets():
    """Strict 013 profile files must have explicit adapter targets."""
    from profile_adapter import STRICT_013_PROFILE_FILES

    expected = {"mindray-bc5380.json", "mindray-bs200.json", "mindray-bs300.json"}
    assert set(STRICT_013_PROFILE_FILES.values()) == expected


@pytest.mark.parametrize(
    "template_name,profile_file",
    [
        ("mindray_bc5380", "mindray-bc5380.json"),
        ("mindray_bs200", "mindray-bs200.json"),
        ("mindray_bs300", "mindray-bs300.json"),
    ],
)
def test_strict_013_templates_follow_profile_obx_codes_and_units(template_name, profile_file):
    """Strict 013 templates should derive OBX code/unit semantics from profile defaults."""
    from server import _load_template

    profiles_dir = os.environ.get("ANALYZER_PROFILES_DIR")
    if not profiles_dir:
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        profiles_dir = os.path.join(repo_root, "projects", "analyzer-profiles", "hl7")
    profile_path = os.path.join(profiles_dir, profile_file)
    with open(profile_path, "r", encoding="utf-8") as fh:
        profile = json.load(fh)

    template = _load_template(template_name)
    assert template is not None

    profile_code_units = {
        (item.get("test_code") or item.get("obx_identifier") or item.get("analyzer_code")): item.get("unit", "")
        for item in profile.get("default_test_mappings", [])
        if item.get("test_code") or item.get("obx_identifier") or item.get("analyzer_code")
    }
    template_code_units = {field.get("code"): field.get("unit", "") for field in template.get("fields", [])}

    assert set(template_code_units.keys()) == set(profile_code_units.keys())
    assert template_code_units == profile_code_units
