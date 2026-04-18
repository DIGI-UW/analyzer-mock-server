"""
Unit tests for AnalyzerNetworkManager._select_subnet_id.

Docker is not required — the docker client is mocked.
"""

import unittest
from unittest.mock import MagicMock, patch

from analyzer_network_manager import AnalyzerNetworkManager, FIXED_SUBNETS


class TestSelectSubnetId(unittest.TestCase):
    def setUp(self):
        # docker is lazy-imported inside AnalyzerNetworkManager.docker property;
        # set _docker directly to avoid needing the docker SDK installed.
        self.mgr = AnalyzerNetworkManager()
        self.mock_docker = MagicMock()
        self.mock_docker.networks.list.return_value = []
        self.mgr._docker = self.mock_docker

    def test_exact_match_returns_fixed_subnet(self):
        """An analyzer named exactly 'genexpert' gets the fixed subnet."""
        self.assertIn("genexpert", FIXED_SUBNETS)
        self.assertEqual(
            self.mgr._select_subnet_id("genexpert"), FIXED_SUBNETS["genexpert"]
        )

    def test_substring_does_not_claim_fixed_subnet(self):
        """Previously, any name *containing* a FIXED_SUBNETS key matched —
        so 'demo-genexpert-site1' and 'demo-genexpert-site2' both claimed
        subnet 20 and the second `docker network create` hit a Pool overlap.
        The fix requires exact match, so these fall through to the dynamic
        range."""
        a = self.mgr._select_subnet_id("demo-genexpert-site1")
        b = self.mgr._select_subnet_id("demo-genexpert-site2")
        self.assertNotEqual(a, FIXED_SUBNETS["genexpert"])
        self.assertNotEqual(b, FIXED_SUBNETS["genexpert"])
        self.assertNotEqual(a, b, "dynamic subnets must be distinct")

    def test_case_insensitive_exact_match(self):
        """Mixed case still resolves to the fixed subnet."""
        self.assertEqual(
            self.mgr._select_subnet_id("GeneXpert"), FIXED_SUBNETS["genexpert"]
        )

    def test_fixed_subnet_in_use_falls_through_to_dynamic(self):
        """If a second instance of the same template name is created while the
        fixed subnet is still occupied, fall through to dynamic allocation
        instead of colliding."""
        fixed = FIXED_SUBNETS["bc5380"]
        # Simulate the fixed subnet already allocated
        net = MagicMock()
        net.attrs = {"IPAM": {"Config": [{"Subnet": f"10.42.{fixed}.0/24"}]}}
        self.mock_docker.networks.list.return_value = [net]

        got = self.mgr._select_subnet_id("bc5380")
        self.assertNotEqual(got, fixed)


if __name__ == "__main__":
    unittest.main()
