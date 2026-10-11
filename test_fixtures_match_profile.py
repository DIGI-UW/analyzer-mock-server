"""Every manufacturer fixture is declared by the pinned baseline profile, and every declared
outcome has a fixture.

The profile is the Bridge's shipped GeneXpert baseline profile named below, read from
ANALYZER_BRIDGE_PROFILES_DIR (the Bridge's src/main/resources/analyzer-profiles).
"""

import json
import os

import pytest

import fixture_messages

pytestmark = pytest.mark.needs_bridge

PROFILE_FILE = "cepheid-genexpert-astm.json"
FAMILY = "genexpert"
# 302-7279 prints the single-result assay's error and invalid examples under the two-assay panel's
# code, so SARSCOV2_3's values are shown only through SARSCOV2's.
SHOWN_THROUGH = {"SARSCOV2_3": "SARSCOV2"}


def _profile():
    directory = os.environ["ANALYZER_BRIDGE_PROFILES_DIR"]
    with open(os.path.join(directory, PROFILE_FILE), encoding="utf-8") as handle:
        return json.load(handle)


def _declared(profile):
    """Records and values the profile declares: {(code, sub_identity): values accepted}."""
    records, values = {}, set()
    for test in profile["default_test_mappings"]:
        if "components" not in test and test.get("result_type") == "text":
            continue  # MTB and RIF declare no vendor-documented values or records
        code = test["test_code"]

        def texts(owner):
            accepted = set(owner.get("values", [])) | set(owner.get("run_failure_values", []))
            for translated in owner.get("translations", {}).values():
                accepted.update(translated)
            return accepted

        records[(code, "")] = texts(test)
        values.update((code, "", value) for value in test.get("values", []))
        for component in test.get("components", []):
            if "sub_identity" not in component:
                continue
            sub = component["sub_identity"]
            records[(code, sub)] = texts(component)
            values.update((code, sub, value) for value in component.get("values", []))
    return records, values


def _records_of(message):
    for line in message.strip().split("\n"):
        if not line.startswith("R|"):
            continue
        fields = line.split("|")
        test_id = fields[2].split("^")
        data = (fields[3].split("^") + ["", ""])[:2] if len(fields) > 3 else ["", ""]
        main = bool(test_id[4].strip()) if len(test_id) > 4 else False
        analyte = "" if main else (test_id[6].strip() if len(test_id) > 6 else "")
        complement = test_id[7].strip() if len(test_id) > 7 else ""
        sub = analyte + ("&" + complement if complement else "")
        yield test_id[3], sub, data[0].strip(), data[1].strip()


def _order_fields(message):
    """The O record's fields, keyed the way the profile names them (O.12 is index 11)."""
    order = next(line for line in message.split("\n") if line.startswith("O|")).split("|")
    return {f"O.{index + 1}": value for index, value in enumerate(order)}


def _matches_a_control_rule(profile, message, specimen_id):
    fields = _order_fields(message)
    for rule in profile["controlResultRecognition"]["rules"].values():
        kind, operand = rule["ruleType"], rule["operand"].lower()
        if kind == "FIELD_EQUALS" and fields.get(rule["targetField"], "").lower() == operand:
            return True
        if kind == "FIELD_CONTAINS" and operand in fields.get(rule["targetField"], "").lower():
            return True
        if kind == "SPECIMEN_ID_PREFIX" and specimen_id.lower().startswith(operand):
            return True
        if kind not in ("FIELD_EQUALS", "FIELD_CONTAINS", "SPECIMEN_ID_PREFIX"):
            raise AssertionError(f"rule type {kind} is not evaluated by this test")
    return False


def test_control_outcomes_match_the_profiles_control_rules_and_patient_outcomes_do_not():
    profile = _profile()
    controls = 0
    for assay, outcomes in fixture_messages.list_fixtures(FAMILY).items():
        for outcome in outcomes:
            message = fixture_messages.render(FAMILY, assay, outcome, sample_id="S-1")
            matched = _matches_a_control_rule(profile, message, "S-1")
            assert matched == outcome.startswith("control-"), f"{assay}/{outcome}"
            controls += matched
    assert controls > 0


def test_every_record_is_declared_and_every_declared_value_is_shown():
    records, values = _declared(_profile())
    undeclared, shown_records, shown_values = [], set(), set()
    for assay, outcomes in fixture_messages.list_fixtures(FAMILY).items():
        for outcome in outcomes:
            message = fixture_messages.render(FAMILY, assay, outcome, sample_id="S")
            for code, sub, call, number in _records_of(message):
                if not call and not number:
                    continue  # a record that says nothing is not a result
                if (code, sub) not in records:
                    undeclared.append(f"{assay}/{outcome} {code}|{sub}")
                    continue
                shown_records.add((code, sub))
                if call:
                    if call in records[(code, sub)]:
                        shown_values.add((code, sub, call))
                        if code in SHOWN_THROUGH.values():
                            shown_values.add(("SARSCOV2_3", sub, call))
                    else:
                        undeclared.append(f"{assay}/{outcome} {code}|{sub} value {call}")
    assert undeclared == []
    assert sorted(set(records) - shown_records) == []
    assert sorted(values - shown_values) == []
