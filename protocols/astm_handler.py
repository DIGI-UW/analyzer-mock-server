"""
ASTM LIS2-A2 protocol handler (M4). Refactored from server.py.

Reference: specs/011-madagascar-analyzer-integration, tasks T072–T073.
"""

import logging
import random
from datetime import datetime
from typing import Any, Dict, List, Optional

from .base_handler import BaseHandler

logger = logging.getLogger(__name__)

STX = b"\x02"
ETX = b"\x03"
CR = b"\x0D"
LF = b"\x0A"
ENQ = b"\x05"
ACK = b"\x06"
EOT = b"\x04"


def build_astm_frames(astm_message: str) -> list:
    """Build ASTM LIS2-A2 frames from newline-separated segments (for serial send)."""
    frames = []
    for i, line in enumerate(astm_message.strip().split("\n")):
        if not line:
            continue
        fn = str((i % 7) + 1)
        content = line.encode("utf-8")
        checksum_data = fn.encode() + content + ETX
        cs = sum(checksum_data) % 256
        frame = STX + fn.encode() + content + ETX + f"{cs:02X}".encode() + CR + LF
        frames.append(frame)
    return frames


def _normalize_fields_from_template(template: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert template 'fields' to legacy shape (name, displayName, astmRef, type, unit, ...)."""
    out = []
    for f in template.get("fields", []):
        name = f.get("name", "Unknown")
        code = f.get("code", name)
        out.append({
            "name": name,
            "displayName": f.get("displayName", name),
            "astmRef": f"^^^{code}" if "astmRef" not in f else f["astmRef"],
            "type": f.get("type", "NUMERIC"),
            "unit": f.get("unit") or "",
            "normalRange": f.get("normalRange", ""),
            "possibleValues": f.get("possibleValues"),
        })
    return out


def _build_astm_message(
    analyzer_name: str,
    fields: List[Dict[str, Any]],
    panel_name: str = "CBC",
    patient_id: Optional[str] = None,
    sample_id: Optional[str] = None,
    patient_name: Optional[str] = None,
    patient_dob: Optional[str] = None,
    patient_sex: Optional[str] = None,
) -> str:
    """Build ASTM H|P|O|R|L message from analyzer name and field list."""
    now = datetime.now()
    timestamp = now.strftime("%Y%m%d%H%M%S")
    order_ts = now.replace(second=0, microsecond=0).strftime("%Y%m%d%H%M%S")
    result_ts = now.strftime("%Y%m%d%H%M%S")

    if not patient_id:
        patient_id = f"PAT-{now.strftime('%Y%m%d')}-{random.randint(100, 999)}"
    if not sample_id:
        sample_id = f"SAMPLE-{now.strftime('%Y%m%d')}-{random.randint(1000, 9999)}"
    if not patient_name:
        first_names = ["John", "Mary", "James", "Sarah", "Robert", "Emily"]
        last_names = ["Smith", "Johnson", "Williams", "Brown", "Jones"]
        patient_name = f"{random.choice(last_names)}^{random.choice(first_names)}"
    if not patient_dob:
        y, m, d = random.randint(1950, 2000), random.randint(1, 12), random.randint(1, 28)
        patient_dob = f"{y}{m:02d}{d:02d}"
    if not patient_sex:
        patient_sex = random.choice(["M", "F"])

    segments = []
    segments.append(f"H|\\^&|||{analyzer_name}|||||||LIS2-A2|{timestamp}")
    segments.append(f"P|1||{patient_id}|{patient_name}||{patient_sex}|{patient_dob}")
    segments.append(f"O|1|{sample_id}^LAB|{panel_name}^{panel_name} Panel||{order_ts}")

    seq = 1
    for field in fields:
        name = field.get("name", "Unknown")
        display = field.get("displayName", name)
        astm_ref = field.get("astmRef", f"^^^{name}")
        typ = field.get("type", "NUMERIC")
        unit = field.get("unit", "")
        normal_range = field.get("normalRange", "")

        test_id = f"{astm_ref}^{display}" if (display and display != name) else astm_ref

        if typ == "NUMERIC":
            if normal_range:
                try:
                    if "-" in normal_range:
                        low, high = map(float, normal_range.split("-"))
                        value = round(random.uniform(low, high), 2)
                    elif normal_range.startswith("<"):
                        max_v = float(normal_range[1:])
                        value = round(random.uniform(0, max_v * 0.9), 2)
                    elif normal_range.startswith(">"):
                        min_v = float(normal_range[1:])
                        value = round(random.uniform(min_v * 1.1, min_v * 2), 2)
                    else:
                        value = round(random.uniform(1, 100), 2)
                except Exception:
                    value = round(random.uniform(1, 100), 2)
            else:
                value = round(random.uniform(1, 100), 2)
            segments.append(f"R|{seq}|{test_id}|{value}|{unit}|{normal_range}|N||F|{result_ts}")
        elif typ == "QUALITATIVE":
            vals = field.get("possibleValues", ["POSITIVE", "NEGATIVE"])
            value = random.choice(vals)
            segments.append(f"R|{seq}|{test_id}|{value}|||N||F|{result_ts}")
        else:
            value = f"Sample result for {display}"
            segments.append(f"R|{seq}|{test_id}|{value}|||N||F|{result_ts}")
        seq += 1

    segments.append("L|1|N")
    return "\n".join(segments) + "\n"


class ASTMHandler(BaseHandler):
    """ASTM LIS2-A2 message generation. Supports template and legacy fields.json."""

    protocol_type = "ASTM"

    def generate(self, template: Dict[str, Any], **kwargs) -> str:
        if not self.validate_template(template):
            raise ValueError("Invalid template: missing analyzer or fields")
        anal = template["analyzer"]
        name = f"{anal.get('manufacturer', '')}^{anal.get('model', '')}^{anal.get('name', '')}".strip("^")
        if not name:
            name = anal.get("name", "MockAnalyzer")
        panel = anal.get("model") or "CBC"
        fields = _normalize_fields_from_template(template)
        return _build_astm_message(
            analyzer_name=name,
            fields=fields,
            panel_name=panel,
            patient_id=kwargs.get("patient_id"),
            sample_id=kwargs.get("sample_id"),
            patient_name=kwargs.get("patient_name"),
            patient_dob=kwargs.get("patient_dob"),
            patient_sex=kwargs.get("patient_sex"),
        )


def generate_astm_message(
    analyzer_type: str,
    fields_config: Dict[str, List[Dict[str, Any]]],
    patient_id: Optional[str] = None,
    sample_id: Optional[str] = None,
    patient_name: Optional[str] = None,
    patient_dob: Optional[str] = None,
    patient_sex: Optional[str] = None,
) -> str:
    """
    Legacy entry point: generate ASTM from analyzer_type + fields_config (fields.json).

    Preserves backward compatibility with existing push/API mode.
    """
    fields = fields_config.get(analyzer_type, [])
    if not fields and fields_config:
        analyzer_type = next(iter(fields_config))
        fields = fields_config[analyzer_type]
        logger.warning("No fields for analyzer type, using %s", analyzer_type)
    if not fields:
        logger.error("No fields configuration available")
        return ""

    names = {
        "HEMATOLOGY": "Sysmex^XN-1000^V1.0",
        "CHEMISTRY": "Beckman^AU5800^V2.1",
        "IMMUNOLOGY": "Roche^Cobas^V1.5",
        "MICROBIOLOGY": "BD^Phoenix^V2.0",
    }
    analyzer_name = names.get(analyzer_type, f"MockAnalyzer^{analyzer_type}^1.0")
    panel = "CBC" if analyzer_type == "HEMATOLOGY" else "CHEM" if analyzer_type == "CHEMISTRY" else analyzer_type
    return _build_astm_message(
        analyzer_name=analyzer_name,
        fields=fields,
        panel_name=panel,
        patient_id=patient_id,
        sample_id=sample_id,
        patient_name=patient_name,
        patient_dob=patient_dob,
        patient_sex=patient_sex,
    )
