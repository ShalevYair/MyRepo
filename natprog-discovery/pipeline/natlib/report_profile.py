"""SYSOBJH job-log/print-report profile, ported from app.js
NAT_REPORT_PROFILE / isReportDataRow / sniffFileProfile
(natprog-discovery/app.js lines ~363-399). Fixed 132-char lines, CRLF;
column 1 is an IBM/ASA print-carriage-control character, not payload.
See README.md "Record layout used: job-log report" for the field table
this mirrors.
"""
from __future__ import annotations

import re

from .natprofile import fld, rtrim  # noqa: F401  (rtrim re-exported for callers)

REPORT_TYPE_TO_LETTER = {
    "PROGRAM": "F", "SUBPROGRAM": "N", "MAP": "M", "LOCAL": "L", "PARAMETER": "P",
    "GLOBAL": "C", "TEXT": "T", "COPYCODE": "G", "SUBROUTINE": "S",
    "HELPROUTINE": "H", "DDM": "V",
}

# field name -> (offset, length)
ROW = {
    "status": (1, 30), "library": (32, 8), "name": (41, 32), "type": (74, 11),
    "sc": (86, 3), "dbidfnr": (90, 11), "date": (102, 10), "time": (113, 8),
    "user": (122, 8), "flag": (131, 1),
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_report_data_row(line: str) -> bool:
    """Structural fingerprint of a data row: works regardless of STATUS
    text (UNLOADED/ERROR/whatever), so a failed row is still recognised."""
    if len(line) < 112 or (line[0] if line else "") != " ":
        return False
    offset, length = ROW["date"]
    return bool(_DATE_RE.match(line[offset : offset + length]))


def sniff_file_profile(text: str) -> str:
    """Returns 'natural-sysobjh' (raw unload), 'natural-sysobjh-report'
    (job-log report), or 'none'. Ported from app.js sniffFileProfile()."""
    lines = re.split(r"\r\n|\n", text)
    raw_tags = 0
    report_rows = 0
    has_banner = False
    for line in lines[:2000]:
        if line.startswith("*H**") or line.startswith("*C**"):
            raw_tags += 1
        elif is_report_data_row(line):
            report_rows += 1
        if "NATURAL OBJECT HANDLER" in line:
            has_banner = True
    if raw_tags >= 1:
        return "natural-sysobjh"
    if report_rows >= 2 or has_banner:
        return "natural-sysobjh-report"
    return "none"
