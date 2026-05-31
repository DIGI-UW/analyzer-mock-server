"""
Deterministic, idempotent Docker network management for mock analyzers.

Each mock analyzer gets its own Docker network (10.42.N.0/24) so it has a
distinct source IP — the bridge identifies analyzers by source IP. The mock
container and bridge container are both attached to each analyzer's network.

Dynamic but deterministic:
- The subnet for an analyzer is a PURE FUNCTION of its name (see
  `_subnet_id_for`): the same analyzer always lands on the same IP across runs,
  independent of creation order, concurrency, or what else is allocated. There
  is no mutable allocation counter.
- Provisioning is CONVERGENT/idempotent (`create_analyzer` = "ensure"): Docker is
  the source of truth. If the network already exists (live, or an orphan from a
  prior run) its actual subnet is adopted rather than guessed, and connecting is
  a no-op when already attached. Re-running for the same analyzer yields the same
  result. A boot-time `reconcile_orphans` drains leftovers from crashed runs.

This is what lets the per-analyzer-network design withstand create/teardown churn
without leaking orphans or flaking on "ip=missing".

Requires:
- Docker socket mounted: /var/run/docker.sock
- `docker` Python package installed
- Env vars: MOCK_CONTAINER_NAME, BRIDGE_CONTAINER_NAME

Usage:
    manager = AnalyzerNetworkManager()
    manager.reconcile_orphans()                      # once, at startup
    result = manager.create_analyzer("bc5380", "mindray_bc5380")
    # result = {"name": "bc5380", "ip": "10.42.21.10", "network": "mock-analyzer-bc5380"}
    manager.remove_analyzer("bc5380")
"""

import functools
import hashlib
import logging
import os
import re
import threading
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def _synchronized(method):
    """Serialize a network-manager method on ``self._lock``.

    The mock's HTTP server is threaded (api.py ThreadingHTTPServer), so concurrent
    ``/analyzers`` requests would otherwise interleave Docker network create/connect
    and ``_analyzers`` updates. With deterministic per-name allocation, creates of
    *different* analyzers no longer contend on a shared counter — the lock is a
    backstop that keeps each ensure/remove atomic. RLock so one guarded method may
    call another (e.g. cleanup_all → remove_analyzer).
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


# Subnet allocation: 10.42.{N}.0/24 — a separate range from analyzer-net
# (172.21.1.0/24) to avoid Docker routing conflicts.
ANALYZER_IP_SUFFIX = 10  # Analyzer (mock) gets .10 on each subnet
BRIDGE_IP_SUFFIX = 2     # Bridge gets .2 (.1 is the Docker gateway)
NETWORK_PREFIX = "mock-analyzer-"

# Fixed subnet assignments for the canonical analyzers — stable, human-readable
# IPs. Everything else gets a deterministic hash-derived slot in the dynamic
# range below (also stable per name, just not human-assigned).
FIXED_SUBNETS: Dict[str, int] = {
    "genexpert": 20,
    "bc5380": 21,
    "bs200": 22,
    "bs300": 23,
}
DYNAMIC_SUBNET_BASE = 50   # Dynamic (hash-derived) slots live in [BASE, MAX]
DYNAMIC_SUBNET_MAX = 250   # (won't collide with the fixed 20-23 range)
MAX_SUBNET_ATTEMPTS = 30   # Probe budget when a chosen /24 collides with a different network


class AnalyzerNetworkManager:
    """Manages Docker networks for mock analyzers (deterministic + idempotent)."""

    def __init__(self):
        self._docker = None
        self._lock = threading.RLock()
        # Cache of provisioned analyzers (name → info). Docker is the source of
        # truth; this is a fast-path cache, reconciled against Docker on miss.
        self._analyzers: Dict[str, dict] = {}
        self._mock_container = os.environ.get("MOCK_CONTAINER_NAME", os.environ.get("HOSTNAME", ""))
        self._bridge_container = os.environ.get("BRIDGE_CONTAINER_NAME", "")

    @property
    def docker(self):
        """Lazy-init Docker client."""
        if self._docker is None:
            try:
                import docker
                self._docker = docker.from_env()
                logger.info("Docker client initialized")
            except ImportError:
                logger.error("Docker SDK not installed. Run: pip install docker")
                raise
            except Exception as e:
                logger.error("Failed to connect to Docker: %s", e)
                raise
        return self._docker

    # --- Deterministic allocation -------------------------------------------

    def _subnet_id_for(self, name: str) -> int:
        """Map an analyzer name → its subnet id (the N in 10.42.N.0/24).

        PURE function of the name: same name → same subnet on every run,
        independent of creation order, concurrency, or what else is allocated.
        Canonical analyzers use FIXED_SUBNETS; everything else gets a stable
        hash-derived slot in the dynamic range. Two distinct dynamic names could
        in principle hash to the same slot; that rare collision is resolved at
        create time by a bounded forward probe (see create_analyzer).
        """
        normalized = name.lower()
        if normalized in FIXED_SUBNETS:
            return FIXED_SUBNETS[normalized]
        span = DYNAMIC_SUBNET_MAX - DYNAMIC_SUBNET_BASE + 1
        digest = int(hashlib.sha1(normalized.encode("utf-8")).hexdigest(), 16)
        return DYNAMIC_SUBNET_BASE + (digest % span)

    @staticmethod
    def _subnet_id_of(network) -> Optional[int]:
        """The N from a network's 10.42.N.0/24 IPAM subnet, or None if absent."""
        for entry in (network.attrs.get("IPAM", {}).get("Config") or []):
            match = re.match(r"^10\.42\.(\d+)\.0/24$", entry.get("Subnet", "") or "")
            if match:
                return int(match.group(1))
        return None

    def _get_network(self, network_name: str):
        """Return the Docker network by name, or None if it doesn't exist."""
        try:
            return self.docker.networks.get(network_name)
        except Exception:
            return None

    def _subnet_in_use(self, subnet_id: int) -> bool:
        target = f"10.42.{subnet_id}.0/24"
        try:
            for network in self.docker.networks.list():
                # IPAM.Config can be explicitly null (not just missing) when a
                # network was created without an address pool — `or []` handles
                # that so iteration doesn't blow up with "'NoneType' is not iterable".
                config = network.attrs.get("IPAM", {}).get("Config") or []
                if any(entry.get("Subnet") == target for entry in config):
                    return True
        except Exception as err:
            logger.warning("Failed to inspect existing Docker subnets: %s", err)
        return False

    # --- Provisioning (convergent / idempotent) -----------------------------

    @_synchronized
    def create_analyzer(
        self,
        name: str,
        template_name: str,
        port: int = 0,
        connect_mock: bool = True,
    ) -> dict:
        """Ensure a Docker network exists for the analyzer and the mock + bridge
        are attached, then return its connection info.

        Convergent / idempotent: re-running for the same name returns the same
        result with no churn. Docker is the source of truth —
        - if the network already exists (live or an orphan), its ACTUAL subnet is
          adopted (so containers connect at IPs inside the network, never the
          "invalid endpoint settings" mismatch);
        - a NEW network's subnet is the deterministic `_subnet_id_for(name)`, so
          the same analyzer always lands on the same IP across runs.

        Args:
            name: Unique analyzer name (e.g., "bc5380")
            template_name: Mock template to associate (e.g., "mindray_bc5380")
            port: Analyzer listen port (informational)
            connect_mock: attach the mock container now (api.py defers this to an
                async step to avoid tearing down the in-flight HTTP socket)

        Returns:
            dict with name, ip, network, subnet, template, port
        """
        network_name = f"{NETWORK_PREFIX}{name}"

        # Fast path: provisioned in this process AND still present in Docker.
        cached = self._analyzers.get(name)
        if cached is not None and self._get_network(network_name) is not None:
            return cached

        import docker.types

        created_here = False
        network = self._get_network(network_name)
        subnet_id: Optional[int] = None

        if network is not None:
            # Adopt the existing network's actual subnet (Docker = truth).
            subnet_id = self._subnet_id_of(network)
            if subnet_id is None:
                logger.warning("Existing %s has no parseable 10.42.x subnet; recreating", network_name)
                self._cleanup_network(network_name)
                network = None

        if network is None:
            subnet_id = self._subnet_id_for(name)
            for _attempt in range(MAX_SUBNET_ATTEMPTS):
                subnet = f"10.42.{subnet_id}.0/24"
                try:
                    ipam = docker.types.IPAMConfig(pool_configs=[docker.types.IPAMPool(subnet=subnet)])
                    network = self.docker.networks.create(network_name, driver="bridge", ipam=ipam)
                    created_here = True
                    logger.info("Created network %s (subnet %s)", network_name, subnet)
                    break
                except Exception as create_err:
                    msg = str(create_err)
                    if "overlap" not in msg.lower():
                        raise
                    # The deterministic slot collided with a DIFFERENT network —
                    # probe forward to the next free subnet (bounded, deterministic
                    # given current Docker state). The analyzer's own network does
                    # not exist yet (we checked), so this is a genuine cross-name
                    # collision, not our own.
                    logger.warning("Subnet %s overlaps another network; probing next free for %s", subnet, name)
                    subnet_id += 1
                    while self._subnet_in_use(subnet_id):
                        subnet_id += 1
                    continue
            if network is None:
                raise RuntimeError(
                    f"Could not allocate a non-overlapping subnet for {name} "
                    f"after {MAX_SUBNET_ATTEMPTS} attempts"
                )

        subnet = f"10.42.{subnet_id}.0/24"
        analyzer_ip = f"10.42.{subnet_id}.{ANALYZER_IP_SUFFIX}"
        bridge_ip = f"10.42.{subnet_id}.{BRIDGE_IP_SUFFIX}"

        try:
            if connect_mock and self._mock_container:
                self._ensure_connected(network, self._mock_container, analyzer_ip, "mock")
            if self._bridge_container:
                self._ensure_connected(network, self._bridge_container, bridge_ip, "bridge")
        except Exception as e:
            logger.error("Failed to wire up analyzer network %s: %s", name, e)
            # Roll back ONLY a network we created in this call — never one we merely
            # adopted (that could be a live/seeded network).
            if created_here:
                try:
                    self._cleanup_network(network_name)
                except Exception:
                    pass
            raise

        result = {
            "name": name,
            "ip": analyzer_ip,
            "network": network_name,
            "subnet": subnet,
            "template": template_name,
            "port": port,
        }
        self._analyzers[name] = result
        return result

    def _ensure_connected(self, network, container: str, ip: str, label: str):
        """Idempotently attach `container` to `network` at `ip`.

        No-op when already connected at `ip`; reconnects at `ip` if attached at a
        different address (e.g. the network was recreated). Raises on a real
        connect failure so the caller can roll back.
        """
        try:
            network.connect(container, ipv4_address=ip)
            logger.info("Connected %s (%s) to %s at %s", label, container, network.name, ip)
            return
        except Exception as err:
            if "already" not in str(err).lower():
                raise
        # Already attached — confirm the IP, reconcile if it drifted. Best-effort.
        try:
            network.reload()
            for cid, info in (network.attrs.get("Containers") or {}).items():
                if info.get("Name") == container or cid == container:
                    current = (info.get("IPv4Address") or "").split("/")[0]
                    if current and current != ip:
                        logger.warning("%s on %s at %s, expected %s — reconnecting",
                                       label, network.name, current, ip)
                        network.disconnect(container, force=True)
                        network.connect(container, ipv4_address=ip)
                    else:
                        logger.info("%s already connected to %s at %s", label, network.name, ip)
                    return
        except Exception as err:
            logger.warning("Could not verify %s attachment on %s: %s", label, network.name, err)

    @_synchronized
    def connect_mock_to_analyzer(self, name: str) -> bool:
        """Attach the running mock container to an existing analyzer network
        (api.py's deferred/async mock attach). Idempotent."""
        info = self._analyzers.get(name)
        if not info or not self._mock_container:
            return False
        network = self._get_network(info["network"])
        if network is None:
            logger.warning("connect_mock_to_analyzer: network %s not found for %s", info["network"], name)
            return False
        try:
            self._ensure_connected(network, self._mock_container, info["ip"], "mock")
            return True
        except Exception as conn_err:
            logger.warning("Failed to connect mock to %s: %s", info["network"], conn_err)
            return False

    @_synchronized
    def remove_analyzer(self, name: str) -> bool:
        """Remove an analyzer's Docker network (cached or orphaned). Idempotent —
        returns True if the network is gone afterwards."""
        self._analyzers.pop(name, None)
        return self._cleanup_network(f"{NETWORK_PREFIX}{name}")

    def list_analyzers(self) -> List[dict]:
        """List all active mock analyzers (from the in-process cache)."""
        return list(self._analyzers.values())

    def get_analyzer(self, name: str) -> Optional[dict]:
        """Get a specific analyzer's info."""
        return self._analyzers.get(name)

    @_synchronized
    def cleanup_all(self):
        """Remove all analyzer networks this process created. Called on shutdown."""
        for name in list(self._analyzers.keys()):
            self.remove_analyzer(name)
        logger.info("Cleaned up all analyzer networks")

    @_synchronized
    def reconcile_orphans(self) -> int:
        """Drain orphaned analyzer networks at startup, converging to a clean
        baseline. An orphan is a ``mock-analyzer-*`` network with NO containers
        attached — a leftover from a crashed/killed prior run. Networks WITH
        containers are live (e.g. seeded analyzers) and are kept. Returns the
        number removed. Safe to call once at boot (before any provisioning).
        """
        removed = 0
        try:
            for network in self.docker.networks.list():
                nm = getattr(network, "name", "") or network.attrs.get("Name", "")
                if not nm.startswith(NETWORK_PREFIX):
                    continue
                if network.attrs.get("Containers"):
                    continue  # live — keep
                if self._cleanup_network(nm):
                    removed += 1
        except Exception as e:
            logger.warning("Orphan reconcile failed: %s", e)
        if removed:
            logger.info("Reconcile removed %d orphaned analyzer network(s)", removed)
        return removed

    def _cleanup_network(self, network_name: str) -> bool:
        """Disconnect all containers and remove a network. Returns True if the
        network is absent afterwards (removed now, or already gone)."""
        try:
            network = self.docker.networks.get(network_name)
        except Exception:
            return True  # already absent
        try:
            for container in (network.attrs.get("Containers") or {}).values():
                try:
                    network.disconnect(container["Name"], force=True)
                except Exception:
                    pass
            network.remove()
            logger.info("Removed network %s", network_name)
            return True
        except Exception as e:
            logger.warning("Failed to cleanup network %s: %s", network_name, e)
            return False
