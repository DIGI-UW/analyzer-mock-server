"""Messages replayed from a manufacturer's own examples, never generated from a profile.

A fixture is one message the vendor prints in its LIS documentation, kept as it is except for
placeholders for what a caller chooses:

    {sample_id}                      the specimen identifier
    {patient_id}, {patient_name}     the patient the instrument reports
    {instrument_code:<profile code>} the code this instrument sends for a profile test code

Fixtures live in fixtures/<family>/<assay>/<outcome>.astm, or .hl7 for an HL7 instrument, one
record or segment per line.
"""

import os
import re
from typing import Dict, List, Optional

FIXTURE_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PLACEHOLDER = re.compile(r"\{(\w+)(?::([^}]+))?\}")
_EXTENSIONS = (".astm", ".hl7")


class FixtureNotFound(LookupError):
    pass


def _path(family: str, assay: str, outcome: str) -> str:
    for part in (family, assay, outcome):
        if not _SAFE.match(part):
            raise FixtureNotFound(f"No such fixture: {family}/{assay}/{outcome}")
    for extension in _EXTENSIONS:
        path = os.path.join(FIXTURE_ROOT, family, assay, outcome + extension)
        if os.path.isfile(path):
            return path
    raise FixtureNotFound(f"No such fixture: {family}/{assay}/{outcome}")


def is_hl7(family: str, assay: str, outcome: str) -> bool:
    """Whether the fixture is an HL7 message rather than ASTM records."""
    return _path(family, assay, outcome).endswith(".hl7")


def list_fixtures(family: str) -> Dict[str, List[str]]:
    """Each assay a family has fixtures for, with its outcomes."""
    if not _SAFE.match(family) or not os.path.isdir(os.path.join(FIXTURE_ROOT, family)):
        raise FixtureNotFound(f"No such fixture family: {family}")
    root = os.path.join(FIXTURE_ROOT, family)
    return {
        assay: sorted(os.path.splitext(name)[0] for name in os.listdir(os.path.join(root, assay))
                      if name.endswith(_EXTENSIONS))
        for assay in sorted(os.listdir(root)) if os.path.isdir(os.path.join(root, assay))
    }


def raw(family: str, assay: str, outcome: str) -> str:
    with open(_path(family, assay, outcome), encoding="utf-8") as handle:
        return handle.read()


def render(family: str, assay: str, outcome: str, sample_id: str, patient_id: str = "",
           patient_name: str = "^^^^", instrument_codes: Optional[Dict[str, str]] = None) -> str:
    """The fixture with its placeholders filled, records separated by newlines."""
    values = {"sample_id": sample_id, "patient_id": patient_id, "patient_name": patient_name}
    codes = instrument_codes or {}

    def fill(match):
        name, argument = match.group(1), match.group(2)
        if name == "instrument_code" and argument:
            return codes.get(argument, argument)
        if name in values and argument is None:
            return values[name]
        raise ValueError(f"Unknown fixture placeholder: {match.group(0)}")

    return _PLACEHOLDER.sub(fill, raw(family, assay, outcome))
