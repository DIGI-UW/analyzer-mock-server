#!/usr/bin/env python3
"""
R3: the mock derives its assay menu (fields) from the canonical analyzer profile
referenced by a transport template's `profile` key — single source of truth. The
template carries only transport/framing + mock test fixtures (seedValues, patient);
the assay codes/LOINC/units/result_type come from projects/analyzer-profiles/*.

These tests pin that derivation against the REAL canonical profiles (resolved via
the repo fallback), so a regression in either the adapter or the profile shape fails.
"""

from profile_adapter import load_profile_backed_template


def test_no_profile_key_returns_none():
    # A template that doesn't reference a profile is not profile-backed.
    assert load_profile_backed_template("x", {"protocol": {"type": "HL7"}}) is None


def test_derives_fields_from_referenced_hl7_profile():
    transport = {
        "profile": "hl7/mindray-bc5380",
        "protocol": {"type": "HL7", "version": "2.3.1"},
        "identification": {"hl7_sending_app": "MINDRAY", "hl7_sending_facility": "BC-5380"},
        "seedValues": {"WBC": 7.5},
    }
    merged = load_profile_backed_template("mindray_bc5380", transport)
    assert merged is not None, "template referencing a profile must be profile-backed"

    codes = {f["code"] for f in merged["fields"]}
    assert "WBC" in codes, f"assay menu must come from the profile; got {codes}"

    # transport preserved (not overwritten by the profile)
    assert merged["protocol"]["type"] == "HL7"
    assert merged["identification"]["hl7_sending_app"] == "MINDRAY"

    wbc = next(f for f in merged["fields"] if f["code"] == "WBC")
    assert wbc["loinc"] == "6690-2", "LOINC must come from the profile mapping"
    # WBC has no result_type in the profile yet -> defaults to NUMERIC
    assert wbc["type"] == "NUMERIC"
    # mock-side deterministic seed applied from the template fixture
    assert wbc.get("seedValue") == 7.5


def test_fields_are_not_taken_from_template():
    # Even if the (legacy) template still carried a bogus fields list, the
    # profile-backed path must derive from the profile, not the template.
    transport = {
        "profile": "hl7/mindray-bc5380",
        "protocol": {"type": "HL7"},
        "fields": [{"code": "BOGUS", "type": "NUMERIC"}],
    }
    merged = load_profile_backed_template("mindray_bc5380", transport)
    codes = {f["code"] for f in merged["fields"]}
    assert "BOGUS" not in codes, "fields must be derived from the profile, not the template"
    assert "WBC" in codes


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
