"""PR #211 R18: unconditional mandatory prefetch; dispatch fail-closed proof."""

import importlib.util
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


def _load_task_module():
    import importlib.machinery
    path = str(Path(__file__).resolve().parent.parent / "bin" / "herdr-task")
    loader = importlib.machinery.SourceFileLoader("herdr_task_cli", path)
    spec = importlib.util.spec_from_loader("herdr_task_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_prefetch_mandatory_offline_raises():
    mod = _load_task_module()
    with patch.dict("os.environ", {"HERDR_HUB_URL": "http://127.0.0.1:59999",
                                   "HERDR_HUB_TOKEN": "x"}):
        import pytest as _pt
        with _pt.raises(RuntimeError):
            mod._prefetch_mandatory_rules(
                {"task_id": "t", "customer": "acme", "task_type": "quote",
                 "mandatory_experience_rules": True})


def test_prefetch_mandatory_online_returns_block():
    from herdr.experience.hub_server import ExperienceHubServer
    from herdr.experience.client import ExperienceClient
    from herdr.experience.models import BusinessRuleExperience, AppliesTo, ExperienceSource
    mod = _load_task_module()
    with tempfile.TemporaryDirectory() as td:
        srv = ExperienceHubServer(host="127.0.0.1", port=0, db_path=Path(td) / "h.db")
        srv.storage.register_token("adm", user_id="a", role="admin", allowed_customers=["*"])
        srv.start(block=False)
        time.sleep(0.05)
        try:
            admin = ExperienceClient(base_url=f"http://127.0.0.1:{srv.port}", token="adm")
            exp = BusinessRuleExperience(
                experience_id="r18-1", title="t", content="VIP customer orders over threshold get priority handling always",
                contributor="a", applies_to=AppliesTo(customer="acme", task_type="quote"),
                source=ExperienceSource(task_id="t", run_id="r"))
            assert admin.submit_candidate(exp).get("ok") is True
            assert admin.review_candidate("r18-1", action="approve").get("status") == "published"
            with patch.dict("os.environ", {"HERDR_HUB_URL": f"http://127.0.0.1:{srv.port}",
                                           "HERDR_HUB_TOKEN": "adm"}):
                block = mod._prefetch_mandatory_rules(
                    {"task_id": "t", "customer": "acme", "task_type": "quote",
                     "mandatory_experience_rules": True})
            assert "r18-1" in block
        finally:
            srv.stop()


def test_dispatch_mandatory_offline_keeps_pending(tmp_path):
    import os
    from herdr import state_db
    mod = _load_task_module()
    db = tmp_path / "s.db"
    state_db.init_db(db)
    state_db.save_task({"task_id": "t-r18", "run_id": "r-r18", "workflow_id": "wf",
                        "customer": "acme", "business_task_type": "quote",
                        "mandatory_experience_rules": True, "status": "pending",
                        "goal": "g", "clone_path": str(tmp_path)}, db_path=db)
    env = {"HERDR_STATE_DB": str(db), "HERDR_HUB_URL": "http://127.0.0.1:59999",
           "HERDR_HUB_TOKEN": "x"}
    with patch.dict(os.environ, env):
        import pytest as _pt
        with _pt.raises(RuntimeError):
            mod.dispatch_task("t-r18", "do work")
    assert state_db.get_task("t-r18", db_path=db)["status"] == "pending"
