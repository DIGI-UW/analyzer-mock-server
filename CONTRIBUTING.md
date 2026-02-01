# Contributing to Analyzer Mock Server

## Development setup

- **Python**: 3.9+ (3.11 recommended, matches Docker base)
- **Dependencies**: `pip install -r requirements.txt`

## Testing expectations

All changes that touch server behavior, protocol handlers, or templates should include or update tests. CI runs on every push and pull request to `main`.

### Test strategy

1. **Unit tests** (`test_protocols.py`)  
   Exercise protocol handlers (ASTM, HL7, Serial, File) and template-based message generation. No server process required; run with:
   ```bash
   python -m pytest test_protocols.py -v
   ```

2. **Integration tests** (`test_server.py`)  
   Exercise the TCP server (handshake, frames, field query, standards compliance). The test module starts and stops the server automatically; run with:
   ```bash
   python -m pytest test_server.py -v
   ```

3. **Template validation**  
   New or changed analyzer templates in `templates/*.json` must conform to `templates/schema.json`. Validate with:
   ```bash
   python template_loader.py --validate templates/<name>.json
   ```
   CI validates a fixed set of templates on every run.

### Running the full suite

```bash
pip install -r requirements.txt
python -m pytest -v
```

Then validate templates as needed:

```bash
python template_loader.py --validate templates/horiba_pentra60.json
```

## Pull requests

- Target branch: `main`
- Ensure the checklist in the PR template is completed.
- CI must pass (pytest, template validation, Docker build and container start).

## Branch protection

Repositories are encouraged to enable branch protection on `main` and require the CI workflow to pass before merging.
