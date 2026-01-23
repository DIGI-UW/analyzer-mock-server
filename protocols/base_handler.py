"""
Protocol abstraction layer base class (M4 multi-protocol simulator).

Reference: specs/011-madagascar-analyzer-integration/plan.md, tasks T071–T073.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Union


class BaseHandler(ABC):
    """Abstract base for protocol handlers (ASTM, HL7, Serial, File)."""

    protocol_type: str = ""

    @abstractmethod
    def generate(self, template: Dict[str, Any], **kwargs) -> Union[str, bytes]:
        """Generate a message from template and optional overrides.

        Args:
            template: Analyzer template dict (conforms to templates/schema.json).
            **kwargs: Overrides (e.g. patient_id, sample_id, tests).

        Returns:
            Message as str (HL7, ASTM text, CSV) or bytes (binary protocols).
        """
        pass

    def validate_template(self, template: Dict[str, Any]) -> bool:
        """Check template has required keys. Override for schema validation."""
        return isinstance(template, dict) and "analyzer" in template and "fields" in template
