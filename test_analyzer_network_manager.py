"""
Unit tests for AnalyzerNetworkManager — deterministic, idempotent provisioning.

Docker is mocked; no real daemon required.
"""

import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from analyzer_network_manager import (
    DYNAMIC_SUBNET_BASE,
    DYNAMIC_SUBNET_MAX,
    FIXED_SUBNETS,
    NETWORK_PREFIX,
    AnalyzerNetworkManager,
)


def _net(name="net", subnet=None, containers=None):
    """A fake Docker network with IPAM subnet + container attachments."""
    n = MagicMock()
    n.name = name
    cfg = [{"Subnet": subnet}] if subnet else []
    n.attrs = {"IPAM": {"Config": cfg}, "Containers": containers or {}}
    return n


class Base(unittest.TestCase):
    def setUp(self):
        self.mgr = AnalyzerNetworkManager()
        self.docker = MagicMock()
        self.docker.networks.list.return_value = []
        # Default: no network exists (get raises NotFound-like).
        self.docker.networks.get.side_effect = Exception("404 network not found")
        self.mgr._docker = self.docker
        # Skip real container connects unless a test opts in.
        self.mgr._mock_container = ""
        self.mgr._bridge_container = ""
        # create_analyzer does `import docker.types` — stub it.
        types = MagicMock()
        p = patch.dict(sys.modules, {"docker": MagicMock(types=types), "docker.types": types})
        p.start()
        self.addCleanup(p.stop)


class TestDeterministicAllocation(Base):
    def test_fixed_exact_match(self):
        self.assertEqual(self.mgr._subnet_id_for("genexpert"), FIXED_SUBNETS["genexpert"])

    def test_fixed_case_insensitive(self):
        self.assertEqual(self.mgr._subnet_id_for("GeneXpert"), FIXED_SUBNETS["genexpert"])

    def test_substring_is_not_the_fixed_subnet(self):
        s = self.mgr._subnet_id_for("demo-genexpert-site1")
        self.assertNotEqual(s, FIXED_SUBNETS["genexpert"])
        self.assertTrue(DYNAMIC_SUBNET_BASE <= s <= DYNAMIC_SUBNET_MAX)

    def test_same_name_same_subnet_across_calls_and_instances(self):
        a = self.mgr._subnet_id_for("demo-outbound-gx")
        b = self.mgr._subnet_id_for("demo-outbound-gx")
        c = AnalyzerNetworkManager()._subnet_id_for("demo-outbound-gx")  # fresh instance, no shared state
        self.assertEqual(a, b)
        self.assertEqual(a, c, "deterministic across instances — no mutable counter")

    def test_distinct_demo_names_get_distinct_subnets(self):
        self.assertNotEqual(
            self.mgr._subnet_id_for("demo-outbound-gx"),
            self.mgr._subnet_id_for("demo-outbound-bc5380"),
        )

    def test_subnet_id_of_parses_actual_subnet(self):
        self.assertEqual(AnalyzerNetworkManager._subnet_id_of(_net(subnet="10.42.77.0/24")), 77)
        self.assertIsNone(AnalyzerNetworkManager._subnet_id_of(_net(subnet=None)))


class TestCreateAnalyzer(Base):
    def test_fresh_create_uses_deterministic_subnet(self):
        self.docker.networks.create.return_value = _net()
        r = self.mgr.create_analyzer("demo-outbound-gx", "tmpl", port=9600)
        expected = self.mgr._subnet_id_for("demo-outbound-gx")
        self.assertEqual(r["subnet"], f"10.42.{expected}.0/24")
        self.assertEqual(r["ip"], f"10.42.{expected}.10")
        self.docker.networks.create.assert_called_once()

    def test_idempotent_adopts_existing_subnet_without_recreating(self):
        # The network already exists with a DIFFERENT subnet than the deterministic
        # one (e.g. an orphan, or a prior different allocation). Adopt its ACTUAL
        # subnet so containers connect at in-subnet IPs — and do NOT recreate.
        existing = _net(name=NETWORK_PREFIX + "bc5380", subnet="10.42.99.0/24")
        self.docker.networks.get.side_effect = None
        self.docker.networks.get.return_value = existing
        r = self.mgr.create_analyzer("bc5380", "tmpl", port=5380)
        self.assertEqual(r["subnet"], "10.42.99.0/24")
        self.assertEqual(r["ip"], "10.42.99.10")
        self.docker.networks.create.assert_not_called()

    def test_second_create_same_name_is_noop_returns_same(self):
        created = _net(name=NETWORK_PREFIX + "demo-x", subnet=None)
        self.docker.networks.create.return_value = created
        first = self.mgr.create_analyzer("demo-x", "tmpl")
        # Second call: now the cache has it AND networks.get must find it.
        self.docker.networks.get.side_effect = None
        self.docker.networks.get.return_value = created
        second = self.mgr.create_analyzer("demo-x", "tmpl")
        self.assertEqual(first, second)
        self.docker.networks.create.assert_called_once()  # not created again

    def test_overlap_probes_to_next_free_subnet(self):
        good = _net()
        self.docker.networks.create.side_effect = [
            Exception("403 Client Error: Pool overlaps with other one on this address space"),
            good,
        ]
        r = self.mgr.create_analyzer("demo-x", "tmpl")
        self.assertEqual(self.docker.networks.create.call_count, 2)
        start = self.mgr._subnet_id_for("demo-x")
        # list() returns [] so the next id is free → start + 1
        self.assertEqual(r["subnet"], f"10.42.{start + 1}.0/24")

    def test_connect_failure_rolls_back_only_a_network_we_created(self):
        self.mgr._mock_container = "mock-ctr"
        created = _net(name=NETWORK_PREFIX + "demo-x")
        created.connect.side_effect = Exception("boom: cannot connect")
        self.docker.networks.create.return_value = created
        # get: existence-check raises (None) → create; then cleanup-lookup returns it.
        self.docker.networks.get.side_effect = [Exception("404"), created]
        with self.assertRaises(Exception):
            self.mgr.create_analyzer("demo-x", "tmpl")
        created.remove.assert_called_once()

    def test_connect_failure_does_NOT_remove_an_adopted_network(self):
        # We adopted an existing (possibly live/seeded) network; a connect failure
        # must NOT remove it.
        self.mgr._mock_container = "mock-ctr"
        existing = _net(name=NETWORK_PREFIX + "bc5380", subnet="10.42.21.0/24")
        existing.connect.side_effect = Exception("boom: cannot connect")
        self.docker.networks.get.side_effect = None
        self.docker.networks.get.return_value = existing
        with self.assertRaises(Exception):
            self.mgr.create_analyzer("bc5380", "tmpl")
        existing.remove.assert_not_called()
        self.docker.networks.create.assert_not_called()


class TestReconcileOrphans(Base):
    def test_drains_zero_container_orphans_keeps_live_and_foreign(self):
        orphan = _net(name=NETWORK_PREFIX + "demo-old", subnet="10.42.60.0/24", containers={})
        live = _net(name=NETWORK_PREFIX + "genexpert", subnet="10.42.20.0/24",
                    containers={"c1": {"Name": "openelis-analyzer-mock"}})
        foreign = _net(name="some-other-net", containers={})
        self.docker.networks.list.return_value = [orphan, live, foreign]
        # _cleanup_network looks the network up by name before removing it.
        self.docker.networks.get.side_effect = None
        self.docker.networks.get.return_value = orphan
        removed = self.mgr.reconcile_orphans()
        self.assertEqual(removed, 1, "only the zero-container mock-analyzer orphan")
        orphan.remove.assert_called_once()
        live.remove.assert_not_called()
        foreign.remove.assert_not_called()


class TestConcurrency(Base):
    def test_concurrent_distinct_analyzers_get_distinct_subnets(self):
        # Deterministic per-name allocation means concurrent creates of DIFFERENT
        # analyzers don't contend on any shared counter — each lands on its own
        # name-derived subnet. The delay widens any race window.
        def slow_create(*args, **kwargs):
            time.sleep(0.02)
            return _net()

        self.docker.networks.create.side_effect = slow_create
        results = {}
        errors = []

        def worker(i):
            try:
                results[i] = self.mgr.create_analyzer(f"demo-conc-{i}", "tmpl")["subnet"]
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"concurrent creates errored: {errors}")
        self.assertEqual(len(set(results.values())), 6, f"subnets collided: {results}")


if __name__ == "__main__":
    unittest.main()
