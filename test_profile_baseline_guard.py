"""
Consumer-contract guard for the canonical analyzer profile baseline.

Two levels, both must hold and block in CI (seam, not stub):

  1. SCHEMA CONFORMANCE — every canonical profile validates against
     projects/analyzer-profiles/schema/analyzer-defaults-1.0.schema.json
     (the contract: required profileMeta{id,version,displayName,confidence},
     category, protocol, configDefaults; socket profiles need analyzer_name/
     manufacturer/transport + non-empty default_test_mappings; FILE profiles
     need supported_extensions/column_mapping; every mapping has non-empty
     test_code+loinc — the exact keys OE2 AnalyzerService parses).

  2. REAL-ADAPTER LOAD — every profile-backed mock template loads through the
     actual profile_adapter.load_profile_backed_template (the real reader the
     mock uses) and yields assay fields that each carry a LOINC. This proves the
     normalized profiles are consumable by the real code path, not just that a
     JSON validator likes them.

This is the RED->GREEN gate for the profile-format normalization workstream.
"""

import glob
import json
import os

import pytest
from jsonschema import Draft7Validator

import profile_adapter

PROFILES_ROOT = profile_adapter._profiles_root()
SCHEMA_PATH = os.path.join(PROFILES_ROOT, "schema", "analyzer-defaults-1.0.schema.json")
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


def _load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _profile_files():
    """All canonical profile JSONs (astm/ hl7/ file/), excluding the schema dir."""
    out = []
    for path in sorted(glob.glob(os.path.join(PROFILES_ROOT, "*", "*.json"))):
        if os.sep + "schema" + os.sep in path:
            continue
        out.append(path)
    return out


def test_baseline_corpus_present():
    """Fail LOUDLY (never silently skip) if the profile corpus/schema isn't on
    disk — this guard is meaningful only in the integrated checkout (OE2 with the
    mock submodule) or when ANALYZER_PROFILES_DIR points at the profiles."""
    assert os.path.isfile(SCHEMA_PATH), (
        f"canonical schema not found at {SCHEMA_PATH}; run from the integrated "
        f"checkout or set ANALYZER_PROFILES_DIR"
    )
    assert _profile_files(), f"no canonical profiles found under {PROFILES_ROOT}"


def _profile_backed_templates():
    names = []
    for path in sorted(glob.glob(os.path.join(TEMPLATES_DIR, "*.json"))):
        if os.path.basename(path) == "schema.json":
            continue
        if _load(path).get("profile"):
            names.append(os.path.basename(path))
    return names


@pytest.fixture(scope="module")
def validator():
    return Draft7Validator(_load(SCHEMA_PATH))


@pytest.mark.parametrize(
    "profile_path", _profile_files(),
    ids=[os.path.relpath(p, PROFILES_ROOT) for p in _profile_files()],
)
def test_profile_conforms_to_canonical_schema(profile_path, validator):
    profile = _load(profile_path)
    errors = sorted(validator.iter_errors(profile), key=lambda e: list(e.path))
    detail = "\n".join(f"    {list(e.path)}: {e.message}" for e in errors)
    assert not errors, (
        f"{os.path.relpath(profile_path, PROFILES_ROOT)} violates canonical schema:\n{detail}"
    )


@pytest.mark.parametrize("template_name", _profile_backed_templates())
def test_profile_backed_template_loads_through_real_adapter(template_name):
    tpl = _load(os.path.join(TEMPLATES_DIR, template_name))
    merged = profile_adapter.load_profile_backed_template(template_name, tpl)
    assert merged is not None, f"{template_name}: adapter returned None (no profile?)"
    fields = merged.get("fields", [])
    assert fields, f"{template_name}: profile yielded zero assay fields"
    missing_loinc = [f.get("code") for f in fields if not f.get("loinc")]
    assert not missing_loinc, f"{template_name}: assay fields missing LOINC: {missing_loinc}"
