"""Tests for persistent job state rehydration, convert/query durability, and Gemini client resilience."""

from __future__ import annotations

import io
import json
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.convert_store import get_convert_job, save_convert_job_meta
from app.main import app, convert_jobs
from app.models import JobStatus
from app.orchestrator import JobOrchestrator
from app.storage import JobStorage, write_json_atomic
from core.ai.gemini_client import get_gemini_callable


def test_orchestrator_job_survives_restart(tmp_path: Path):
    storage1 = JobStorage(base_dir=str(tmp_path / "jobs"))
    orch1 = JobOrchestrator(storage=storage1)

    job = orch1.create_job(filename="users.csv", file_size=64, user_id="tenant_a")
    raw_path = storage1.raw_file_path(job.id, "users.csv", user_id="tenant_a")
    raw_path.write_text("id,name,age\n1,Alice,25\n1,Alice,25\n2,Bob,30\n", encoding="utf-8")

    orch1.run_profile_and_ai_draft(job.id)
    assert orch1.get_job(job.id, user_id="tenant_a").status == JobStatus.AWAITING_APPROVAL

    # Simulate user editing recipe via PUT /api/jobs/{id}/recipe
    detail_before = orch1.get_job_detail(job.id, user_id="tenant_a")
    assert detail_before is not None
    edited_recipe = dict(detail_before.recipe)
    edited_recipe["global_notes"] = "Persisted user edit note"
    assert orch1.update_recipe(job.id, edited_recipe, user_id="tenant_a") is not None

    # New orchestrator & storage instance simulates full Uvicorn process restart (empty RAM)
    storage2 = JobStorage(base_dir=str(tmp_path / "jobs"))
    orch2 = JobOrchestrator(storage=storage2)
    assert job.id not in orch2.jobs

    rehydrated_detail = orch2.get_job_detail(job.id, user_id="tenant_a")
    assert rehydrated_detail is not None
    assert rehydrated_detail.job.status == JobStatus.AWAITING_APPROVAL
    assert rehydrated_detail.profile is not None
    assert rehydrated_detail.recipe is not None

    # Tenant isolation holds on disk rehydration too
    assert orch2.get_job_detail(job.id, user_id="tenant_b") is None

    # Cleaning & exporting work cleanly on the rehydrated job
    orch2.execute_cleaning_and_verification(job.id, user_id="tenant_a")
    done_detail = orch2.get_job_detail(job.id, user_id="tenant_a")
    assert done_detail is not None
    assert done_detail.job.status in (JobStatus.DONE, JobStatus.DONE_WITH_WARNINGS)

    # Another restart after completion still serves downloads & verification
    orch3 = JobOrchestrator(storage=JobStorage(base_dir=str(tmp_path / "jobs")))
    final_detail = orch3.get_job_detail(job.id, user_id="tenant_a")
    assert final_detail is not None
    assert final_detail.job.status in (JobStatus.DONE, JobStatus.DONE_WITH_WARNINGS)
    assert final_detail.verification is not None
    exports = orch3.export_dataset(job.id, "csv", user_id="tenant_a")
    assert len(exports) == 1 and exports[0].exists()


def test_interrupted_inflight_jobs_recover_gracefully(tmp_path: Path):
    storage = JobStorage(base_dir=str(tmp_path / "jobs"))
    orch1 = JobOrchestrator(storage=storage)

    # Case 1: Interrupted during PROFILING -> marked FAILED with restart notice
    job_prof = orch1.create_job(filename="a.csv", file_size=10, user_id="tenant_x")
    job_prof.status = JobStatus.PROFILING
    orch1._save_job_state(job_prof.id)

    # Case 2: Interrupted during CLEANING with staged.parquet + recipe -> recovers to AWAITING_APPROVAL
    job_clean = orch1.create_job(filename="b.csv", file_size=20, user_id="tenant_x")
    raw_b = storage.raw_file_path(job_clean.id, "b.csv", user_id="tenant_x")
    raw_b.write_text("id,val\n1,10\n2,20\n", encoding="utf-8")
    orch1.run_profile_and_ai_draft(job_clean.id)
    orch1.jobs[job_clean.id].status = JobStatus.CLEANING
    orch1._save_job_state(job_clean.id)

    # Fresh orchestrator simulates server restart
    orch2 = JobOrchestrator(storage=JobStorage(base_dir=str(tmp_path / "jobs")))
    recovered_prof = orch2.get_job(job_prof.id, user_id="tenant_x")
    assert recovered_prof is not None
    assert recovered_prof.status == JobStatus.FAILED
    assert "server restart" in (recovered_prof.error or "").lower()

    recovered_clean = orch2.get_job(job_clean.id, user_id="tenant_x")
    assert recovered_clean is not None
    assert recovered_clean.status == JobStatus.AWAITING_APPROVAL
    assert "interrupted" in recovered_clean.current_stage.lower()


def test_expired_state_json_is_not_rehydrated(tmp_path: Path):
    storage = JobStorage(base_dir=str(tmp_path / "jobs"))
    orch1 = JobOrchestrator(storage=storage)
    job = orch1.create_job(filename="old.csv", file_size=10, user_id="tenant_x")
    job.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    orch1._save_job_state(job.id)

    orch2 = JobOrchestrator(storage=JobStorage(base_dir=str(tmp_path / "jobs")))
    assert orch2.get_job(job.id, user_id="tenant_x") is None


def test_convert_and_query_jobs_survive_ram_eviction():
    client = TestClient(app)
    uid = "tenant_persist_test"
    headers = {"X-User-Id": uid}
    created_dirs: list[Path] = []
    created_clean_dirs: list[Path] = []

    try:
        csv_bytes = b"id,name\n1,Alice\n2,Bob\n"
        res = client.post(
            "/api/convert/execute",
            headers=headers,
            files={"file": ("sample.csv", io.BytesIO(csv_bytes), "text/csv")},
            data={"target_format": "json"},
        )
        assert res.status_code == 200
        convert_id = res.json()["convert_id"]
        created_dirs.append(Path("data/convert_jobs").resolve() / uid / convert_id)

        # Simulate server restart by clearing convert_jobs dictionary in RAM
        convert_jobs.pop(convert_id, None)

        dl_res = client.get(f"/api/convert/download/{convert_id}", headers=headers)
        assert dl_res.status_code == 200

        # Clear RAM again and test handoff-to-clean
        convert_jobs.pop(convert_id, None)
        handoff_res = client.post(f"/api/convert/{convert_id}/handoff-to-clean", headers=headers)
        assert handoff_res.status_code == 200
        clean_id = handoff_res.json()["id"]
        created_clean_dirs.append(Path("data/jobs").resolve() / uid / clean_id)

        # Wrong tenant still gets 404 from disk rehydration
        convert_jobs.pop(convert_id, None)
        forbidden_res = client.get(
            f"/api/convert/download/{convert_id}",
            headers={"X-User-Id": "other_tenant"},
        )
        assert forbidden_res.status_code == 404

        # Query upload also survives RAM eviction
        q_res = client.post(
            "/api/query/upload",
            headers=headers,
            files={"file": ("query.csv", io.BytesIO(csv_bytes), "text/csv")},
        )
        assert q_res.status_code == 200
        query_id = q_res.json()["query_id"]
        created_dirs.append(Path("data/query_jobs").resolve() / uid / query_id)

        convert_jobs.pop(query_id, None)
        q_handoff = client.post(f"/api/convert/{query_id}/handoff-to-clean", headers=headers)
        assert q_handoff.status_code == 200
        created_clean_dirs.append(Path("data/jobs").resolve() / uid / q_handoff.json()["id"])
    finally:
        for d in created_dirs + created_clean_dirs:
            shutil.rmtree(d, ignore_errors=True)
            if d.parent.exists() and not any(d.parent.iterdir()):
                shutil.rmtree(d.parent, ignore_errors=True)


def test_gemini_client_falls_back_on_timeout_without_blocking(monkeypatch):
    monkeypatch.setenv("GEMINI_RECIPE_TIMEOUT_S", "0.15")

    calls = []

    def fake_generate_content(*, model, contents, config):
        calls.append(model)
        if model == "gemini-3.5-flash":
            time.sleep(0.5)
            return MagicMock(text='{"late": true}')
        return MagicMock(text='{"ok": true}')

    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = fake_generate_content

    import core.ai.gemini_client as gc

    gc._circuit_broken_until = 0.0
    gc._circuit_breaker_reason = ""

    with patch("google.genai.Client", return_value=fake_client):
        fn = get_gemini_callable(api_key="test-key", model_name="gemini-3.5-flash")
        assert fn is not None
        t0 = time.perf_counter()
        out = fn("sys", "usr")
        elapsed = time.perf_counter() - t0

    assert out == '{"ok": true}'
    assert calls == ["gemini-3.5-flash", "gemini-flash-latest"]
    # Must return promptly around timeout (0.15s) rather than blocking on first call's 0.5s sleep
    assert elapsed < 0.4
