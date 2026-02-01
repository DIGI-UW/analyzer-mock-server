#!/usr/bin/env python3
"""
Generate CSV/TXT files from FILE protocol templates.

This script generates file-based analyzer results (CSV/TXT) from analyzer templates
that specify "protocol.type": "FILE". It supports the astm-mock-server template system
for testing file-based analyzer plugins.

Usage:
    python3 generate_file.py --template hain_fluorocycler --output /tmp/test.csv --count 5
    python3 generate_file.py -t hain_fluorocycler -o /path/to/watch/dir/results.csv -c 10
"""

from template_loader import TemplateLoader
import argparse
import random
import sys
from datetime import datetime


def generate_csv(template, output_path, count=5):
    """Generate CSV file from FILE protocol template.
    
    Args:
        template: Loaded template dictionary with fileFormat and columns
        output_path: Output file path
        count: Number of data rows to generate
    """
    # Validate protocol type
    protocol = template.get('protocol', {})
    if protocol.get('type') != 'FILE':
        print(f"Error: Template protocol type is '{protocol.get('type')}', expected 'FILE'", 
              file=sys.stderr)
        return False
    
    file_format = template.get('fileFormat', {})
    delimiter = file_format.get('delimiter', ';')
    # Sort columns by index to ensure correct output order
    columns = sorted(template.get('columns', []), key=lambda c: c.get('index', 0))
    fields = template.get('fields', [])
    test_samples = template.get('testSamples', [])
    
    if not columns:
        print("Error: Template has no columns defined", file=sys.stderr)
        return False
    
    lines = []
    
    # Write header if specified
    if file_format.get('hasHeader', True):
        header = delimiter.join([c['name'] for c in columns])
        lines.append(header)
    
    # Generate data rows - use testSamples if available, otherwise auto-generate
    if test_samples and len(test_samples) >= count:
        # Use deterministic test samples from template
        for i in range(count):
            sample = test_samples[i]
            values = []
            for col in columns:
                col_name = col['name'].lower()

                # Map column names to test sample fields
                if col_name == 'position':
                    values.append(sample.get('position', f"{chr(65 + (i // 12))}{(i % 12) + 1:02d}"))
                elif 'sample' in col_name and 'id' in col_name:
                    values.append(sample.get('sampleId', f"SIM-{datetime.now().strftime('%Y%m%d')}-{i+1:04d}"))
                elif col_name == 'result':
                    values.append(sample.get('result', ''))
                elif col_name == 'interpretation':
                    values.append(sample.get('interpretation', ''))
                else:
                    # Try to find value in sample by column name
                    values.append(sample.get(col['name'], ''))

            lines.append(delimiter.join(values))
    else:
        # Auto-generate samples
        for i in range(count):
            sample_id = f"SIM-{datetime.now().strftime('%Y%m%d')}-{i+1:04d}"
            # Generate position: A01, A02, ..., A12, B01, B02, etc.
            position = f"{chr(65 + (i // 12))}{(i % 12) + 1:02d}"

            # Generate field values for each column
            values = []
            for col in columns:
                col_name = col['name']

                # Handle special column names
                if col_name.lower() == 'position':
                    values.append(position)
                elif 'sample' in col_name.lower() and 'id' in col_name.lower():
                    values.append(sample_id)
                else:
                    # Find matching field and pick random value from possibleValues
                    value_found = False
                    for field in fields:
                        # Match by code or name (case-insensitive partial match)
                        if (field['code'].lower() in col_name.lower() or
                            col_name.lower() in field.get('name', '').lower()):
                            possible = field.get('possibleValues', ['Unknown'])
                            values.append(random.choice(possible))
                            value_found = True
                            break

                    if not value_found:
                        # Default to empty string for unmatched columns
                        values.append('')

            lines.append(delimiter.join(values))
    
    # Write to file with specified encoding
    encoding = file_format.get('encoding', 'UTF-8')
    try:
        with open(output_path, 'w', encoding=encoding) as f:
            f.write('\n'.join(lines))
            # Add trailing newline if the last line doesn't have one
            if not lines[-1].endswith('\n'):
                f.write('\n')
        
        print(f"✓ Generated {count} samples to {output_path}")
        print(f"  Template: {template.get('analyzer', {}).get('name', 'Unknown')}")
        print(f"  Delimiter: '{delimiter}'")
        print(f"  Columns: {len(columns)}")
        return True
        
    except IOError as e:
        print(f"Error writing file: {e}", file=sys.stderr)
        return False


def main():
    """CLI entry point for file generator."""
    parser = argparse.ArgumentParser(
        description='Generate CSV/TXT files from FILE protocol analyzer templates'
    )
    parser.add_argument(
        '--template', '-t',
        type=str,
        required=True,
        help='Template name (without .json extension)'
    )
    parser.add_argument(
        '--output', '-o',
        type=str,
        required=True,
        help='Output file path'
    )
    parser.add_argument(
        '--count', '-c',
        type=int,
        default=5,
        help='Number of data rows to generate (default: 5)'
    )
    parser.add_argument(
        '--templates-dir',
        type=str,
        help='Templates directory path (optional)'
    )
    
    args = parser.parse_args()
    
    # Load template
    try:
        loader = TemplateLoader(args.templates_dir)
        template = loader.load_template(args.template, validate=True)
    except FileNotFoundError:
        print(f"Error: Template '{args.template}' not found", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error loading template: {e}", file=sys.stderr)
        return 1
    
    # Generate file
    success = generate_csv(template, args.output, args.count)
    return 0 if success else 1


if __name__ == '__main__':
    sys.exit(main())
