import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

import api
import server
from profile_adapter import ProfileResolutionError, load_profile_backed_template


TEMPLATES_DIR = Path(__file__).parent / "templates"


def _write_profile(path, profile_id="priority-profile", revision=1):
    path.write_text(
        json.dumps(
            {
                "profileMeta": {
                    "id": profile_id,
                    "displayName": "Priority Profile",
                },
                "analyzer_name": "Priority Analyzer",
                "manufacturer": "Example Manufacturer",
                "model": "Example Model",
                "category": "MOLECULAR",
                "protocol": {
                    "name": "ASTM",
                    "version": "LIS2-A2",
                },
                "catalog": {
                    "revision": revision,
                    "revisionFingerprint": "sha256:test-fingerprint",
                },
                "default_test_mappings": [
                    {
                        "test_code": "TEST-1",
                        "loinc": "1234-5",
                        "result_type": "quantitative",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _template(revision=1):
    return {
        "profileRef": {"profileId": "priority-profile", "revision": revision},
    }


def test_exact_bridge_profile_ref_resolves(tmp_path, monkeypatch):
    _write_profile(tmp_path / "profile.json")
    monkeypatch.setenv("ANALYZER_BRIDGE_PROFILES_DIR", str(tmp_path))

    merged = load_profile_backed_template("priority", _template())

    assert merged["fields"][0]["code"] == "TEST-1"
    assert merged["analyzer"] == {
        "name": "Priority Analyzer",
        "model": "Example Model",
        "manufacturer": "Example Manufacturer",
        "category": "MOLECULAR",
    }
    assert merged["protocol"] == {"type": "ASTM", "version": "LIS2-A2"}
    assert merged["resolvedProfile"] == {
        "profileId": "priority-profile",
        "revision": 1,
        "revisionFingerprint": "sha256:test-fingerprint",
    }


def test_unversioned_profile_key_is_not_a_profile_reference():
    assert (
        load_profile_backed_template(
            "unsupported",
            {"profile": "hl7/unsupported", "protocol": {"type": "HL7"}},
        )
        is None
    )


def test_template_schema_rejects_unversioned_profile_reference():
    with open("templates/schema.json", encoding="utf-8") as schema_file:
        validator = Draft7Validator(json.load(schema_file))

    errors = list(
        validator.iter_errors(
            {
                "analyzer": {
                    "name": "Unsupported",
                    "model": "Unsupported",
                    "manufacturer": "Unsupported",
                },
                "protocol": {"type": "HL7"},
                "profile": "hl7/unsupported",
            }
        )
    )

    assert errors


@pytest.mark.parametrize(
    "template_path",
    sorted(path for path in TEMPLATES_DIR.glob("*.json") if path.name != "schema.json"),
    ids=lambda path: path.name,
)
def test_template_conforms_to_schema(template_path):
    with (TEMPLATES_DIR / "schema.json").open(encoding="utf-8") as schema_file:
        validator = Draft7Validator(json.load(schema_file))
    with template_path.open(encoding="utf-8") as template_file:
        errors = list(validator.iter_errors(json.load(template_file)))

    assert not errors, [error.message for error in errors]


def test_unknown_bridge_profile_revision_fails_closed(tmp_path, monkeypatch):
    _write_profile(tmp_path / "profile.json", revision=1)
    monkeypatch.setenv("ANALYZER_BRIDGE_PROFILES_DIR", str(tmp_path))

    with pytest.raises(ProfileResolutionError, match="priority-profile@2"):
        load_profile_backed_template("priority", _template(revision=2))


def test_duplicate_bridge_profile_revision_fails_closed(tmp_path, monkeypatch):
    _write_profile(tmp_path / "first.json")
    _write_profile(tmp_path / "second.json")
    monkeypatch.setenv("ANALYZER_BRIDGE_PROFILES_DIR", str(tmp_path))

    with pytest.raises(ProfileResolutionError, match="multiple documents"):
        load_profile_backed_template("priority", _template())


def test_exact_profile_ref_rejects_profile_owned_field_overrides(tmp_path, monkeypatch):
    _write_profile(tmp_path / "profile.json")
    monkeypatch.setenv("ANALYZER_BRIDGE_PROFILES_DIR", str(tmp_path))
    template = _template()
    template["fieldOverrides"] = {"TEST-1": {"loinc": "9999-9"}}

    with pytest.raises(ProfileResolutionError, match="profile-owned fields"):
        load_profile_backed_template("priority", template)


@pytest.mark.parametrize(
    "duplicate",
    [
        {"analyzer": {"name": "Copied"}},
        {"protocol": {"type": "ASTM"}},
        {"fields": []},
        {"file_config": {"format": "CSV"}},
        {"identification": {"file_pattern": "*.csv"}},
    ],
)
def test_exact_profile_ref_rejects_profile_owned_template_data(
    duplicate, tmp_path, monkeypatch
):
    _write_profile(tmp_path / "profile.json")
    monkeypatch.setenv("ANALYZER_BRIDGE_PROFILES_DIR", str(tmp_path))
    template = _template()
    template.update(duplicate)

    with pytest.raises(ProfileResolutionError, match="duplicates profile-owned fields"):
        load_profile_backed_template("priority", template)


def test_exact_qualitative_profile_requires_explicit_result_values(tmp_path, monkeypatch):
    path = tmp_path / "profile.json"
    _write_profile(path)
    profile = json.loads(path.read_text(encoding="utf-8"))
    profile["default_test_mappings"][0]["result_type"] = "qualitative"
    path.write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.setenv("ANALYZER_BRIDGE_PROFILES_DIR", str(tmp_path))

    with pytest.raises(ProfileResolutionError, match="must declare result values"):
        load_profile_backed_template("priority", _template())


@pytest.mark.parametrize(
    "runtime_loader,template_name",
    [
        (api._load_template, "quantstudio7"),
        (server._load_template, "genexpert_astm"),
    ],
)
def test_runtime_does_not_fall_back_when_exact_profile_is_unavailable(
    runtime_loader, template_name, tmp_path, monkeypatch
):
    monkeypatch.setenv("ANALYZER_BRIDGE_PROFILES_DIR", str(tmp_path))

    assert runtime_loader(template_name) is None
