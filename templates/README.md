# ASTM Mock Server Templates

This directory contains analyzer templates for generating deterministic ASTM LIS2-A2 messages.

## Purpose

Templates enable the mock server to generate ASTM messages with **exact field alignment** to OpenELIS analyzer plugins, supporting:

- **Reproducible E2E testing**: Deterministic seed values for consistent assertions
- **Plugin validation**: Generated messages match plugin `TestMapping` arrays exactly
- **Out-of-the-box testing**: Zero manual configuration needed

## Available Templates

| Template | Analyzer | Fields | Protocol |
|----------|----------|--------|----------|
| `horiba_pentra60.json` | Horiba ABX Pentra 60 | 20 (5-part differential) | ASTM LIS2-A2 |
| `horiba_micros60.json` | Horiba ABX Micros 60 | 16 (3-part differential) | ASTM LIS2-A2 |

## Template Structure

```json
{
  "analyzer": {
    "name": "Horiba ABX Pentra 60",
    "manufacturer": "Horiba ABX",
    "model": "Pentra 60 C+",
    "category": "HEMATOLOGY"
  },
  "protocol": {
    "type": "ASTM",
    "version": "LIS2-A2"
  },
  "identification": {
    "astm_header": "ABX^PENTRA60^V2.0"
  },
  "fields": [
    {
      "code": "WBC",
      "name": "White Blood Cells",
      "loinc": "6690-2",
      "unit": "10^3/uL",
      "normalRange": "4.0-10.0",
      "seedValue": 5.8
    }
  ],
  "testPatient": {
    "id": "PAT-2026-001",
    "name": "Rakoto^Jean^A",
    "sex": "M",
    "dob": "19850315"
  },
  "testSample": {
    "id": "SAMPLE-2026-0001",
    "type": "CBC^Complete Blood Count"
  }
}
```

## Usage

### Generate Deterministic Message

```bash
python template_generator.py --template horiba_pentra60 --deterministic
```

### Generate Random Values Within Normal Ranges

```bash
python template_generator.py --template horiba_micros60 --random
```

### List Available Templates

```bash
python template_loader.py --list
```

### Validate a Template

```bash
python template_loader.py --validate templates/horiba_pentra60.json
```

### Get Template Info

```bash
python template_loader.py --info horiba_pentra60
```

## Creating New Templates

1. Copy an existing template as a starting point
2. Update `analyzer`, `protocol`, and `identification` sections
3. Define `fields` array matching the plugin's `TestMapping` entries
4. Set `seedValue` for each field (extract from test fixtures)
5. Validate against schema: `python template_loader.py --validate your_template.json`

### Field Alignment with Plugins

Fields must match the plugin's `TestMapping` array exactly:

```java
// From HoribaPentra60Analyzer.java
private List<TestMapping> createTestMappings() {
    return Arrays.asList(
        new TestMapping("WBC", "White Blood Cells", "6690-2"),  // 3-arg: with LOINC
        new TestMapping("MXD%", "Mixed Cells Percent")           // 2-arg: no LOINC
    );
}
```

Map to template fields:
```json
{
  "code": "WBC",      // Matches first TestMapping argument
  "name": "White Blood Cells",  // Matches second argument
  "loinc": "6690-2"   // Matches third argument (or empty for 2-arg)
}
```

## Schema Validation

Templates are validated against `schema.json` which enforces:

- Required fields: `analyzer`, `protocol`, `identification`, `fields`
- Field types: `code`, `name`, `unit` are required per field
- Patient sex: Must be `M`, `F`, or `U`
- Date format: DOB must be `YYYYMMDD`

## Integration with E2E Tests

Templates power the E2E testing workflow:

```bash
# 1. Generate deterministic ASTM message
python template_generator.py --template horiba_pentra60 --deterministic > /tmp/test.astm

# 2. Send to OpenELIS ASTM endpoint
curl -X POST https://localhost/api/OpenELIS-Global/analyzer/astm \
  -H "Content-Type: text/plain" \
  --data-binary @/tmp/test.astm

# 3. Assert results in Cypress with known values
cy.get('[data-testid="wbc-value"]').should('contain', '5.8');  // seedValue
```

## Related Documentation

- [Plan: M9-M10 Testing Infrastructure](../../../.claude/plans/refactored-hatching-manatee.md)
- [Checklist: m9-m10-testing-infrastructure.md](../../../specs/011-madagascar-analyzer-integration/checklists/m9-m10-testing-infrastructure.md)
- [Environment Detection Guide](../../../frontend/cypress/ENVIRONMENT-DETECTION.md)
