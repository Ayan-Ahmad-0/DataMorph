"""Architectural and API modularity verification tests."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import app.main
from app.storage import JobStorage


def test_app_main_line_budget_and_router_registration():
    """app/main.py remains a slim wiring entrypoint while exposing all domain routes."""
    main_path = Path("app/main.py")
    line_count = len(main_path.read_text(encoding="utf-8").splitlines())
    assert line_count < 250, f"Expected slim app/main.py (<250 lines), found {line_count}"

    route_paths = set(app.main.app.openapi()["paths"].keys())
    expected_routes = {
        "/api/health",
        "/api/maintenance/cleanup",
        "/api/maintenance/storage",
        "/api/formats/detect",
        "/api/convert",
        "/api/convert/execute",
        "/api/jobs/upload",
        "/api/jobs/{job_id}/status",
        "/api/jobs/{job_id}",
        "/api/explore/{job_id}/schema",
        "/api/explore/{job_id}/query",
        "/api/charts/{job_id}/stats",
        "/api/charts/{job_id}/build",
        "/api/jobs/{job_id}/pii/detect",
        "/api/jobs/{job_id}/wrangle",
        "/api/modeling/sessions",
        "/api/connectors",
    }
    missing = expected_routes - route_paths
    assert not missing, f"Missing expected domain routes on app.main.app: {missing}"


def test_extracted_routers_honor_runtime_storage_monkeypatch(tmp_path: Path):
    """Routers resolve singletons via app.dependencies so app.main.storage patching still works."""
    custom_storage = JobStorage(base_dir=str(tmp_path / "custom_jobs"))
    original_storage = app.main.storage
    app.main.storage = custom_storage
    try:
        client = TestClient(app.main.app)
        resp = client.get("/api/explore/nonexistent_job/schema", headers={"X-User-Id": "arch_test"})
        assert resp.status_code == 404
    finally:
        app.main.storage = original_storage
