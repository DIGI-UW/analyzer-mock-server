"""
Protocol handlers for multi-protocol analyzer simulator (M4).

Reference: specs/011-madagascar-analyzer-integration/plan.md.
"""

from .base_handler import BaseHandler
from .astm_handler import ASTMHandler, generate_astm_message
from .hl7_handler import HL7Handler, generate_oru_r01
from .serial_handler import SerialHandler
from .file_handler import FileHandler

__all__ = [
    "BaseHandler",
    "ASTMHandler",
    "HL7Handler",
    "SerialHandler",
    "FileHandler",
    "generate_astm_message",
    "generate_oru_r01",
]
