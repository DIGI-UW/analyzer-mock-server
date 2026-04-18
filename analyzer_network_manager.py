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

import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

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


class AnalyzerNetworkManager:
    """Manages Docker networks for mock analyzers."""

    def __init__(self):
        self._docker = None
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
                config = network.attrs.get("IPAM", {}).get("Config", [])
                if any(entry.get("Subnet") == target for entry in config):
                    return True
        except Exception as err:
            logger.warning("Failed to inspect existing Docker subnets: %s", err)
        return False

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

        subnet = f"10.42.{subnet_id}.0/24"
        analyzer_ip = f"10.42.{subnet_id}.{ANALYZER_IP_SUFFIX}"
        bridge_ip = f"10.42.{subnet_id}.{BRIDGE_IP_SUFFIX}"
        network_name = f"{NETWORK_PREFIX}{name}"

        try:
            import docker.types
            network = None

            # Try to create network, or reuse existing (idempotent)
            try:
                ipam_pool = docker.types.IPAMPool(subnet=subnet)
                ipam_config = docker.types.IPAMConfig(pool_configs=[ipam_pool])
                network = self.docker.networks.create(
                    network_name, driver="bridge", ipam=ipam_config
                )
                logger.info("Created network %s (subnet %s)", network_name, subnet)
            except Exception as create_err:
                if "Conflict" in str(create_err) or "already exists" in str(create_err):
                    # Reuse existing Docker network (e.g., left over from previous run)
                    network = self.docker.networks.get(network_name)
                    logger.info("Reusing existing network %s", network_name)
                else:
                    raise

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
            raise

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
