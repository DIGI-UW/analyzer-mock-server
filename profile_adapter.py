#!/usr/bin/env python3
"""Derive analyzer-mock fields from an exact Bridge profile revision.

Profile-backed templates pin an immutable Bridge profile with ``profileRef``.
The adapter resolves the exact ID and catalog revision and fails closed if the
catalog does not contain exactly one match. Transport mechanics and deterministic
simulation values remain mock-owned.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Default qualitative vocabulary when a profile mapping declares a qualitative
# result_type but no explicit `values`. Aligns (case-insensitively) with the
# common OE2 dictionary entries Detected/Not Detected.
_QUALITATIVE_TYPES = {"qualitative", "ordinal", "nominal", "ord", "nom", "qual"}
_PROFILE_OWNED_FIELD_KEYS = {"code", "loinc", "unit", "type", "possibleValues"}
_PROFILE_OWNED_TEMPLATE_KEYS = {
    "analyzer",
    "protocol",
    "fields",
    "fileFormat",
    "file_config",
    "columns",
}
_MOCK_COLUMN_KEYS = {
    "sampleId": "sample_id",
    "testCode": "test_code",
    "result": "result",
}


class ProfileResolutionError(RuntimeError):
    """The requested immutable Bridge profile revision cannot be resolved."""


def _bridge_profiles_root() -> Path:
    configured = os.environ.get("ANALYZER_BRIDGE_PROFILES_DIR")
    if not configured:
        raise ProfileResolutionError(
            "ANALYZER_BRIDGE_PROFILES_DIR is required for a versioned profileRef"
        )
    root = Path(configured)
    if not root.is_dir():
        raise ProfileResolutionError(f"Bridge profile catalog does not exist: {root}")
    return root


def _load_exact_bridge_profile(profile_ref: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(profile_ref, dict):
        raise ProfileResolutionError("profileRef must contain profileId and revision")

    profile_id = profile_ref.get("profileId")
    revision = profile_ref.get("revision")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ProfileResolutionError("profileRef.profileId must be a non-empty string")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ProfileResolutionError("profileRef.revision must be a positive integer")

    matches = []
    for path in sorted(_bridge_profiles_root().rglob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                candidate = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileResolutionError(f"Cannot read Bridge profile document {path}: {exc}") from exc

        if (
            candidate.get("profileMeta", {}).get("id") == profile_id
            and candidate.get("catalog", {}).get("revision") == revision
        ):
            matches.append((path, candidate))

    exact_ref = f"{profile_id}@{revision}"
    if not matches:
        raise ProfileResolutionError(f"Bridge profile {exact_ref} was not found")
    if len(matches) > 1:
        paths = ", ".join(str(path) for path, _ in matches)
        raise ProfileResolutionError(
            f"Bridge profile {exact_ref} matched multiple documents: {paths}"
        )

    profile = matches[0][1]
    fingerprint = profile.get("catalog", {}).get("revisionFingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ProfileResolutionError(
            f"Bridge profile {exact_ref} has no catalog revision fingerprint"
        )
    return profile


def _default_negative(values: List[str]) -> str:
    """Pick a sensible 'no-finding' default from a qualitative vocabulary."""
    for v in values:
        u = v.upper()
        if "NOT DETECTED" in u or u in ("NEGATIVE", "NEG", "NOT DETECTED"):
            return v
    return values[-1] if values else "NOT DETECTED"


def _required_profile_text(profile: Dict[str, Any], key: str, source_label: str) -> str:
    value = profile.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProfileResolutionError(
            f"Analyzer profile {source_label} must declare non-empty {key}"
        )
    return value


def _profile_file_config(profile: Dict[str, Any]) -> Dict[str, Any]:
    defaults = profile.get("configDefaults") or {}
    protocol = profile.get("protocol") or {}
    config: Dict[str, Any] = {}

    file_format = defaults.get("fileFormat") or protocol.get("format")
    if isinstance(file_format, str) and file_format.strip():
        config["format"] = file_format
    if isinstance(defaults.get("hasHeader"), bool):
        config["has_header"] = defaults["hasHeader"]

    columns: Dict[str, str] = {}
    for header, normalized_key in (profile.get("column_mapping") or {}).items():
        mock_key = _MOCK_COLUMN_KEYS.get(normalized_key)
        if mock_key and mock_key not in columns:
            columns[mock_key] = header
    if columns:
        config["column_mapping"] = columns
    return config


def load_profile_backed_template(
    template_name: str, transport_template: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Derive a full mock template by merging the transport template with the
    assay `fields` built from its referenced canonical profile.

    Returns the merged template, or None if the transport template declares no
    profile reference and the caller should use its inline fields.
    """
    exact_profile_ref = transport_template.get("profileRef")
    if not exact_profile_ref:
        return None

    duplicate_keys = sorted(_PROFILE_OWNED_TEMPLATE_KEYS.intersection(transport_template))
    if "file_pattern" in (transport_template.get("identification") or {}):
        duplicate_keys.append("identification.file_pattern")
    if duplicate_keys:
        names = ", ".join(duplicate_keys)
        raise ProfileResolutionError(
            f"Profile-backed mock template {template_name} duplicates profile-owned fields: {names}"
        )

    profile = _load_exact_bridge_profile(exact_profile_ref)
    profile_id = profile["profileMeta"]["id"]
    revision = profile["catalog"]["revision"]
    source_label = f"{profile_id}@{revision}"

    protocol_profile = profile.get("protocol")
    if not isinstance(protocol_profile, dict):
        raise ProfileResolutionError(
            f"Analyzer profile {source_label} must declare protocol"
        )
    protocol_name = protocol_profile.get("name")
    if not isinstance(protocol_name, str) or not protocol_name.strip():
        raise ProfileResolutionError(
            f"Analyzer profile {source_label} must declare protocol.name"
        )

    seed_values = transport_template.get("seedValues", {})
    # Per-code simulation-fidelity overrides (mock test data, NOT coverage): e.g. a
    # GeneXpert assay's wire vocabulary, cartridge `version` for the 8-component test
    # ID, or `complementaryResults` (Ct/Conc sub-results). Merged onto the
    # profile-derived field so the profile still owns the assay menu + result_type.
    field_overrides = transport_template.get("fieldOverrides", {})

    fields: List[Dict[str, Any]] = []
    for mapping in profile.get("default_test_mappings", []):
        code = (
            mapping.get("test_code")
            or mapping.get("obx_identifier")
            or mapping.get("analyzer_code")
        )
        if not code:
            continue
        unit = mapping.get("unit", "")
        result_type = str(mapping.get("result_type", "")).strip().lower()
        field: Dict[str, Any] = {
            "code": code,
            "loinc": mapping.get("loinc"),
            "unit": unit,
            "name": mapping.get("test_name_hint", code),
        }
        if result_type in _QUALITATIVE_TYPES:
            values = mapping.get("values")
            if not isinstance(values, list) or not values:
                raise ProfileResolutionError(
                    f"Analyzer profile {source_label} qualitative test {code} must declare result values"
                )
            field["type"] = "QUALITATIVE"
            field["possibleValues"] = values
            seed_q = seed_values.get(code)
            field["seedQualitative"] = seed_q if seed_q is not None else _default_negative(values)
        else:
            # Default to NUMERIC when result_type is absent or quantitative.
            # Always carry a deterministic seedValue (0 when none supplied) so the
            # mock is reproducible — un-seeded numerics must not emit random values.
            field["type"] = "NUMERIC"
            field["seedValue"] = seed_values.get(code, 0)
        overrides = field_overrides.get(code)
        if overrides:
            owned_overrides = _PROFILE_OWNED_FIELD_KEYS.intersection(overrides)
            if owned_overrides:
                names = ", ".join(sorted(owned_overrides))
                raise ProfileResolutionError(
                    f"Mock override for {source_label} test {code} changes profile-owned fields: {names}"
                )
            field.update(overrides)
        fields.append(field)

    merged: Dict[str, Any] = dict(transport_template)
    merged["fields"] = fields
    merged["resolvedProfile"] = {
        "profileId": profile["profileMeta"]["id"],
        "revision": profile["catalog"]["revision"],
        "revisionFingerprint": profile["catalog"]["revisionFingerprint"],
    }
    merged["analyzer"] = {
        "name": _required_profile_text(profile, "analyzer_name", source_label),
        "model": _required_profile_text(profile, "model", source_label),
        "manufacturer": _required_profile_text(profile, "manufacturer", source_label),
        "category": _required_profile_text(profile, "category", source_label),
    }
    merged["protocol"] = {"type": protocol_name}
    protocol_version = protocol_profile.get("version")
    if isinstance(protocol_version, str) and protocol_version.strip():
        merged["protocol"]["version"] = protocol_version
    if protocol_name == "FILE":
        merged["file_config"] = _profile_file_config(profile)
    logger.info(
        "Profile-backed template '%s' from %s: %d assays",
        template_name, source_label, len(fields),
    )
    return merged
