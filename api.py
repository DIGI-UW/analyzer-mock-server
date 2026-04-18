"""
Unified HTTP API handler for the analyzer mock server.

Single handler for all routes:
- /health — service health check
- /simulate/hl7/{template} — generate/push HL7 messages
- /simulate/astm/{template} — generate/push ASTM messages
- /simulate/file/{template} — generate/write FILE payloads
- /analyzers — dynamic Docker network management
"""

import json
import logging
import os
import re
import shutil
import threading
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Dict, Optional
from urllib.parse import urlparse, parse_qs

from fixture_parser import parse_fixture

from protocols.astm_handler import ASTMHandler
from protocols.hl7_handler import HL7Handler
from protocols.file_handler import FileHandler
from push import push_hl7_to_destination, push_astm_to_destination

logger = logging.getLogger(__name__)


def _load_template(analyzer: str) -> Optional[Dict]:
    """Load analyzer template — profile-backed first, then local file."""
    try:
        from profile_adapter import load_strict_013_profile_template
        profile_template = load_strict_013_profile_template(analyzer)
        if profile_template is not None:
            return profile_template
    except Exception as e:
        logger.warning("Failed to load profile template for %s: %s", analyzer, e)

    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "templates", f"{analyzer}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to load template %s: %s", path, e)
        return None


def _safe_file_output_path(target_dir, filename, template_name, default_pattern):
    """Construct safe file output path, stripping path traversal to basename."""
    if not target_dir or not os.path.isdir(target_dir):
        return None
    if filename:
        base = os.path.basename(filename)
        if not base or base in ('.', '..'):
            return None
        return os.path.join(target_dir, base)
    if '%' in default_pattern:
        fname = default_pattern % uuid.uuid4().hex[:8]
    else:
        fname = f"sim_{template_name}_{uuid.uuid4().hex[:8]}.csv"
    return os.path.join(target_dir, fname)


def _extract_sample_id_from_hl7(msg: str) -> Optional[str]:
    """Extract sample_id from OBR-3 (filler order number = accession in OE)."""
    for seg in msg.split('\r'):
        if seg.startswith('OBR|'):
            fields = seg.split('|')
            if len(fields) > 3:
                return fields[3]
            break
    return None


def _extract_sample_id_from_astm(msg: str) -> Optional[str]:
    """Extract sample_id from O-segment field 2 (specimen ID)."""
    for line in msg.split('\n'):
        if line.startswith('O|'):
            fields = line.split('|')
            if len(fields) > 2:
                return fields[2].split('^')[0]
            break
    return None


class MockAPIHandler(BaseHTTPRequestHandler):
    """Unified HTTP API handler for the analyzer mock server."""

    _network_manager = None

    @classmethod
    def _get_network_manager(cls):
        if cls._network_manager is None:
            try:
                from analyzer_network_manager import AnalyzerNetworkManager
                cls._network_manager = AnalyzerNetworkManager()
            except Exception as e:
                logger.error("Failed to initialize AnalyzerNetworkManager: %s", e)
                return None
        return cls._network_manager

    def do_GET(self):
        if self.path == "/health" or self.path == "/":
            self._send_json(200, {
                "status": "ok",
                "service": "Multi-Protocol Analyzer Simulator",
                "endpoints": {
                    "GET /health": "Health check",
                    "GET /simulate/hl7/{template}": "Generate HL7 ORU^R01",
                    "POST /simulate/hl7/{template}": "Generate + push HL7 (body: destination, count)",
                    "GET /simulate/astm/{template}": "Generate ASTM message",
                    "POST /simulate/astm/{template}": "Generate + push ASTM",
                    "GET /simulate/file/{template}": "Generate FILE payload",
                    "POST /simulate/file/{template}": "Generate + write FILE",
                    "GET /analyzers": "List active mock analyzers",
                    "POST /analyzers": "Create mock analyzer with unique network+IP",
                    "DELETE /analyzers/{name}": "Remove mock analyzer",
                },
            })
            return
        if self.path == "/analyzers" or self.path == "/analyzers/":
            mgr = self._get_network_manager()
            if not mgr:
                self._send_json(500, {"error": "Docker API not available"})
                return
            self._send_json(200, {"analyzers": mgr.list_analyzers()})
            return
        if self.path.startswith("/simulate/hl7/"):
            analyzer = self._extract_name("/simulate/hl7/")
            if not analyzer:
                self._send_json(400, {"error": "Missing analyzer name"})
                return
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            params = {
                "patient_id": (qs.get("patientId") or qs.get("patient_id") or [None])[0],
                "sample_id": (qs.get("sampleId") or qs.get("sample_id") or [None])[0],
            }
            self._handle_hl7(analyzer, params)
            return
        if self.path.startswith("/simulate/astm/"):
            name = self._extract_name("/simulate/astm/")
            if not name:
                self._send_json(400, {"error": "Missing template name"})
                return
            self._handle_astm_get(name)
            return
        if self.path.startswith("/simulate/file/"):
            name = self._extract_name("/simulate/file/")
            if not name:
                self._send_json(400, {"error": "Missing template name"})
                return
            self._handle_file_get(name)
            return
        self.send_error(404, "Not Found")

    def do_POST(self):
        if self.path == "/analyzers" or self.path == "/analyzers/":
            self._handle_create_analyzer()
            return
        if self.path.startswith("/simulate/hl7/"):
            analyzer = self._extract_name("/simulate/hl7/")
            if not analyzer:
                self._send_json(400, {"error": "Missing analyzer name"})
                return
            body = self._read_json_body()
            if body is self._JSON_PARSE_ERROR:
                self._send_json(400, {"error": "Invalid JSON body"})
                return
            params = body or {}
            kwargs = {
                "patient_id": params.get("patientId") or params.get("patient_id"),
                "sample_id": params.get("sampleId") or params.get("sample_id"),
                "tests": params.get("tests"),
                "destination": params.get("destination"),
                "count": params.get("count", 1),
            }
            self._handle_hl7(analyzer, kwargs)
            return
        if self.path.startswith("/simulate/astm/"):
            name = self._extract_name("/simulate/astm/")
            if not name:
                self._send_json(400, {"error": "Missing template name"})
                return
            self._handle_astm_post(name)
            return
        if self.path.startswith("/simulate/file/"):
            name = self._extract_name("/simulate/file/")
            if not name:
                self._send_json(400, {"error": "Missing template name"})
                return
            self._handle_file_post(name)
            return
        self.send_error(404, "Not Found")

    def do_DELETE(self):
        if self.path.startswith("/analyzers/"):
            name = self._extract_name("/analyzers/")
            if not name:
                self._send_json(400, {"error": "Analyzer name required"})
                return
            mgr = self._get_network_manager()
            if not mgr:
                self._send_json(500, {"error": "Docker API not available"})
                return
            removed = mgr.remove_analyzer(name)
            if removed:
                self._send_json(200, {"removed": True, "name": name})
            else:
                self._send_json(404, {"removed": False, "error": f"Analyzer '{name}' not found"})
            return
        self.send_error(404, "Not Found")

    # ── Route handlers ───────────────────────────────────────────

    def _handle_hl7(self, analyzer: str, kwargs: Dict):
        template = _load_template(analyzer)
        if not template:
            self._send_json(404, {"error": f"Template not found: {analyzer}"})
            return
        try:
            destination = kwargs.get("destination")
            count = min(max(int(kwargs.get("count", 1)), 1), 1000)

            gen_kwargs = {k: v for k, v in kwargs.items()
                         if k in ("patient_id", "sample_id", "tests") and v is not None}
            results = []
            pushed_count = 0
            first_message = None

            for i in range(count):
                msg = HL7Handler().generate(template, **gen_kwargs)
                if first_message is None:
                    first_message = msg
                pushed = False
                if destination:
                    pushed = push_hl7_to_destination(destination, msg)
                    if pushed:
                        pushed_count += 1
                results.append({
                    "message_number": i + 1,
                    "pushed": pushed,
                    "sample_id": _extract_sample_id_from_hl7(msg),
                    "preview": msg.split("\r")[0][:80] + "...",
                })

            if destination or count > 1:
                self._send_json(200, {
                    "status": "completed",
                    "analyzer": analyzer,
                    "count": count,
                    "destination": destination,
                    "pushed": pushed_count if destination else None,
                    "results": results,
                })
                return

            msg = first_message or HL7Handler().generate(template, **gen_kwargs)
            msg_id = "MSG-" + msg.split("ORU^R01|")[-1].split("|")[0] if "ORU^R01|" in msg else "MSG-UNK"
            self._send_json(200, {
                "status": "sent",
                "messageId": msg_id,
                "sample_id": _extract_sample_id_from_hl7(msg),
                "message": msg,
            })
        except Exception as e:
            logger.exception("HL7 simulate failed for %s", analyzer)
            self._send_json(500, {"error": str(e)})

    def _handle_astm_get(self, template_name: str):
        template = _load_template(template_name)
        if not template:
            self._send_json(404, {"error": f"Template not found: {template_name}"})
            return
        if template.get('protocol', {}).get('type') != 'ASTM':
            self._send_json(400, {"error": "Template is not ASTM protocol"})
            return
        try:
            msg = ASTMHandler().generate(template, use_seed=True)
            self._send_json(200, {"status": "generated", "template": template_name, "message": msg})
        except Exception as e:
            logger.exception("ASTM GET failed for %s", template_name)
            self._send_json(500, {"error": str(e)})

    def _handle_astm_post(self, template_name: str):
        template = _load_template(template_name)
        if not template:
            self._send_json(404, {"error": f"Template not found: {template_name}"})
            return
        if template.get('protocol', {}).get('type') != 'ASTM':
            self._send_json(400, {"error": "Template is not ASTM protocol"})
            return

        body = self._read_json_body()
        if body is self._JSON_PARSE_ERROR:
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        params = body or {}
        count = min(max(int(params.get("count", 1)), 1), 100)
        destination = params.get("destination")

        gen_kwargs = {"use_seed": True}
        if params.get("sample_id"):
            gen_kwargs["sample_id"] = params["sample_id"]

        results = []
        success_count = 0
        handler = ASTMHandler()

        for i in range(count):
            msg = handler.generate(template, **gen_kwargs)
            pushed = False
            if destination:
                pushed = push_astm_to_destination(destination, msg)
                if pushed:
                    success_count += 1
            results.append({
                "message_number": i + 1,
                "pushed": pushed,
                "sample_id": _extract_sample_id_from_astm(msg),
                "preview": msg.split('\n')[0][:80] + "..." if msg else "",
            })

        self._send_json(200, {
            "status": "completed",
            "template": template_name,
            "count": count,
            "pushed": success_count if destination else None,
            "destination": destination,
            "results": results,
        })

    def _handle_file_get(self, template_name: str):
        template = _load_template(template_name)
        if not template:
            self._send_json(404, {"error": f"Template not found: {template_name}"})
            return
        if template.get("protocol", {}).get("type") != "FILE":
            self._send_json(400, {"error": "Template is not FILE protocol"})
            return
        try:
            fixture_cfg = template.get("fixture")
            if fixture_cfg:
                fixture_path = os.path.join(os.path.dirname(__file__), fixture_cfg["file"])
                metadata_results = parse_fixture(fixture_path, fixture_cfg)
                self._send_json(200, {
                    "status": "generated",
                    "template": template_name,
                    "metadata": {
                        "analyzerName": template.get("analyzer", {}).get("name", template_name),
                        "format": fixture_cfg.get("format", "CSV"),
                        "fixture": fixture_cfg["file"],
                        "results": metadata_results,
                    },
                })
            else:
                content = FileHandler().generate(template)
                self._send_json(200, {"status": "generated", "template": template_name, "content": content})
        except Exception as e:
            logger.exception("FILE GET failed for %s", template_name)
            self._send_json(500, {"error": str(e)})

    def _handle_file_post(self, template_name: str):
        template = _load_template(template_name)
        if not template:
            self._send_json(404, {"error": f"Template not found: {template_name}"})
            return
        if template.get("protocol", {}).get("type") != "FILE":
            self._send_json(400, {"error": "Template is not FILE protocol"})
            return
        body = self._read_json_body()
        if body is self._JSON_PARSE_ERROR:
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        params = body or {}
        target_dir = params.get("target_dir")

        fixture_cfg = template.get("fixture")
        if fixture_cfg:
            # Fixture-based: copy real file + return parsed metadata
            try:
                self._handle_fixture_file_post(template_name, template, fixture_cfg, target_dir, params)
            except Exception as e:
                logger.exception("Fixture FILE POST failed for %s", template_name)
                self._send_json(500, {"error": str(e)})
        else:
            # Fallback: synthetic generation (legacy templates without fixture section)
            try:
                content = FileHandler().generate(template)
                written_path = None
                if target_dir:
                    default_pattern = (template.get("identification") or {}).get("file_pattern", "sim_%s.csv")
                    out_path = _safe_file_output_path(target_dir, params.get("filename"), template_name, default_pattern)
                    if not out_path:
                        self._send_json(400, {"error": "Invalid target_dir or filename"})
                        return
                    written_path = FileHandler().write_text_to_path(out_path, content)
                    if written_path is None:
                        self._send_json(500, {"error": "Failed to write file"})
                        return
                self._send_json(200, {
                    "status": "completed",
                    "template": template_name,
                    "written_path": written_path,
                    "content": content,
                })
            except Exception as e:
                logger.exception("FILE POST failed for %s", template_name)
                self._send_json(500, {"error": str(e)})

    def _handle_fixture_file_post(self, template_name, template, fixture_cfg, target_dir, params):
        """Deliver a fixture file.

        Two delivery modes:

        1. Bridge upload (production-parity): set ``bridge_upload.analyzer_id``
           in the request body. The mock POSTs the fixture multipart to the
           bridge's ``/admin/upload`` — identical to what a Madagascar lab tech
           does via the bridge admin UI. When the file has no per-row test
           code column, pass ``bridge_upload.test_code`` to declare it
           (same UI field the tech fills in).

        2. Watched-directory drop (legacy): set ``target_dir`` to a path
           under ``/data/analyzer-imports``. Mock copies the fixture verbatim.
           Preserved for tests that exercise the FileWatcher path directly.
        """
        fixture_rel = fixture_cfg["file"]
        fixture_path = os.path.join(os.path.dirname(__file__), fixture_rel)
        if not os.path.isfile(fixture_path):
            self._send_json(404, {"error": f"Fixture file not found: {fixture_rel}"})
            return

        # Parse metadata from the fixture
        metadata_results = parse_fixture(fixture_path, fixture_cfg)

        # --- Mode 1: upload via bridge (production-parity) ---
        bridge_upload = params.get("bridge_upload")
        if bridge_upload:
            return self._upload_fixture_to_bridge(
                template_name, template, fixture_cfg, fixture_path, fixture_rel,
                metadata_results, bridge_upload,
            )

        # --- Mode 2: watched-directory drop (legacy) ---
        written_path = None
        if target_dir:
            # Validate target_dir: resolve to real path to prevent path traversal.
            # Only allow output under /data/analyzer-imports (Docker) or /tmp (tests).
            resolved_dir = os.path.realpath(target_dir)
            allowed_roots = ["/data/analyzer-imports", "/tmp"]
            if not any(resolved_dir.startswith(root) for root in allowed_roots):
                self._send_json(400, {"error": f"target_dir must be under {allowed_roots}"})
                return
            os.makedirs(resolved_dir, exist_ok=True)
            ext = os.path.splitext(fixture_path)[1]
            filename = params.get("filename") or f"{template_name}-{uuid.uuid4().hex[:8]}{ext}"
            out_path = os.path.join(resolved_dir, os.path.basename(filename))

            # Copy the real file (preserving binary format for .xls/.xlsx)
            shutil.copy2(fixture_path, out_path)

            # Append unique timestamp to prevent bridge hash-based dedup across test runs
            if ext.lower() in ('.csv', '.tsv', '.txt'):
                import time
                with open(out_path, "a", encoding="utf-8") as f:
                    f.write(f"\n{int(time.time() * 1000)}")

            written_path = out_path
            logger.info("Dropped fixture %s to %s (%d results)", fixture_rel, out_path, len(metadata_results))

        self._send_json(200, {
            "status": "completed",
            "template": template_name,
            "written_path": written_path,
            "metadata": {
                "analyzerName": template.get("analyzer", {}).get("name", template_name),
                "format": fixture_cfg.get("format", "CSV"),
                "fixture": fixture_rel,
                "results": metadata_results,
            },
        })

    def _upload_fixture_to_bridge(
        self, template_name, template, fixture_cfg, fixture_path, fixture_rel,
        metadata_results, bridge_upload,
    ):
        """POST the fixture to the bridge's /admin/upload endpoint.

        Production-parity simulation: replays what a lab tech does in the
        bridge admin upload UI — select analyzer, optionally declare test
        code, upload file. The bridge's FileUploadController handles the
        rest (including FileNameSelfDeclarationScanner for files whose
        test code isn't declared explicitly).
        """
        analyzer_id = bridge_upload.get("analyzer_id")
        if not analyzer_id:
            self._send_json(400, {"error": "bridge_upload.analyzer_id is required"})
            return
        test_code = bridge_upload.get("test_code")
        bridge_url = (bridge_upload.get("bridge_url")
                      or os.environ.get("BRIDGE_URL")
                      or "https://openelis-analyzer-bridge:8443")
        bridge_user = (bridge_upload.get("bridge_user")
                       or os.environ.get("BRIDGE_USER")
                       or "bridge")
        bridge_pass = (bridge_upload.get("bridge_pass")
                       or os.environ.get("BRIDGE_PASS")
                       or "changeme")

        # Build multipart request. stdlib doesn't ship multipart encoder,
        # so build the body manually — this matches what curl --form does.
        import urllib.request, urllib.error, ssl, uuid as _uuid
        boundary = f"----mock-server-{_uuid.uuid4().hex}"
        with open(fixture_path, "rb") as f:
            file_bytes = f.read()
        ext = os.path.splitext(fixture_path)[1]
        upload_filename = f"{template_name}-{_uuid.uuid4().hex[:8]}{ext}"

        parts = []
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(b'Content-Disposition: form-data; name="analyzerId"\r\n\r\n')
        parts.append(str(analyzer_id).encode() + b"\r\n")
        if test_code:
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(b'Content-Disposition: form-data; name="testCode"\r\n\r\n')
            parts.append(test_code.encode() + b"\r\n")
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="file"; filename="{upload_filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode()
        )
        parts.append(file_bytes)
        parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)

        upload_url = bridge_url.rstrip("/") + "/admin/upload"
        req = urllib.request.Request(upload_url, data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.add_header("Content-Length", str(len(body)))
        import base64
        auth = base64.b64encode(f"{bridge_user}:{bridge_pass}".encode()).decode()
        req.add_header("Authorization", f"Basic {auth}")

        # Bridge uses a self-signed cert in dev/test — accept it.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        try:
            with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
                resp_body = resp.read().decode("utf-8", errors="replace")
                resp_status = resp.status
        except urllib.error.HTTPError as e:
            resp_body = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
            resp_status = e.code
        except Exception as e:  # noqa: BLE001
            logger.exception("Bridge upload failed for %s", template_name)
            self._send_json(502, {"error": f"Bridge upload failed: {e}"})
            return

        logger.info(
            "Uploaded fixture %s to bridge %s (analyzer %s, testCode=%s) — status %d",
            fixture_rel, bridge_url, analyzer_id, test_code, resp_status,
        )

        self._send_json(200, {
            "status": "uploaded",
            "template": template_name,
            "bridge_status": resp_status,
            "bridge_response_preview": resp_body[:500],
            "upload_filename": upload_filename,
            "metadata": {
                "analyzerName": template.get("analyzer", {}).get("name", template_name),
                "format": fixture_cfg.get("format", "CSV"),
                "fixture": fixture_rel,
                "results": metadata_results,
            },
        })

    def _handle_create_analyzer(self):
        mgr = self._get_network_manager()
        if not mgr:
            self._send_json(500, {"error": "Docker API not available"})
            return
        body = self._read_json_body()
        if body is self._CONTENT_LENGTH_ERROR:
            self._send_json(400, {"error": "Invalid Content-Length header"})
            return
        if body is self._JSON_PARSE_ERROR:
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        if not body:
            self._send_json(400, {"error": "Request body required: {name, template, port?}"})
            return
        name = body.get("name")
        template = body.get("template")
        port = body.get("port", 0)
        if not name or not template:
            self._send_json(400, {"error": "name and template are required"})
            return
        if not re.match(r'^[A-Za-z0-9_-]+$', name):
            self._send_json(400, {"error": "name must be alphanumeric/dash/underscore only"})
            return
        try:
            # Connecting the current mock container to a new Docker network can
            # tear down the in-flight HTTP socket. Return the API response first,
            # then attach the mock container asynchronously.
            result = mgr.create_analyzer(name, template, port, connect_mock=False)
        except Exception as e:
            error_str = str(e)
            if "Conflict" in error_str or "already exists" in error_str:
                # Network exists — return the existing analyzer info if cached
                existing = mgr._analyzers.get(name)
                if existing:
                    self._send_json(200, existing)
                else:
                    self._send_json(409, {"error": error_str})
            else:
                self._send_json(500, {"error": error_str})
            return

        self._send_json(201, result)

        # Fire-and-forget docker attach. Wrap in a logger so failures are
        # visible — otherwise the analyzer ends up created-but-unreachable
        # with no signal to the caller.
        def _connect_and_log():
            try:
                if not mgr.connect_mock_to_analyzer(name):
                    logger.warning(
                        "connect_mock_to_analyzer returned False for %s", name
                    )
            except Exception as err:  # noqa: BLE001 — background thread
                logger.exception(
                    "connect_mock_to_analyzer raised for %s: %s", name, err
                )

        threading.Thread(target=_connect_and_log, daemon=True).start()

    # ── Helpers ──────────────────────────────────────────────────

    def _extract_name(self, prefix: str) -> Optional[str]:
        name = self.path.split(prefix)[-1].split("?")[0].strip("/")
        if not name:
            return None
        # Reject path traversal and separator-based names up front.
        # Template names are expected to be simple slugs (letters/numbers/_/-).
        if not re.match(r"^[A-Za-z0-9_-]+$", name):
            return None
        return name

    # Sentinels for body parse failures (distinct from "no body")
    _JSON_PARSE_ERROR = object()
    _CONTENT_LENGTH_ERROR = object()

    def _read_json_body(self):
        """Read and parse JSON body. Returns dict, None, _JSON_PARSE_ERROR, or _CONTENT_LENGTH_ERROR."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            return self._CONTENT_LENGTH_ERROR
        if length <= 0:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, Exception):
            return self._JSON_PARSE_ERROR

    def _send_json(self, code: int, obj):
        payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(payload)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            logger.warning("Client disconnected while sending JSON response: %s", exc)

    def log_message(self, format, *args):
        logger.info("%s - %s", self.address_string(), format % args)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Thread-per-request HTTP server. Required so ASTM/MLLP TCP push operations
    don't block the HTTP handler thread (Nagle/buffering interaction)."""
    daemon_threads = True


def start_api_server(port: int):
    """Start the mock API HTTP server."""
    server = ThreadingHTTPServer(("0.0.0.0", port), MockAPIHandler)
    logger.info("Mock API server started on port %s", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Mock API server stopped")
        server.shutdown()
