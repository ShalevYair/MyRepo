"""Raw-unload record layout, ported from app.js NAT_PROFILE
(natprog-discovery/app.js lines ~271-345) and fld()/parseNatTs()
(same file, ~400-420). Offsets are 0-based character offsets applied
AFTER decoding, exactly as documented in README.md "Record layout used:
raw unload" — that table is the same data as TYPE_MAP below.

Confidence values ('confirmed' | 'inferred' | 'guess' | 'none') come
from cross-referencing real source bodies against a real SYSOBJH
job-log report — see README.md "Object type letters" for the evidence
behind each one. Do not add a new letter here from a guess; app.js and
this file must describe the same reality.
"""
from __future__ import annotations

RECORD_PAD = 12          # every record length observed as a multiple of 12 chars
PREFIX_LEN = 4
KNOWN_TAGS = ("*H**", "*C**", "*D01", "*D02", "*D03", "*D04", "*S**")

# field name -> (offset, length)
H = {
    "flag": (4, 1), "prod": (5, 3), "version": (8, 4), "timestamp": (12, 15),
    "os": (27, 8), "user_or_node": (60, 8), "version_text": (70, 4),
}
C = {
    "library": (36, 8), "name": (44, 32), "type": (76, 1),
    "src_obj": (78, 1), "flag_n": (82, 1), "num": (84, 4),
}
D01 = {
    "prod": (4, 3), "version": (7, 4), "type": (11, 1),
    "library": (13, 8), "name": (21, 32),
    "user1": (53, 8), "user2": (61, 8), "user3": (69, 8), "flag_s": (77, 1),
}
D02 = {"saved_ts": (16, 15), "cataloged_ts": (31, 15), "size": (46, 10)}
D03 = {"os": (4, 8), "tp_monitor": (12, 8), "tp_extra": (20, 8)}
D04 = {"codepage": (21, 8)}

# letter -> {name, kind, confidence, evidence} — ported verbatim from
# app.js NAT_PROFILE.typeMap. kind in {'exec','map','data','text', None}.
TYPE_MAP = {
    "F": {"name": "Program", "kind": "exec", "confidence": "confirmed",
          "evidence": "DEFINE DATA + executable statements; report confirms A/ASKZBTP1 -> PROGRAM."},
    "N": {"name": "Subprogram", "kind": "exec", "confidence": "confirmed",
          "evidence": 'Bodies self-describe as "SUBPROGRAM : <name>"; report confirms.'},
    "S": {"name": "Subroutine", "kind": "exec", "confidence": "confirmed",
          "evidence": 'Bodies self-describe as "SUBROUTINE"; report confirms.'},
    "M": {"name": "Map", "kind": "map", "confidence": "confirmed",
          "evidence": "Map prototype header + DEFINE DATA PARAMETER; report confirms."},
    "L": {"name": "Local Data Area", "kind": "data", "confidence": "confirmed",
          "evidence": "Internal data-area format (**DF/**DR/**C); report confirms."},
    "P": {"name": "Parameter Data Area", "kind": "data", "confidence": "confirmed",
          "evidence": 'Data-area format; bodies say "PARAMETER : <name>"; report confirms.'},
    "C": {"name": "Global Data Area", "kind": "data", "confidence": "confirmed",
          "evidence": "Data-area format; report confirms (ADLDMF/ADBGLOBA -> GLOBAL)."},
    "T": {"name": "Text", "kind": "text", "confidence": "confirmed",
          "evidence": "Free-form text, no Natural syntax; report confirms."},
    "G": {"name": "Copycode", "kind": "exec", "confidence": "inferred",
          "evidence": "NOT Global Data Area. COPYCODE:GLOBAL report ratio matches this scan's G:C "
                      "ratio once C is pinned to GLOBAL; most declared_type_vs_body_mismatch samples "
                      "are type G with an executable-looking body."},
    "H": {"name": "Helproutine", "kind": "exec", "confidence": "inferred",
          "evidence": 'Confirmed as a real type via report ("HELPROUTINE"); body shape not '
                      "independently verified."},
    "A": {"name": "Parameter Data Area?", "kind": "data", "confidence": "guess", "evidence": "not seen in sample"},
    "4": {"name": "Class?", "kind": "exec", "confidence": "guess", "evidence": "not seen in sample"},
    "8": {"name": "Adapter?", "kind": "exec", "confidence": "guess", "evidence": "not seen in sample"},
    "7": {"name": "UNKNOWN", "kind": None, "confidence": "none",
          "evidence": "Seen in a real 770MB scan but no matching report row. Needs a source-body sample."},
    "5": {"name": "UNKNOWN", "kind": None, "confidence": "none",
          "evidence": "Seen once in a real 770MB scan. Needs a source-body sample."},
    "V": {"name": "DDM (Adabas field layout)", "kind": "data", "confidence": "confirmed",
          "evidence": "Report cross-reference confirms (SYSTEM/ACCOUNTING -> DDM). Field layout of one "
                      "physical Adabas file, not executable Natural."},
}


def rtrim(s: str) -> str:
    """Right-trim ASCII spaces only — matches app.js rtrim() (hot path,
    not Python's str.rstrip(), which would also strip other whitespace)."""
    e = len(s)
    while e > 0 and s[e - 1] == " ":
        e -= 1
    return s[:e]


def fld(line: str, spec: tuple[int, int]) -> str:
    """Extract and right-trim a fixed-offset field. Ported from app.js fld()."""
    offset, length = spec
    return rtrim(line[offset : offset + length])


def parse_nat_ts(s: str) -> dict:
    """Natural timestamp YYYYMMDDHHMMSSt (t = tenths of a second).
    Ported from app.js parseNatTs()."""
    if len(s) != 15 or not s.isdigit():
        return {"ok": False, "empty": False, "raw": s}
    if s == "000000000000000":
        return {"ok": False, "empty": True, "raw": s}
    y, mo, d = int(s[0:4]), int(s[4:6]), int(s[6:8])
    h, mi, se = int(s[8:10]), int(s[10:12]), int(s[12:14])
    if not (1960 <= y <= 2100) or not (1 <= mo <= 12) or not (1 <= d <= 31) or h > 23 or mi > 59 or se > 59:
        return {"ok": False, "empty": False, "raw": s}
    return {
        "ok": True, "empty": False, "raw": s, "year": y,
        "iso": f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[8:10]}:{s[10:12]}:{s[12:14]}",
    }
