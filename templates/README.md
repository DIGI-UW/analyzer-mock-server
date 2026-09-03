# Analyzer Simulator Templates

This directory contains deterministic simulator inputs for ASTM, HL7, and FILE
analyzer traffic.

## Authority

Analyzer Bridge profiles are authoritative for analyzer type behavior and
connection defaults. A mock template is not another profile.

A profile-backed template references one exact Bridge profile revision:

```json
{
  "profileRef": {
    "profileId": "example-profile",
    "revision": 1
  },
  "seedValues": {
    "TEST-CODE": "NOT DETECTED"
  }
}
```

Set `ANALYZER_BRIDGE_PROFILES_DIR` before loading such a template. The adapter
fails closed unless exactly one matching profile revision exists and has a
revision fingerprint.

## Ownership

Bridge profile data supplies:

- analyzer identity
- protocol and version
- analyzer test codes
- result types and units
- qualitative value vocabularies
- FILE format and column mapping

The mock template may supply only simulation concerns:

- deterministic result values
- representative patient and sample data
- captured fixture files
- transport-fidelity details needed to reproduce analyzer traffic

A profile-backed template cannot duplicate or override profile-owned fields.

## Priority Fixtures

The priority acceptance fixtures are:

| Template | Traffic | Profile source |
| --- | --- | --- |
| `genexpert_astm.json` | ASTM TCP | Exact Bridge profile revision |
| `hain_fluorocycler.json` | FILE watch directory | Exact Bridge profile revision |

These are exercised across the real mock process and Bridge runtime. Other
standalone templates remain simulator coverage until their corresponding Bridge
profiles are standardized and validated.

## Generate Traffic

Start the control API:

```bash
export ANALYZER_BRIDGE_PROFILES_DIR=/path/to/openelis-analyzer-bridge/src/main/resources/analyzer-profiles
export ASTM_TEMPLATE=genexpert_astm
python3 server.py --simulate-api-port 8081
```

Generate without sending:

```bash
curl http://localhost:8081/simulate/astm/genexpert_astm
curl http://localhost:8081/simulate/file/hain_fluorocycler
```

Send ASTM through a saved Bridge listener:

```bash
curl -X POST http://localhost:8081/simulate/astm/genexpert_astm \
  -H 'Content-Type: application/json' \
  -d '{"destination":"tcp://127.0.0.1:12001"}'
```

Write FILE traffic into a saved Bridge connection's watch directory:

```bash
curl -X POST http://localhost:8081/simulate/file/hain_fluorocycler \
  -H 'Content-Type: application/json' \
  -d '{"target_dir":"/data/analyzer-imports/fluorocycler"}'
```

## Add A Simulator Fixture

1. Establish or identify the authoritative Bridge profile revision.
2. Add a template with only `profileRef` plus simulation-owned values and
   fixtures.
3. Add representative protocol traffic captured from instrument evidence.
4. Validate the template and exact profile resolution.
5. Add protocol tests in this repository.
6. Add a cross-process Bridge test before treating the analyzer as validated.

Do not copy profile mappings into the mock, invent default connection values, or
send results directly to OpenELIS.

## Validation

Validate one template:

```bash
python3 template_loader.py --validate templates/genexpert_astm.json
```

Run all mock tests:

```bash
uv run --with-requirements requirements.txt python -m pytest
```

The Bridge cross-process harness is the acceptance layer for profile-backed
transport and normalized result output.
