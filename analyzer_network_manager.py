"""
Dynamic Docker network management for mock analyzers.

Creates per-analyzer Docker networks at runtime, giving each mock analyzer
a unique IP address. The mock container and bridge container are both
connected to each analyzer's network.

Requires:
- Docker socket mounted: /var/run/docker.sock
- `docker` Python package installed
- Env vars: MOCK_CONTAINER_NAME, BRIDGE_CONTAINER_NAME

Usage:
    manager = AnalyzerNetworkManager()
    result = manager.create_analyzer("bc5380", "mindray_bc5380")
    # result = {"name": "bc5380", "ip": "172.21.10.10", "network": "mock-analyzer-bc5380"}
    manager.remove_analyzer("bc5380")
"""

import functools
import logging
import os
import threading
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def _synchronized(method):
    """Serialize a network-manager method on ``self._lock``.

    The mock's HTTP server is threaded (api.py ThreadingHTTPServer), so
    concurrent ``/analyzers`` requests would otherwise race on the shared subnet
    allocator (``_next_dynamic_subnet``), the ``_analyzers`` map, and Docker
    network create/connect. Guarding the *mutating* methods makes concurrent
    provisioning behave exactly as if sequential — creates queue behind each
    other while read-only endpoints (``list_analyzers``/``get_analyzer``) stay
    responsive. RLock so one guarded method may call another (e.g. cleanup_all →
    remove_analyzer).
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper

# Subnet allocation: 10.42.{N}.0/24 — completely separate range from
# analyzer-net (172.21.1.0/24) to avoid Docker routing conflicts
ANALYZER_IP_SUFFIX = 10  # Analyzer (mock) gets .10 on each subnet
BRIDGE_IP_SUFFIX = 2     # Bridge gets .2 (.1 is the Docker gateway)
NETWORK_PREFIX = "mock-analyzer-"

# Fixed subnet assignments for stable, deterministic IPs per analyzer.
# Each analyzer always gets the same IP regardless of creation order.
FIXED_SUBNETS: Dict[str, int] = {
    "genexpert": 20,
    "bc5380": 21,
    "bs200": 22,
    "bs300": 23,
}
DYNAMIC_SUBNET_BASE = 50  # Dynamic allocations start here (won't collide with fixed)
MAX_SUBNET_ATTEMPTS = 30  # Retry budget when a chosen /24 overlaps an existing network


class AnalyzerNetworkManager:
    """Manages Docker networks for mock analyzers."""

    def __init__(self):
        self._docker = None
        self._lock = threading.RLock()
        self._analyzers: Dict[str, dict] = {}  # name → {network, ip, template, ...}
        self._next_dynamic_subnet = DYNAMIC_SUBNET_BASE
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

    def _select_subnet_id(self, name: str) -> int:
        """Choose a stable subnet id, skipping any already in use.

        FIXED_SUBNETS is matched by exact name only. Substring match would let
        e.g. "demo-genexpert-site1" and "demo-genexpert-site2" both claim
        subnet 20, causing Docker "Pool overlaps" errors on the second create.
        When the fixed subnet is already in use (e.g. the caller is creating
        a second instance of the same template), fall through to the dynamic
        range.
        """
        normalized = name.lower()
        fixed = FIXED_SUBNETS.get(normalized)
        if fixed is not None and not self._subnet_in_use(fixed):
            return fixed

        subnet_id = self._next_dynamic_subnet
        while self._subnet_in_use(subnet_id):
            subnet_id += 1
        self._next_dynamic_subnet = subnet_id + 1
        return subnet_id

    def _subnet_in_use(self, subnet_id: int) -> bool:
        target = f"10.42.{subnet_id}.0/24"
        try:
            for network in self.docker.networks.list():
                # IPAM.Config can be explicitly null (not just missing) when
                # a network was created without an address pool — `or []`
                # handles that case so the iteration doesn't blow up with
                # "'NoneType' object is not iterable".
                config = network.attrs.get("IPAM", {}).get("Config") or []
                if any(entry.get("Subnet") == target for entry in config):
                    return True
        except Exception as err:
            logger.warning("Failed to inspect existing Docker subnets: %s", err)
        return False

    @_synchronized
    def create_analyzer(
        self,
        name: str,
        template_name: str,
        port: int = 0,
        connect_mock: bool = True,
    ) -> dict:
        """Create a Docker network for a mock analyzer.

        Args:
            name: Unique analyzer name (e.g., "bc5380")
            template_name: Mock template to associate (e.g., "mindray_bc5380")
            port: Analyzer listen port (informational)

        Returns:
            dict with name, ip, network, template, port
        """
        if name in self._analyzers:
            return self._analyzers[name]

        subnet_id = self._select_subnet_id(name)
        network_name = f"{NETWORK_PREFIX}{name}"

        try:
            import docker.types
            network = None

            # Create the network. A live analyzer's network is reused earlier
            # (self._analyzers cache); here we only ever create, draining any
            # stale same-named orphan first (see the Conflict branch below).
            #
            # Retry on Docker "Pool overlaps": the dynamically chosen /24 can
            # collide with a network created concurrently (e.g. two demo
            # analyzers provisioning at once) or one that _subnet_in_use's
            # exact-subnet scan didn't match. On overlap, advance to the next
            # free subnet id and retry — without this the create raises and the
            # analyzer comes back with no IP (the "ip=missing" harness failure).
            for _attempt in range(MAX_SUBNET_ATTEMPTS):
                subnet = f"10.42.{subnet_id}.0/24"
                try:
                    ipam_pool = docker.types.IPAMPool(subnet=subnet)
                    ipam_config = docker.types.IPAMConfig(pool_configs=[ipam_pool])
                    network = self.docker.networks.create(
                        network_name, driver="bridge", ipam=ipam_config
                    )
                    logger.info("Created network %s (subnet %s)", network_name, subnet)
                    break
                except Exception as create_err:
                    msg = str(create_err)
                    if "Conflict" in msg or "already exists" in msg:
                        # A network with this name already exists but is NOT one we
                        # created this session — idempotent reuse for a live analyzer
                        # is handled above via self._analyzers. So this is an ORPHAN
                        # left by a previous run, and its subnet may differ from the
                        # one we just tried. Reusing it would connect containers at
                        # IPs outside its subnet (Docker "invalid endpoint settings:
                        # no configured subnet ... contain the IP"). Remove it and
                        # recreate fresh at our intended subnet.
                        logger.warning("Removing orphaned network %s and recreating fresh", network_name)
                        self._cleanup_network(network_name)
                        continue
                    if "overlap" not in msg.lower():
                        raise
                    logger.warning(
                        "Subnet %s overlaps an existing network; retrying %s with the next free subnet",
                        subnet, name,
                    )
                    subnet_id += 1
                    while self._subnet_in_use(subnet_id):
                        subnet_id += 1
                    self._next_dynamic_subnet = max(self._next_dynamic_subnet, subnet_id + 1)
            if network is None:
                raise RuntimeError(
                    f"Could not allocate a non-overlapping subnet for {name} "
                    f"after {MAX_SUBNET_ATTEMPTS} attempts"
                )

            subnet = f"10.42.{subnet_id}.0/24"
            analyzer_ip = f"10.42.{subnet_id}.{ANALYZER_IP_SUFFIX}"
            bridge_ip = f"10.42.{subnet_id}.{BRIDGE_IP_SUFFIX}"

            # Connect mock container (skip if already connected)
            if connect_mock and self._mock_container:
                try:
                    network.connect(self._mock_container, ipv4_address=analyzer_ip)
                    logger.info("Connected mock (%s) to %s at %s",
                                self._mock_container, network_name, analyzer_ip)
                except Exception as conn_err:
                    if "already" in str(conn_err).lower():
                        logger.info("Mock already connected to %s", network_name)
                    else:
                        raise

            # Connect bridge container (skip if already connected)
            if self._bridge_container:
                try:
                    network.connect(self._bridge_container, ipv4_address=bridge_ip)
                    logger.info("Connected bridge (%s) to %s at %s",
                                self._bridge_container, network_name, bridge_ip)
                except Exception as conn_err:
                    if "already" in str(conn_err).lower():
                        logger.info("Bridge already connected to %s", network_name)
                    else:
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

        except Exception as e:
            logger.error("Failed to create analyzer network %s: %s", name, e)
            # Roll back a network we created but couldn't fully wire up (e.g. a
            # connect failure), so a failed create never leaves a half-created
            # orphan that poisons the next run's name-conflict path. Safe no-op
            # if nothing was created.
            try:
                self._cleanup_network(network_name)
            except Exception:
                pass
            raise

    @_synchronized
    def connect_mock_to_analyzer(self, name: str) -> bool:
        """Attach the running mock container to an existing analyzer network."""
        info = self._analyzers.get(name)
        if not info or not self._mock_container:
            return False

        try:
            network = self.docker.networks.get(info["network"])
            network.connect(self._mock_container, ipv4_address=info["ip"])
            logger.info(
                "Connected mock (%s) to %s at %s",
                self._mock_container,
                info["network"],
                info["ip"],
            )
            return True
        except Exception as conn_err:
            if "already" in str(conn_err).lower():
                logger.info("Mock already connected to %s", info["network"])
                return True
            logger.warning(
                "Failed to connect mock (%s) to %s: %s",
                self._mock_container,
                info["network"],
                conn_err,
            )
            return False

    @_synchronized
    def remove_analyzer(self, name: str) -> bool:
        """Remove an analyzer's Docker network.

        Handles both cached and orphaned networks (e.g., from a previous
        mock process that didn't clean up). Returns True if removed.
        """
        if name in self._analyzers:
            info = self._analyzers.pop(name)
            network_name = info["network"]
            return self._cleanup_network(network_name)

        # Try to remove orphaned Docker network (not in cache but may exist)
        network_name = f"{NETWORK_PREFIX}{name}"
        return self._cleanup_network(network_name)

    def list_analyzers(self) -> List[dict]:
        """List all active mock analyzers."""
        return list(self._analyzers.values())

    def get_analyzer(self, name: str) -> Optional[dict]:
        """Get a specific analyzer's info."""
        return self._analyzers.get(name)

    @_synchronized
    def cleanup_all(self):
        """Remove all analyzer networks. Called on shutdown."""
        for name in list(self._analyzers.keys()):
            self.remove_analyzer(name)
        logger.info("Cleaned up all analyzer networks")

    def _cleanup_network(self, network_name: str) -> bool:
        """Disconnect all containers and remove a network."""
        try:
            network = self.docker.networks.get(network_name)
            # Disconnect all containers
            for container in network.attrs.get("Containers", {}).values():
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
