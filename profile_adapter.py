#!/usr/bin/env python3
"""
Profile-backed template derivation.

The canonical analyzer profile (projects/analyzer-profiles/{astm,hl7,file}/*.json)
is the single source of truth for an analyzer's assay menu: each
`default_test_mappings` entry carries the analyzer test_code, its LOINC, unit, and
(once enriched) `result_type` + value domain. The mock derives its result `fields`
from that profile.

A "transport template" (templates/*.json) carries only the messaging/framing
mechanics (protocol, astm_config, HL7 sending app/facility, file format) plus
mock-side test fixtures (deterministic `seedValues` keyed by code, testPatient,
testSample). It references its profile via a `profile` key, e.g.
`"profile": "hl7/mindray-bc5380"`.

`load_profile_backed_template` merges the two: profile → fields, template →
everything else. This eliminates the historical drift where assay coverage was
hand-maintained in templates separately from the profiles.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Default qualitative vocabulary when a profile mapping declares a qualitative
# result_type but no explicit `values`. Aligns (case-insensitively) with the
# common OE2 dictionary entries Detected/Not Detected.
DEFAULT_QUALITATIVE_VALUES = ["DETECTED", "NOT DETECTED"]

_QUALITATIVE_TYPES = {"qualitative", "ordinal", "nominal", "ord", "nom", "qual"}


def _profiles_root() -> str:
    """Root of the canonical profiles tree (contains astm/ hl7/ file/ subdirs).

    In the deployed mock this is mounted and pointed at by ANALYZER_PROFILES_DIR.
    Locally (tests/dev) it resolves to <repo>/projects/analyzer-profiles — three
    dirnames up from this file: tools/analyzer-mock-server/profile_adapter.py.
    """
    env = os.environ.get("ANALYZER_PROFILES_DIR")
    if env:
        return env
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(repo_root, "projects", "analyzer-profiles")


def _default_negative(values: List[str]) -> str:
    """Pick a sensible 'no-finding' default from a qualitative vocabulary."""
    for v in values:
        u = v.upper()
        if "NOT DETECTED" in u or u in ("NEGATIVE", "NEG", "NOT DETECTED"):
            return v
    return values[-1] if values else "NOT DETECTED"


def load_profile_backed_template(
    template_name: str, transport_template: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Derive a full mock template by merging the transport template with the
    assay `fields` built from its referenced canonical profile.

    Returns the merged template, or None if the transport template declares no
    `profile` (i.e. it is not yet profile-backed and the caller should fall back).
    """
    profile_ref = transport_template.get("profile")
    if not profile_ref:
        return None

    profile_path = os.path.join(_profiles_root(), f"{profile_ref}.json")
    with open(profile_path, "r", encoding="utf-8") as fh:
        profile = json.load(fh)

    seed_values = transport_template.get("seedValues", {})

    fields: List[Dict[str, Any]] = []
    for mapping in profile.get("default_test_mappings", []):
        code = (
            mapping.get("test_code")
            or mapping.get("obx_identifier")
            or mapping.get("analyzer_code")
        )
        if not code:
            continue
        unit = mapping.get("unit", "")
        result_type = str(mapping.get("result_type", "")).strip().lower()
        field: Dict[str, Any] = {
            "code": code,
            "loinc": mapping.get("loinc"),
            "unit": unit,
            "name": mapping.get("test_name_hint", code),
        }
        if result_type in _QUALITATIVE_TYPES:
            values = mapping.get("values") or DEFAULT_QUALITATIVE_VALUES
            field["type"] = "QUALITATIVE"
            field["possibleValues"] = values
            seed_q = seed_values.get(code)
            field["seedQualitative"] = seed_q if seed_q is not None else _default_negative(values)
        else:
            # Default to NUMERIC when result_type is absent or quantitative.
            field["type"] = "NUMERIC"
            if code in seed_values:
                field["seedValue"] = seed_values[code]
        fields.append(field)

    merged: Dict[str, Any] = dict(transport_template)
    merged["fields"] = fields
    # Fill the analyzer block from the profile if the transport template omitted it.
    if "analyzer" not in merged:
        merged["analyzer"] = {
            "name": profile.get("analyzer_name", template_name),
            "manufacturer": profile.get("manufacturer", "Unknown"),
            "category": profile.get("category", "UNKNOWN"),
        }
    logger.info(
        "Profile-backed template '%s' from %s: %d assays",
        template_name, profile_ref, len(fields),
    )
    return merged
