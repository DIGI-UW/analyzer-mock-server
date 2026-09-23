"""Per-instance sender identity for simulated instruments.

A real instrument names itself in the message header: a GeneXpert PC puts the
System Name from its own configuration in component 1 of ASTM H.5 and HL7 MSH-3
(Cepheid LIS Interface Protocol Specification 302-2261, Rev F). Templates carry
only the type-level token, so every simulated instance of a template would
otherwise look identical to the LIS. These helpers replace component 1 of that
field and leave every other byte of the message untouched.
"""

from typing import Optional


def _replace_first_component(field: str, component_separator: str, sender_id: str) -> str:
    components = field.split(component_separator)
    components[0] = sender_id
    return component_separator.join(components)


def _rewrite_lines(message: str, rewrite_line) -> str:
    # Preserve whichever record separator the generator used (ASTM "\n", HL7 "\r").
    separator = "\r" if "\r" in message else "\n"
    return separator.join(rewrite_line(line) for line in message.split(separator))


def with_astm_sender_id(message: str, sender_id: Optional[str]) -> str:
    """Set component 1 of H.5 (Sender Name or ID) in every header record."""
    if not sender_id:
        return message

    def rewrite(line: str) -> str:
        if not line.startswith("H|"):
            return line
        fields = line.split("|")
        # fields[1] is the delimiter definition, e.g. "\^&": repeat, component, escape.
        component_separator = fields[1][1] if len(fields) > 1 and len(fields[1]) > 1 else "^"
        while len(fields) <= 4:
            fields.append("")
        fields[4] = _replace_first_component(fields[4], component_separator, sender_id)
        return "|".join(fields)

    return _rewrite_lines(message, rewrite)


def with_hl7_sender_id(message: str, sender_id: Optional[str]) -> str:
    """Set component 1 of MSH-3 (Sending Application) in every message header."""
    if not sender_id:
        return message

    def rewrite(line: str) -> str:
        if not line.startswith("MSH|"):
            return line
        fields = line.split("|")
        # fields[1] is MSH-2, the encoding characters; its first character separates components.
        component_separator = fields[1][0] if len(fields) > 1 and fields[1] else "^"
        while len(fields) <= 2:
            fields.append("")
        fields[2] = _replace_first_component(fields[2], component_separator, sender_id)
        return "|".join(fields)

    return _rewrite_lines(message, rewrite)
