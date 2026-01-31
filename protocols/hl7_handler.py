"""
HL7 v2.x ORU^R01 protocol handler (M4).

Generates valid ORU^R01 result messages for simulator.
Reference: specs/011-madagascar-analyzer-integration, tasks T074–T078.
"""

import logging
import random
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from .base_handler import BaseHandler

logger = logging.getLogger(__name__)

SEGMENT_TERM = "\r"


def _value_type(field: Dict[str, Any]) -> str:
    t = (field.get("type") or "NUMERIC").upper()
    if t == "NUMERIC":
        return "NM"
    if t == "QUALITATIVE":
        return "CE"
    return "ST"


def _normalize_fields(template: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for f in template.get("fields", []):
        name = f.get("name", "Unknown")
        code = f.get("code", name)
        out.append({
            "name": name,
            "displayName": f.get("displayName", name),
            "code": code,
            "type": f.get("type", "NUMERIC"),
            "unit": f.get("unit") or "",
            "normalRange": f.get("normalRange", ""),
            "possibleValues": f.get("possibleValues"),
        })
    return out


def _random_value(field: Dict[str, Any]) -> str:
    typ = (field.get("type") or "NUMERIC").upper()
    unit = field.get("unit") or ""
    normal_range = field.get("normalRange", "")

    if typ == "NUMERIC":
        if normal_range:
            try:
                if "-" in normal_range:
                    low, high = map(float, normal_range.split("-"))
                    return str(round(random.uniform(low, high), 2))
                if normal_range.startswith("<"):
                    max_v = float(normal_range[1:])
                    return str(round(random.uniform(0, max_v * 0.9), 2))
                if normal_range.startswith(">"):
                    min_v = float(normal_range[1:])
                    return str(round(random.uniform(min_v * 1.1, min_v * 2), 2))
            except Exception:
                pass
        return str(round(random.uniform(1, 100), 2))
    if typ == "QUALITATIVE":
        vals = field.get("possibleValues", ["NEGATIVE", "POSITIVE"])
        return random.choice(vals)
    return f"Result {field.get('name', '')}"


class HL7Handler(BaseHandler):
    """HL7 v2.x ORU^R01 message generation."""

    protocol_type = "HL7"

    def generate(self, template: Dict[str, Any], **kwargs) -> str:
        if not self.validate_template(template):
            raise ValueError("Invalid template: missing analyzer or fields")

        ident = template.get("identification") or {}
        msh_sender = ident.get("msh_sender") or "SIMULATOR"
        anal = template["analyzer"]
        manufacturer = anal.get("manufacturer", "")
        model = anal.get("model", "")
        name = anal.get("name", "Mock")

        patient_id = kwargs.get("patient_id")
        sample_id = kwargs.get("sample_id")
        patient_name = kwargs.get("patient_name")
        patient_dob = kwargs.get("patient_dob")
        patient_sex = kwargs.get("patient_sex")
        tests = kwargs.get("tests")

        now = datetime.now()
        ts = now.strftime("%Y%m%d%H%M%S")
        msg_id = str(uuid.uuid4())[:8].upper()

        if not patient_id:
            patient_id = f"PAT-{now.strftime('%Y%m%d')}-{random.randint(100, 999)}"
        if not sample_id:
            sample_id = f"S{now.strftime('%Y%m%d')}-{random.randint(1000, 9999)}"
        if not patient_name:
            fn = ["John", "Mary", "James", "Sarah"]
            ln = ["Smith", "Johnson", "Williams"]
            patient_name = f"{random.choice(ln)}^{random.choice(fn)}"
        if not patient_dob:
            y, m, d = random.randint(1950, 2000), random.randint(1, 12), random.randint(1, 28)
            patient_dob = f"{y}{m:02d}{d:02d}"
        if not patient_sex:
            patient_sex = random.choice(["M", "F"])

        fields = _normalize_fields(template)
        if tests:
            fields = [f for f in fields if f.get("name") in tests or f.get("code") in tests]
        if not fields:
            fields = _normalize_fields(template)

        segments = []

        msh = (
            f"MSH|^~\\&|{msh_sender}|{manufacturer}|OPENELIS|LAB|{ts}||ORU^R01|{msg_id}|P|2.5||||||"
        )
        segments.append(msh)

        pid = f"PID|1||{patient_id}^^^^^MR||{patient_name}||{patient_dob}|{patient_sex}"
        segments.append(pid)

        obr = f"OBR|1|{sample_id}|{sample_id}|{model}^{model}|||{ts}"
        segments.append(obr)

        for i, f in enumerate(fields, 1):
            vt = _value_type(f)
            code = f.get("code", f.get("name", ""))
            disp = f.get("displayName", f.get("name", ""))
            val = _random_value(f)
            unit = f.get("unit", "")
            ref = f.get("normalRange", "")
            obx = f"OBX|{i}|{vt}|{code}^{disp}||{val}|{unit}|{ref}|||F|||{ts}"
            segments.append(obx)

        return SEGMENT_TERM.join(segments) + SEGMENT_TERM
