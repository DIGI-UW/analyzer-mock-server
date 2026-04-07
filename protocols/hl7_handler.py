"""
HL7 ORU^R01 message generator for analyzer simulator.

Generates HL7 v2.x ORU^R01 (Observation Report) messages from template definitions
so OpenELIS can route and parse them via analyzer plugins (e.g. Abbott Architect).

Template identification.hl7_sending_app -> MSH-3, hl7_sending_facility -> MSH-4.
Each template field becomes one OBX segment (value type ST for TEXT/QUALITATIVE, NM for NUMERIC).
"""

import itertools
import logging
from datetime import datetime
from typing import Dict, List, Optional

from .accession import next_site_year_num, validate_accession

# Atomic counter for sequential sample IDs — never resets, always unique.
# Each template gets its own counter via _sample_counters dict.
_sample_counters: Dict[str, itertools.count] = {}
logger = logging.getLogger(__name__)


def _next_sample_id(lane_code: str, timestamp: datetime) -> str:
    """Generate a valid SiteYearNum accession number.

    Format: DEV0126{LANE}{SEQ:011d} e.g., DEV01264000000000001
    where DEV01 is the harness site prefix, 26 is the 2-digit year,
    LANE is a 2-digit lane code, and SEQ is an 11-digit zero-padded sequence.
    Total length: exactly 20 chars (OE SiteYearNum requirement).
    Counter is per-lane and never resets.
    """
    return next_site_year_num(_sample_counters, lane_code, "HL7 template testSample.id")


def generate_oru_r01(
    template: Dict,
    deterministic: bool = True,
    timestamp: Optional[datetime] = None,
    patient_id: Optional[str] = None,
    sample_id: Optional[str] = None,
    placer_order_id: Optional[str] = None,
    filler_order_id: Optional[str] = None,
    message_control_id: Optional[str] = None,
) -> str:
    """
    Generate a complete HL7 ORU^R01 message from a template.

    Args:
        template: Loaded template dict (analyzer, protocol, identification, fields, testPatient, testSample).
        deterministic: If True, use seedValue from fields; else use placeholder values.
        timestamp: Optional timestamp (defaults to now).
        patient_id: Override patient ID.
        sample_id: Override sample/accession ID.
        placer_order_id: Override ORC-2/OBR-2 placer order number.
        filler_order_id: Override ORC-3/OBR-3 filler order number.
        message_control_id: Override MSH-10 message control ID.

    Returns:
        Complete ORU^R01 message as newline-separated string (segment terminator \\r).
    """
    if template.get("protocol", {}).get("type") != "HL7":
        raise ValueError("Template protocol type must be HL7")

    if timestamp is None:
        timestamp = datetime.now()

    identification = template.get("identification", {})
    fields = template.get("fields", [])
    test_patient = template.get("testPatient", {})
    test_sample = template.get("testSample", {})

    # Prefer new HL7 identification fields, but fall back to legacy msh_sender for backward compatibility
    sending_app = identification.get("hl7_sending_app") or identification.get("msh_sender", "SIMULATOR")
    sending_facility = identification.get("hl7_sending_facility", "LAB")
    receiving_app = "OpenELIS"
    receiving_facility = "LAB"
    hl7_version = template.get("protocol", {}).get("version", "2.5.1")

    ts = timestamp.strftime("%Y%m%d%H%M%S")
    if message_control_id is None:
        message_control_id = f"SIM{timestamp.strftime('%Y%m%d%H%M%S')}"
    # Generate unique sequential sample ID like a real analyzer would.
    # Template testSample.id is the prefix; full ID includes date + sequence.
    if sample_id is None:
        prefix = test_sample.get("id")
        sample_id = _next_sample_id(prefix, timestamp)
    else:
        sample_id = validate_accession(sample_id, "HL7 sample_id override")
    if placer_order_id is None:
        placer_order_id = sample_id
    if filler_order_id is None:
        filler_order_id = sample_id  # OE's HL7 reader uses OBR-3 (filler) as accession
    if patient_id is None:
        patient_id = test_patient.get("id", "PAT001")

    pid_name = test_patient.get("name", "RAKOTO^JAO")
    pid_dob = test_patient.get("dob", "19850412")
    pid_sex = test_patient.get("sex", "F")
    panel_type = test_sample.get("type", "IMMUNO^IMMUNOASSAY PANEL")
    if "^" in panel_type:
        panel_code, panel_label = panel_type.split("^", 1)
    else:
        panel_code = panel_type
        panel_label = panel_type
    obr_filler = f"^^^{panel_code}^{panel_label}"

    segments: List[str] = []

    # MSH|^~\&|SENDING_APP|SENDING_FACILITY|RECV_APP|RECV_FAC|datetime||ORU^R01|control_id|P|version||||||||
    msh = "|".join([
        "MSH", "^~\\&", sending_app, sending_facility,
        receiving_app, receiving_facility, ts, "", "ORU^R01",
        message_control_id, "P", hl7_version, "", "", "", "", "", "", ""
    ])
    segments.append(msh)

    # PID|1||PATIENT_ID^^^HOSPITAL||Last^First^Middle||DOB|Sex|...
    pid = "|".join([
        "PID", "1", "", f"{patient_id}^^^HOSPITAL", "", pid_name, "", pid_dob, pid_sex,
        "", "", "", "", "", "", "", "", "", "", "", ""
    ])
    segments.append(pid)

    # ORC|RE|placer|filler|...
    orc = "|".join([
        "ORC", "RE", placer_order_id, filler_order_id,
        "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""
    ])
    segments.append(orc)

    # OBR|1|placer|filler|^^^panel_code^panel_label|||datetime|...
    # OBR-4 is Universal Service Identifier (test/panel info), not a sequence number
    obr = "|".join([
        "OBR", "1", placer_order_id, filler_order_id,
        obr_filler,
        "", "", "", ts, "", "", "", "", "", "", "", "", "", "", "", "F", "", "", "", "", "", "", ""
    ])
    segments.append(obr)

    for seq, field in enumerate(fields, start=1):
        obx = _obx_segment(seq, field, deterministic)
        segments.append(obx)

    return "\r".join(segments) + "\r"


def _obx_segment(seq: int, field: Dict, deterministic: bool) -> str:
    """Build one OBX segment. Value type ST for TEXT/QUALITATIVE, NM for NUMERIC."""
    code = field.get("code", f"TEST{seq}")
    name = field.get("name", code)
    unit = field.get("unit", "")
    field_type = field.get("type", "NUMERIC")

    if field_type == "NUMERIC":
        if deterministic and "seedValue" in field:
            raw = field["seedValue"]
            value = str(int(raw)) if isinstance(raw, float) and raw == int(raw) else str(raw)
        else:
            value = "0"
    elif field_type == "QUALITATIVE":
        possible = field.get("possibleValues", ["NEGATIVE", "POSITIVE"])
        value = possible[0] if possible else "NEGATIVE"
    else:
        value = field.get("seedValue", f"Result for {code}")
        if not isinstance(value, str):
            value = str(value)

    value_type = "NM" if field_type == "NUMERIC" else "ST"
    # OBX|seq|value_type|^^^code^name||value|unit||N|||F||||||
    obx_id = f"^^^{code}^{name}"
    parts = ["OBX", str(seq), value_type, obx_id, "", value, unit, "", "N", "", "", "F", "", "", "", "", ""]
    return "|".join(parts)


class HL7Handler:
    """M4-compatible wrapper: generate(template, **kwargs) delegates to generate_oru_r01."""

    def generate(self, template, deterministic=True, **kwargs):
        return generate_oru_r01(template, deterministic=deterministic, **kwargs)
