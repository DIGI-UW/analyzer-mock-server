# Analyzer Mock Server Agent Guide

## Purpose

This repository simulates laboratory analyzers for integration and acceptance
testing. It emits deterministic analyzer-native traffic and is not a runtime
configuration authority.

## Ownership Boundary

The only supported result path is:

```text
Analyzer mock -> Analyzer Bridge -> normalized result contract -> OpenELIS
```

- Analyzer Bridge owns profiles, durable connections, listeners, parsing,
  probes, FILE watching, connection identity, and normalized transport.
- OpenELIS owns lab-facing orchestration, local catalog bindings, audit,
  activation intent, held-result review, and operational quality control.
- The mock owns protocol-faithful traffic generation, representative fixture
  files, deterministic values, and isolated analyzer network identities.

Never add a direct result path from the mock to OpenELIS, OpenELIS database
seeding, a Bridge upload shortcut for FILE traffic, or a second profile
contract.

## Profiles And Templates

Bridge profiles are authoritative. A profile has two jobs:

1. define communication and runtime behavior for one analyzer type;
2. supply defaults for creating a new Bridge connection.

A profile-backed mock template pins one exact Bridge profile revision through
`profileRef`. Set `ANALYZER_BRIDGE_PROFILES_DIR` to the Bridge profile
catalog. The adapter must fail closed when that exact revision cannot be
resolved.

Profile-backed templates may contain simulator-owned seed values and captured
fixtures. They must not duplicate analyzer identity, protocol, mappings, units,
result types, qualitative vocabularies, or FILE layout from the profile.

Production code must not special-case a profile ID, revision, manufacturer,
model, test code, fixture name, or vendor-specific value. Named analyzers belong
in profile data and parameterized test fixtures only.

## Supported Traffic

- ASTM uses framed TCP sessions.
- HL7 uses MLLP sessions.
- FILE traffic is written into the directory watched by a saved Bridge
  connection.
- The HTTP API is a test-control surface, not a result transport.

ASTM and HL7 POST requests require explicit Bridge destinations. FILE POST
requests require `target_dir`. Do not invent ports, host names, source
addresses, analyzer types, or profile defaults.

## Development

Use Python 3 and `uv`:

```bash
uv run --with-requirements requirements.txt python -m pytest
```

Start a listener with an explicit template:

```bash
export ANALYZER_BRIDGE_PROFILES_DIR=/path/to/openelis-analyzer-bridge/src/main/resources/analyzer-profiles
export ASTM_TEMPLATE=genexpert_astm
python3 server.py --port 5000 --simulate-api-port 8081
```

## Testing

Use the narrowest owning layer:

- protocol unit tests for framing, parsing, generated fields, and errors;
- real socket tests for ASTM TCP and HL7 MLLP behavior;
- fixture tests for FILE format fidelity;
- profile adapter tests for exact Bridge profile resolution;
- Bridge cross-process tests for priority analyzer traffic and normalized
  output;
- visible Playwright stories only in the assembled OpenELIS acceptance stack.

Begin behavior changes with a failing test, make the smallest green change, then
refactor while green. Tests must assert behavior. Do not add source-text or
filesystem scans for code that should be removed.

GeneXpert ASTM and Hain FluoroCycler are the current priority cross-process
fixtures. Other analyzer templates are not accepted profiles until their Bridge
profiles and traffic have equivalent evidence.

## Validation

Before a PR is review-ready:

1. Run focused tests for each changed behavior.
2. Run the complete mock test suite.
3. Run Python compilation and `git diff --check`.
4. Run the Bridge cross-process harness against this exact mock checkout.
5. Inspect logs and retained failure artifacts.
6. Confirm no result bypass, copied profile authority, hardcoded profile
   behavior, or obsolete runtime remains.
