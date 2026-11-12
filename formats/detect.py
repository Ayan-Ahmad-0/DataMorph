"""Format detection engine combining file extension mapping and content sniffing."""

from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from formats.base import FormatDetectionError


def detect_format(
    path: Path,
    adapters: Optional[List[Any]] = None,
    min_confidence: float = 0.50,
) -> Tuple[str, float]:
    """
    Detect the format of a file using content sniffing and extension matching.

    Content sniffing takes precedence over file extension when they disagree.

    Returns:
        (format_id, confidence)
    """
    path = Path(path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"File not found for format detection: {path}")

    if path.stat().st_size == 0:
        return "unknown", 0.0

    # Lazy import registry if adapters not explicitly provided
    if adapters is None:
        from formats.registry import AdapterRegistry
        adapters = AdapterRegistry.list_adapters()

    best_format = "unknown"
    best_score = 0.0

    ext_lower = path.name.lower()
    # Handle composite extensions like .csv.gz, .ndjson.gz
    is_gz = ext_lower.endswith(".gz")

    for adapter in adapters:
        # 1. Content sniff score
        try:
            sniff_score = adapter.sniff(path)
        except Exception:
            sniff_score = 0.0

        # 2. Extension match boost
        ext_match = False
        for ext in adapter.extensions:
            if ext_lower.endswith(ext):
                ext_match = True
                break

        # Calculate final confidence:
        # High content sniff score is trusted above all
        if sniff_score >= 0.85:
            final_score = sniff_score
        elif ext_match and sniff_score >= 0.30:
            final_score = min(1.0, sniff_score + 0.35)
        elif ext_match:
            final_score = 0.60
        else:
            final_score = sniff_score

        if final_score > best_score:
            best_score = final_score
            best_format = adapter.format_id

    if best_score < min_confidence:
        return "unknown", best_score

    return best_format, best_score
