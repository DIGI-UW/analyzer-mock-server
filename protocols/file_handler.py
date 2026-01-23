"""
File-based protocol handler (M4). CSV/TXT generation for file import testing.

Reference: specs/011-madagascar-analyzer-integration, tasks T083–T086.
"""

import csv
import io
import logging
import os
import random
from datetime import datetime
from typing import Any, Dict, List, Optional

from .base_handler import BaseHandler

logger = logging.getLogger(__name__)


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
            sid = sample_id or f"S{now.strftime('%Y%m%d')}-{random.randint(1000, 9999)}"
            for f in fields:
                w.writerow([sid, f.get("code", f.get("name")), _random_value(f), ts])

        return buf.getvalue()

    def write_to_file(self, template: Dict[str, Any], path: str, **kwargs) -> Optional[str]:
        """Generate and write to path. Creates parent dirs. Returns path or None."""
        content = self.generate(template, **kwargs)
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", newline="", encoding="utf-8") as f:
                f.write(content)
            logger.info("Wrote %s (%s bytes)", path, len(content))
            return path
        except Exception as e:
            logger.exception("Write failed %s: %s", path, e)
            return None
