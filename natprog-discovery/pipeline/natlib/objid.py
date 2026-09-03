"""object_id normalization and content hashing.

object_id is LIBRARY/NAME (uppercase) everywhere in this pipeline's own
output — SCHEMAS.md section 0. This is the one place that builds it and
the one place that defines sha256_norm (SCHEMAS.md section 6); no other
program in the pipeline re-implements either.
"""
from __future__ import annotations

import hashlib
import re

_WS_RUN = re.compile(r"[ \t]+")


def normalize_object_id(library: str, name: str) -> str:
    lib = (library or "").strip().upper()
    nm = (name or "").strip().upper()
    return f"{lib}/{nm}"


def split_object_id(object_id: str) -> tuple[str, str] | None:
    """Inverse of normalize_object_id(). Returns None for a legacy
    (name-only) id with no '/' — see SCHEMAS.md section 0 on how callers
    must handle that case (ambiguous_match, not a silent guess)."""
    if "/" not in object_id:
        return None
    lib, _, nm = object_id.partition("/")
    return lib, nm


def normalize_source(text: str) -> str:
    """SCHEMAS.md section 6: trim each line on both ends, collapse internal
    whitespace runs to a single space, drop empty lines, join with \\n.

    Leading whitespace is stripped along with trailing: for a dedup hash
    there is no principled reason indentation is "real" while trailing
    padding is "incidental" — both are exactly the kind of formatting
    noise a copy-before-you-change duplicate (README.md's RC/RC1.../RCOLD
    family) can pick up without the underlying program actually differing.
    """
    out_lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip(" \t")
        line = _WS_RUN.sub(" ", line)
        if line != "":
            out_lines.append(line)
    return "\n".join(out_lines)


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_raw(text: str) -> str:
    return sha256_of(text)


def sha256_norm(text: str) -> str:
    return sha256_of(normalize_source(text))
