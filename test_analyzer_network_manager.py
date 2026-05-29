"""
Unit tests for AnalyzerNetworkManager._select_subnet_id.

Docker is not required — the docker client is mocked.
"""

import unittest
from unittest.mock import MagicMock, patch

from analyzer_network_manager import (
    DYNAMIC_SUBNET_BASE,
    FIXED_SUBNETS,
    AnalyzerNetworkManager,
)


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

    def test_subnet_in_use_handles_explicit_null_ipam_config(self):
        # Docker can return networks whose IPAM.Config is explicitly null
        # (not just missing) — for example, networks created without an
        # address pool. _subnet_in_use must treat that as an empty config
        # and not raise "'NoneType' object is not iterable".
        net = MagicMock()
        net.attrs = {"IPAM": {"Config": None}}
        self.mock_docker.networks.list.return_value = [net]

        # Should not raise; should not falsely report any subnet in use.
        for subnet_id in (FIXED_SUBNETS["bc5380"], 99):
            self.assertFalse(self.mgr._subnet_in_use(subnet_id))


class TestCreateAnalyzerOverlapRetry(unittest.TestCase):
    """create_analyzer must survive Docker 'Pool overlaps' on the chosen /24.

    A concurrently-created network (two demo analyzers provisioning at once)
    can take the subnet this manager picked, so `docker network create` fails
    with a 403 'Pool overlaps' that is NOT a name conflict. Before the retry,
    that propagated and the analyzer came back with no IP — the 'ip=missing'
    harness failure. create_analyzer now advances to the next free subnet and
    retries.
    """

    def setUp(self):
        self.mgr = AnalyzerNetworkManager()
        self.mock_docker = MagicMock()
        self.mock_docker.networks.list.return_value = []
        self.mgr._docker = self.mock_docker
        # Only attempt container connects when these are set — keep empty so the
        # test exercises subnet allocation without real Docker connect calls.
        self.mgr._mock_container = ""
        self.mgr._bridge_container = ""

    def test_create_retries_on_pool_overlap(self):
        good_net = MagicMock()
        self.mock_docker.networks.create.side_effect = [
            Exception(
                "403 Client Error: invalid pool request: Pool overlaps with"
                " other one on this address space"
            ),
            good_net,
        ]
        fake_types = MagicMock()
        with patch.dict(
            "sys.modules",
            {"docker": MagicMock(types=fake_types), "docker.types": fake_types},
        ):
            result = self.mgr.create_analyzer("demo-outbound-bc5380", "mindray_bc5380", port=5380)

        # Retried past the overlap and returned a usable analyzer IP.
        self.assertEqual(self.mock_docker.networks.create.call_count, 2)
        self.assertTrue(result["ip"].startswith("10.42."))
        self.assertTrue(result["ip"].endswith(".10"))
        # The second (successful) subnet differs from the first (overlapped) one.
        self.assertEqual(result["ip"], f"10.42.{DYNAMIC_SUBNET_BASE + 1}.10")

    def test_orphan_name_conflict_is_removed_and_recreated_not_reused(self):
        # A leftover network with the same name (orphan from a previous run) may
        # have a different subnet than the one we're allocating. Reusing it would
        # connect containers at IPs outside its subnet ("invalid endpoint
        # settings: no configured subnet ... contain the IP"). create_analyzer
        # must REMOVE the orphan and recreate fresh, so the returned IP is in the
        # network that actually got created.
        good_net = MagicMock()
        self.mock_docker.networks.create.side_effect = [
            Exception("409 Client Error: Conflict — network with name ... already exists"),
            good_net,
        ]
        orphan = MagicMock()
        orphan.attrs = {"Containers": {}}
        self.mock_docker.networks.get.return_value = orphan

        fake_types = MagicMock()
        with patch.dict(
            "sys.modules",
            {"docker": MagicMock(types=fake_types), "docker.types": fake_types},
        ):
            result = self.mgr.create_analyzer("demo-outbound-bc5380", "mindray_bc5380", port=5380)

        # Recreated, not reused: two create attempts, orphan removed exactly once.
        self.assertEqual(self.mock_docker.networks.create.call_count, 2)
        orphan.remove.assert_called_once()
        # IP is in the subnet that was actually created (dynamic base; nothing else in use).
        self.assertEqual(result["subnet"], f"10.42.{DYNAMIC_SUBNET_BASE}.0/24")
        self.assertEqual(result["ip"], f"10.42.{DYNAMIC_SUBNET_BASE}.10")

    def test_connect_failure_rolls_back_created_network(self):
        # If wiring the network up fails after create, the half-created network
        # must be removed — otherwise it becomes the orphan that breaks the next
        # run's name-conflict path.
        self.mgr._mock_container = "mock-ctr"
        good_net = MagicMock()
        good_net.connect.side_effect = Exception("boom: cannot connect endpoint")
        self.mock_docker.networks.create.return_value = good_net
        rollback_net = MagicMock()
        rollback_net.attrs = {"Containers": {}}
        self.mock_docker.networks.get.return_value = rollback_net

        fake_types = MagicMock()
        with patch.dict(
            "sys.modules",
            {"docker": MagicMock(types=fake_types), "docker.types": fake_types},
        ):
            with self.assertRaises(Exception):
                self.mgr.create_analyzer("demo-x", "some_template", port=0)

        # Rolled back: _cleanup_network removed the network we'd created.
        rollback_net.remove.assert_called_once()

    def test_concurrent_creates_serialize_to_distinct_subnets(self):
        # The mock's HTTP server is threaded, so create_analyzer can be called
        # concurrently. Without serialization the subnet allocator races and two
        # analyzers get the same /24 (the "ip=missing"/overlap failures). The
        # @_synchronized lock must make concurrent creates yield DISTINCT subnets
        # and all succeed. A small delay in the mocked create widens the race
        # window so an unlocked allocator would reliably collide.
        import threading
        import time

        def slow_create(*args, **kwargs):
            time.sleep(0.02)
            return MagicMock()

        self.mock_docker.networks.create.side_effect = slow_create
        fake_types = MagicMock()
        results = {}
        errors = []

        def worker(i):
            try:
                results[i] = self.mgr.create_analyzer(f"demo-conc-{i}", "tmpl", port=0)["subnet"]
            except Exception as e:  # noqa: BLE001 — surface in assertion
                errors.append(repr(e))

        with patch.dict(
            "sys.modules",
            {"docker": MagicMock(types=fake_types), "docker.types": fake_types},
        ):
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(errors, [], f"concurrent creates errored: {errors}")
        self.assertEqual(
            len(set(results.values())), 5, f"subnets collided under concurrency: {results}"
        )


if __name__ == "__main__":
    unittest.main()
