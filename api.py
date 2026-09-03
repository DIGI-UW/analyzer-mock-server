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
import time
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

# Provisioning is convergent/idempotent, so a transient Docker-churn failure is
# safe to retry — this is what prevents the intermittent `ip=missing` the harness
# saw (a one-shot create that hit a transient attach error returned no IP).
CREATE_MAX_RETRIES = 3
CREATE_RETRY_BACKOFF_S = 0.4


def _load_template(analyzer: str) -> Optional[Dict]:
    """Load a template and resolve any declared profile before returning it."""
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "templates", f"{analyzer}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            template = json.load(f)
    except Exception as e:
        logger.warning("Failed to load template %s: %s", path, e)
        return None

    try:
        from profile_adapter import load_profile_backed_template
        merged = load_profile_backed_template(analyzer, template)
        if merged is not None:
            return merged
    except Exception as e:
        logger.warning("Failed to derive profile-backed template for %s: %s", analyzer, e)
        if template.get("profileRef"):
            return None

    return template


def _template_not_found(requested: str, resolved: str) -> str:
    """404 message that names the actually-missing template — and the instance it
    was resolved from, when an instance name resolved to a different template."""
    if resolved and resolved != requested:
        return f"Template not found: '{resolved}' (resolved from instance '{requested}')"
    return f"Template not found: {requested}"


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


FILE_OUTPUT_ROOTS = ("/data/analyzer-imports", "/tmp")


def _is_allowed_file_output_dir(target_dir: str) -> bool:
    resolved_dir = os.path.realpath(target_dir)
    for root in FILE_OUTPUT_ROOTS:
        resolved_root = os.path.realpath(root)
        try:
            if os.path.commonpath((resolved_dir, resolved_root)) == resolved_root:
                return True
        except ValueError:
            continue
    return False


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

    def _resolve_instance(self, name: str):
        """Resolve a /simulate URL segment as a provisioned analyzer *instance*.

        The instance is the single identity key: its registry record carries
        both the template (message/sender shape) and the per-analyzer source IP,
        so one lookup yields a coherent identity. Returns
        ``(template_name, instance_ip)``.

        Falls back to ``(name, None)`` when ``name`` is not a provisioned
        instance — i.e. a caller addressing a bare template directly (hermetic /
        QC unit tests that never stand up a Docker network, so there is no IP to
        sync). This keeps the IP-bearing path single-keyed while preserving the
        template-only path where there is no instance by design.
        """
        try:
            mgr = self._get_network_manager()
            info = mgr.get_analyzer(name) if mgr else None
        except Exception as e:
            logger.debug("Instance resolution failed for '%s': %s", name, e)
            info = None
        if info:
            return info.get("template") or name, info.get("ip")
        return name, None

    def do_GET(self):
        if self.path == "/health" or self.path == "/":
            self._send_json(200, {
                "status": "ok",
                "service": "Multi-Protocol Analyzer Simulator",
                "endpoints": {
                    "GET /health": "Health check",
                    "GET /simulate/hl7/{template}": "Generate HL7 ORU^R01",
                    "POST /simulate/hl7/{template}": "Generate + push HL7 (body: destination, count, qc, qc_deviation)",
                    "GET /simulate/astm/{template}": "Generate ASTM message",
                    "POST /simulate/astm/{template}": "Generate + push ASTM (body: destination, count, sample_id, results, source_ip, qc, qc_deviation)",
                    "GET /simulate/file/{template}": "Generate FILE payload",
                    "POST /simulate/file/{template}": "Generate + write FILE (body: target_dir, filename, qc, qc_deviation)",
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
            if not params.get("destination"):
                self._send_json(400, {
                    "error": "destination is required for the saved Bridge MLLP listener"
                })
                return
            kwargs = {
                "patient_id": params.get("patientId") or params.get("patient_id"),
                "sample_id": params.get("sampleId") or params.get("sample_id"),
                "tests": params.get("tests"),
                "destination": params.get("destination"),
                "count": params.get("count", 1),
                "qc": params.get("qc"),
                "qc_deviation": params.get("qc_deviation"),
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
            if not mgr.get_analyzer(name):
                self._send_json(404, {"removed": False, "error": f"Analyzer '{name}' not found"})
                return

            # The simulator is attached to the analyzer network so it can send
            # from the instrument's source IP. Removing that network before the
            # HTTP response completes drops the caller's in-flight connection.
            # Acknowledge teardown first, then perform it off the request thread.
            self._send_json(202, {"removalScheduled": True, "name": name})

            def _remove_after_response():
                try:
                    if not mgr.remove_analyzer(name):
                        logger.warning("Scheduled removal failed for analyzer %s", name)
                except Exception:
                    logger.exception("Scheduled removal raised for analyzer %s", name)

            threading.Thread(target=_remove_after_response, daemon=True).start()
            return
        self.send_error(404, "Not Found")

    # ── Route handlers ───────────────────────────────────────────

    def _handle_hl7(self, analyzer: str, kwargs: Dict):
        template_name, instance_ip = self._resolve_instance(analyzer)
        template = _load_template(template_name)
        if not template:
            self._send_json(404, {"error": _template_not_found(analyzer, template_name)})
            return
        try:
            destination = kwargs.get("destination")
            # The provisioned instance owns the source IP: a push must leave from
            # the analyzer's own interface so the bridge identifies it by the
            # connection's source address. Only the caller can override.
            source_ip = kwargs.get("source_ip") or instance_ip
            count = min(max(int(kwargs.get("count", 1)), 1), 1000)
            qc_mode = bool(kwargs.get("qc"))
            qc_deviation = kwargs.get("qc_deviation")

            gen_kwargs = {k: v for k, v in kwargs.items()
                         if k in ("patient_id", "sample_id", "tests") and v is not None}
            results = []
            pushed_count = 0
            first_message = None
            handler = HL7Handler()

            for i in range(count):
                if qc_mode:
                    qc_kwargs = {}
                    if qc_deviation is not None:
                        qc_kwargs["deviation"] = float(qc_deviation)
                    try:
                        msg = handler.generate_qc(template, **qc_kwargs)
                    except ValueError as e:
                        self._send_json(400, {"error": str(e)})
                        return
                else:
                    msg = handler.generate(template, **gen_kwargs)
                if first_message is None:
                    first_message = msg
                pushed = False
                push_err = None
                if destination:
                    pushed, push_err = push_hl7_to_destination(destination, msg, source_ip=source_ip)
                    if pushed:
                        pushed_count += 1
                results.append({
                    "message_number": i + 1,
                    "pushed": pushed,
                    "error": push_err,
                    "sample_id": _extract_sample_id_from_hl7(msg),
                    "preview": msg.split("\r")[0][:80] + "...",
                })

            if destination or count > 1:
                self._send_json(200, {
                    "status": "completed",
                    "analyzer": analyzer,
                    "count": count,
                    "qc": qc_mode,
                    "qc_deviation": qc_deviation if qc_mode else None,
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
                "qc": qc_mode,
                "qc_deviation": qc_deviation if qc_mode else None,
                "sample_id": _extract_sample_id_from_hl7(msg),
                "message": msg,
            })
        except Exception as e:
            logger.exception("HL7 simulate failed for %s", analyzer)
            self._send_json(500, {"error": str(e)})

    def _handle_astm_get(self, template_name: str):
        resolved_template, _ = self._resolve_instance(template_name)
        template = _load_template(resolved_template)
        if not template:
            self._send_json(404, {"error": _template_not_found(template_name, resolved_template)})
            return
        if template.get('protocol', {}).get('type') != 'ASTM':
            self._send_json(400, {"error": "Template is not ASTM protocol"})
            return
        try:
            msg = ASTMHandler().generate(template, use_seed=True)
            self._send_json(200, {"status": "generated", "template": resolved_template, "message": msg})
        except Exception as e:
            logger.exception("ASTM GET failed for %s", template_name)
            self._send_json(500, {"error": str(e)})

    def _handle_astm_post(self, template_name: str):
        resolved_template, instance_ip = self._resolve_instance(template_name)
        template = _load_template(resolved_template)
        if not template:
            self._send_json(404, {"error": _template_not_found(template_name, resolved_template)})
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
        if not destination:
            self._send_json(400, {
                "error": "destination is required for the saved Bridge ASTM listener"
            })
            return
        # The provisioned instance owns the source IP (see _handle_hl7).
        source_ip = params.get("source_ip") or instance_ip
        qc_mode = bool(params.get("qc"))
        qc_deviation = params.get("qc_deviation")

        gen_kwargs = {"use_seed": True}
        if params.get("sample_id"):
            gen_kwargs["sample_id"] = params["sample_id"]
        if "results" in params:
            gen_kwargs["results"] = params["results"]

        results = []
        success_count = 0
        handler = ASTMHandler()

        for i in range(count):
            if qc_mode:
                qc_kwargs = {}
                if qc_deviation is not None:
                    qc_kwargs["deviation"] = float(qc_deviation)
                try:
                    msg = handler.generate_qc(template, **qc_kwargs)
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                    return
            else:
                msg = handler.generate(template, **gen_kwargs)
            pushed = False
            push_err = None
            if destination:
                pushed, push_err = push_astm_to_destination(destination, msg, source_ip=source_ip)
                if pushed:
                    success_count += 1
            results.append({
                "message_number": i + 1,
                "pushed": pushed,
                "error": push_err,
                "sample_id": _extract_sample_id_from_astm(msg),
                "preview": msg.split('\n')[0][:80] + "..." if msg else "",
            })

        self._send_json(200, {
            "status": "completed",
            "template": resolved_template,
            "count": count,
            "qc": qc_mode,
            "pushed": success_count if destination else None,
            "destination": destination,
            "source_ip": source_ip,
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
        if not target_dir:
            self._send_json(400, {
                "error": "target_dir is required for the Bridge-owned FILE watch directory"
            })
            return
        qc_mode = bool(params.get("qc"))
        qc_deviation = params.get("qc_deviation")

        # QC mode generates a deterministic file in the Bridge-owned watch
        # directory rather than bypassing the analyzer FILE transport.
        if qc_mode:
            try:
                qc_kwargs = {}
                if qc_deviation is not None:
                    qc_kwargs["deviation"] = float(qc_deviation)
                content = FileHandler().generate_qc(template, **qc_kwargs)
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
                return
            except Exception as e:  # noqa: BLE001
                logger.exception("FILE QC generation failed for %s", template_name)
                self._send_json(500, {"error": str(e)})
                return

            if not _is_allowed_file_output_dir(target_dir):
                self._send_json(400, {"error": f"target_dir must be under {list(FILE_OUTPUT_ROOTS)}"})
                return
            resolved_dir = os.path.realpath(target_dir)
            os.makedirs(resolved_dir, exist_ok=True)
            ext = FileHandler.qc_extension(template)
            fname = params.get("filename") or f"qc-{template_name}-{uuid.uuid4().hex[:8]}{ext}"
            out_path = os.path.join(resolved_dir, os.path.basename(fname))
            with open(out_path, "wb") as f:
                f.write(content)
            written_path = out_path
            qc_format = FileHandler.qc_format(template)
            response = {
                "status": "completed",
                "template": template_name,
                "qc": True,
                "qc_deviation": qc_deviation,
                "written_path": written_path,
                "format": qc_format,
            }
            # XLSX is binary — don't echo the bytes in JSON; return text inline
            # only for CSV/TSV where it's diagnostic-friendly.
            if qc_format in ("CSV", "TSV"):
                response["content"] = content.decode("utf-8")
            self._send_json(200, response)
            return

        fixture_cfg = template.get("fixture")
        if fixture_cfg:
            # Fixture-based: copy real file + return parsed metadata
            try:
                self._handle_fixture_file_post(template_name, template, fixture_cfg, target_dir, params)
            except Exception as e:
                logger.exception("Fixture FILE POST failed for %s", template_name)
                self._send_json(500, {"error": str(e)})
        else:
            # Templates without a captured fixture generate deterministic output.
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
        """Copy a real analyzer fixture into the Bridge-owned watch directory."""
        fixture_rel = fixture_cfg["file"]
        fixture_path = os.path.join(os.path.dirname(__file__), fixture_rel)
        if not os.path.isfile(fixture_path):
            self._send_json(404, {"error": f"Fixture file not found: {fixture_rel}"})
            return

        # Parse metadata from the fixture
        metadata_results = parse_fixture(fixture_path, fixture_cfg)

        if not _is_allowed_file_output_dir(target_dir):
            self._send_json(400, {"error": f"target_dir must be under {list(FILE_OUTPUT_ROOTS)}"})
            return
        resolved_dir = os.path.realpath(target_dir)
        os.makedirs(resolved_dir, exist_ok=True)
        ext = os.path.splitext(fixture_path)[1]
        filename = params.get("filename") or f"{template_name}-{uuid.uuid4().hex[:8]}{ext}"
        out_path = os.path.join(resolved_dir, os.path.basename(filename))

        shutil.copy2(fixture_path, out_path)

        # Prevent Bridge hash deduplication from hiding repeated text fixtures.
        if ext.lower() in ('.csv', '.tsv', '.txt'):
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
        # Provisioning is idempotent, so retry transient Docker-churn failures
        # rather than ever returning a response without an IP (the `ip=missing`
        # flake). Connecting the current mock container to a new network can tear
        # down the in-flight HTTP socket, so the mock attach is deferred to the
        # async step below; only the deterministic IP is needed in the response.
        result = None
        last_error = None
        last_was_conflict = False
        for attempt in range(1, CREATE_MAX_RETRIES + 1):
            try:
                result = mgr.create_analyzer(name, template, port, connect_mock=False)
                break
            except Exception as e:
                error_str = str(e)
                last_was_conflict = "Conflict" in error_str or "already exists" in error_str
                if last_was_conflict:
                    # Network exists — return cached info immediately if we have
                    # it; otherwise retry so create_analyzer adopts it (idempotent)
                    # and returns the IP rather than a bare 409.
                    existing = mgr._analyzers.get(name)
                    if existing:
                        self._send_json(200, existing)
                        return
                last_error = error_str
                logger.warning("create_analyzer for %s failed (attempt %d/%d): %s",
                               name, attempt, CREATE_MAX_RETRIES, error_str)
                if attempt < CREATE_MAX_RETRIES:
                    time.sleep(CREATE_RETRY_BACKOFF_S * attempt)
        if result is None:
            # A persistent "already exists" is a client-visible conflict (409),
            # not a server error (500) — preserve the pre-retry 409 semantics.
            if last_was_conflict:
                self._send_json(409, {"error": last_error})
            else:
                self._send_json(500, {
                    "error": f"provisioning failed for {name} after {CREATE_MAX_RETRIES} "
                             f"attempts: {last_error}"
                })
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
    # Converge to a clean baseline: drain orphaned analyzer networks left by a
    # crashed/killed prior run (zero-container mock-analyzer-* nets). Live/seeded
    # networks (with containers attached) are kept. Best-effort — never blocks
    # startup if Docker is unavailable.
    try:
        mgr = MockAPIHandler._get_network_manager()
        if mgr is not None:
            mgr.reconcile_orphans()
    except Exception as e:  # noqa: BLE001 — startup hygiene must not crash the server
        logger.warning("Startup orphan reconcile skipped: %s", e)

    server = ThreadingHTTPServer(("0.0.0.0", port), MockAPIHandler)
    logger.info("Mock API server started on port %s", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Mock API server stopped")
        server.shutdown()
