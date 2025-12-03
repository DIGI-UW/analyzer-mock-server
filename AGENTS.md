# ASTM Mock Server - Agent Instructions

This file provides context and instructions for AI agents working on the ASTM Mock Server project.

## Project Overview

This project is a lightweight, Python-based mock server that simulates an ASTM LIS2-A2 compatible laboratory analyzer. It is used to test the OpenELIS ASTM Bridge.

**Core Purpose**: To provide a rigorous, standards-compliant (CLSI LIS1-A) endpoint for validating ASTM protocol handling in OpenELIS.

## Governance & Principles

Refer to [.specify/memory/constitution.md](.specify/memory/constitution.md) for the project's core principles. Key constraints:
- **Strict Standards Compliance**: Must implement mandatory checksums, frame sequencing, and timeouts.
- **Container-Native**: Designed for Docker/Swarm.
- **Simplicity**: Minimal dependencies (standard library preferred).

## Setup & Environment

- **Language**: Python 3 (3.9+ recommended)
- **Dependencies**: `pip install -r requirements.txt` (Mainly `pytest` for testing; server uses standard library).

## Common Commands

### Development
```bash
# Start server locally (default port 5000)
python server.py

# Start with verbose logging (RECOMMENDED for debugging)
python server.py --verbose

# Start with custom analyzer type
python server.py --analyzer-type CHEMISTRY
```

### Testing
**Integration Tests (Primary Verification)**
This script verifies the full ASTM handshake and data transfer protocol.
```bash
# Run communication pathway test
python test_communication.py --host localhost --port 5000
```

**Unit Tests**
```bash
# Run pytest suite
python -m pytest
```

### Docker
```bash
# Build image
docker build -t astm-mock-server .

# Run container
docker run -p 5000:5000 astm-mock-server
```

## Code Style & Conventions

- **Style**: Follow PEP 8.
- **Docstrings**: Ensure all functions and classes have clear docstrings explaining their role in the ASTM protocol.
- **Logging**: Use `print` or `logging` to output detailed protocol states (e.g., `[RX] <ENQ>`, `[TX] <ACK>`).
- **Protocol Implementation**:
  - Always validate Checksums.
  - Always enforce Frame Numbers (1-7, wrap to 1).
  - Handle Timeouts (15s/30s) explicitly.

## Key Files

- `server.py`: Main entry point. Contains `ASTMProtocolHandler` class and `ThreadedTCPServer`.
- `fields.json`: Configuration for analyzer fields (test definitions).
- `test_communication.py`: Client simulator for testing the server.
- `COMMUNICATION_PATHWAY.md`: Detailed protocol specification.
- `ACCESS.md`: Guide for accessing the server in various environments.

## Troubleshooting

- **Connection Refused**: Check if port 5000 is occupied or if the Docker container is running.
- **NAK Responses**: Indicates checksum error or invalid frame sequence. Check logs for `[RX] ...` to see what was received.
- **Timeout**: Ensure the client sends `ENQ` to start and `EOT` to finish.

