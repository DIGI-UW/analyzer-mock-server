#!/usr/bin/env python3
"""
HL7 ORU^R01 Message Generator

Generates HL7 v2.x ORU^R01 (observation result) messages from analyzer
templates for use with OpenELIS POST /analyzer/hl7. Matches structure of
Mindray BC-5380 / BS-360E fixtures for parser compatibility.

Usage:
    from template_loader import TemplateLoader
    from hl7_generator import generate_oru_r01

    loader = TemplateLoader()
    template = loader.load_template('mindray_bc5380')
    message = generate_oru_r01(template, deterministic=True)
"""

import random
import uuid
from datetime import datetime
from typing import Dict, Optional


def generate_oru_r01(
    template: Dict,
    deterministic: bool = True,
    timestamp: Optional[datetime] = None,
    patient_id: Optional[str] = None,
    sample_id: Optional[str] = None,
) -> str:
    """Generate a single HL7 ORU^R01 message from a template.

    Args:
        template: Loaded template dict (protocol.type must be HL7).
        deterministic: If True, use seedValue from template fields.
        timestamp: Optional message timestamp (defaults to now).
        patient_id: Override patient ID.
        sample_id: Override sample/placer ID.

    Returns:
        Complete HL7 message as newline-separated string (MSH, PID, ORC, OBR, OBX...).
    """
    if timestamp is None:
        timestamp = datetime.now()

    identification = template.get('identification', {})
    fields = template.get('fields', [])
    test_patient = template.get('testPatient', {})
    test_sample = template.get('testSample', {})

    # Prefer HL7-specific identification fields, but fall back to msh_sender for backward compatibility
    sending_app = identification.get('hl7_sending_app') or identification.get('msh_sender', 'MINDRAY')
    sending_facility = identification.get('hl7_sending_facility', 'LAB')
    ts_str = timestamp.strftime('%Y%m%d%H%M%S')
    msg_id = f"{sending_app}{timestamp.strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:6].upper()}"

    pid = patient_id or test_patient.get('id', 'PAT001')
    name = test_patient.get('name', 'DOE^JOHN')
    sex = test_patient.get('sex', 'M')
    dob = test_patient.get('dob', '19800115')
    placer = sample_id or test_sample.get('id', 'PLACER123')
    filler = 'FILLER456'
    panel = test_sample.get('type', 'CBC^COMPLETE BLOOD COUNT')

    segments = []

    # MSH - Message Header (delimiters |^~\&)
    msh = (
        f"MSH|^~\\&|{sending_app}|{sending_facility}|OpenELIS|LAB|{ts_str}"
        f"||ORU^R01|{msg_id}|P|2.5.1||||||||"
    )
    segments.append(msh)

    # PID - Patient Identification
    pid_seg = f"PID|1||{pid}^^^HOSPITAL||{name}||{dob}|{sex}||||||||||||||||||||"
    segments.append(pid_seg)

    # ORC - Common Order
    orc = f"ORC|RE|{placer}|{filler}|" + "|" * 20
    segments.append(orc)

    # OBR - Observation Request
    obr = (
        f"OBR|1|{placer}|{filler}|1|^^^{panel}|||{ts_str}"
        + "|" * 25 + "F|" + "|" * 20
    )
    segments.append(obr)

    # OBX - Observation/Result (one per field)
    for idx, field in enumerate(fields, start=1):
        code = field.get('code', f'TEST{idx}')
        name_ = field.get('name', code)
        unit = field.get('unit', '')
        field_type = field.get('type', 'NUMERIC')

        if deterministic and field.get('seedValue') is not None:
            value = field['seedValue']
        else:
            value = _random_value_for_field(field)

        if isinstance(value, float):
            value_str = f"{value:.2f}" if value != int(value) else str(int(value))
        else:
            value_str = str(value)

        # Value type: NM numeric, ST text, etc. Observation ID: ^^^CODE^NAME (uppercase, spaces ok)
        obx_value_type = 'NM' if field_type == 'NUMERIC' else 'ST'
        obs_id = f"^^^{code}^{name_.upper()}"
        obx = f"OBX|{idx}|{obx_value_type}|{obs_id}||{value_str}|{unit}|||||F||||||"
        segments.append(obx)

    return '\n'.join(segments) + '\n'


def _random_value_for_field(field: Dict):
    """Generate a random value appropriate for the field type.

    Args:
        field: Field definition with optional normalRange/possibleValues

    Returns:
        A randomly generated value appropriate for the field type.
    """
    normal_range = field.get('normalRange', '')
    field_type = field.get('type', 'NUMERIC')

    # QUALITATIVE fields: choose from possibleValues if available
    if field_type == 'QUALITATIVE':
        possible_values = field.get('possibleValues')
        if isinstance(possible_values, (list, tuple)) and possible_values:
            return random.choice(possible_values)
        return 'UNKNOWN'

    # TEXT fields: generate a simple random alphanumeric string
    if field_type == 'TEXT':
        chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
        return ''.join(random.choices(chars, k=8))

    # NUMERIC fields: generate within normal range
    if not normal_range:
        return round(random.uniform(1, 100), 2)

    try:
        if '-' in normal_range:
            parts = normal_range.split('-')
            low = float(parts[0].strip())
            high = float(parts[1].strip())
            return round(random.uniform(low, high), 2)
        if normal_range.startswith('<'):
            max_val = float(normal_range[1:].strip())
            return round(random.uniform(0, max_val * 0.9), 2)
        if normal_range.startswith('>'):
            min_val = float(normal_range[1:].strip())
            return round(random.uniform(min_val * 1.1, min_val * 2), 2)
    except (ValueError, IndexError):
        # If normalRange format is malformed, fall back to generic range
        pass
    return round(random.uniform(1, 100), 2)
