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


def _build_qc_xlsx(header, rows) -> bytes:
    """Build an Office Open XML (.xlsx) workbook holding the QC rows.

    Single sheet named ``Results`` (matches QuantStudio's native sheet name
    so the bridge's header_scan strategy hits row 1 immediately). Returns
    the binary bytes ready to write to a file or POST as multipart content.

    Imported lazily so the rest of the module stays importable when
    openpyxl isn't installed; CSV/TSV paths don't need it.
    """
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Results"
    ws.append(list(header))
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


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

    # Placeholders the qc_sample_id_pattern accepts. Anything else raises a
    # clear ValueError instead of leaking a KeyError as a 500.
    _QC_SAMPLE_PATTERN_PLACEHOLDERS = ("lot", "level", "field_code", "task")

    # Format → file extension. Used by callers to name the emitted file
    # (e.g. /admin/upload filename, /simulate/file/{template} target_dir
    # output) so the bridge's extension-based dispatch picks the right
    # parser. Aliases preserved for back-compat with older templates.
    _QC_FORMAT_EXTENSION = {
        "CSV": ".csv",
        "TSV": ".tsv",
        "TXT": ".csv",
        "XLSX": ".xlsx",
        "EXCEL": ".xlsx",
        "XLS": ".xlsx",  # we always emit xlsx — POI/openpyxl read both, real
                         #     QuantStudio outputs are .xls but the bridge's
                         #     WorkbookFactory parses both transparently
    }

    @classmethod
    def qc_format(cls, template: Dict[str, Any]) -> str:
        """Resolve the canonical QC format name (CSV/TSV/XLSX) for a template.

        Reads from ``file_config.format`` (case-insensitive); aliases are
        normalized so callers can rely on exactly one of {CSV, TSV, XLSX}.
        Defaults to CSV when nothing is declared.
        """
        fcfg = template.get("file_config") or {}
        raw = (fcfg.get("format") or "CSV").upper()
        if raw in ("XLSX", "EXCEL", "XLS"):
            return "XLSX"
        if raw == "TSV":
            return "TSV"
        return "CSV"

    @classmethod
    def qc_extension(cls, template: Dict[str, Any]) -> str:
        """Return the file extension (with leading dot) the QC content
        should be written under, given the template's declared format."""
        return cls._QC_FORMAT_EXTENSION.get(cls.qc_format(template), ".csv")

    def generate_qc(self, template: Dict[str, Any], deviation: Optional[float] = None, **kwargs) -> bytes:
        """Generate a QC payload from the template's qc_controls section.

        Returns raw bytes whose encoding follows ``file_config.format``:

          - **CSV** (default): comma-delimited UTF-8 text
          - **TSV**: tab-delimited UTF-8 text
          - **XLSX** / **EXCEL** / **XLS**: native Office Open XML binary
            (always emitted as ``.xlsx`` regardless of the declared alias —
            Apache POI and openpyxl both read .xlsx transparently and that
            keeps the writer single-implementation)

        Each qc_controls entry produces one row. The sample-name pattern
        defaults to ``QC-{lot}-{level}`` but can be overridden per template
        via ``qc_sample_id_pattern`` so the generated samples align with the
        analyzer's OE-side qcRules:

            - QuantStudio profile uses ``SPECIMEN_ID_PREFIX LPC|HPC|...`` →
              pattern should put ``{level}`` first (e.g. ``"{level}-{lot}"``)
            - Mindray BS-200 uses ``SPECIMEN_ID_PREFIX QC`` → default pattern
              works as-is

        Pattern placeholders supported: ``{lot}`` (lot_number from qc_control),
        ``{level}`` (level), ``{field_code}`` (field_code), ``{task}`` (task).
        Result column = target + (deviation × sd).

        Raises ValueError if the template has no qc_controls defined or
        if ``qc_sample_id_pattern`` references an unsupported placeholder.
        """
        if not self.validate_template(template):
            raise ValueError("Invalid template: missing analyzer or fields")

        qc_controls_list = template.get("qc_controls", [])
        if not qc_controls_list:
            raise ValueError(
                f"Template '{template['analyzer'].get('name')}' has no qc_controls defined"
            )

        fcfg = template.get("file_config") or {}
        col_map = fcfg.get("column_mapping") or {}
        sample_col = col_map.get("sample_id", "Sample Name")
        test_col = col_map.get("test_code", "Target Name")
        result_col = col_map.get("result", "Quantity Mean")

        sample_pattern = template.get("qc_sample_id_pattern") or "QC-{lot}-{level}"
        # Up-front validation so a bad pattern fails with a clear message
        # instead of leaking a KeyError as a 500 at row-build time.
        try:
            sample_pattern.format(**{k: "" for k in self._QC_SAMPLE_PATTERN_PLACEHOLDERS})
        except KeyError as e:
            raise ValueError(
                f"Unsupported placeholder {e} in qc_sample_id_pattern "
                f"'{sample_pattern}'. Supported: "
                + ", ".join("{" + k + "}" for k in self._QC_SAMPLE_PATTERN_PLACEHOLDERS)
            ) from None

        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        header = [sample_col, test_col, "Task", result_col, "Timestamp"]

        rows = []
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

            sample_name = sample_pattern.format(
                lot=lot, level=level, field_code=field_code, task=task)
            rows.append([sample_name, field_code, task, value, ts])

        fmt = self.qc_format(template)
        if fmt == "XLSX":
            return _build_qc_xlsx(header, rows)
        # CSV / TSV path — the historical default
        delim = "\t" if fmt == "TSV" else (fcfg.get("delimiter") or ",")
        if not delim or len(delim) != 1:
            delim = ","
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=delim, lineterminator="\n")
        w.writerow(header)
        for r in rows:
            w.writerow(r)
        return buf.getvalue().encode("utf-8")

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
