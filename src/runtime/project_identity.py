"""Canonical project identity shared by contracts, ledgers, grants, and archives."""

from __future__ import annotations

import unicodedata


def canonical_project_id(value: str) -> str:
    """Return the stable comparison/storage form of an explicit project id."""
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()
