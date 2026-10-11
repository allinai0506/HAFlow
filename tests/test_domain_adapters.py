import os
import tempfile
import unittest
from pathlib import Path

from herdr.domain import (
    BaseDomainAdapter,
    GenericDomainAdapter,
    SoftwareDomainAdapter,
    get_domain_adapter,
    list_domain_adapters,
    register_domain_adapter,
)


class TestDomainAdapters(unittest.TestCase):
    def test_generic_adapter_is_neutral_and_zero_side_effect(self):
        adapter = GenericDomainAdapter()
        self.assertEqual(adapter.name, "generic")

        with tempfile.TemporaryDirectory() as td:
            clone = Path(td)
            res = adapter.on_task_init(clone, {"task_id": "t-1", "task_type": "bid"})
            self.assertEqual(res, {"scaffolded": False})
            self.assertEqual(len(list(clone.iterdir())), 0)

            contract = {
                "version": 1,
                "allowed_paths": ["proposals/*"],
                "required_files": [],
            }
            res_contract = adapter.prepare_delivery_contract(contract, {"task_id": "t-1", "task_type": "bid"})
            self.assertEqual(res_contract, contract)

            diff_res = adapter.evaluate_differential_tests("test output")
            self.assertEqual(diff_res.get("verdict"), "pass")
            self.assertEqual(diff_res.get("domain"), "generic")

            self.assertFalse(adapter.should_allow_rebase({"task_id": "t-1"}))
            self.assertTrue(adapter.should_allow_rebase({"task_id": "t-1"}, explicit_flag=True))

    def test_software_adapter_hooks(self):
        adapter = SoftwareDomainAdapter()
        self.assertEqual(adapter.name, "software")

        with tempfile.TemporaryDirectory() as td:
            clone = Path(td)
            (clone / "package.json").write_text("{}", encoding="utf-8")
            (clone / ".husky").mkdir()

            res = adapter.on_task_init(clone, {
                "task_id": "fix-100",
                "task_type": "fix",
                "branch": "fix/auth",
                "agent": "opencode",
            })
            self.assertTrue(res.get("scaffolded"))
            self.assertTrue((clone / res["path"]).exists())

            contract = {
                "version": 1,
                "allowed_paths": ["src/*"],
                "required_files": [],
            }
            updated = adapter.prepare_delivery_contract(contract, {
                "task_id": "fix-100",
                "task_type": "fix",
                "branch": "fix/auth",
            })
            self.assertIsNotNone(updated)
            self.assertTrue(any("docs/bug-reports" in p for p in updated["allowed_paths"]))

    def test_software_adapter_differential_testing(self):
        adapter = SoftwareDomainAdapter()
        cand_out = """
=========================== short test summary info ============================
FAILED tests/test_a.py::test_legacy - AssertionError: expected True
"""
        base_snap = {"failing_tests": ["tests/test_a.py::test_legacy"]}

        diff = adapter.evaluate_differential_tests(cand_out, base_snapshot=base_snap, candidate_exit_code=1)
        self.assertEqual(diff.get("verdict"), "pass")
        self.assertTrue(diff.get("pre_existing_ignored"))
        self.assertEqual(diff.get("domain"), "software")

    def test_get_domain_adapter_resolution(self):
        # 1. Explicit domain
        self.assertIsInstance(get_domain_adapter(domain="software"), SoftwareDomainAdapter)
        self.assertIsInstance(get_domain_adapter(domain="generic"), GenericDomainAdapter)
        self.assertIsInstance(get_domain_adapter(domain="bid"), GenericDomainAdapter)
        self.assertIsInstance(get_domain_adapter(domain="legal"), GenericDomainAdapter)

        # 2. Task inference
        self.assertIsInstance(get_domain_adapter(task_type="code"), SoftwareDomainAdapter)
        self.assertIsInstance(get_domain_adapter(task_type="bugfix"), SoftwareDomainAdapter)
        self.assertIsInstance(get_domain_adapter(task_type="docs"), GenericDomainAdapter)
        self.assertIsInstance(get_domain_adapter(task_type="tender"), GenericDomainAdapter)

        # 3. Template inference
        self.assertIsInstance(get_domain_adapter(template_name="software-development-v1"), SoftwareDomainAdapter)
        self.assertIsInstance(get_domain_adapter(template_name="tender-procurement-v1"), GenericDomainAdapter)

        # 4. Fallback default
        self.assertIsInstance(get_domain_adapter(), GenericDomainAdapter)

    def test_custom_domain_registration(self):
        class LegalDomainAdapter(BaseDomainAdapter):
            @property
            def name(self):
                return "legal_specialized"

        register_domain_adapter("legal_custom", LegalDomainAdapter)
        ad = get_domain_adapter(domain="legal_custom")
        self.assertIsInstance(ad, LegalDomainAdapter)
        self.assertEqual(ad.name, "legal_specialized")


if __name__ == "__main__":
    unittest.main()
