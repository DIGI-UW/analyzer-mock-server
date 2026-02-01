<!--
Sync Impact Report:
- Version change: 0.0.0 -> 1.0.0 (Initial Ratification)
- Modified principles: Defined all core principles from template.
- Added sections: Architecture & Tech Stack, Quality Assurance & Verification.
- Templates requiring updates: None (Initial setup).
-->
# Analyzer Mock Server Constitution

## Core Principles

### I. Strict Standards Compliance
The server MUST implement strict adherence to protocol standards for each supported transport: ASTM LIS2-A2 (message format) and CLSI LIS1-A (low-level protocol) for ASTM/TCP and serial; HL7 v2.x for ORU^R01; and documented formats for file-based output. For ASTM, this includes mandatory checksum validation, frame number sequencing, timeout handling (15s establishment/ACK, 30s receiver), and retransmission logic (abort after 6 failures). "Good enough" is not acceptable; the mock must behave like a rigorous real-world analyzer to validly test the bridge.

### II. Container-Native Deployment
The application MUST be designed as a container-native service. All configuration MUST be exposed via environment variables or mounted configuration files (e.g., `fields.json`). The system MUST function correctly in Docker, Docker Compose, and Docker Swarm environments, supporting service discovery and networking typical of these platforms.

### III. Comprehensive Testability
The project exists to facilitate testing. It MUST provide built-in tools for verification, including:
- Unit tests for internal logic (pytest).
- Integration/Communication tests (`test_communication.py`) that verify the full protocol handshake and data exchange.
- Manual testing support (via `nc` or simple scripts).
- "Push" and "API" modes to simulate various analyzer behaviors (unsolicited results, query responses).

### IV. Simplicity & Configurability
The implementation SHOULD remain lightweight and Python-based to ensure ease of modification and deployment. Complexity should be kept low. Analyzer behaviors (types, fields, response delays) MUST be configurable without code changes, allowing the mock to simulate various instruments (Hematology, Chemistry, etc.) by adjusting `fields.json` or env vars.

### V. Observability & Debugging
The server MUST provide clear, verbose logging capabilities to aid in debugging protocol issues. Logs should clearly indicate protocol states (Establishment, Transfer, Termination), raw frame data (for checksum verification), and error conditions (NAK, timeouts).

## Architecture & Tech Stack

- **Language**: Python 3.
- **Dependencies**: Minimal external dependencies to ensure fast build times and small container images.
- **Concurrency**: Implementation MUST handle TCP connections and HTTP push requests efficiently, likely using threading or asynchronous I/O to support multiple simultaneous connections if required by the standard or testing scenarios.
- **Documentation**: Protocol details (`COMMUNICATION_PATHWAY.md`) and usage (`README.md`, `ACCESS.md`) MUST be kept up-to-date with code changes.

## Quality Assurance & Verification

- **Protocol Verification**: Changes to protocol logic MUST be verified against `test_communication.py` to ensure no regression in standard compliance.
- **New Features**: Any new record types or protocol extensions MUST include corresponding unit and integration tests.
- **Manual Testing**: The system MUST support manual interaction for debugging, allowing developers to use `netcat` to send raw frames and observe responses.

## Governance

This Constitution defines the core requirements for the Analyzer Mock Server.

- **Amendments**: Require valid justification, particularly if relaxing standards compliance.
- **Compliance Priority**: Protocol compliance is the highest priority; features that break strict LIS1-A/LIS2-A compliance MUST be rejected or strictly opt-in for edge-case testing.
- **Versioning**: Semantic Versioning (MAJOR.MINOR.PATCH) applies to the project releases.

**Version**: 1.0.0 | **Ratified**: 2025-12-03 | **Last Amended**: 2025-12-03
