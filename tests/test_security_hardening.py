"""Security & production hardening regression tests."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import UploadTooLargeError, copy_stream_to_path
from app.main import app
from app.storage import JobStorage
from charts.dashboard_export import generate_dashboard_html
from connectors.postgres import PostgresConnector
from connectors.s3 import S3Connector
from core.rate_limit import RateLimitMiddleware, SlidingWindowRateLimiter
from core.security import escape_sql_literal, sanitize_filename, validate_external_host, validate_safe_id


def test_sanitize_filename_strips_traversal_and_null_bytes():
    assert sanitize_filename("../../app/main.py") == "main.py"
    assert sanitize_filename("..\\..\\windows\\system32\\evil.csv") == "evil.csv"
    assert sanitize_filename("bad\x00name/../../secret.parquet") == "secret.parquet"
    assert sanitize_filename("..") == "dataset.csv"
    assert sanitize_filename("") == "dataset.csv"


def test_validate_safe_id_rejects_traversal():
    assert validate_safe_id("job_1234abcd") == "job_1234abcd"
    for bad_id in ("../secret", "job/../../etc", "bad id", "", "a" * 65):
        with pytest.raises(Exception) as exc:
            validate_safe_id(bad_id, label="job_id")
        assert getattr(exc.value, "status_code", 400) == 400


def test_storage_prevents_job_and_filename_traversal(tmp_path: Path):
    storage = JobStorage(base_dir=str(tmp_path / "jobs"))
    raw_path = storage.raw_file_path("job_safe1", "../../outside.csv", user_id="tenant_a")
    assert raw_path.resolve().is_relative_to(storage.base_dir.resolve())
    assert raw_path.name == "outside.csv"

    with pytest.raises(Exception):
        storage.job_dir("../escape", user_id="tenant_a")


def test_copy_stream_to_path_enforces_max_bytes_and_cleans_up(tmp_path: Path):
    dest = tmp_path / "oversized.csv"
    payload = io.BytesIO(b"a" * 2048)

    with pytest.raises(UploadTooLargeError) as exc:
        copy_stream_to_path(payload, dest, max_bytes=1024)

    assert exc.value.status_code == 413
    assert not dest.exists()


def test_ssrf_blocks_cloud_metadata_and_private_networks(monkeypatch: pytest.MonkeyPatch):
    # Cloud metadata is unconditionally blocked regardless of dev/prod setting
    monkeypatch.setenv("CONNECTOR_ALLOW_PRIVATE_NETWORKS", "true")
    for meta in ("169.254.169.254", "http://169.254.169.254/latest/meta-data/", "metadata.google.internal"):
        with pytest.raises(ValueError, match="metadata|link-local"):
            validate_external_host(meta)

    # Private LAN and localhost blocked when CONNECTOR_ALLOW_PRIVATE_NETWORKS=false
    monkeypatch.setenv("CONNECTOR_ALLOW_PRIVATE_NETWORKS", "false")
    for private_target in ("localhost", "127.0.0.1", "10.0.0.5", "192.168.1.10", "http://data-cleaner:8000"):
        with pytest.raises(ValueError, match="disabled"):
            validate_external_host(private_target)


def test_s3_connector_escapes_sql_and_blocks_server_env_creds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "SERVER_SECRET_KEY_ID")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SERVER_TOP_SECRET")
    monkeypatch.setenv("ALLOW_SERVER_CONNECTOR_ENV_CREDS", "false")

    connector = S3Connector()
    mock_con = MagicMock()
    mock_con.execute.return_value.fetchone.return_value = [1]

    source_pq = tmp_path / "sample.parquet"
    source_pq.write_bytes(b"pq")

    with patch("duckdb.connect", return_value=mock_con):
        connector.export_dataset(
            source_pq,
            config={
                "bucket": "analytics-bucket",
                "endpoint_url": "https://r2.example.com",
                "access_key_id": "abc'; DROP TABLE x; --",
            },
        )

    executed_sqls = "\n".join(call[0][0] for call in mock_con.execute.call_args_list if call[0])
    assert "SERVER_SECRET_KEY_ID" not in executed_sqls
    assert "SERVER_TOP_SECRET" not in executed_sqls
    assert "abc''; DROP TABLE x; --" in executed_sqls


def test_postgres_connector_rejects_malicious_import_query(tmp_path: Path):
    connector = PostgresConnector()
    target_pq = tmp_path / "out.parquet"

    for bad_query in (
        "SELECT * FROM read_parquet('/etc/passwd')",
        "DROP TABLE users",
        "SELECT 1; DROP TABLE users",
    ):
        with pytest.raises(ValueError):
            connector.import_dataset(
                config={
                    "host": "db.example.com",
                    "database": "app",
                    "username": "readonly",
                    "password": "pw",
                    "query": bad_query,
                },
                target_parquet_path=target_pq,
            )


def test_maintenance_endpoints_require_admin_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_KEY", "super-secret-admin-key")
    client = TestClient(app)

    res_missing = client.get("/api/maintenance/storage")
    assert res_missing.status_code == 403

    res_wrong = client.post("/api/maintenance/cleanup", headers={"X-Admin-Key": "wrong"})
    assert res_wrong.status_code == 403

    res_ok = client.get("/api/maintenance/storage", headers={"X-Admin-Key": "super-secret-admin-key"})
    assert res_ok.status_code == 200
    assert "retention_policy" in res_ok.json()


def test_security_headers_present_on_api_responses():
    client = TestClient(app)
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.headers.get("X-Content-Type-Options") == "nosniff"
    assert res.headers.get("X-Frame-Options") == "DENY"
    assert res.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"


def test_dashboard_html_export_escapes_xss_payloads():
    xss_title = 'Q1 Report <img src=x onerror="alert(1)">'
    html = generate_dashboard_html(
        title=xss_title,
        dataset_name="bad<script>alert(2)</script>.csv",
        cards_data=[
            {
                "title": "Revenue",
                "spec": {"chart_type": "big_number", "title": "<svg onload=alert(3)>"},
                "data": {"columns": ["total"], "rows": [["<script>evil()</script>"]]},
            }
        ],
    )
    assert '<img src=x onerror="alert(1)">' not in html
    assert "<script>alert(2)</script>" not in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html
    assert "chartDiv.innerHTML =" not in html


def test_rate_limit_middleware_returns_429_and_retry_after():
    mini_app = FastAPI()
    mini_app.add_middleware(
        RateLimitMiddleware,
        limiter=SlidingWindowRateLimiter(),
        upload_limit=2,
        ai_connector_limit=2,
        mutation_limit=5,
        window_seconds=60,
    )

    @mini_app.post("/api/jobs/upload")
    def _upload():
        return {"ok": True}

    @mini_app.get("/api/health")
    def _health():
        return {"ok": True}

    client = TestClient(mini_app)
    assert client.post("/api/jobs/upload").status_code == 200
    assert client.post("/api/jobs/upload").status_code == 200
    limited = client.post("/api/jobs/upload")
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert client.get("/api/health").status_code == 200


def test_escape_sql_literal_neutralizes_quotes_and_nul():
    assert escape_sql_literal("o'malley\x00.csv") == "o''malley.csv"

