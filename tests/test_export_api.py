"""Integration tests for POST /api/jobs/{job_id}/export-code endpoint."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main
from app.models import JobStatus
from app.orchestrator import JobOrchestrator
from app.storage import JobStorage


@pytest.fixture
def isolated_client(tmp_path: Path):
    """Provide a TestClient backed by an isolated temporary JobStorage and JobOrchestrator."""
    custom_storage = JobStorage(base_dir=str(tmp_path / "jobs"))
    custom_orchestrator = JobOrchestrator(storage=custom_storage)
    orig_storage = app.main.storage
    orig_orchestrator = app.main.orchestrator
    app.main.storage = custom_storage
    app.main.orchestrator = custom_orchestrator
    try:
        yield TestClient(app.main.app), custom_orchestrator
    finally:
        app.main.storage = orig_storage
        app.main.orchestrator = orig_orchestrator


def _upload_sample_job(client: TestClient, headers: dict[str, str] | None = None) -> str:
    csv_content = (
        "id,name,age,signup_date\n"
        "usr_1,  Alice  ,25,2025-01-10\n"
        "usr_2,Bob,N/A,2025-01-11\n"
        "usr_2,Bob,N/A,2025-01-11\n"
        "usr_3,Charlie,-5,2025-01-12\n"
    )
    res = client.post(
        "/api/jobs/upload",
        files={"file": ("users.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")},
        data={"privacy_mode": "false"},
        headers=headers or {},
    )
    assert res.status_code == 200
    return res.json()["id"]


def _complete_sample_job(client: TestClient, headers: dict[str, str] | None = None) -> str:
    job_id = _upload_sample_job(client, headers=headers)
    clean_res = client.post(f"/api/jobs/{job_id}/clean", headers=headers or {})
    assert clean_res.status_code == 200
    detail_res = client.get(f"/api/jobs/{job_id}", headers=headers or {})
    assert detail_res.status_code == 200
    assert detail_res.json()["job"]["status"] in ("DONE", "DONE_WITH_WARNINGS")
    return job_id


def test_export_code_json_all_targets(isolated_client):
    client, _ = isolated_client
    headers = {"X-User-Id": "export_user"}
    job_id = _complete_sample_job(client, headers=headers)

    expected_languages = {
        "sql": {"sql"},
        "dbt": {"sql", "yaml"},
        "pandas": {"python"},
        "polars": {"python"},
    }

    for target, langs in expected_languages.items():
        body = {"target": target, "dialect": "snowflake", "include_tests": True, "source_name": "raw.users"}
        res = client.post(f"/api/jobs/{job_id}/export-code", json=body, headers=headers)
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["target"] == target
        assert isinstance(data["warnings"], list)
        assert len(data["files"]) >= (2 if target == "dbt" else 1)
        for exported_file in data["files"]:
            assert exported_file["name"]
            assert exported_file["content"].strip()
            assert exported_file["language"] in langs


def test_export_code_zip_download(isolated_client):
    client, _ = isolated_client
    headers = {"X-User-Id": "zip_user"}
    job_id = _complete_sample_job(client, headers=headers)

    res = client.post(
        f"/api/jobs/{job_id}/export-code?download=zip",
        json={"target": "dbt", "dialect": "postgres", "include_tests": True},
        headers=headers,
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/zip")
    assert 'filename="datamorph_export_dbt.zip"' in res.headers.get("content-disposition", "")

    with zipfile.ZipFile(io.BytesIO(res.content), "r") as zf:
        names = zf.namelist()
        assert any(name.endswith(".sql") for name in names)
        assert any(name == "schema.yml" or name.endswith("/schema.yml") for name in names)
        for name in names:
            assert zf.read(name).decode("utf-8").strip()


def test_export_code_cross_tenant_and_validation_isolation(isolated_client):
    client, _ = isolated_client
    job_id = _complete_sample_job(client, headers={"X-User-Id": "tenant_alice"})

    # Cross-tenant request must return 404 (IDOR protection)
    res_other = client.post(
        f"/api/jobs/{job_id}/export-code",
        json={"target": "sql"},
        headers={"X-User-Id": "tenant_bob"},
    )
    assert res_other.status_code == 404

    # Non-existent job must return 404
    res_missing = client.post(
        "/api/jobs/nonexistent_job_123/export-code",
        json={"target": "sql"},
        headers={"X-User-Id": "tenant_alice"},
    )
    assert res_missing.status_code == 404

    # Unsafe job_id or tenant ID returns 400
    res_bad_job = client.post(
        "/api/jobs/bad$id/export-code",
        json={"target": "sql"},
        headers={"X-User-Id": "tenant_alice"},
    )
    assert res_bad_job.status_code == 400

    res_bad_tenant = client.post(
        f"/api/jobs/{job_id}/export-code",
        json={"target": "sql"},
        headers={"X-User-Id": "../../evil"},
    )
    assert res_bad_tenant.status_code == 400


def test_export_code_requires_completed_status_and_recipe(isolated_client):
    client, orch = isolated_client
    headers = {"X-User-Id": "status_user"}

    # AWAITING_APPROVAL (not yet cleaned) -> 409 Conflict
    uncleaned_job_id = _upload_sample_job(client, headers=headers)
    res_uncleaned = client.post(
        f"/api/jobs/{uncleaned_job_id}/export-code",
        json={"target": "sql"},
        headers=headers,
    )
    assert res_uncleaned.status_code == 409

    # FAILED status -> 409 Conflict
    job = orch.get_job(uncleaned_job_id, user_id="status_user")
    assert job is not None
    job.status = JobStatus.FAILED
    res_failed = client.post(
        f"/api/jobs/{uncleaned_job_id}/export-code",
        json={"target": "pandas"},
        headers=headers,
    )
    assert res_failed.status_code == 409

    # DONE without a recipe -> 409 Conflict
    done_job_id = _complete_sample_job(client, headers=headers)
    orch.job_recipes.pop(done_job_id, None)
    res_no_recipe = client.post(
        f"/api/jobs/{done_job_id}/export-code",
        json={"target": "polars"},
        headers=headers,
    )
    assert res_no_recipe.status_code == 409


def test_export_code_invalid_payload_returns_422(isolated_client):
    client, _ = isolated_client
    headers = {"X-User-Id": "validation_user"}
    job_id = _complete_sample_job(client, headers=headers)

    invalid_payloads = [
        {},
        {"target": "spark"},
        {"target": "sql", "dialect": "oracle"},
        {"target": "sql", "unexpected_field": True},
    ]
    for payload in invalid_payloads:
        res = client.post(f"/api/jobs/{job_id}/export-code", json=payload, headers=headers)
        assert res.status_code == 422, f"Expected 422 for payload {payload}, got {res.status_code}"

    res_bad_download = client.post(
        f"/api/jobs/{job_id}/export-code?download=tar",
        json={"target": "sql"},
        headers=headers,
    )
    assert res_bad_download.status_code == 422


def test_export_code_warns_on_post_clean_wrangle_history(isolated_client):
    client, _ = isolated_client
    headers = {"X-User-Id": "wrangle_user"}
    job_id = _complete_sample_job(client, headers=headers)

    wrangle_res = client.post(
        f"/api/jobs/{job_id}/wrangle",
        json={"action": "change_casing", "column": "name", "params": {"case": "upper"}},
        headers=headers,
    )
    assert wrangle_res.status_code == 200

    export_res = client.post(
        f"/api/jobs/{job_id}/export-code",
        json={"target": "sql"},
        headers=headers,
    )
    assert export_res.status_code == 200
    warnings = export_res.json()["warnings"]
    assert any("post-clean Data Wrangler" in w for w in warnings)
