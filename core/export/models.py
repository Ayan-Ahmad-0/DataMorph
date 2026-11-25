"""Pydantic request/response contracts and shared option types for recipe code export."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

ExportTarget = Literal["sql", "dbt", "pandas", "polars"]
SqlDialect = Literal["duckdb", "snowflake", "bigquery", "postgres"]
ExportLanguage = Literal["sql", "yaml", "python"]

_SAFE_SOURCE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,127}$")
_FORBIDDEN_SOURCE_TOKENS = ("--", "/*", "*/", ";", "\x00", "\n", "\r", "'", '"', "`", "\\")


class ExportCodeRequest(BaseModel):
    """Validated request payload for exporting a cleaning recipe as standalone code."""

    model_config = ConfigDict(extra="forbid")

    target: ExportTarget
    dialect: SqlDialect = "duckdb"
    include_tests: bool = True
    source_name: Optional[str] = None

    @field_validator("source_name")
    @classmethod
    def validate_source_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = v.strip()
        if not cleaned:
            return None
        if any(tok in cleaned for tok in _FORBIDDEN_SOURCE_TOKENS):
            raise ValueError("source_name contains forbidden characters or SQL comment tokens.")
        if ".." in cleaned or cleaned.startswith(".") or cleaned.endswith("."):
            raise ValueError("source_name contains invalid dot notation.")
        if not _SAFE_SOURCE_NAME_RE.match(cleaned):
            raise ValueError(
                "source_name must start with a letter or underscore and contain only "
                "alphanumeric characters, underscores, hyphens, or dots (max 128 chars)."
            )
        return cleaned


class ExportedFile(BaseModel):
    """Single generated source file inside an export response."""

    name: str
    content: str
    language: ExportLanguage


class ExportCodeResponse(BaseModel):
    """Structured response returned by recipe-to-code export service and API."""

    target: ExportTarget
    dialect: Optional[str] = None
    files: List[ExportedFile] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


@dataclass(frozen=True)
class ExportOptions:
    """Internal emitter execution options."""

    job_id: str
    dialect: SqlDialect = "duckdb"
    include_tests: bool = True
    source_name: Optional[str] = None
    dataset_name: Optional[str] = None
    generated_at: Optional[datetime] = None

    def utc_timestamp_iso(self) -> str:
        dt = self.generated_at or datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
