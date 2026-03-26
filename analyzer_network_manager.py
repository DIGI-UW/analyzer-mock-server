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

# Subnet allocation: 172.21.{10+N}.0/24 — avoids conflict with
# analyzer-net (172.21.1.0/24) used by compose
SUBNET_BASE = 10
ANALYZER_IP_SUFFIX = 10  # Analyzer gets .10 on each subnet
BRIDGE_IP_SUFFIX = 1     # Bridge gets .1 (gateway-like)
NETWORK_PREFIX = "mock-analyzer-"


class AnalyzerNetworkManager:
    """Manages Docker networks for mock analyzers."""

    def __init__(self):
        self._docker = None
        self._analyzers: Dict[str, dict] = {}  # name → {network, ip, template, ...}
        self._next_subnet = SUBNET_BASE
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

    def create_analyzer(self, name: str, template_name: str, port: int = 0) -> dict:
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

        subnet_id = self._next_subnet
        self._next_subnet += 1

        subnet = f"172.21.{subnet_id}.0/24"
        analyzer_ip = f"172.21.{subnet_id}.{ANALYZER_IP_SUFFIX}"
        bridge_ip = f"172.21.{subnet_id}.{BRIDGE_IP_SUFFIX}"
        network_name = f"{NETWORK_PREFIX}{name}"

        network_created = False
        try:
            # Create network
            import docker.types
            ipam_pool = docker.types.IPAMPool(subnet=subnet)
            ipam_config = docker.types.IPAMConfig(pool_configs=[ipam_pool])
            network = self.docker.networks.create(
                network_name, driver="bridge", ipam=ipam_config
            )
            network_created = True
            logger.info("Created network %s (subnet %s)", network_name, subnet)

            # Connect mock container
            if self._mock_container:
                network.connect(self._mock_container, ipv4_address=analyzer_ip)
                logger.info("Connected mock (%s) to %s at %s",
                            self._mock_container, network_name, analyzer_ip)

            # Connect bridge container
            if self._bridge_container:
                network.connect(self._bridge_container, ipv4_address=bridge_ip)
                logger.info("Connected bridge (%s) to %s at %s",
                            self._bridge_container, network_name, bridge_ip)

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
            if network_created:
                self._cleanup_network(network_name)
            raise

    def remove_analyzer(self, name: str) -> bool:
        """Remove an analyzer's Docker network.

        Returns True if removed, False if not found.
        """
        if name not in self._analyzers:
            return False

        info = self._analyzers.pop(name)
        network_name = info["network"]
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
