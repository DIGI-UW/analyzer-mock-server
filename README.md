# ASTM LIS2-A2 Mock Server

> **For AI Agents:** See [AGENTS.md](AGENTS.md) for context and instructions.

A simple mock server that simulates an ASTM-compatible laboratory analyzer for
testing the OpenELIS analyzer field mapping feature.

## Overview

This server implements a minimal subset of the ASTM LIS2-A2 protocol sufficient
for:

- Testing analyzer connection (ENQ/ACK handshake)
- Querying available analyzer fields
- Receiving sample results
- Processing QC (Quality Control) data

## Quick Start

### Running Locally

```bash
# Start the server
python server.py

# With custom options
python server.py --port 5000 --analyzer-type HEMATOLOGY --response-delay 100 --verbose
```

### Running with Docker

```bash
# Build the image
docker build -t astm-mock-server .

# Run the container
docker run -p 5000:5000 astm-mock-server

# With environment variables
docker run -p 5000:5000 \
  -e ANALYZER_TYPE=CHEMISTRY \
  -e RESPONSE_DELAY_MS=50 \
  astm-mock-server
```

### Running with Docker Compose (OpenELIS Integration)

From the repository root:

```bash
# Start OpenELIS with mock ASTM server
docker compose -f dev.docker-compose.yml -f docker-compose.astm-test.yml up -d
```

## Configuration

### Environment Variables

| Variable               | Default    | Description                                              |
| ----------------------- | ---------- | --------------------------------------------------------- |
| `ASTM_PORT`             | 5000       | Server port (single-port mode)                            |
| `ASTM_TEMPLATE`         | —          | Template name for single-port mode (e.g. `genexpert_astm`)|
| `ASTM_PORT_TEMPLATES`   | —          | JSON map of port→template for multi-port (overrides file) |
| `ANALYZER_TYPE`         | HEMATOLOGY | Default analyzer type                                    |
| `RESPONSE_DELAY_MS`     | 100        | Simulated response delay                                 |

**Port-to-template (multi-port):** When `config/port_templates.json` exists (or `ASTM_PORT_TEMPLATES` is set), the server listens on all ports listed in that mapping (and does not use `ASTM_PORT`). Include `5000` in the mapping if you want the default port to remain available (e.g. for `python server.py` or Docker with no overrides). Example: connection to port 9601 uses the template mapped to 9601 (e.g. `mindray_ba88a`). Optional: send a frame `X|1|template_name|` before other records to override the port-based template.

### Field Configuration

Edit `fields.json` to customize available analyzer fields:

```json
{
  "HEMATOLOGY": [
    {"name": "WBC", "type": "NUMERIC", "unit": "10^3/μL"},
    ...
  ],
  "CHEMISTRY": [...],
  "IMMUNOLOGY": [...],
  "MICROBIOLOGY": [...]
}
```

## ASTM Protocol Reference

### Control Characters

| Char | Hex  | Name                | Description        |
| ---- | ---- | ------------------- | ------------------ |
| ENQ  | 0x05 | Enquiry             | Start transmission |
| ACK  | 0x06 | Acknowledge         | Positive response  |
| NAK  | 0x15 | Neg Acknowledge     | Negative response  |
| EOT  | 0x04 | End of Transmission | End session        |
| STX  | 0x02 | Start of Text       | Frame start        |
| ETX  | 0x03 | End of Text         | Frame end          |
| CR   | 0x0D | Carriage Return     | Record separator   |
| LF   | 0x0A | Line Feed           |                    |

### Message Flow

1. Client sends ENQ
2. Server responds ACK (ready) or NAK (busy)
3. Client sends data frames: `<STX><FN><data><ETX><checksum><CR><LF>`
4. Server ACKs each frame
5. Client sends EOT to end transmission

### Record Types

| Type | Description | Example |
| ---- | ----------- | ------- | --- | -------------- | --------- | ---------------------- | ---------- | ------ | ---- |
| H    | Header      | `H      | \^& |                |           | Analyzer^Model^Version | ...`       |
| P    | Patient     | `P      | 1   |                | PatientID |                        |            | Gender | DOB` |
| O    | Order       | `O      | 1   | SampleID       |           | TestCode               | ...`       |
| R    | Result      | `R      | 1   | ^^^TestCode    | Value     | Unit                   | Flag`      |
| Q    | QC          | `Q      | 1   | Test^Lot^Level | Value     | Unit                   | Timestamp` |
| L    | Terminator  | `L      | 1   | N`             |

## Push Mode: Sending Results to OpenELIS

The mock server can push ASTM messages directly to OpenELIS via HTTP POST,
simulating an analyzer sending results:

```bash
# Push a single message
python server.py --push https://localhost:8443 --analyzer-type HEMATOLOGY

# Push multiple messages with interval
python server.py --push https://localhost:8443 --analyzer-type CHEMISTRY --push-count 5 --push-interval 2

# Verbose output
python server.py --push https://localhost:8443 --analyzer-type IMMUNOLOGY --verbose
```

**Push Options:**

- `--push URL`: OpenELIS base URL (e.g., `https://localhost:8443`)
- `--push-count N`: Number of messages to push (default: 1, ignored if
  `--push-continuous` used)
- `--push-interval N`: Seconds between pushes (default: 1)
- `--push-continuous`: Continuous push mode - push messages indefinitely
  (simulates real analyzer behavior)
- `--analyzer-type TYPE`: Analyzer type for message generation (HEMATOLOGY,
  CHEMISTRY, etc.)

**Push Modes:**

1. **Batch Mode** (default): Push a specified number of messages

   ```bash
   python server.py --push https://localhost:8443 --push-count 5 --push-interval 2
   ```

2. **Continuous Mode** (simulates real analyzer): Push messages indefinitely
   ```bash
   python server.py --push https://localhost:8443 --push-continuous --push-interval 30
   ```
   This mode simulates how real analyzers push results automatically when ready.
   Press Ctrl+C to stop.

**How It Works:**

1. Generates complete ASTM messages (H, P, O, R, L segments) based on field
   configuration
2. Creates realistic test data (patient IDs, sample IDs, result values)
3. Sends messages via HTTP POST to `/api/OpenELIS-Global/analyzer/astm`
4. Handles SSL verification (disabled for development with self-signed certs)

**Note:**

- Push mode requires OpenELIS to be running and accessible
- Messages are sent without authentication (OpenELIS may require session-based
  auth for production use)
- Continuous mode (`--push-continuous`) simulates real analyzer behavior where
  results are pushed automatically when ready

### API Mode: HTTP API for Triggering Pushes

The mock server can run an HTTP API server that accepts requests to trigger
pushes:

```bash
# Start API server on port 8080
python server.py --push https://localhost:8443 --api-port 8080
```

**API Endpoints:**

1. **POST `/push`** - Trigger a push to OpenELIS

   - Query parameters:
     - `analyzer_type` (optional): HEMATOLOGY, CHEMISTRY, IMMUNOLOGY, etc.
       (default: HEMATOLOGY)
     - `count` (optional): Number of messages to push (default: 1)
   - Request body (JSON, optional):
     ```json
     {
       "analyzer_type": "CHEMISTRY",
       "count": 3
     }
     ```
   - Response:
     ```json
     {
       "status": "completed",
       "total": 3,
       "successful": 3,
       "failed": 0,
       "results": [
         { "message_number": 1, "success": true, "analyzer_type": "CHEMISTRY" },
         { "message_number": 2, "success": true, "analyzer_type": "CHEMISTRY" },
         { "message_number": 3, "success": true, "analyzer_type": "CHEMISTRY" }
       ]
     }
     ```

2. **GET `/health`** - Health check and API information

**Usage Examples:**

```bash
# Trigger single push via curl
curl -X POST "http://localhost:8080/push?analyzer_type=HEMATOLOGY&count=1"

# Trigger multiple pushes with JSON body
curl -X POST http://localhost:8080/push \
  -H "Content-Type: application/json" \
  -d '{"analyzer_type": "CHEMISTRY", "count": 5}'

# Health check
curl http://localhost:8080/health
```

**Use Cases:**

- Automated testing scripts
- Integration with CI/CD pipelines
- Manual testing via HTTP client (Postman, etc.)
- Triggering pushes from other services

### HL7 ORU^R01 Simulation

The server can generate HL7 v2.x ORU^R01 (Observation Report) messages from
template definitions for OpenELIS analyzer plugins (e.g. Abbott Architect).
Templates live in `templates/` and are selected by name.

**Run locally (CLI):**

```bash
# Push one Abbott Architect ORU^R01 to OpenELIS
python server.py --hl7 --push https://localhost:8443 --hl7-template abbott_architect_hl7

# Push multiple messages
python server.py --hl7 --push https://localhost:8443 --hl7-template abbott_architect_hl7 --push-count 3

# Continuous HL7 push (Ctrl+C to stop)
python server.py --hl7 --push https://localhost:8443 --push-continuous
```

**HTTP API (when running with `--api-port`):**

- **GET `/simulate/hl7/<analyzer>`** – Generate one HL7 ORU^R01 for the template
  (e.g. `abbott_architect_hl7`) and return it as `text/plain`.
- **POST `/simulate/hl7/<analyzer>`** – Generate one or more messages; optionally
  push to OpenELIS. Body (JSON): `{ "count": 1, "destination": "https://localhost:8443" }`.

```bash
# Generate one Abbott message (no push)
curl http://localhost:8080/simulate/hl7/abbott_architect_hl7

# Generate and push to OpenELIS
curl -X POST http://localhost:8080/simulate/hl7/abbott_architect_hl7 \
  -H "Content-Type: application/json" \
  -d '{"count": 2, "destination": "https://localhost:8443"}'
```

HL7 messages use template `identification` (e.g. `hl7_sending_app` → MSH-3) so
OpenELIS can route to the correct analyzer plugin.

## QC (Quality Control) Push Mode

The mock server can generate and push QC messages that flow through OE's full
Westgard rule evaluation pipeline. QC messages use the ASTM R+Q segment pattern
where R carries the measurement and Q identifies the control lot.

### Prerequisites

Before pushing QC data, OE must have the required metadata seeded:

```bash
# Generate and apply the seed SQL (analyzers, tests, control lots, Westgard rules)
python generate_analyzer_sql.py
docker exec -i openelisglobal-database psql -U clinlims -d clinlims < seed_analyzers.sql
```

This seeds:
- Analyzers with `identifier_pattern` for ASTM header matching
- `analyzer_test_map` entries (field code → OE test)
- `qc_control_lot` with manufacturer mean/SD (calculation method: `MANUFACTURER_FIXED`)
- `qc_statistics` for immediate z-score calculation
- `westgard_rule_config` with all 8 standard rules enabled per test/instrument

### Usage

```bash
# Normal QC: realistic random scatter around target (Gaussian noise)
# Values vary each run, z-scores typically between -2 and +2
python server.py --push tcp://localhost:12000 --template horiba_pentra60 --qc

# Exact target: z-score = 0, always ACCEPTED
python server.py --push tcp://localhost:12000 --template horiba_pentra60 --qc --qc-deviation 0

# Warning level: 2.5 SD above target (triggers 1₂ₛ WARNING)
python server.py --push tcp://localhost:12000 --template horiba_pentra60 --qc --qc-deviation 2.5

# Rejection level: 3.5 SD above target (triggers 1₃ₛ REJECTION)
python server.py --push tcp://localhost:12000 --template horiba_pentra60 --qc --qc-deviation 3.5

# Below target (negative deviation)
python server.py --push tcp://localhost:12000 --template horiba_pentra60 --qc --qc-deviation -3.5

# Simulate 30 days of daily QC runs
python server.py --push tcp://localhost:12000 --template horiba_pentra60 --qc -c 30

# Preview without sending (dry run)
python server.py --push tcp://localhost:12000 --template horiba_pentra60 --qc --dry-run
```

### QC Flags

| Flag | Description |
| --- | --- |
| `--qc` | Enable QC mode (uses template's `qc_controls` section) |
| `--qc-deviation SD` | Shift all results by exactly N standard deviations from target. Omit for realistic random scatter. |
| `--dry-run` | Print the generated ASTM message without sending it |
| `-c N` | Number of messages to push (default: 1) |

### How It Works

1. The mock server reads the template's `qc_controls` section (target values, lot numbers, levels)
2. For each QC field, it generates a value:
   - **No `--qc-deviation`**: random Gaussian scatter around target (SD = 5% of target for hematology)
   - **`--qc-deviation N`**: exact shift of N standard deviations from target
3. Builds an ASTM message with R+Q segment pairs per field
4. Sends via TCP with ASTM LIS2-A2 framing (ENQ/ACK/STX/ETX) to the bridge
5. The bridge forwards to OE's `/analyzer/astm` endpoint
6. OE's GenericASTM plugin parses the message, creates `analyzer_results`, then
   the QC pipeline matches Q segments to control lots and evaluates Westgard rules

### ASTM QC Message Structure

`generate_qc` / `--qc` mints a **SiteYearNum** specimen ID on lane `98`, pads the O-record so **O.12 = Q** (GenericASTM), and emits R+Q pairs. Patient line below uses a non-accession placeholder ID only.

```
H|\^&|||ABX^PENTRA60^V2.0|||||||LIS2-A2|20260320150301    ← Header (analyzer ID)
P|1||QCCTRL001|QC^Control||U|19000101                    ← Patient (not the lab accession)
O|1|DEV01269800000000001|||||||||Q|||||||||||||           ← O.3 accession; O.12=Q
R|1|^^^WBC|5.55|10^3/uL|4.0-10.0|N||F|20260320150301      ← Result (measurement)
Q|1|WBC^LOT-WBC-N^N|5.55|10^3/uL|20260320150301           ← QC metadata (lot + level)
R|2|^^^RBC|5.02|10^6/uL|4.0-5.5|N||F|20260320150301
Q|2|RBC^LOT-RBC-N^N|5.02|10^6/uL|20260320150301
...
L|1|N                                                      ← Terminator
```

- **R segment**: the measured value — goes into `analyzer_results`
- **Q segment**: identifies the control lot (`WBC^LOT-WBC-N^N` = test^lot_number^level) — triggers QC evaluation
- **O and R segments** are required by OE's current implementation even for pure QC data

### Westgard Rules

The seed script enables all 8 standard Westgard rules for each QC test/instrument:

| Rule | Severity | Corrective Action | Trigger |
| --- | --- | --- | --- |
| 1₂ₛ | WARNING | No | Single result > 2 SD from mean |
| 1₃ₛ | REJECTION | Yes | Single result > 3 SD from mean |
| 2₂ₛ | REJECTION | Yes | Two consecutive results > 2 SD same side |
| R₄ₛ | REJECTION | Yes | Range of two consecutive results > 4 SD |
| 3₁ₛ | WARNING | No | Three consecutive results > 1 SD same side |
| 4₁ₛ | WARNING | No | Four consecutive results > 1 SD same side |
| 7ₜ | WARNING | No | Seven consecutive results trending same direction |
| 10ₓ | REJECTION | Yes | Ten consecutive results on same side of mean |

### Verifying QC Results

```bash
# Check QC dashboard
curl -sk https://localhost:8443/api/OpenELIS-Global/rest/qc/dashboard/summary \
  -u admin:adminADMIN!

# Check individual instrument
curl -sk https://localhost:8443/api/OpenELIS-Global/rest/qc/dashboard/instruments/19 \
  -u admin:adminADMIN!

# Check qc_result table directly
docker exec openelisglobal-database psql -U clinlims -d clinlims \
  -c "SELECT result_value, z_score, result_status FROM clinlims.qc_result ORDER BY last_updated DESC LIMIT 10;"
```

## Testing

### Run Communication Test (Recommended)

The `test_communication.py` script demonstrates the complete ASTM communication
pathway:

```bash
# Run comprehensive communication test
python test_communication.py --host localhost --port 5000
```

This test:

- ✅ Tests basic ENQ/ACK handshake
- ✅ Demonstrates complete message exchange (H, P, O, R, L records)
- ✅ Tests QC segment communication
- ✅ Verifies multiple simultaneous connections

See [COMMUNICATION_PATHWAY.md](COMMUNICATION_PATHWAY.md) for detailed protocol
documentation.

### Run Unit Tests

```bash
# Install test dependencies
pip install pytest

# Run unit tests (server must be running)
python server.py &
python -m pytest test_server.py -v
```

### Manual Testing

Use netcat or telnet to test the server:

```bash
# Connect
nc localhost 5000

# Send ENQ (Ctrl+E) and observe ACK response
```

## File Structure

```
tools/astm-mock-server/
├── server.py                    # Main server implementation
├── test_server.py               # Unit tests (TDD)
├── test_communication.py        # Communication pathway test & demo
├── fields.json                  # Analyzer field configuration
├── requirements.txt             # Python dependencies
├── Dockerfile                   # Container build file
├── README.md                    # This file
├── COMMUNICATION_PATHWAY.md     # Protocol documentation
└── ACCESS.md                    # Access guide for OpenELIS integration
```

## Multi-Protocol Simulator (M4)

**New in M4**: The simulator now supports HL7 v2.x, RS232 serial, and file-based protocols in addition to ASTM.

### Accession contract (mock)

- **Emitted** specimen/accession values are **SiteYearNum**: `DEV01` + 15 digits (20 characters), validated at generation time.
- **Templates** use `testSample.id` as a **two-digit lane code** to mint IDs (HL7/ASTM), except `qcSample.id` which must be a **full** accession string.
- **Overrides** (`sampleId` query/body) must already be a valid SiteYearNum when provided.

### HL7 v2.x Simulation

Generate HL7 ORU^R01 result messages via HTTP API:

```bash
# Start simulate API server
python server.py --simulate-api-port 8081

# Generate HL7 message (GET) — sampleId must be a valid SiteYearNum if set
curl "http://localhost:8081/simulate/hl7/mindray_bc5380?patientId=P001&sampleId=DEV01264000000000001"

# Generate HL7 message (POST with JSON)
curl -X POST http://localhost:8081/simulate/hl7/sysmex_xn \
  -H "Content-Type: application/json" \
  -d '{"patientId":"P001","sampleId":"DEV01264000000000001","tests":["WBC","RBC"]}'
```

**Available HL7 Templates**: `mindray_bc5380`, `sysmex_xn`, `abbott_architect_hl7`, `mindray_bs360e`, `genexpert`

### RS232 Serial Simulation

Send ASTM messages over virtual serial ports (via socat):

```bash
# Create virtual serial pair
socat -d -d pty,raw,echo=0 pty,raw,echo=0
# Output: /dev/pts/X and /dev/pts/Y

# Send ASTM over serial (use one end)
python server.py --serial-port /dev/pts/X --serial-analyzer horiba_pentra60
```

**Available Serial Templates**: `horiba_pentra60`, `horiba_micros60`, `mindray_ba88a`, `stago_start4`

### File-Based Generation

Generate CSV/TXT files for file import testing:

```bash
# Generate QuantStudio CSV
python server.py --generate-files /tmp/import --generate-files-analyzer quantstudio7

# Generate FluoroCycler CSV
python server.py --generate-files /tmp/import --generate-files-analyzer hain_fluorocycler
```

**Available File Templates**: `quantstudio7`, `hain_fluorocycler`

### Protocol Handlers

The simulator uses a protocol abstraction layer:

- **ASTMHandler**: ASTM LIS2-A2 message generation (backward compatible)
- **HL7Handler**: HL7 v2.x ORU^R01 generation
- **SerialHandler**: RS232 transport (uses ASTMHandler for message generation)
- **FileHandler**: CSV/TXT file generation

### Analyzer Templates

Templates are JSON files in `templates/` following `templates/schema.json`. Each template defines:
- Analyzer metadata (name, model, manufacturer)
- Protocol configuration (type, version, transport)
- Identification (MSH sender, ASTM header, file pattern)
- Test fields (name, code, type, unit, normal range)

**Template Inventory** (12 analyzers):
- HL7: `mindray_bc5380`, `sysmex_xn`, `abbott_architect_hl7`, `mindray_bs360e`, `genexpert`
- RS232: `horiba_pentra60`, `horiba_micros60`, `mindray_ba88a`, `stago_start4`
- File: `quantstudio7`, `hain_fluorocycler`

### Testing

Run protocol handler unit tests:

```bash
python test_protocols.py
```

## References

- [specs/004-astm-analyzer-mapping/research.md](../../specs/004-astm-analyzer-mapping/research.md) -
  ASTM protocol details
- [specs/004-astm-analyzer-mapping/spec.md](../../specs/004-astm-analyzer-mapping/spec.md) -
  Feature specification
- [specs/011-madagascar-analyzer-integration/plan.md](../../specs/011-madagascar-analyzer-integration/plan.md) -
  M4 multi-protocol simulator design
- ASTM E1381 / LIS2-A2 Standard
