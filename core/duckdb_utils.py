"""DuckDB resource allocation, configuration, and connection utilities."""

from __future__ import annotations
import os
import tempfile
from pathlib import Path
from typing import Optional
import duckdb


def get_system_ram_mb() -> Optional[int]:
    """Detect physical host RAM in MB if on Linux or inside a Docker container."""
    try:
        meminfo = Path("/proc/meminfo")
        if meminfo.exists():
            with open(meminfo, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        parts = line.split()
                        return int(parts[1]) // 1024
    except Exception:
        pass
    return None


def get_duckdb_memory_limit() -> str:
    """
    Return configured memory limit or auto-tune to available physical RAM.
    On low-memory instances (e.g. 1GB VMs), keeps limit at 70% of physical RAM
    to prevent the OS kernel from thrashing swap memory on disk.
    """
    limit = os.getenv("DUCKDB_MEMORY_LIMIT")
    if limit and limit.strip():
        return limit.strip()

    ram_mb = get_system_ram_mb()
    if ram_mb is not None:
        if ram_mb <= 1500:
            # 1GB VM: ~650MB allocated to DuckDB, leaving ~300MB for OS + Uvicorn
            return f"{max(300, int(ram_mb * 0.70))}MB"
        elif ram_mb <= 4000:
            return f"{int(ram_mb * 0.75)}MB"
        elif ram_mb <= 8000:
            return "4GB"
        else:
            return "8GB"

    return "4GB"


def get_duckdb_threads() -> int:
    """Return configured thread count or auto-tune to available CPU cores."""
    custom = os.getenv("DUCKDB_THREADS")
    if custom and custom.strip():
        try:
            return max(1, int(custom.strip()))
        except ValueError:
            pass
    cpu = os.cpu_count() or 1
    # On 1-core machines, use 1 thread to eliminate context switching and lock contention
    return min(4, max(1, cpu))


def get_max_concurrent_pipelines() -> int:
    """Return max concurrent heavy profiling/cleaning pipelines safe for host RAM."""
    custom = os.getenv("MAX_CONCURRENT_PIPELINES")
    if custom and custom.strip():
        try:
            return max(1, int(custom.strip()))
        except ValueError:
            pass

    ram_mb = get_system_ram_mb()
    if ram_mb is not None:
        if ram_mb <= 2048:
            return 1
        if ram_mb <= 4096:
            return 2
        return min(4, max(2, os.cpu_count() or 2))
    return 2


def get_chart_concurrency_limits() -> tuple[int, int]:
    """Return (max_global_chart_queries, max_per_user_chart_queries) scaled to host RAM."""
    ram_mb = get_system_ram_mb()
    if ram_mb is not None:
        if ram_mb <= 2048:
            return (4, 3)
        if ram_mb <= 4096:
            return (8, 5)
    return (12, 8)


def get_duckdb_temp_directory() -> str:
    """
    Return a safe, cross-platform temporary directory for DuckDB spilling.
    Translates invalid Unix paths like '/tmp/duckdb' to native OS temp paths on Windows.
    Prefers high-capacity mounted data directory (/app/data/duckdb_temp) when inside Docker.
    """
    custom = os.getenv("DUCKDB_TEMP_DIRECTORY") or os.getenv("DUCKDB_TEMP_DIR")
    if custom:
        if os.name == "nt" and custom.startswith("/tmp"):
            base_name = Path(custom).name or "duckdb_temp"
            custom = str(Path(tempfile.gettempdir()) / base_name)
    else:
        # Check if running in Docker container with mounted data volume
        if Path("/app/data").exists() and os.access("/app/data", os.W_OK):
            custom = "/app/data/duckdb_temp"
        elif Path("data").exists() and os.access("data", os.W_OK):
            custom = str(Path("data").resolve() / "duckdb_temp")
        else:
            custom = str(Path(tempfile.gettempdir()) / "duckdb_temp")

    os.makedirs(custom, exist_ok=True)
    return custom.replace("\\", "/")


def configure_duckdb_connection(
    conn: duckdb.DuckDBPyConnection,
    memory_limit: Optional[str] = None,
    threads: Optional[int] = None,
    temp_directory: Optional[str] = None,
) -> duckdb.DuckDBPyConnection:
    """Apply consistent high-throughput resource settings to a DuckDB connection."""
    conn.execute("SET preserve_insertion_order = false;")

    t_dir = temp_directory or get_duckdb_temp_directory()
    if t_dir:
        conn.execute(f"SET temp_directory = '{t_dir}';")

    mem = memory_limit or get_duckdb_memory_limit()
    if mem:
        conn.execute(f"SET memory_limit = '{mem}';")

    th = threads or get_duckdb_threads()
    if th:
        conn.execute(f"SET threads = {th};")

    return conn

