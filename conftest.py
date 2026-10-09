"""Test setup shared by every module.

The file templates generate from a Bridge profile they pin by ID and revision. The mock's
own tests use the copies in test_profiles. OpenELIS runs the tests marked needs_bridge with
ANALYZER_BRIDGE_PROFILES_DIR set to its Bridge submodule, which checks those copies, the
templates and the fixtures against the profiles the Bridge actually ships.
"""

import os

import pytest

BRIDGE_PROFILES_SUPPLIED = bool(os.environ.get("ANALYZER_BRIDGE_PROFILES_DIR"))
os.environ.setdefault(
    "ANALYZER_BRIDGE_PROFILES_DIR", os.path.join(os.path.dirname(__file__), "test_profiles"))


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "needs_bridge: needs the Bridge's own profiles; runs in OpenELIS CI")


def pytest_collection_modifyitems(config, items):
    if BRIDGE_PROFILES_SUPPLIED:
        return
    skip = pytest.mark.skip(reason="needs the Bridge's profiles; OpenELIS runs it with both")
    for item in items:
        if "needs_bridge" in item.keywords:
            item.add_marker(skip)
