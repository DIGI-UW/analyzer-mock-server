"""
File-based protocol handler (M4). CSV/TXT generation for file import testing.

Reference: specs/011-madagascar-analyzer-integration, tasks T083–T086.
"""

import csv
import io
import itertools
import logging
import os
import random
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from .accession import validate_accession
from .base_handler import BaseHandler

logger = logging.getLogger(__name__)

_file_sample_lock = threading.Lock()
_file_sample_counter = itertools.count(1)


def _next_file_sample_id() -> str:
    with _file_sample_lock:
        n = next(_file_sample_counter)
    return validate_accession(f"DEV012699{n:011d}", "FILE generated accession")


def _normalize_fields(template: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for f in template.get("fields", []):
        out.append({
            "name": f.get("name", "Unknown"),
            "code": f.get("code", f.get("name", "")),
            "type": f.get("type", "NUMERIC"),
            "unit": f.get("unit") or "",
            "normalRange": f.get("normalRange", ""),
            "possibleValues": f.get("possibleValues"),
        })
    return out


def _random_value(field: Dict[str, Any]) -> str:
    typ = (field.get("type") or "NUMERIC").upper()
    nr = field.get("normalRange", "")
    if typ == "NUMERIC":
        if nr:
            try:
                if "-" in nr:
                    a, b = map(float, nr.split("-"))
                    return str(round(random.uniform(a, b), 2))
                if nr.startswith("<"):
                    return str(round(random.uniform(0, float(nr[1:]) * 0.9), 2))
                if nr.startswith(">"):
                    return str(round(random.uniform(float(nr[1:]) * 1.1, float(nr[1:]) * 2), 2))
            except Exception:
                pass
        return str(round(random.uniform(1, 100), 2))
    if typ == "QUALITATIVE":
        vals = field.get("possibleValues", ["NEGATIVE", "POSITIVE"])
        return random.choice(vals)
    return f"Result_{field.get('name', '')}"


class FileHandler(BaseHandler):
    """File protocol handler: generates CSV/TXT from template."""

    protocol_type = "FILE"

    def generate(self, template: Dict[str, Any], **kwargs) -> str:
        if not self.validate_template(template):
            raise ValueError("Invalid template: missing analyzer or fields")
        fcfg = template.get("file_config") or {}
        fmt = (fcfg.get("format") or "CSV").upper()
        delim = fcfg.get("delimiter", ",")
        if fmt == "TSV":
            delim = "\t"
        has_header = fcfg.get("has_header", True)
        col_map = fcfg.get("column_mapping") or {}
        sample_id = kwargs.get("sample_id")
        sample_count = int(kwargs.get("sample_count", 1))
        target_dir = kwargs.get("target_directory", ".")

        fields = _normalize_fields(template)
        now = datetime.now()
        ts = now.strftime("%Y%m%d%H%M%S")
        sample_col = col_map.get("sample_id", "Sample Name")
        test_col = col_map.get("test_code", "Target")
        result_col = col_map.get("result", "Result")

        buf = io.StringIO()
        w = csv.writer(buf, delimiter=delim if delim and len(delim) == 1 else ",", lineterminator="\n")
        if has_header:
            header = [sample_col, test_col, result_col, "Timestamp"]
            w.writerow(header)

        for i in range(sample_count):
            sid = validate_accession(sample_id, "FILE sample_id override") if sample_id else _next_file_sample_id()
            for f in fields:
                w.writerow([sid, f.get("code", f.get("name")), _random_value(f), ts])

        return buf.getvalue()

    def generate_qc(self, template: Dict[str, Any], deviation: Optional[float] = None, **kwargs) -> str:
        """Generate a CSV QC payload from the template's qc_controls section.

        Each qc_controls entry produces one row whose Sample Name = "QC-{lot}-{level}"
        and result column = target + (deviation × sd) — matching the OE-side
        FILE profile's `qcRules`:
            - SPECIMEN_ID_PREFIX operand=QC (sample name starts with "QC-")
            - FIELD_EQUALS targetField=QC_TASK operand=STANDARD (Task column)

        Raises ValueError if the template has no qc_controls defined.
        """
        if not self.validate_template(template):
            raise ValueError("Invalid template: missing analyzer or fields")

        qc_controls_list = template.get("qc_controls", [])
        if not qc_controls_list:
            raise ValueError(
                f"Template '{template['analyzer'].get('name')}' has no qc_controls defined"
            )

        fcfg = template.get("file_config") or {}
        delim = fcfg.get("delimiter", ",")
        if (fcfg.get("format") or "CSV").upper() == "TSV":
            delim = "\t"
        col_map = fcfg.get("column_mapping") or {}
        sample_col = col_map.get("sample_id", "Sample Name")
        test_col = col_map.get("test_code", "Target Name")
        result_col = col_map.get("result", "Quantity Mean")

        ts = datetime.now().strftime("%Y%m%d%H%M%S")

        buf = io.StringIO()
        # Header includes Task column so FIELD_EQUALS QC_TASK=STANDARD matches.
        w = csv.writer(buf, delimiter=delim if delim and len(delim) == 1 else ",", lineterminator="\n")
        w.writerow([sample_col, test_col, "Task", result_col, "Timestamp"])

        for ctrl in qc_controls_list:
            field_code = ctrl.get("field_code", "")
            lot = ctrl.get("lot_number", f"LOT-{field_code}-N")
            level = ctrl.get("level", "N")
            target = ctrl.get("target")
            sd = ctrl.get("sd", 0.0)
            task = ctrl.get("task", "STANDARD")

            try:
                target_num = float(target) if target is not None else 0.0
            except (TypeError, ValueError):
                target_num = 0.0
            try:
                sd_num = float(sd)
            except (TypeError, ValueError):
                sd_num = 0.0

            if deviation is not None:
                value = round(target_num + (float(deviation) * sd_num), 2)
            else:
                value = round(random.gauss(target_num, sd_num) if sd_num else target_num, 2)

            sample_name = f"QC-{lot}-{level}"
            w.writerow([sample_name, field_code, task, value, ts])

        return buf.getvalue()

    def write_to_file(self, template: Dict[str, Any], path: str, **kwargs) -> Optional[str]:
        """Generate and write to path. Creates parent dirs. Returns path or None."""
        content = self.generate(template, **kwargs)
        return self.write_text_to_path(path, content)

    def write_text_to_path(self, path: str, content: str) -> Optional[str]:
        """Write pre-generated text to path (same bytes as a single generate call)."""
        try:
            parent = os.path.dirname(path) or "."
            os.makedirs(parent, exist_ok=True)
            with open(path, "w", newline="", encoding="utf-8") as f:
                f.write(content)
            logger.info("Wrote %s (%s bytes)", path, len(content))
            return path
        except Exception as e:
            logger.exception("Write failed %s: %s", path, e)
            return None
