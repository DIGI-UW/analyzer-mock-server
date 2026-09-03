# Analyzer Mock Server

> See [AGENTS.md](AGENTS.md) for repository guidance.

This project simulates laboratory analyzers for integration and acceptance
testing. It emits deterministic analyzer traffic through the same transport
that a real instrument uses.

## System Boundary

The supported result path is:

```text
Analyzer mock -> Analyzer Bridge -> normalized result contract -> OpenELIS
```

The mock never posts analyzer results directly to OpenELIS and never creates or
updates OpenELIS analyzer records. Analyzer Bridge owns protocol listeners,
connection identity, parsing, FILE watching, and normalized transport. OpenELIS
owns lab-facing setup, local catalog bindings, audit, activation, held-result
review, and operational quality control.

## Supported Analyzer Traffic

| Protocol | Mock behavior | Bridge entry point |
| --- | --- | --- |
| ASTM LIS2-A2 | Opens an ASTM TCP session and sends framed records | Saved ASTM listener |
| HL7 v2 | Opens an MLLP session and sends an ORU message | Saved MLLP listener |
| FILE | Writes a result file into the configured watch directory | Saved FILE connection |

The HTTP API is a test control surface. It tells the mock to emit analyzer
traffic; it is not a result transport to OpenELIS.

## Profile-Backed Simulation

A mock template may contain an exact Bridge `profileRef`:

```json
{
  "profileRef": {
    "profileId": "example-profile",
    "revision": 1
  }
}
```

When `profileRef` is present, set `ANALYZER_BRIDGE_PROFILES_DIR` to the
Bridge profile catalog. The mock resolves exactly that profile revision and
derives profile-owned analyzer identity, protocol, test codes, result types,
units, value vocabularies, and FILE layout. It fails closed when the revision is
missing, duplicated, or invalid.

The mock template retains only simulation concerns such as deterministic sample
values and transport fixtures. It cannot override profile-owned fields.

GeneXpert ASTM and Hain FluoroCycler are the priority cross-process fixtures.
Their messages are tested against exact Bridge profile revisions.

## Quick Start

Requirements:

- Python 3
- `uv` for the reproducible test environment
- Docker for dynamic analyzer networks and cross-process tests

Start the control API:

```bash
export ANALYZER_BRIDGE_PROFILES_DIR=/path/to/openelis-analyzer-bridge/src/main/resources/analyzer-profiles
export ASTM_TEMPLATE=genexpert_astm
python3 server.py --simulate-api-port 8081
```

Check health:

```bash
curl http://localhost:8081/health
```

Generate a message without sending it:

```bash
curl http://localhost:8081/simulate/astm/genexpert_astm
curl http://localhost:8081/simulate/file/hain_fluorocycler
```

## Send ASTM Through Bridge

```bash
curl -X POST http://localhost:8081/simulate/astm/genexpert_astm \
  -H 'Content-Type: application/json' \
  -d '{
    "destination": "tcp://127.0.0.1:12001",
    "sample_id": "LAB-2026-00001"
  }'
```

The destination must use `tcp://`. HTTP destinations are rejected.

## Send HL7 Through Bridge

```bash
curl -X POST http://localhost:8081/simulate/hl7/abbott_architect_hl7 \
  -H 'Content-Type: application/json' \
  -d '{
    "destination": "mllp://127.0.0.1:2575",
    "sample_id": "LAB-2026-00001"
  }'
```

The destination must use `mllp://`. HTTP destinations are rejected.

## Write FILE Traffic For Bridge

```bash
curl -X POST http://localhost:8081/simulate/file/hain_fluorocycler \
  -H 'Content-Type: application/json' \
  -d '{
    "target_dir": "/data/analyzer-imports/fluorocycler",
    "filename": "result.xlsx"
  }'
```

`target_dir` must be the directory watched by the saved Bridge FILE
connection. Bridge discovers and transports the file through that watched
directory.

## Dynamic Analyzer Instances

The control API can provision isolated mock analyzer network identities:

```bash
curl -X POST http://localhost:8081/analyzers \
  -H 'Content-Type: application/json' \
  -d '{"name":"genexpert-lab-1","template":"genexpert_astm"}'

curl http://localhost:8081/analyzers
```

Use the instance name in a `/simulate/{protocol}/{name}` request when a test
must send from that instance's source address. Network provisioning is test
infrastructure only; Bridge remains the durable connection authority.

## Tests

Run the complete mock suite:

```bash
uv run --with-requirements requirements.txt python -m pytest
```

The Bridge repository owns the cross-process result-traffic harness. Point that
harness at this checkout to exercise the real mock process, Bridge listeners,
profile parsing, and normalized OpenELIS contract.

Focused tests should assert observable protocol behavior. Do not add filesystem
scans or source-text guards for code that should simply be removed.

## Repository Layout

- `api.py`: HTTP test-control API
- `server.py`: analyzer listener and command-line entry point
- `push.py`: ASTM TCP and HL7 MLLP senders
- `protocols/`: protocol message generation and framing
- `templates/`: deterministic simulator inputs
- `profile_adapter.py`: exact Bridge profile revision adapter
- `fixtures/`: representative analyzer output files
- `tests/` and `test_*.py`: behavior and contract tests

## Standards

- ASTM E1381/E1394 and LIS2-A2 where implemented by analyzer fixtures
- HL7 v2 ORU over MLLP
- Analyzer-specific FILE formats represented by curated fixtures

This repository simulates analyzer behavior. Bridge profile and runtime
contracts remain authoritative.
