#!/usr/bin/env python3
"""
Template-Based ASTM Message Generator

Generates ASTM LIS2-A2 messages from analyzer templates with support for
deterministic (reproducible) and randomized value generation.

Usage:
    from template_generator import TemplateMessageGenerator
    from template_loader import TemplateLoader

    loader = TemplateLoader()
    template = loader.load_template('horiba_pentra60')
    generator = TemplateMessageGenerator(template)
    message = generator.generate_astm_message(deterministic=True)

CLI:
    python template_generator.py --template horiba_pentra60 --deterministic
    python template_generator.py --template horiba_micros60 --random
"""

import argparse
import random
import sys
from datetime import datetime
from typing import Dict, List


class TemplateMessageGenerator:
    """Generates ASTM LIS2-A2 messages from analyzer templates."""

    def __init__(self, template: Dict):
        """Initialize generator with a template.

        Args:
            template: Loaded template dictionary
        """
        self.template = template
        self.analyzer = template.get('analyzer', {})
        self.protocol = template.get('protocol', {})
        self.identification = template.get('identification', {})
        self.fields = template.get('fields', [])
        self.test_patient = template.get('testPatient', {})
        self.test_sample = template.get('testSample', {})

    def generate_astm_message(self,
                               deterministic: bool = True,
                               timestamp: datetime = None,
                               patient_id: str = None,
                               sample_id: str = None) -> str:
        """Generate a complete ASTM LIS2-A2 message.

        Args:
            deterministic: If True, use seedValues from template.
                          If False, generate random values within normal ranges.
            timestamp: Optional timestamp (defaults to now)
            patient_id: Override patient ID
            sample_id: Override sample ID

        Returns:
            Complete ASTM message as newline-separated string
        """
        if timestamp is None:
            timestamp = datetime.now()

        lines = []

        # H - Header Record
        lines.append(self._generate_header(timestamp))

        # P - Patient Record
        lines.append(self._generate_patient(patient_id))

        # O - Order Record
        lines.append(self._generate_order(sample_id, timestamp))

        # R - Result Records (one per field)
        for idx, field in enumerate(self.fields, start=1):
            lines.append(self._generate_result(idx, field, timestamp, deterministic))

        # L - Terminator Record
        lines.append("L|1|N")

        return '\n'.join(lines) + '\n'

    def _generate_header(self, timestamp: datetime) -> str:
        """Generate H (Header) record."""
        astm_header = self.identification.get('astm_header',
                                               f"{self.analyzer.get('manufacturer', 'Mock')}^"
                                               f"{self.analyzer.get('model', 'Analyzer')}^V1.0")
        ts = timestamp.strftime('%Y%m%d%H%M%S')
        return f"H|\\^&|||{astm_header}|||||||LIS2-A2|{ts}"

    def _generate_patient(self, patient_id: str = None) -> str:
        """Generate P (Patient) record."""
        pid = patient_id or self.test_patient.get('id', 'PAT-TEST-001')
        name = self.test_patient.get('name', 'TEST^PATIENT')
        sex = self.test_patient.get('sex', 'M')
        dob = self.test_patient.get('dob', '19900101')
        return f"P|1||{pid}|{name}||{sex}|{dob}"

    def _generate_order(self, sample_id: str = None, timestamp: datetime = None) -> str:
        """Generate O (Order) record."""
        sid = sample_id or self.test_sample.get('id', 'SAMPLE-001')
        sample_type = self.test_sample.get('type', 'CBC^Complete Blood Count')
        ts = timestamp.strftime('%Y%m%d%H%M%S') if timestamp else ''
        return f"O|1|{sid}^LAB|{sample_type}||{ts}"

    def _generate_result(self, seq: int, field: Dict,
                         timestamp: datetime, deterministic: bool) -> str:
        """Generate R (Result) record for a single field."""
        code = field.get('code', f'TEST{seq}')
        unit = field.get('unit', '')
        normal_range = field.get('normalRange', '')

        # Get value - deterministic or random
        if deterministic:
            value = field.get('seedValue')
            if value is None:
                value = self._generate_random_value(field)
        else:
            value = self._generate_random_value(field)

        # Format value consistently for both int and float
        if isinstance(value, (int, float)):
            numeric = float(value)
            value = f"{numeric:.2f}" if numeric != int(numeric) else str(int(numeric))
        else:
            value = str(value)

        flag = 'N'  # Normal
        status = 'F'  # Final
        ts = timestamp.strftime('%Y%m%d%H%M%S')

        return f"R|{seq}|^^^{code}|{value}|{unit}|{normal_range}|{flag}||{status}|{ts}"

    def _generate_random_value(self, field: Dict):
        """Generate a random value appropriate for the field type.

        Args:
            field: Field definition with optional normalRange/possibleValues

        Returns:
            A randomly generated value:
                - float for NUMERIC fields (within normalRange when possible)
                - one of possibleValues for QUALITATIVE fields
                - a random string for TEXT fields
        """
        normal_range = field.get('normalRange', '')
        field_type = field.get('type', 'NUMERIC')

        # QUALITATIVE fields: choose from possibleValues if available
        if field_type == 'QUALITATIVE':
            possible_values = field.get('possibleValues')
            if isinstance(possible_values, (list, tuple)) and possible_values:
                return random.choice(possible_values)
            # Fallback if no possibleValues defined
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
                low = float(parts[0])
                high = float(parts[1])
                return round(random.uniform(low, high), 2)
            elif normal_range.startswith('<'):
                max_val = float(normal_range[1:])
                return round(random.uniform(0, max_val * 0.9), 2)
            elif normal_range.startswith('>'):
                min_val = float(normal_range[1:])
                return round(random.uniform(min_val * 1.1, min_val * 2), 2)
            else:
                return round(random.uniform(1, 100), 2)
        except (ValueError, IndexError):
            # If normalRange format is malformed, fall back to generic range
            return round(random.uniform(1, 100), 2)

    def get_field_count(self) -> int:
        """Get the number of fields in this template."""
        return len(self.fields)

    def get_field_codes(self) -> List[str]:
        """Get list of field codes."""
        return [f.get('code', '') for f in self.fields]


def main():
    """CLI entry point for template generator."""
    parser = argparse.ArgumentParser(
        description='Generate ASTM messages from analyzer templates'
    )
    parser.add_argument(
        '--template', '-t',
        type=str,
        required=True,
        help='Template name (without .json extension)'
    )
    parser.add_argument(
        '--deterministic', '-d',
        action='store_true',
        help='Use deterministic seed values from template'
    )
    parser.add_argument(
        '--random', '-r',
        action='store_true',
        help='Generate random values within normal ranges'
    )
    parser.add_argument(
        '--patient-id', '-p',
        type=str,
        help='Override patient ID'
    )
    parser.add_argument(
        '--sample-id', '-s',
        type=str,
        help='Override sample ID'
    )
    parser.add_argument(
        '--output', '-o',
        type=str,
        help='Output file (default: stdout)'
    )
    parser.add_argument(
        '--templates-dir',
        type=str,
        help='Templates directory path'
    )

    args = parser.parse_args()

    # Import template loader
    from template_loader import TemplateLoader

    # Load template
    try:
        loader = TemplateLoader(args.templates_dir)
        template = loader.load_template(args.template)
    except FileNotFoundError:
        print(f"Error: Template '{args.template}' not found", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error loading template: {e}", file=sys.stderr)
        return 1

    # Determine mode
    deterministic = args.deterministic or not args.random

    # Generate message
    generator = TemplateMessageGenerator(template)
    message = generator.generate_astm_message(
        deterministic=deterministic,
        patient_id=args.patient_id,
        sample_id=args.sample_id
    )

    # Output
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(message)
        print(f"Generated {generator.get_field_count()} results to {args.output}")
    else:
        print(message, end='')

    return 0


if __name__ == '__main__':
    sys.exit(main())
