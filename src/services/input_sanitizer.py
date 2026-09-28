"""Template adaptation of ingest-file-sanitizer quality policy."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.ingest_file_sanitizer import (
    TextQualityPolicy,
    assess_text_quality,
    sanitize_file_bytes,
    sniff_channel,
)


def reject_binary_upload(raw: bytes) -> str | None:
    """Reject PDF/xlsx by magic bytes; declarations are intentionally ignored."""

    channel = sniff_channel(raw)
    if raw.startswith(b"%PDF-") or channel == "xlsx":
        return "binary_format_rejected"
    return None


def text_quality_reason(text: str, config: Mapping[str, Any]) -> str | None:
    """NFKC quality gate for text that will enter the precedent index."""

    if not isinstance(text, str):
        return "text_not_string"
    if len(text) > int(config.get("max_body_chars", 4000)):
        return "text_too_long"
    accepted, reason = assess_text_quality(
        text,
        TextQualityPolicy(
            min_text_chars=int(config.get("min_body_chars", 20)),
            min_printable_ratio=float(config.get("min_readable_char_ratio", 0.80)),
            max_replacement_ratio=float(config.get("max_mojibake_ratio", 0.005)),
            max_control_ratio=float(config.get("max_control_char_ratio", 0.001)),
            min_signal_ratio=0.15,
        ),
    )
    if accepted:
        return None
    return {
        "empty_or_too_short": "text_too_short",
        "replacement_char_ratio": "text_mojibake",
        "control_char_ratio": "text_control_chars",
        "printable_ratio": "text_unreadable",
        "low_text_signal": "text_unreadable",
    }.get(reason or "", "text_unreadable")


__all__ = ["reject_binary_upload", "sanitize_file_bytes", "text_quality_reason"]
