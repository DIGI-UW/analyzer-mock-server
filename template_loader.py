#!/usr/bin/env python3
"""
Template Loader for ASTM Mock Server

Loads and validates analyzer templates against the JSON schema.
Templates define analyzer-specific field lists, ASTM identifiers,
and deterministic seed values for reproducible testing.

Usage:
    from template_loader import TemplateLoader
    loader = TemplateLoader()
    template = loader.load_template('horiba_pentra60')

CLI:
    python template_loader.py --list
    python template_loader.py --validate templates/horiba_pentra60.json
"""

import json
import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any

# Try to import jsonschema, provide helpful message if not installed
try:
    import jsonschema
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False


class TemplateLoader:
    """Loads and validates analyzer templates from JSON files."""

    def __init__(self, templates_dir: str = None):
        """Initialize loader with templates directory.

        Args:
            templates_dir: Path to templates directory. Defaults to 'templates/'
                          relative to this script.
        """
        if templates_dir is None:
            self.templates_dir = Path(__file__).parent / 'templates'
        else:
            self.templates_dir = Path(templates_dir)

        self.schema = self._load_schema()
        self._template_cache: Dict[str, Dict] = {}

    def _load_schema(self) -> Optional[Dict]:
        """Load JSON schema for template validation."""
        schema_path = self.templates_dir / 'schema.json'

        if not schema_path.exists():
            print(f"Warning: Schema file not found at {schema_path}", file=sys.stderr)
            return None

        try:
            with open(schema_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in schema file: {e}", file=sys.stderr)
            return None

    def load_template(self, template_name: str, validate: bool = True) -> Dict:
        """Load a template by name.

        Args:
            template_name: Template name (without .json extension)
            validate: Whether to validate against schema (default True)

        Returns:
            Template dictionary

        Raises:
            FileNotFoundError: If template file doesn't exist
            json.JSONDecodeError: If template is invalid JSON
            jsonschema.ValidationError: If template fails schema validation
        """
        # Check cache first
        if template_name in self._template_cache:
            return self._template_cache[template_name]

        # Build template path
        template_path = self.templates_dir / f'{template_name}.json'

        if not template_path.exists():
            raise FileNotFoundError(f"Template not found: {template_path}")

        # Load template
        with open(template_path, 'r', encoding='utf-8') as f:
            template = json.load(f)

        # Validate against schema
        if validate and self.schema and HAS_JSONSCHEMA:
            jsonschema.validate(template, self.schema)

        # Cache and return
        self._template_cache[template_name] = template
        return template

    def validate_template(self, template_path: str) -> bool:
        """Validate a template file against the schema.

        Args:
            template_path: Path to template file

        Returns:
            True if valid, False otherwise
        """
        if not HAS_JSONSCHEMA:
            print("Warning: jsonschema not installed. Install with: pip install jsonschema")
            print("Performing basic JSON syntax check only.")

        try:
            with open(template_path, 'r', encoding='utf-8') as f:
                template = json.load(f)

            if HAS_JSONSCHEMA and self.schema:
                jsonschema.validate(template, self.schema)

            return True

        except json.JSONDecodeError as e:
            print(f"Invalid JSON: {e}", file=sys.stderr)
            return False
        except Exception as e:
            # Handle jsonschema.ValidationError when jsonschema is installed
            if HAS_JSONSCHEMA and isinstance(e, jsonschema.ValidationError):
                print(f"Schema validation failed: {e.message}", file=sys.stderr)
            else:
                print(f"Validation error: {e}", file=sys.stderr)
            return False

    def list_templates(self) -> List[str]:
        """List all available template names.

        Returns:
            List of template names (without .json extension)
        """
        templates = []

        if self.templates_dir.exists():
            for f in self.templates_dir.glob('*.json'):
                if f.stem != 'schema':  # Exclude schema.json
                    templates.append(f.stem)

        return sorted(templates)

    def get_template_info(self, template_name: str) -> Dict[str, Any]:
        """Get summary information about a template.

        Args:
            template_name: Template name

        Returns:
            Dictionary with template info (name, manufacturer, field count, etc.)
        """
        template = self.load_template(template_name, validate=False)

        analyzer = template.get('analyzer', {})
        protocol = template.get('protocol', {})
        fields = template.get('fields', [])

        return {
            'name': analyzer.get('name', 'Unknown'),
            'manufacturer': analyzer.get('manufacturer', 'Unknown'),
            'model': analyzer.get('model', ''),
            'protocol': protocol.get('type', 'Unknown'),
            'version': protocol.get('version', ''),
            'field_count': len(fields),
            'fields': [f.get('code', '') for f in fields]
        }


def main():
    """CLI entry point for template loader."""
    parser = argparse.ArgumentParser(
        description='Load and validate ASTM mock server analyzer templates'
    )
    parser.add_argument(
        '--list', '-l',
        action='store_true',
        help='List all available templates'
    )
    parser.add_argument(
        '--validate', '-v',
        type=str,
        metavar='FILE',
        help='Validate a template file against the schema'
    )
    parser.add_argument(
        '--info', '-i',
        type=str,
        metavar='NAME',
        help='Show information about a template'
    )
    parser.add_argument(
        '--templates-dir', '-d',
        type=str,
        help='Templates directory path'
    )

    args = parser.parse_args()

    loader = TemplateLoader(args.templates_dir)

    if args.list:
        templates = loader.list_templates()
        if templates:
            print("Available templates:")
            for t in templates:
                try:
                    info = loader.get_template_info(t)
                    print(f"  {t}: {info['name']} ({info['field_count']} fields)")
                except Exception as e:
                    print(f"  {t}: (error loading: {e})")
        else:
            print("No templates found.")
        return 0

    if args.validate:
        print(f"Validating: {args.validate}")
        if loader.validate_template(args.validate):
            print("✅ Template is valid")
            return 0
        else:
            print("❌ Template validation failed")
            return 1

    if args.info:
        try:
            info = loader.get_template_info(args.info)
            print(f"Template: {args.info}")
            print(f"  Analyzer: {info['name']}")
            print(f"  Manufacturer: {info['manufacturer']}")
            print(f"  Model: {info['model']}")
            print(f"  Protocol: {info['protocol']} {info['version']}")
            print(f"  Fields ({info['field_count']}): {', '.join(info['fields'])}")
            return 0
        except FileNotFoundError:
            print(f"Template not found: {args.info}")
            return 1

    parser.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())
