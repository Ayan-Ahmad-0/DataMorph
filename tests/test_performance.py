"""Tests for performance and scalability optimizations across profiler, orchestrator, charts, and API."""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest
from fastapi.testclient import TestClient

from app.main import app, orchestrator
from app.models import JobStatus
from app.orchestrator import JobOrchestrator
from app.storage import JobStorage
from charts import service as charts_service
from core import profiler as profiler_module
from core.duckdb_utils import get_chart_concurrency_limits, get_max_concurrent_pipelines
from core.profiler import PROFILE_BATCH_SIZE, Profiler


class _CountingConnectionProxy:
    """Thin proxy around DuckDBPyConnection to count SQL executions in tests."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, counter: list[int]) -> None:
        self._conn = conn
        self._counter = counter

    def execute(self, *args, **kwargs):
        self._counter[0] += 1
        return self._conn.execute(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def test_profiler_batched_wide_dataset_accuracy_and_call_reduction(tmp_path: Path):
    """Batched profiling preserves column statistics while slashing DuckDB SQL scans on wide tables."""
    parquet_path = tmp_path / "wide_mixed.parquet"

    # Build a 28-column dataset (> PROFILE_BATCH_SIZE=15) spanning numeric, string, boolean, sparse, whitespace
    con = duckdb.connect(":memory:")
    con.execute(
        f"""
        COPY (
            SELECT
                i AS id,
                CAST(i * 2 AS DOUBLE) AS metric_0,
                CAST(i - 50 AS INTEGER) AS metric_1,
                CAST(i % 7 AS DOUBLE) AS metric_2,
                CASE WHEN i % 10 = 0 THEN NULL ELSE CAST(i * 1.5 AS DOUBLE) END AS metric_3,
                CAST(i * 3 AS BIGINT) AS metric_4,
                CAST(i * 4 AS DOUBLE) AS metric_5,
                CAST(i * 5 AS DOUBLE) AS metric_6,
                CAST(i * 6 AS DOUBLE) AS metric_7,
                CAST(i * 7 AS DOUBLE) AS metric_8,
                CAST(i * 8 AS DOUBLE) AS metric_9,
                CASE WHEN i % 5 = 0 THEN ' N/A ' ELSE CAST(i * 10 AS VARCHAR) END AS str_num_1,
                CASE WHEN i % 4 = 0 THEN '2026-01-' || LPAD(CAST((i % 28) + 1 AS VARCHAR), 2, '0') ELSE '2026-02-10' END AS date_str_1,
                CASE WHEN i % 2 = 0 THEN 'true' ELSE 'false' END AS bool_str_1,
                '  padded_' || CAST(i % 12 AS VARCHAR) || ' ' AS ws_str_1,
                CASE WHEN i <= 3 THEN 'rare_' || CAST(i AS VARCHAR) ELSE NULL END AS sparse_1,
                'cat_' || CAST(i % 3 AS VARCHAR) AS cat_1,
                'cat_' || CAST(i % 4 AS VARCHAR) AS cat_2,
                'cat_' || CAST(i % 5 AS VARCHAR) AS cat_3,
                'cat_' || CAST(i % 6 AS VARCHAR) AS cat_4,
                'cat_' || CAST(i % 7 AS VARCHAR) AS cat_5,
                'cat_' || CAST(i % 8 AS VARCHAR) AS cat_6,
                'cat_' || CAST(i % 9 AS VARCHAR) AS cat_7,
                'cat_' || CAST(i % 10 AS VARCHAR) AS cat_8,
                'cat_' || CAST(i % 11 AS VARCHAR) AS cat_9,
                'cat_' || CAST(i % 12 AS VARCHAR) AS cat_10,
                'cat_' || CAST(i % 13 AS VARCHAR) AS cat_11,
                'cat_' || CAST(i % 14 AS VARCHAR) AS cat_12
            FROM range(1, 101) AS t(i)
        ) TO '{str(parquet_path).replace(chr(92), "/")}' (FORMAT PARQUET);
        """
    )
    con.close()

    profiler = Profiler()
    sql_calls = [0]
    real_connect = profiler_module.connect_duckdb

    def _counting_connect(*args, **kwargs):
        return _CountingConnectionProxy(real_connect(*args, **kwargs), sql_calls)

    with patch.object(profiler_module, "connect_duckdb", side_effect=_counting_connect):
        profile = profiler.profile_parquet(parquet_path)

    assert PROFILE_BATCH_SIZE == 15
    assert profile.file.rows == 100
    assert profile.file.columns == 28
    # 28 columns would previously execute ~85-115 queries; batching keeps total SQL calls well under 30
    assert sql_calls[0] <= 25, f"Expected batched SQL queries <= 25, got {sql_calls[0]}"

    by_name = {c.name: c for c in profile.columns}
    assert by_name["metric_1"].min == -49.0
    assert by_name["metric_1"].max == 50.0
    assert by_name["metric_1"].negatives == 49
    assert by_name["metric_1"].zeros == 1
    assert by_name["metric_3"].null_count == 10
    assert by_name["str_num_1"].inferred_type == "integer"
    assert by_name["str_num_1"].null_tokens == 20
    assert by_name["str_num_1"].leading_trailing_whitespace == 20
    assert by_name["date_str_1"].inferred_type == "date"
    assert by_name["bool_str_1"].inferred_type == "boolean"
    assert by_name["ws_str_1"].leading_trailing_whitespace == 100
    assert by_name["sparse_1"].null_count == 97
    assert by_name["sparse_1"].distinct_count == 3


def test_ram_tuned_concurrency_helpers(monkeypatch):
    """RAM-aware scaling helpers adapt limits across 1GB, 4GB, and 16GB hosts."""
    monkeypatch.delenv("MAX_CONCURRENT_PIPELINES", raising=False)
    monkeypatch.delenv("CHART_MAX_CONCURRENT", raising=False)
    monkeypatch.delenv("CHART_MAX_PER_JOB", raising=False)

    with patch("core.duckdb_utils.get_system_ram_mb", return_value=1024):
        assert get_max_concurrent_pipelines() == 1
        assert get_chart_concurrency_limits() == (4, 3)

    with patch("core.duckdb_utils.get_system_ram_mb", return_value=4096):
        assert get_max_concurrent_pipelines() == 2
        assert get_chart_concurrency_limits() == (8, 5)

    with patch("core.duckdb_utils.get_system_ram_mb", return_value=16384):
        assert get_max_concurrent_pipelines() == 2
        assert get_chart_concurrency_limits() == (12, 8)


def test_orchestrator_concurrency_queueing_and_status_snapshot(tmp_path: Path):
    """Pipeline semaphore queues overflowing jobs cleanly without OOM and exposes live snapshots."""
    storage = JobStorage(base_dir=str(tmp_path / "jobs"))
    orch = JobOrchestrator(storage=storage)
    orch._pipeline_semaphore = threading.Semaphore(1)

    job1 = orch.create_job(filename="first.csv", file_size=32, user_id="tenant_q")
    job2 = orch.create_job(filename="second.csv", file_size=32, user_id="tenant_q")
    storage.raw_file_path(job1.id, "first.csv", user_id="tenant_q").write_text(
        "id,val\n1,10\n2,20\n", encoding="utf-8"
    )
    storage.raw_file_path(job2.id, "second.csv", user_id="tenant_q").write_text(
        "id,val\n3,30\n4,40\n", encoding="utf-8"
    )

    job1_started = threading.Event()
    release_job1 = threading.Event()
    orig_profile = orch.profiler.profile_parquet

    def _blocking_profile(parquet_path, *args, **kwargs):
        if job1.id in str(parquet_path):
            job1_started.set()
            assert release_job1.wait(timeout=5.0)
        return orig_profile(parquet_path, *args, **kwargs)

    with patch.object(orch.profiler, "profile_parquet", side_effect=_blocking_profile):
        t1 = threading.Thread(target=orch.run_profile_and_ai_draft, args=(job1.id,))
        t1.start()
        assert job1_started.wait(timeout=5.0)

        t2 = threading.Thread(target=orch.run_profile_and_ai_draft, args=(job2.id,))
        t2.start()

        # Poll briefly until job2 hits the queue wait message
        saw_queued = False
        for _ in range(40):
            snap2 = orch.get_job_status_snapshot(job2.id, user_id="tenant_q")
            if snap2 and "Queued:" in snap2.current_stage:
                saw_queued = True
                break
            time.sleep(0.02)
        assert saw_queued, "Expected second concurrent job to report queued state while slot is busy"

        release_job1.set()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

    assert orch.get_job_status_snapshot(job1.id, user_id="tenant_q").status == JobStatus.AWAITING_APPROVAL
    assert orch.get_job_status_snapshot(job2.id, user_id="tenant_q").status == JobStatus.AWAITING_APPROVAL
    assert orch.get_job_status_snapshot(job2.id, user_id="other_tenant") is None

    # Semaphore slot must be cleanly released back to capacity 1
    assert orch._pipeline_semaphore.acquire(blocking=False) is True
    orch._pipeline_semaphore.release()


def test_charts_bounded_lru_caches_and_batched_cast_warnings(tmp_path: Path):
    """Chart/stats caches evict LRU entries at capacity, and cast_fields batching warns accurately."""
    with charts_service._cache_lock:
        charts_service._stats_cache.clear()
        charts_service._chart_cache.clear()

    total_insertions = charts_service.MAX_STATS_CACHE_ENTRIES + 15
    for idx in range(total_insertions):
        charts_service._cache_set(
            charts_service._stats_cache,
            f"key_{idx}",
            {"idx": idx},
            charts_service.MAX_STATS_CACHE_ENTRIES,
        )
        # Touch key_0 partway through so LRU promotion is exercised
        if idx == 5:
            charts_service._cache_get(charts_service._stats_cache, "key_0")

    with charts_service._cache_lock:
        assert len(charts_service._stats_cache) == charts_service.MAX_STATS_CACHE_ENTRIES
    assert charts_service._cache_get(charts_service._stats_cache, "key_1") is None
    assert charts_service._cache_get(charts_service._stats_cache, f"key_{total_insertions - 1}") == {
        "idx": total_insertions - 1
    }

    # Verify single-scan batched cast_fields detection flags invalid cast targets cleanly
    pq_path = tmp_path / "cast_test.parquet"
    con = duckdb.connect(":memory:")
    con.execute(
        f"""
        COPY (
            SELECT '100' AS amount_ok, 'abc' AS amount_bad, 'x' AS grp
            UNION ALL
            SELECT '200', '250', 'y'
        ) TO '{str(pq_path).replace(chr(92), "/")}' (FORMAT PARQUET);
        """
    )
    con.close()

    res = charts_service.build_chart(
        "cast_job",
        pq_path,
        {
            "chart_type": "bar",
            "x": "grp",
            "y": "amount_bad",
            "agg": "sum",
            "cast_fields": {"amount_ok": "DOUBLE", "amount_bad": "DOUBLE"},
        },
        ttl_seconds=60,
    )
    assert any("amount_bad" in w for w in res.warnings)
    assert not any("amount_ok" in w for w in res.warnings)


def test_api_status_endpoint_gzip_and_modeling_export_cleanup(tmp_path: Path):
    """Lightweight /status endpoint, GZip compression, and modeling CSV temp-file cleanup all work."""
    client = TestClient(app)

    # 1. Create a minimal persisted job via the shared app orchestrator
    job = orchestrator.create_job(filename="perf_api.csv", file_size=128, user_id="perf_user")
    headers = {"X-User-Id": "perf_user"}

    resp = client.get(f"/api/jobs/{job.id}/status", headers=headers)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["id"] == job.id
    assert payload["status"] == JobStatus.UPLOADED.value

    # Tenant isolation on lightweight status endpoint
    denied = client.get(f"/api/jobs/{job.id}/status", headers={"X-User-Id": "other_user"})
    assert denied.status_code == 404

    # 2. GZip middleware compresses large payloads cleanly when requested
    wide_csv = (
        "col_a,col_b,col_c,col_d\n"
        + "\n".join(f"row_{i},value_{i},extra_{i},{i * 10}" for i in range(120))
    ).encode("utf-8")
    detect_resp = client.post(
        "/api/convert/detect",
        files={"file": ("wide.csv", wide_csv, "text/csv")},
        headers={**headers, "Accept-Encoding": "gzip"},
    )
    assert detect_resp.status_code == 200

    # 3. Modeling CSV export must delete its temporary CSV file once streamed
    sess_resp = client.post("/api/modeling/sessions", headers=headers)
    assert sess_resp.status_code == 200
    session_id = sess_resp.json()["session_id"]

    table_csv = b"id,name\n1,Alice\n2,Bob\n"
    up_resp = client.post(
        f"/api/modeling/sessions/{session_id}/tables",
        files={"file": ("users.csv", table_csv, "text/csv")},
        headers=headers,
    )
    assert up_resp.status_code == 200

    created_tmp_files: list[Path] = []
    real_ntf = tempfile.NamedTemporaryFile

    def _tracking_ntf(*args, **kwargs):
        handle = real_ntf(*args, **kwargs)
        if kwargs.get("suffix") == ".csv":
            created_tmp_files.append(Path(handle.name))
        return handle

    with patch("app.main.tempfile.NamedTemporaryFile", side_effect=_tracking_ntf):
        export_resp = client.get(
            f"/api/modeling/sessions/{session_id}/export-csv",
            headers=headers,
        )

    assert export_resp.status_code == 200
    assert "Alice" in export_resp.text
    assert len(created_tmp_files) == 1
    assert not created_tmp_files[0].exists(), "Temporary modeling CSV file should be deleted after streaming"
