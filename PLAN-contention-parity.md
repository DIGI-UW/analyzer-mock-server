# Plan: Mock Server GeneXpert Contention Parity

## Context

The ASTM mock server responds with ACK when receiving ENQ — a purely passive
behavior. The real GeneXpert Dx v6.5 in Server mode sends ENQ proactively when a
client connects (it has queued results), causing ASTM line contention
(ENQ→ENQ). This discrepancy means the mock doesn't exercise the bridge's
contention handling code path, reducing test fidelity.

**Goal:** Make the GeneXpert mock behave like the real instrument — send ENQ on
connection when it has data to transmit. This ensures the mock test-connection
exercises the same contention → poll → success path as the real GeneXpert.

## What the Real GeneXpert Does

Per CLSI LIS1-A and observed behavior on the real Dx v6.5 VM:

```
1. Client opens TCP connection to GeneXpert listen port
2. GeneXpert immediately sends ENQ (has queued data)
3. If client also sends ENQ simultaneously → CONTENTION
4. Per §8.2.7.1: instrument has priority → client yields
5. Client sends ACK → GeneXpert sends framed data → EOT
```

## What the Mock Currently Does

```
1. Client opens TCP connection
2. Mock waits passively (does nothing)
3. Client sends ENQ → Mock sends ACK (no contention)
4. Client sends framed data → Mock ACKs → EOT
```

## Change

### Template config: add `proactive_enq` flag

**File:** `tools/analyzer-mock-server/templates/genexpert_astm.json`

Add to `astm_config`:
```json
"proactive_enq": true
```

This makes the behavior template-driven — only GeneXpert (and any future
analyzers with this behavior) send ENQ on connection. Other templates like
`mindray_ba88a` keep the passive behavior.

### Mock server: send ENQ on connection when configured

**File:** `tools/analyzer-mock-server/server.py`

In `ASTMProtocolHandler.handle()` (line 128), after connection is established but
before entering the receive loop:

```python
def handle(self):
    logger.info(f"Client connected: {self.addr}")
    self.conn.settimeout(SOCKET_TIMEOUT)

    # NEW: If template has proactive_enq, send ENQ immediately (like real
    # GeneXpert which has queued results). This creates contention if the
    # client also sends ENQ — matching real instrument behavior per
    # CLSI LIS1-A §8.2.7.1.
    if self.astm_template and self.astm_template.get('astm_config', {}).get('proactive_enq'):
        logger.info(f"[PROACTIVE_ENQ] Sending ENQ to {self.addr} (instrument has data)")
        self._send(ENQ)
        # Wait briefly for client's response
        try:
            response = self._receive_byte()
            if response == ACK:
                # Client accepted — send our data
                self.send_field_query_response_frames_only()
            elif response == ENQ:
                # CONTENTION: both sides sent ENQ. Per spec, instrument wins.
                # Client should yield and send ACK on our next ENQ.
                logger.info(f"[PROACTIVE_ENQ] Contention detected with {self.addr}")
                time.sleep(1)  # Per spec: instrument waits >= 1s
                self._send(ENQ)  # Re-send ENQ
                response = self._receive_byte()
                if response == ACK:
                    self.send_field_query_response_frames_only()
                else:
                    logger.warning(f"[PROACTIVE_ENQ] Expected ACK after contention, got: {response}")
            # Fall through to normal receive loop regardless
        except socket.timeout:
            logger.debug(f"[PROACTIVE_ENQ] No response, entering receive mode")

    # ... existing receive loop continues
```

### Extract frame-sending from `send_field_query_response`

The existing `send_field_query_response()` sends ENQ, waits for ACK, then sends
frames. We need a variant that only sends the frames (ENQ/ACK already handled).
Extract `send_field_query_response_frames_only()`:

```python
def send_field_query_response_frames_only(self):
    """Send template data as ASTM frames (ENQ/ACK already established)."""
    if self.astm_template:
        message = ASTMHandler().generate(self.astm_template, use_seed=True)
        records = [r for r in message.strip().split('\n') if r.strip()]
        for i, record in enumerate(records):
            if not self._send_frame(record.strip()):
                break
        logger.info(f"[PROACTIVE_ENQ] Sent {len(records)} records to {self.addr}")
    self._send(EOT)
```

### Tests

**File:** `tools/analyzer-mock-server/test_protocols.py`

Add a test that verifies the `proactive_enq` config is present in the GeneXpert
template and absent from other templates (e.g. mindray).

## Files to Modify

| File | Change |
|------|--------|
| `tools/analyzer-mock-server/templates/genexpert_astm.json` | Add `"proactive_enq": true` to `astm_config` |
| `tools/analyzer-mock-server/server.py` | Proactive ENQ on connection + contention resolution + extract frame sender |
| `tools/analyzer-mock-server/test_protocols.py` | Test proactive_enq template config |

## Branch & PR

- Branch: `fix/mock-genexpert-contention-parity` off `main` in the
  `analyzer-mock-server` submodule
- PR on `DIGI-UW/analyzer-mock-server`
- After merge, update submodule pointer in parent PR #2955

## Verification

```bash
# 1. Run mock server unit tests
cd tools/analyzer-mock-server && python -m pytest test_protocols.py -v

# 2. Rebuild mock, restart harness
cd projects/analyzer-harness && docker compose ... up -d --build astm-simulator

# 3. Raw TCP test: connect to mock, expect to receive ENQ
python3 -c "
import socket
s = socket.socket(); s.settimeout(5)
s.connect(('172.20.1.100', 9600))
data = s.recv(1)
print(f'Got: 0x{data.hex()} ({\"ENQ\" if data == b\"\\x05\" else \"unexpected\"})')
s.close()
"

# 4. Playwright mock test still green (contention handled by bridge)
cd frontend && BASE_URL=https://madagascar.openelis-global.org \
  TEST_USER=admin TEST_PASS=adminADMIN! \
  npx playwright test analyzer-test-connection --reporter=list
```
