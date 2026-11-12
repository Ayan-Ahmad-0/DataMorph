"""Centralized security utilities: path safety, SSRF validation, SQL literal escaping, and admin auth."""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import secrets
import socket
from pathlib import Path, PurePosixPath
from typing import Optional
from urllib.parse import urlparse

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)

SAFE_ID_REGEX = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,63}$")
SAFE_FILENAME_CHAR_REGEX = re.compile(r"[^a-zA-Z0-9._\- ]")

_ALWAYS_BLOCKED_HOSTS = frozenset(
    {
        "169.254.169.254",
        "metadata.google.internal",
        "metadata.azure.internal",
        "instance-data",
        "metadata",
    }
)

_PRIVATE_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "data-cleaner",
        "data-cleaner-app",
        "caddy",
        "datamorph-caddy",
    }
)


def is_production_env() -> bool:
    """Return True when running in an explicit production environment."""
    return os.getenv("ENVIRONMENT", "").strip().lower() in ("production", "prod")


def allow_private_connector_networks() -> bool:
    """Determine whether external connectors may reach private/loopback networks."""
    raw = os.getenv("CONNECTOR_ALLOW_PRIVATE_NETWORKS")
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return not is_production_env()


def validate_safe_id(identifier: str, label: str = "id") -> str:
    """Validate that a job/session/convert identifier contains only safe filename characters."""
    clean = (identifier or "").strip()
    if not clean or not SAFE_ID_REGEX.match(clean):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {label}. Must be 1-64 alphanumeric characters, underscores, or hyphens.",
        )
    return clean


def sanitize_filename(filename: Optional[str], fallback: str = "dataset.csv") -> str:
    """Strip directory traversal segments and unsafe characters from an uploaded filename."""
    raw = (filename or "").replace("\x00", "").strip()
    # Extract basename handling both Windows (\) and POSIX (/) separators uniformly
    raw = raw.replace("\\", "/")
    basename = PurePosixPath(raw).name if raw else ""
    if basename in ("", ".", ".."):
        return fallback

    stem = Path(basename).stem
    suffix = Path(basename).suffix
    clean_stem = SAFE_FILENAME_CHAR_REGEX.sub("_", stem).strip(" .") or "dataset"
    clean_suffix = re.sub(r"[^a-zA-Z0-9.]", "", suffix)[:16]
    if len(clean_stem) > 120:
        clean_stem = clean_stem[:120]
    return f"{clean_stem}{clean_suffix}"


def resolve_within_base(base_dir: Path, *parts: str) -> Path:
    """Join parts under base_dir and reject any resolved path escaping base_dir."""
    base_resolved = base_dir.resolve()
    target = base_resolved
    for part in parts:
        target = target / part
    resolved = target.resolve()
    if not resolved.is_relative_to(base_resolved):
        raise ValueError("Resolved path escapes storage base directory.")
    return resolved


def escape_sql_literal(value: str) -> str:
    """Escape a string literal for safe single-quoted inclusion in DuckDB SQL statements."""
    return value.replace("\x00", "").replace("'", "''")


def _extract_hostname(host_or_url: str) -> str:
    raw = (host_or_url or "").strip()
    if not raw:
        raise ValueError("Host or URL cannot be empty.")
    if "://" in raw:
        parsed = urlparse(raw)
        if not parsed.hostname:
            raise ValueError(f"Could not parse hostname from URL '{host_or_url}'.")
        return parsed.hostname.strip().lower()
    # Strip optional host:port notation for bare hostnames/IPv4
    if raw.startswith("[") and "]" in raw:
        return raw[1 : raw.index("]")].strip().lower()
    if ":" in raw and raw.count(":") == 1:
        return raw.split(":", 1)[0].strip().lower()
    return raw.strip().lower()


def _check_ip_address(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address, allow_private: bool, source_host: str) -> None:
    if ip_obj.is_link_local or str(ip_obj).lower().startswith("fd00:ec2:"):
        raise ValueError(f"Access to cloud metadata/link-local address '{source_host}' is prohibited.")
    if not allow_private and (
        ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_multicast or ip_obj.is_unspecified or ip_obj.is_reserved
    ):
        raise ValueError(f"Connections to private or loopback network address '{source_host}' are disabled.")


def validate_external_host(host_or_url: str, allow_private: Optional[bool] = None) -> str:
    """Validate an external connector hostname or URL against SSRF & cloud metadata rules."""
    allow_priv = allow_private_connector_networks() if allow_private is None else allow_private
    hostname = _extract_hostname(host_or_url)

    if hostname in _ALWAYS_BLOCKED_HOSTS or hostname.endswith(".internal") and "metadata" in hostname:
        raise ValueError(f"Access to cloud metadata host '{hostname}' is prohibited.")

    if not allow_priv and (hostname in _PRIVATE_BLOCKED_HOSTS or hostname.endswith(".local")):
        raise ValueError(f"Connections to internal or loopback host '{hostname}' are disabled.")

    # Direct IP literal check
    try:
        ip_obj = ipaddress.ip_address(hostname)
    except ValueError:
        ip_obj = None

    if ip_obj is not None:
        _check_ip_address(ip_obj, allow_priv, hostname)
        return hostname

    # Resolve DNS if reachable; validate every returned record against SSRF rules
    try:
        addr_infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return hostname

    for _, _, _, _, sockaddr in addr_infos:
        resolved_ip = sockaddr[0].split("%", 1)[0]
        try:
            _check_ip_address(ipaddress.ip_address(resolved_ip), allow_priv, hostname)
        except ValueError as err:
            if "is prohibited" in str(err) or "are disabled" in str(err):
                raise

    return hostname


def verify_admin_access(request: Request) -> None:
    """Protect administrative maintenance endpoints without forcing end-user logins."""
    configured_key = (os.getenv("ADMIN_API_KEY") or "").strip()
    if configured_key:
        provided = (request.headers.get("X-Admin-Key") or "").strip()
        if not provided:
            auth_header = (request.headers.get("Authorization") or "").strip()
            if auth_header.lower().startswith("bearer "):
                provided = auth_header[7:].strip()
        if not provided or not secrets.compare_digest(provided, configured_key):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Valid X-Admin-Key is required for maintenance operations.",
            )
        return

    if is_production_env():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Maintenance endpoints are disabled in production until ADMIN_API_KEY is configured.",
        )

    client_host = (request.client.host if request.client else "") or ""
    if client_host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Maintenance endpoints require loopback access or ADMIN_API_KEY.",
        )
