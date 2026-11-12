"""Multi-tenancy context extraction, validation, and directory traversal defense."""

from __future__ import annotations
import os
import re
from typing import Optional
from fastapi import HTTPException, Request, status

TENANT_ID_REGEX = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")
DEFAULT_TENANT_ID = os.getenv("DEFAULT_TENANT_ID", "default")


def validate_tenant_id(tenant_id: Optional[str]) -> str:
    """Validate and sanitize a tenant/user identifier to prevent directory traversal.
    
    Raises:
        HTTPException: 400 Bad Request if the tenant ID contains path separators,
                       null bytes, or invalid characters.
    """
    if not tenant_id:
        return DEFAULT_TENANT_ID


def resolve_active_tenant_id(header_uid: str, body_uid: Optional[str] = None) -> str:
    """Resolve effective tenant ID, validating fallback body user_id when header is default."""
    if header_uid != DEFAULT_TENANT_ID:
        return header_uid
    return validate_tenant_id(body_uid) if body_uid else DEFAULT_TENANT_ID
    clean = tenant_id.strip()
    if not clean or clean.lower() in ("default", "anonymous", "none"):
        return DEFAULT_TENANT_ID


def resolve_active_tenant_id(header_uid: str, body_uid: Optional[str] = None) -> str:
    """Resolve effective tenant ID, validating fallback body user_id when header is default."""
    if header_uid != DEFAULT_TENANT_ID:
        return header_uid
    return validate_tenant_id(body_uid) if body_uid else DEFAULT_TENANT_ID
    if not TENANT_ID_REGEX.match(clean):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id. Must be 1-64 alphanumeric characters, underscores, or hyphens without path separators.",
        )
    return clean


def get_current_tenant_id(request: Request) -> str:
    """FastAPI dependency to extract and sanitize the tenant ID for the current request.
    
    Resolution order:
    1. 'X-User-Id' HTTP header (primary for API and frontend fetch calls)
    2. 'user_id' query parameter (for direct browser file downloads / links)
    3. DEFAULT_TENANT_ID ('default') for backward compatibility
    """
    # 1. Header takes precedence
    tenant_header = request.headers.get("X-User-Id")
    if tenant_header:
        return validate_tenant_id(tenant_header)

    # 2. Query param fallback (for direct browser downloads)
    tenant_query = request.query_params.get("user_id")
    if tenant_query:
        return validate_tenant_id(tenant_query)

    return DEFAULT_TENANT_ID


def resolve_active_tenant_id(header_uid: str, body_uid: Optional[str] = None) -> str:
    """Resolve effective tenant ID, validating fallback body user_id when header is default."""
    if header_uid != DEFAULT_TENANT_ID:
        return header_uid
    return validate_tenant_id(body_uid) if body_uid else DEFAULT_TENANT_ID
