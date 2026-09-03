import os
from pathlib import Path

import pytest

from profile_adapter import load_profile_backed_template
from template_loader import TemplateLoader


PRIORITY_PROFILE_REFS = {
    "genexpert_astm": {"profileId": "genexpert-astm", "revision": 3},
    "hain_fluorocycler": {"profileId": "fluorocycler-xt", "revision": 3},
    "quantstudio5": {"profileId": "quantstudio", "revision": 3},
    "quantstudio7": {"profileId": "quantstudio", "revision": 3},
}


@pytest.mark.parametrize("template_name,profile_ref", PRIORITY_PROFILE_REFS.items())
def test_priority_template_pins_exact_bridge_profile_revision(template_name, profile_ref):
    template = TemplateLoader().load_template(template_name)

    assert template["profileRef"] == profile_ref
    assert "profile" not in template
    assert "analyzer" not in template
    assert "protocol" not in template
    assert "fields" not in template
    assert "fileFormat" not in template
    assert "file_config" not in template
    assert "columns" not in template
    assert "file_pattern" not in template.get("identification", {})
    assert "perFileTestCode" not in template.get("fixture", {})


@pytest.mark.parametrize("template_name,profile_ref", PRIORITY_PROFILE_REFS.items())
def test_priority_template_loads_exact_bridge_profile(template_name, profile_ref):
    profiles_dir = os.environ.get("ANALYZER_BRIDGE_PROFILES_DIR")
    assert profiles_dir, "set ANALYZER_BRIDGE_PROFILES_DIR to the Bridge profile catalog"
    assert Path(profiles_dir).is_dir(), profiles_dir

    template = TemplateLoader().load_template(template_name)
    merged = load_profile_backed_template(template_name, template)

    assert merged is not None
    assert merged["profileRef"] == profile_ref
    assert merged["resolvedProfile"]["profileId"] == profile_ref["profileId"]
    assert merged["resolvedProfile"]["revision"] == profile_ref["revision"]
    assert merged["resolvedProfile"]["revisionFingerprint"].startswith("sha256:")
    assert merged["analyzer"]["name"]
    assert merged["analyzer"]["manufacturer"]
    assert merged["protocol"]["type"] in {"ASTM", "FILE"}
    assert merged["fields"]
