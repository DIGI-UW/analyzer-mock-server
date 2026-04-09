#!/usr/bin/env python3
"""
Thin adapter that derives strict 013 HL7 mock templates from analyzer profiles.
"""

import json
import os
from typing import Any, Dict, Optional


STRICT_013_PROFILE_FILES = {
    "mindray_bc5380": "mindray-bc5380.json",
    "mindray_bs200": "mindray-bs200.json",
    "mindray_bs300": "mindray-bs300.json",
}

STRICT_013_OVERLAYS: Dict[str, Dict[str, Any]] = {
    "mindray_bc5380": {
        "model": "BC-5380",
        "sending_facility": "BC-5380",
        "testPatient": {"id": "PAT001", "name": "DOE^JOHN", "sex": "M", "dob": "19800115"},
        "testSample": {"id": "40", "type": "CBC^COMPLETE BLOOD COUNT"},
        "seed_values": {
            "WBC": 7.5,
            "RBC": 4.82,
            "HGB": 14.2,
            "HCT": 42.0,
            "MCV": 90.1,
            "MCH": 29.4,
            "MCHC": 33.1,
            "PLT": 250.0,
            "NEUT": 57.0,
            "LYMPH": 33.0,
            "MONO": 6.0,
            "EOS": 3.0,
            "BASO": 1.0,
        },
    },
    "mindray_bs200": {
        "model": "BS-200",
        "sending_facility": "BS-200",
        "testPatient": {"id": "PAT003", "name": "RABE^FARA", "sex": "M", "dob": "19750810"},
        "testSample": {"id": "41", "type": "CHEM^CHEMISTRY PANEL"},
        "seed_values": {
            "GLU": 92,
            "CREA": 1.1,
            "ALT": 32,
            "AST": 28,
            "ALB": 4.2,
            "TP": 7.3,
            "TBIL": 1.0,
            "UREA": 15,
        },
    },
    "mindray_bs300": {
        "model": "BS-300",
        "sending_facility": "BS-300",
        "testPatient": {"id": "PAT004", "name": "NIRINA^HERY", "sex": "F", "dob": "19880305"},
        "testSample": {"id": "42", "type": "CHEM^CHEMISTRY PANEL"},
        "seed_values": {
            "GLU": 88,
            "CREA": 0.8,
            "ALT": 19,
            "AST": 24,
            "ALB": 4.1,
            "TP": 7.0,
            "TBIL": 0.9,
            "UREA": 12,
        },
    },
}


def load_strict_013_profile_template(template_name: str) -> Optional[Dict[str, Any]]:
    """Return profile-derived template for strict 013 analyzers, else None."""
    profile_file = STRICT_013_PROFILE_FILES.get(template_name)
    if not profile_file:
        return None

    # In Docker, profiles should be mounted at /app/profiles/hl7/.
    # Locally, resolve from repo root (3 dirname calls from tools/analyzer-mock-server/file.py).
    profiles_dir = os.environ.get("ANALYZER_PROFILES_DIR")
    if not profiles_dir:
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        profiles_dir = os.path.join(repo_root, "projects", "analyzer-profiles", "hl7")
    profile_path = os.path.join(profiles_dir, profile_file)
    with open(profile_path, "r", encoding="utf-8") as fh:
        profile = json.load(fh)

    overlay = STRICT_013_OVERLAYS.get(template_name, {})
    seed_values = overlay.get("seed_values", {})

    fields = []
    for mapping in profile.get("default_test_mappings", []):
        code = mapping.get("test_code") or mapping.get("obx_identifier") or mapping.get("analyzer_code")
        if not code:
            continue
        field = {
            "code": code,
            "name": mapping.get("test_name_hint", code).upper(),
            "unit": mapping.get("unit", ""),
            "type": "NUMERIC",
            "seedValue": seed_values.get(code, 0),
        }
        fields.append(field)

    protocol = profile.get("protocol", {})
    sending_app = _derive_sending_app(profile.get("msh3_pattern"), profile.get("manufacturer", "SIMULATOR"))

    return {
        "analyzer": {
            "name": profile.get("analyzer_name", template_name),
            "model": overlay.get("model", profile.get("analyzer_name", template_name)),
            "manufacturer": profile.get("manufacturer", "Unknown"),
            "category": profile.get("category", "UNKNOWN"),
        },
        "protocol": {
            "type": "HL7",
            "version": protocol.get("version", "2.3.1"),
            "transport": "TCP",
        },
        "identification": {
            "hl7_sending_app": sending_app,
            "hl7_sending_facility": overlay.get("sending_facility", "LAB"),
        },
        "fields": fields,
        "testPatient": overlay.get("testPatient", {"id": "PAT001", "name": "DOE^JANE", "sex": "F", "dob": "19860720"}),
        "testSample": overlay.get("testSample", {"id": "43", "type": "CHEM^CHEMISTRY PANEL"}),
    }


def _derive_sending_app(msh3_pattern: Optional[str], manufacturer: str) -> str:
    if not msh3_pattern:
        return manufacturer.upper()
    token = msh3_pattern.split("|")[0].split(".*")[0].split("^")[0].strip()
    return token or manufacturer.upper()
