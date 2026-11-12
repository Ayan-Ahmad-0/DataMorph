"""AWS S3, Cloudflare R2, MinIO, and S3-compatible cloud storage connector."""

from __future__ import annotations
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional
import duckdb
from pydantic import BaseModel, Field

from core.security import escape_sql_literal, validate_external_host
from connectors.base import (
    BaseConnector,
    ConnectionTestResult,
    ConnectorMetadata,
    ConnectorType,
    ExportFormat,
    SyncResult,
)

logger = logging.getLogger(__name__)


class S3ConnectorConfig(BaseModel):
    """Configuration for AWS S3 and S3-compatible storage services."""
    bucket: str = Field(default="", description="Target S3 or R2 bucket name")
    prefix: str = Field("", description="Optional folder/prefix path inside bucket")
    filename: Optional[str] = Field(None, description="Custom filename for destination object")
    key: Optional[str] = Field(None, description="Object key for import (e.g. path/to/dataset.parquet)")
    source_uri: Optional[str] = Field(None, description="Direct S3 URI (e.g. s3://bucket/data.parquet) for import")
    region: str = Field("us-east-1", description="AWS or cloud region (e.g., us-east-1, auto)")
    endpoint_url: Optional[str] = Field(
        None,
        description="Custom endpoint for Cloudflare R2 (https://<account>.r2.cloudflarestorage.com) or MinIO (http://localhost:9000)",
    )
    access_key_id: Optional[str] = Field(None, description="Access Key ID (optional if AWS_ACCESS_KEY_ID is in env)")
    secret_access_key: Optional[str] = Field(None, description="Secret Access Key (optional if AWS_SECRET_ACCESS_KEY is in env)")
    session_token: Optional[str] = Field(None, description="Optional AWS Session Token for temporary credentials")
    url_style: str = Field("path", description="S3 URL style: 'path' (recommended for MinIO/R2) or 'vhost'")
    use_ssl: bool = Field(True, description="Whether to use HTTPS/SSL for requests")
    export_format: ExportFormat = Field(ExportFormat.PARQUET, description="Target export format")


class S3Connector(BaseConnector):
    """High-performance direct cloud storage connector powered by DuckDB httpfs."""

    connector_type = ConnectorType.S3
    display_name = "AWS S3 / Cloudflare R2"
    description = "Stream datasets directly to AWS S3, Cloudflare R2, MinIO, or Google Cloud Storage."
    category = "cloud_storage"

    def get_metadata(self) -> ConnectorMetadata:
        return ConnectorMetadata(
            id=self.connector_type.value,
            display_name=self.display_name,
            description=self.description,
            category=self.category,
            supported_formats=[
                ExportFormat.PARQUET.value,
                ExportFormat.CSV.value,
                ExportFormat.TSV.value,
                ExportFormat.JSON.value,
                ExportFormat.NDJSON.value,
            ],
            supports_import=True,
            supports_export=True,
            config_fields=[
                {"name": "bucket", "label": "Bucket Name", "type": "string", "required": True, "placeholder": "my-analytics-bucket"},
                {"name": "prefix", "label": "Folder / Prefix", "type": "string", "required": False, "placeholder": "cleaned_data/2026"},
                {"name": "filename", "label": "Destination Filename", "type": "string", "required": False, "placeholder": "transactions.csv"},
                {"name": "region", "label": "Region", "type": "string", "required": False, "default": "us-east-1", "placeholder": "us-east-1 or auto"},
                {"name": "endpoint_url", "label": "Custom Endpoint URL", "type": "string", "required": False, "placeholder": "https://<account>.r2.cloudflarestorage.com or http://localhost:9000"},
                {"name": "access_key_id", "label": "Access Key ID", "type": "string", "required": False, "secret": False},
                {"name": "secret_access_key", "label": "Secret Access Key", "type": "password", "required": False, "secret": True},
                {"name": "export_format", "label": "Export Format", "type": "select", "options": ["parquet", "csv", "tsv", "json", "ndjson"], "default": "csv"},
            ],
        )

    def _configure_connection(self, con: duckdb.DuckDBPyConnection, cfg: S3ConnectorConfig) -> None:
        """Load httpfs and configure DuckDB S3 session parameters safely."""
        con.execute("INSTALL httpfs; LOAD httpfs;")

        allow_env_creds = os.getenv("ALLOW_SERVER_CONNECTOR_ENV_CREDS", "false").strip().lower() in ("1", "true", "yes")
        key_id = (cfg.access_key_id or (os.getenv("AWS_ACCESS_KEY_ID") if allow_env_creds else "") or "").strip()
        secret_key = (cfg.secret_access_key or (os.getenv("AWS_SECRET_ACCESS_KEY") if allow_env_creds else "") or "").strip()
        token = (cfg.session_token or (os.getenv("AWS_SESSION_TOKEN") if allow_env_creds else "") or "").strip()

        # Clean region: strip any parentheses or human labels (e.g. "us-east-1 (N. Virginia)" -> "us-east-1")
        raw_region = cfg.region or (os.getenv("AWS_DEFAULT_REGION") if allow_env_creds else "") or "us-east-1"
        region = raw_region.split("(")[0].strip() if raw_region else "us-east-1"

        if key_id:
            con.execute(f"SET s3_access_key_id = '{escape_sql_literal(key_id)}';")
        if secret_key:
            con.execute(f"SET s3_secret_access_key = '{escape_sql_literal(secret_key)}';")
        if token:
            con.execute(f"SET s3_session_token = '{escape_sql_literal(token)}';")
        if region:
            con.execute(f"SET s3_region = '{escape_sql_literal(region)}';")

        # Parse endpoint and validate against SSRF / cloud metadata access
        raw_endpoint = (
            cfg.endpoint_url
            or ((os.getenv("AWS_ENDPOINT_URL_S3") or os.getenv("AWS_ENDPOINT_URL")) if allow_env_creds else "")
            or ""
        ).strip()
        is_custom_non_aws = False
        use_ssl = cfg.use_ssl

        # Discard placeholder values containing angle brackets (e.g. "<id>", "<account>") or spaces
        if raw_endpoint and not ("<" in raw_endpoint or ">" in raw_endpoint or " " in raw_endpoint):
            endpoint = raw_endpoint
            if endpoint.startswith("http://"):
                use_ssl = False
                endpoint = endpoint[len("http://"):]
            elif endpoint.startswith("https://"):
                use_ssl = True
                endpoint = endpoint[len("https://"):]
            elif endpoint.startswith("s3://"):
                endpoint = endpoint[len("s3://"):]

            # Strip any trailing slashes or subpaths
            endpoint = endpoint.split("/")[0].strip()
            if endpoint:
                validate_external_host(endpoint)

            # Only configure custom endpoint for non-AWS S3 (MinIO, Cloudflare R2, LocalStack).
            # If the user typed standard AWS endpoints (e.g. s3.amazonaws.com), let DuckDB use native regional resolution.
            if endpoint and not endpoint.endswith("amazonaws.com"):
                con.execute(f"SET s3_endpoint = '{escape_sql_literal(endpoint)}';")
                con.execute(f"SET s3_use_ssl = {'true' if use_ssl else 'false'};")
                is_custom_non_aws = True

        # URL Style: AWS S3 requires 'vhost' (virtual-hosted style) since 2020.
        # Custom non-AWS storage (MinIO / R2) or explicitly requested path style uses 'path'.
        if cfg.url_style in ("path", "vhost"):
            url_style = cfg.url_style
        else:
            url_style = "path" if is_custom_non_aws else "vhost"

        con.execute(f"SET s3_url_style = '{escape_sql_literal(url_style)}';")

    def _build_destination_uri(self, cfg: S3ConnectorConfig, default_name: str) -> str:
        """Construct a normalized, safe s3:// URI."""
        bucket = cfg.bucket.strip().removeprefix("s3://").strip("/")
        if not bucket:
            raise ValueError("Bucket name cannot be empty.")

        # Determine user-provided path/key or filename
        raw_target = (cfg.filename or cfg.key or "").strip()
        fmt = cfg.export_format.value

        if raw_target:
            target = raw_target.removeprefix("s3://").lstrip("/")
            if target.startswith(f"{bucket}/"):
                target = target[len(bucket) + 1:].lstrip("/")
        else:
            target = default_name.strip().lstrip("/")

        prefix = cfg.prefix.strip().strip("/")
        if prefix and not target.startswith(f"{prefix}/") and target != prefix:
            target = f"{prefix}/{target}"

        # Adjust extension: match selected export format
        target_path = Path(target)
        known_exts = {".parquet", ".csv", ".tsv", ".json", ".ndjson", ".jsonl", ".txt", ".xlsx", ".xls"}
        if target_path.suffix.lower() in known_exts:
            if target_path.suffix.lower() != f".{fmt}":
                target = target_path.with_suffix(f".{fmt}").as_posix()
        else:
            target = f"{target}.{fmt}"

        return f"s3://{bucket}/{target.strip('/')}"

    def test_connection(self, config: Dict[str, Any]) -> ConnectionTestResult:
        """Validate S3 credentials and endpoint reachability."""
        start_time = time.perf_counter()
        con = None
        try:
            cfg = S3ConnectorConfig.model_validate(config)
            con = duckdb.connect(":memory:")
            self._configure_connection(con, cfg)

            # Sanity check bucket name
            clean_bucket = cfg.bucket.strip().removeprefix("s3://").strip("/")
            if not clean_bucket or not re.match(r"^[a-zA-Z0-9.\-_]{3,63}$", clean_bucket):
                return ConnectionTestResult(
                    success=False,
                    message=f"Invalid S3 bucket name '{cfg.bucket}'. Must be 3-63 characters (letters, numbers, hyphens, dots).",
                )

            is_export_mode = config.get("mode") == "export" or config.get("test_mode") == "export"

            # If an object key or source URI is provided for import, attempt a lightweight metadata scan (LIMIT 0)
            if not is_export_mode and (cfg.key or cfg.source_uri):
                source_uri = self._resolve_source_uri(cfg)
                safe_uri = escape_sql_literal(source_uri)
                fmt = Path(source_uri).suffix.lower().lstrip(".")
                if fmt == "parquet":
                    scan_expr = f"read_parquet('{safe_uri}')"
                elif fmt in ("csv", "tsv"):
                    scan_expr = f"read_csv_auto('{safe_uri}')"
                elif fmt in ("json", "ndjson", "jsonl"):
                    scan_expr = f"read_json_auto('{safe_uri}')"
                else:
                    scan_expr = f"read_csv_auto('{safe_uri}')"

                con.execute(f"SELECT * FROM {scan_expr} LIMIT 0")
            elif is_export_mode:
                # In export mode, verify bucket connectivity without requiring target key to exist
                try:
                    con.execute(f"SELECT count(*) FROM glob('s3://{escape_sql_literal(clean_bucket)}/*') LIMIT 1")
                except Exception as probe_err:
                    err_str = str(probe_err).lower()
                    if "403" in err_str or "accessdenied" in err_str or "forbidden" in err_str:
                        raise
                    if "invalid s3 endpoint" in err_str or "host not found" in err_str:
                        raise
                    logger.debug("Export connection probe response: %s", probe_err)

            elapsed = (time.perf_counter() - start_time) * 1000
            return ConnectionTestResult(
                success=True,
                message=f"Successfully connected to S3 service (bucket: {clean_bucket}, region: {cfg.region}).",
                latency_ms=round(elapsed, 2),
                details={"bucket": clean_bucket, "region": cfg.region, "endpoint": cfg.endpoint_url or "aws-standard"},
            )
        except Exception as e:
            elapsed = (time.perf_counter() - start_time) * 1000
            err_msg = str(e)
            for s_key in (config.get("secret_access_key"), config.get("session_token")):
                if s_key and len(str(s_key).strip()) > 1:
                    err_msg = err_msg.replace(str(s_key), "••••••••")
            logger.warning("S3 connection test failed: %s", err_msg)
            return ConnectionTestResult(
                success=False,
                message=f"S3 Connection failed: {err_msg}",
                latency_ms=round(elapsed, 2),
            )
        finally:
            if con:
                try:
                    con.close()
                except Exception:
                    pass

    def export_dataset(
        self,
        source_parquet_path: Path,
        config: Dict[str, Any],
        options: Optional[Dict[str, Any]] = None,
    ) -> SyncResult:
        """Export local Parquet dataset to S3/R2/MinIO object store."""
        if not source_parquet_path.exists():
            raise FileNotFoundError(f"Source Parquet dataset not found at {source_parquet_path}")

        start_time = time.perf_counter()
        cfg = S3ConnectorConfig.model_validate(config)
        options = options or {}

        # Determine default stem from options or original job filename without appending '_cleaned'
        job_filename = options.get("job_filename")
        if job_filename:
            default_stem = Path(job_filename).stem
        else:
            default_stem = source_parquet_path.stem
            if default_stem == "cleaned":
                default_stem = "dataset"

        destination_uri = self._build_destination_uri(cfg, default_name=default_stem)

        con = duckdb.connect(":memory:")
        self._configure_connection(con, cfg)

        try:
            # Determine total row count
            row_count_res = con.execute("SELECT count(*) FROM read_parquet(?)", [str(source_parquet_path.resolve())]).fetchone()
            row_count = int(row_count_res[0]) if row_count_res else 0

            # Execute vectorized COPY TO S3 based on format
            fmt = cfg.export_format
            src_path_str = escape_sql_literal(str(source_parquet_path.resolve()).replace("\\", "/"))
            safe_dest_uri = escape_sql_literal(destination_uri)

            if fmt == ExportFormat.PARQUET:
                con.execute(f"COPY (SELECT * FROM read_parquet('{src_path_str}')) TO '{safe_dest_uri}' (FORMAT PARQUET, COMPRESSION 'ZSTD');")
            elif fmt == ExportFormat.CSV:
                con.execute(f"COPY (SELECT * FROM read_parquet('{src_path_str}')) TO '{safe_dest_uri}' (FORMAT CSV, HEADER TRUE);")
            elif fmt == ExportFormat.TSV:
                con.execute(f"COPY (SELECT * FROM read_parquet('{src_path_str}')) TO '{safe_dest_uri}' (FORMAT CSV, DELIMITER '\t', HEADER TRUE);")
            elif fmt == ExportFormat.JSON:
                con.execute(f"COPY (SELECT * FROM read_parquet('{src_path_str}')) TO '{safe_dest_uri}' (FORMAT JSON, ARRAY TRUE);")
            elif fmt == ExportFormat.NDJSON:
                con.execute(f"COPY (SELECT * FROM read_parquet('{src_path_str}')) TO '{safe_dest_uri}' (FORMAT JSON);")
            else:
                raise ValueError(f"Unsupported export format: {fmt}")

            elapsed = time.perf_counter() - start_time
            return SyncResult(
                success=True,
                rows_transferred=row_count,
                destination_uri=destination_uri,
                elapsed_seconds=round(elapsed, 3),
                message=f"Successfully exported {row_count:,} rows to {destination_uri} in {elapsed:.2f}s",
                details={
                    "format": fmt.value,
                    "bucket": cfg.bucket,
                    "region": cfg.region,
                },
            )
        except Exception as e:
            logger.exception("S3 export failed for %s to %s", source_parquet_path, destination_uri)
            raise RuntimeError(f"Failed to export to S3 target '{destination_uri}': {str(e)}") from e
        finally:
            con.close()

    def _resolve_source_uri(self, cfg: S3ConnectorConfig) -> str:
        """Resolve a normalized s3:// URI for dataset import."""
        if cfg.source_uri:
            uri = cfg.source_uri.strip()
            if not uri.startswith("s3://"):
                uri = f"s3://{uri.lstrip('/')}"
            return uri

        clean_bucket = cfg.bucket.strip().removeprefix("s3://").strip("/")
        if not clean_bucket:
            raise ValueError("Either bucket or source_uri must be provided for S3 import.")

        key = (cfg.key or cfg.filename or "").strip().lstrip("/")
        prefix = cfg.prefix.strip().strip("/")
        if prefix and key:
            full_key = f"{prefix}/{key}"
        elif prefix and not key:
            full_key = prefix
        else:
            full_key = key

        if not full_key:
            raise ValueError("An S3 object key or filename must be provided for import.")

        return f"s3://{clean_bucket}/{full_key}"

    def import_dataset(
        self,
        config: Dict[str, Any],
        target_parquet_path: Path,
        options: Optional[Dict[str, Any]] = None,
    ) -> SyncResult:
        """Import dataset from S3/R2/MinIO object store directly into canonical local Parquet."""
        start_time = time.perf_counter()
        cfg = S3ConnectorConfig.model_validate(config)
        source_uri = self._resolve_source_uri(cfg)

        target_parquet_path.parent.mkdir(parents=True, exist_ok=True)
        target_path_str = str(target_parquet_path.resolve()).replace("\\", "/")

        con = duckdb.connect(":memory:")
        self._configure_connection(con, cfg)

        try:
            fmt = ((options or {}).get("format") or "").lower()
            safe_source_uri = escape_sql_literal(source_uri)
            safe_target_str = escape_sql_literal(target_path_str)
            uri_lower = source_uri.lower()
            if fmt == "parquet" or uri_lower.endswith(".parquet"):
                scan_expr = f"read_parquet('{safe_source_uri}')"
            elif fmt in ("csv", "tsv") or uri_lower.endswith((".csv", ".tsv")):
                scan_expr = f"read_csv_auto('{safe_source_uri}')"
            elif fmt in ("json", "ndjson", "jsonl") or uri_lower.endswith((".json", ".ndjson", ".jsonl")):
                scan_expr = f"read_json_auto('{safe_source_uri}')"
            else:
                scan_expr = f"read_parquet('{safe_source_uri}')"

            con.execute(f"COPY (SELECT * FROM {scan_expr}) TO '{safe_target_str}' (FORMAT PARQUET, COMPRESSION 'ZSTD');")

            row_count_res = con.execute(f"SELECT count(*) FROM read_parquet('{safe_target_str}')").fetchone()
            row_count = int(row_count_res[0]) if row_count_res else 0

            elapsed = time.perf_counter() - start_time
            return SyncResult(
                success=True,
                rows_transferred=row_count,
                destination_uri=source_uri,
                elapsed_seconds=round(elapsed, 3),
                message=f"Successfully imported {row_count:,} rows from {source_uri} in {elapsed:.2f}s",
                details={
                    "source_uri": source_uri,
                    "target_path": str(target_parquet_path),
                },
            )
        except Exception as e:
            logger.exception("S3 import failed from %s", source_uri)
            raise RuntimeError(f"Failed to import from S3 target '{source_uri}': {str(e)}") from e
        finally:
            con.close()

