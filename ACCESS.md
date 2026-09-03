# Analyzer Mock Access Guide

The analyzer mock is reached by Analyzer Bridge through analyzer-native
transports. OpenELIS does not connect to the mock directly.

## Start One ASTM Listener

```bash
export ANALYZER_BRIDGE_PROFILES_DIR=/path/to/openelis-analyzer-bridge/src/main/resources/analyzer-profiles
export ASTM_TEMPLATE=genexpert_astm
python3 server.py --port 5000
```

The selected template controls the analyzer identity and message behavior. A
single listener will not start without a valid template.

## Start Port-Mapped Listeners

Set `PORT_TEMPLATES` or provide `config/port_templates.json`:

```json
{
  "5000": "genexpert_astm",
  "5001": "mindray_bc5380"
}
```

The template protocol selects ASTM TCP or HL7 MLLP handling for each port.

## Start The Control API

```bash
export ANALYZER_BRIDGE_PROFILES_DIR=/path/to/openelis-analyzer-bridge/src/main/resources/analyzer-profiles
export ASTM_TEMPLATE=genexpert_astm
python3 server.py --port 5000 --simulate-api-port 8081
```

The API controls simulated traffic. ASTM and HL7 POST requests require an
explicit Bridge listener destination. FILE POST requests require the directory
watched by the saved Bridge FILE connection.

## Bridge Connection Setup

Create the analyzer connection through the OpenELIS setup workflow. That flow
creates and configures the durable connection in Bridge from a selected profile.
For local testing, its host, port, or watch directory must point at the mock
instance.

Use the visible **Test connection** action from the analyzer setup workflow to
exercise the configured Bridge probe. Do not seed analyzer records with SQL or
change container addresses in the OpenELIS database.

## Troubleshooting

1. Confirm the mock process and selected template loaded successfully.
2. Confirm Bridge can reach the mock host and listener port.
3. Confirm the Bridge connection is pinned to the expected profile revision.
4. For source-address routing tests, confirm the mock is attached to the
   connection's isolated analyzer network.
5. For FILE, confirm the mock writes into the exact directory watched by Bridge.
6. Inspect mock and Bridge logs before changing configuration.
