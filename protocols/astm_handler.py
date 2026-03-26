"""
ASTM LIS2-A2 protocol handler (M4). Refactored from server.py.

Reference: specs/011-madagascar-analyzer-integration, tasks T072–T073.
Cepheid GeneXpert LIS Protocol Specification Rev E (Sections 4-6).
"""

import itertools
import logging
import random
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .base_handler import BaseHandler

# Sequential sample ID generator — shared with HL7 handler pattern.
_astm_sample_counters: Dict[str, itertools.count] = {}


def _next_astm_sample_id(prefix: str, timestamp: Optional[datetime] = None) -> str:
    """Generate a unique sequential sample ID for ASTM messages."""
    if timestamp is None:
        timestamp = datetime.now()
    if prefix not in _astm_sample_counters:
        _astm_sample_counters[prefix] = itertools.count(1)
    seq = next(_astm_sample_counters[prefix])
    return f"{prefix}-{timestamp.strftime('%Y%m%d')}-{seq:03d}"

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
    frame_idx = 0
    for line in astm_message.strip().split("\n"):
        if not line:
            continue
        # CLSI LIS1-A frame numbering: cycles 1,2,3,4,5,6,7,0,1,2,...
        fn = str((frame_idx + 1) % 8)
        content = line.encode("utf-8")
        checksum_data = fn.encode() + content + ETX
        cs = sum(checksum_data) % 256
        frame = STX + fn.encode() + content + ETX + f"{cs:02X}".encode() + CR + LF
        frames.append(frame)
        frame_idx += 1
    return frames


def _normalize_fields_from_template(template: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert template 'fields' to internal shape with GeneXpert extensions."""
    out = []
    for f in template.get("fields", []):
        name = f.get("name", "Unknown")
        code = f.get("code", name)
        out.append({
            "name": name,
            "code": code,
            "displayName": f.get("displayName", name),
            "astmRef": f"^^^{code}" if "astmRef" not in f else f["astmRef"],
            "type": f.get("type", "NUMERIC"),
            "unit": f.get("unit") or "",
            "normalRange": f.get("normalRange", ""),
            "possibleValues": f.get("possibleValues"),
            "seedValue": f.get("seedValue"),
            "seedQualitative": f.get("seedQualitative"),
            "version": f.get("version"),
            "complementaryResults": f.get("complementaryResults", []),
        })
    return out


def _generate_value(field: Dict[str, Any], use_seed: bool = False) -> Any:
    """Generate a result value for a field based on its type."""
    typ = field.get("type", "NUMERIC")

    if typ == "NUMERIC":
        if use_seed and field.get("seedValue") is not None:
            return field["seedValue"]
        normal_range = field.get("normalRange", "")
        if normal_range:
            try:
                if "-" in normal_range:
                    low, high = map(float, normal_range.split("-"))
                    return round(random.uniform(low, high), 2)
                elif normal_range.startswith("<"):
                    max_v = float(normal_range[1:])
                    return round(random.uniform(0, max_v * 0.9), 2)
                elif normal_range.startswith(">"):
                    min_v = float(normal_range[1:])
                    return round(random.uniform(min_v * 1.1, min_v * 2), 2)
            except Exception:
                pass
        return round(random.uniform(1, 100), 2)

    elif typ == "QUALITATIVE":
        if use_seed and field.get("seedQualitative"):
            return field["seedQualitative"]
        vals = field.get("possibleValues", ["POSITIVE", "NEGATIVE"])
        return random.choice(vals)

    else:
        return f"Sample result for {field.get('displayName', field.get('name', 'Unknown'))}"


def _build_test_id(field: Dict[str, Any], has_astm_config: bool) -> str:
    """Build the R.3 Universal Test ID field.

    Without astm_config (legacy): ^^^CODE or ^^^CODE^DisplayName
    With astm_config (GeneXpert-style): 8-component ^^^CODE^Name^Version^^
      where component 8 is empty for main results, filled by
      _build_complementary_test_id for sub-results (e.g., Conc/LOG).
    """
    if has_astm_config and field.get("version"):
        code = field.get("code", field.get("name", "Unknown"))
        name = field.get("name", "")
        version = field.get("version", "")
        return f"^^^{code}^{name}^{version}^^"
    else:
        astm_ref = field.get("astmRef", f"^^^{field.get('name', 'Unknown')}")
        display = field.get("displayName", field.get("name", ""))
        name = field.get("name", "")
        if display and display != name:
            return f"{astm_ref}^{display}"
        return astm_ref


def _build_complementary_test_id(field: Dict[str, Any], comp_name: str) -> str:
    """Build R.3 for a complementary result (e.g., Conc/LOG, Ct)."""
    code = field.get("code", field.get("name", "Unknown"))
    name = field.get("name", "")
    version = field.get("version", "")
    return f"^^^{code}^{name}^{version}^^{comp_name}"


def _build_astm_message(
    analyzer_name: str,
    fields: List[Dict[str, Any]],
    panel_name: str = "CBC",
    patient_id: Optional[str] = None,
    sample_id: Optional[str] = None,
    patient_name: Optional[str] = None,
    patient_dob: Optional[str] = None,
    patient_sex: Optional[str] = None,
    astm_config: Optional[Dict[str, Any]] = None,
    action_code: str = "",
    operator_id: Optional[str] = None,
    use_seed: bool = False,
) -> str:
    """Build ASTM H|P|O|R|L message from analyzer name and field list.

    When astm_config is provided, generates standards-compliant messages with:
    - H.3 Message ID, H.10 Receiver, H.12 Processing ID, H.13 Version
    - O.5 Universal Test ID, O.6 Priority, O.12 Action Code, O.16 Specimen Descriptor
    - Extended R.3 with 8-component test ID (GeneXpert format)
    - Multi-level results (main + complementary)
    - Comment records for ERROR results
    """
    cfg = astm_config or {}
    now = datetime.now()
    timestamp = now.strftime("%Y%m%d%H%M%S")
    start_ts = now.strftime("%Y%m%d%H%M%S")
    end_ts = (now + timedelta(minutes=random.randint(5, 30))).strftime("%Y%m%d%H%M%S")

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
    if not operator_id:
        operator_id = f"OP-{random.randint(100, 999)}"

    has_cfg = bool(cfg)
    segments = []

    # H-record: Header
    if has_cfg:
        msg_id = f"MSG-{uuid.uuid4().hex[:12]}"
        receiver = cfg.get("receiver_id", "")
        processing = cfg.get("processing_id", "P")
        version = cfg.get("version_number", "LIS2-A2")
        segments.append(
            f"H|\\^&|{msg_id}||{analyzer_name}|||||{receiver}||{processing}|{version}|{timestamp}"
        )
    else:
        segments.append(f"H|\\^&|||{analyzer_name}|||||||LIS2-A2|{timestamp}")

    # P-record: Patient
    segments.append(f"P|1||{patient_id}|{patient_name}||{patient_sex}|{patient_dob}")

    # O-record: Order (26 fields per ASTM E-1394-97)
    if has_cfg:
        first_code = fields[0].get("code", "TEST") if fields else "TEST"
        specimen_desc = cfg.get("specimen_descriptor", "")
        report_type = "F"
        o_fields: List[str] = [""] * 26
        o_fields[0] = "O"                   # O.1:  Record type
        o_fields[1] = "1"                    # O.2:  Sequence number
        o_fields[2] = sample_id              # O.3:  Specimen ID
        # o_fields[3]                        # O.4:  Instrument Specimen ID (unused)
        o_fields[4] = f"^^^{first_code}"     # O.5:  Universal test ID
        o_fields[5] = "R"                    # O.6:  Priority (R = routine)
        o_fields[6] = timestamp              # O.7:  Order date/time
        # o_fields[7..10]                    # O.8–O.11: collection/volume/collector (unused)
        o_fields[11] = action_code           # O.12: Action code ("Q" for QC)
        # o_fields[12..14]                   # O.13–O.15: physician/phone/user (unused)
        o_fields[15] = specimen_desc         # O.16: Specimen descriptor
        # o_fields[16..24]                   # O.17–O.25: additional fields (unused)
        o_fields[25] = report_type           # O.26: Report type
        segments.append("|".join(o_fields))
    else:
        segments.append(f"O|1|{sample_id}^LAB|{panel_name}^{panel_name} Panel||{timestamp}")

    # R-records: Results
    seq = 1
    for field in fields:
        test_id = _build_test_id(field, has_cfg)
        value = _generate_value(field, use_seed=use_seed)
        typ = field.get("type", "NUMERIC")
        unit = field.get("unit", "")
        normal_range = field.get("normalRange", "")

        if typ == "NUMERIC":
            if has_cfg:
                segments.append(
                    f"R|{seq}|{test_id}|{value}|{unit}|{normal_range}|N||F||{operator_id}|{start_ts}|{end_ts}"
                )
            else:
                segments.append(f"R|{seq}|{test_id}|{value}|{unit}|{normal_range}|N||F|{end_ts}")
        elif typ == "QUALITATIVE":
            if has_cfg:
                # GeneXpert format: qualitative value in R.4 component 1
                segments.append(
                    f"R|{seq}|{test_id}|{value}^|||||F||{operator_id}|{start_ts}|{end_ts}"
                )
            else:
                segments.append(f"R|{seq}|{test_id}|{value}|||N||F|{end_ts}")
        else:
            segments.append(f"R|{seq}|{test_id}|{value}|||N||F|{end_ts}")

        # C-record: Comment for ERROR results (timestamp aligns with result completion)
        if has_cfg and str(value) == "ERROR":
            segments.append(f"C|1|I|Error^^Error^^{end_ts}|N")

        seq += 1

        # Complementary results (e.g., Conc/LOG, Ct for GeneXpert)
        for comp in field.get("complementaryResults", []):
            comp_test_id = _build_complementary_test_id(field, comp["name"])
            comp_unit = comp.get("unit", "")
            if use_seed and comp.get("seedValue") is not None:
                comp_value = comp["seedValue"]
            else:
                comp_value = round(random.uniform(0.1, 10.0), 2)
            segments.append(
                f"R|{seq}|{comp_test_id}|^{comp_value}|{comp_unit}||||F||{operator_id}|{start_ts}|{end_ts}"
            )
            seq += 1

    # L-record: Terminator
    segments.append("L|1|N")
    return "\n".join(segments) + "\n"


def _build_qc_message(
    analyzer_name: str,
    template: Dict[str, Any],
    astm_config: Dict[str, Any],
    operator_id: Optional[str] = None,
) -> str:
    """Build a QC ASTM message using the template's qcSample config.

    Per GeneXpert spec (Section 6.1 & 6.3.4.1.4), QC samples are marked
    with Action Code 'Q' in the Order record (O.12).
    """
    qc_config = template.get("qcSample", {})
    if not qc_config:
        return ""

    qc_id = qc_config.get("id", f"QC-{random.randint(1000, 9999)}")
    action_code = qc_config.get("actionCode", "Q")

    # Build QC-specific field overrides
    qc_field_overrides = {}
    for qf in qc_config.get("fields", []):
        qc_field_overrides[qf["code"]] = qf

    # Use all template fields, applying QC overrides where specified
    fields = _normalize_fields_from_template(template)
    qc_fields = []
    for field in fields:
        code = field.get("code", "")
        if code in qc_field_overrides:
            override = qc_field_overrides[code]
            field = dict(field)
            if "seedQualitative" in override:
                field["seedQualitative"] = override["seedQualitative"]
            if "seedValue" in override:
                field["seedValue"] = override["seedValue"]
        qc_fields.append(field)

    return _build_astm_message(
        analyzer_name=analyzer_name,
        fields=qc_fields,
        sample_id=qc_id,
        patient_id=f"QC-PAT-{random.randint(100, 999)}",
        patient_name="QC^Control",
        astm_config=astm_config,
        action_code=action_code,
        operator_id=operator_id,
        use_seed=True,
    )


def _build_qc_astm_message(
    analyzer_name: str,
    fields: List[Dict[str, Any]],
    qc_controls: Dict[str, Dict[str, Any]],
    deviation: Optional[float] = None,
    category: str = "",
) -> str:
    """Build an ASTM QC message with Q segments per LIS2-A2.

    Structure: H | P | O | (R + Q)× | L

    Q segment format (per OE GenericASTM convention):
      Q|{seq}|{field_code}^{lot_number}^{level}|{value}|{unit}|{timestamp}

    Only fields present in qc_controls generate R+Q pairs.
    O+R segments are required to trigger OE isAnalyzerResult();
    Q carries the lot/level/target metadata for QC processing.

    Args:
        deviation: Number of standard deviations to shift results.
            None = realistic random scatter (Gaussian around target).
            0 = exact target value (z-score will be 0).
            3.5 = exactly 3.5 SD above target (triggers 1₃ₛ rejection).
            SD is estimated as a percentage of target (5% hematology, 10% molecular).
    """
    # SD estimates as percentage of mean, matching generate_analyzer_sql.py
    SD_PCT = {
        "HEMATOLOGY": 0.05, "CHEMISTRY": 0.05,
        "MOLECULAR": 0.10, "COAGULATION": 0.08, "IMMUNOLOGY": 0.10,
    }
    sd_pct = SD_PCT.get(category, 0.05)

    now = datetime.now()
    timestamp = now.strftime("%Y%m%d%H%M%S")
    qc_sample_id = f"QC-{timestamp}"

    segments = [
        f"H|\\^&|||{analyzer_name}|||||||LIS2-A2|{timestamp}",
        f"P|1||QC-CTRL-001|QC^Control||U|19000101",
        f"O|1|{qc_sample_id}^LAB|QC^QC Panel||{timestamp}",
    ]

    seq = 1
    for field in fields:
        code = field.get("code", "")
        ctrl = qc_controls.get(code)
        if not ctrl:
            continue

        lot    = ctrl.get("lot_number", f"LOT-{code}-N")
        level  = ctrl.get("level", "N")
        target = ctrl.get("target")
        if target is None:
            target = field.get("seedValue") or _generate_value(field, use_seed=True)
        unit         = field.get("unit", "")
        normal_range = field.get("normalRange", "")

        # Apply deviation or realistic scatter
        sd = target * sd_pct if target else 0
        if deviation is not None and target:
            # Fixed deviation: shift by exactly N standard deviations
            value = round(target + (deviation * sd), 2)
        elif target and sd > 0:
            # Normal operation: random scatter within ~±1.5 SD (realistic instrument noise)
            value = round(random.gauss(target, sd), 2)
        else:
            value = target

        # Clamp to 0 — physical measurements can't be negative
        if isinstance(value, (int, float)) and value < 0:
            value = 0.0

        segments.append(f"R|{seq}|^^^{code}|{value}|{unit}|{normal_range}|N||F|{timestamp}")
        segments.append(f"Q|{seq}|{code}^{lot}^{level}|{value}|{unit}|{timestamp}")
        seq += 1

    if seq == 1:
        raise ValueError("No fields matched any qc_controls entry — check field_code values")

    segments.append("L|1|N")
    return "\n".join(segments) + "\n"


class ASTMHandler(BaseHandler):
    """ASTM LIS2-A2 message generation. Supports template and legacy fields.json."""

    protocol_type = "ASTM"

    def generate(self, template: Dict[str, Any], **kwargs) -> str:
        if not self.validate_template(template):
            raise ValueError("Invalid template: missing analyzer or fields")

        anal = template["analyzer"]
        astm_config = template.get("astm_config", {})

        # Use identification.astm_header if available, otherwise build from analyzer metadata
        ident = template.get("identification", {})
        if ident.get("astm_header"):
            name = ident["astm_header"]
        else:
            name = f"{anal.get('manufacturer', '')}^{anal.get('model', '')}^{anal.get('name', '')}".strip("^")
            if not name:
                name = anal.get("name", "MockAnalyzer")

        fields = _normalize_fields_from_template(template)

        # Determine if we should use seed values for deterministic output
        use_seed = kwargs.get("use_seed", False)

        # Get test patient/sample from template if available
        test_patient = template.get("testPatient", {})
        test_sample = template.get("testSample", {})

        patient_id = kwargs.get("patient_id") or test_patient.get("id")
        # Generate unique sequential sample ID (like a real analyzer)
        explicit_sample_id = kwargs.get("sample_id")
        if explicit_sample_id:
            sample_id = explicit_sample_id
        else:
            prefix = test_sample.get("id", "SAMPLE")
            sample_id = _next_astm_sample_id(prefix)
        patient_name = kwargs.get("patient_name") or test_patient.get("name")
        patient_dob = kwargs.get("patient_dob") or test_patient.get("dob")
        patient_sex = kwargs.get("patient_sex") or test_patient.get("sex")

        # Build patient message
        patient_msg = _build_astm_message(
            analyzer_name=name,
            fields=fields,
            panel_name=anal.get("model") or "CBC",
            patient_id=patient_id,
            sample_id=sample_id,
            patient_name=patient_name,
            patient_dob=patient_dob,
            patient_sex=patient_sex,
            astm_config=astm_config if astm_config else None,
            operator_id=kwargs.get("operator_id"),
            use_seed=use_seed,
        )

        # Build QC message if enabled
        qc_msg = ""
        if astm_config.get("enable_qc") and template.get("qcSample"):
            qc_msg = _build_qc_message(
                analyzer_name=name,
                template=template,
                astm_config=astm_config,
                operator_id=kwargs.get("operator_id"),
            )

        if qc_msg:
            return patient_msg + qc_msg
        return patient_msg

    def generate_qc(self, template: Dict[str, Any], **kwargs) -> str:
        """Generate an ASTM QC message from the template's qc_controls section.

        Each field listed in qc_controls produces an R+Q segment pair.
        Raises ValueError if the template has no qc_controls defined.
        """
        if not self.validate_template(template):
            raise ValueError("Invalid template: missing analyzer or fields")

        qc_controls_list = template.get("qc_controls", [])
        if not qc_controls_list:
            raise ValueError(
                f"Template '{template['analyzer'].get('name')}' has no qc_controls defined"
            )

        qc_controls = {c["field_code"]: c for c in qc_controls_list}

        ident = template.get("identification", {})
        anal  = template["analyzer"]
        if ident.get("astm_header"):
            name = ident["astm_header"]
        else:
            name = f"{anal.get('manufacturer', '')}^{anal.get('model', '')}^{anal.get('name', '')}".strip("^") or "MockAnalyzer"

        fields = _normalize_fields_from_template(template)
        return _build_qc_astm_message(
            analyzer_name=name,
            fields=fields,
            qc_controls=qc_controls,
            deviation=kwargs.get("deviation"),
            category=anal.get("category", ""),
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
