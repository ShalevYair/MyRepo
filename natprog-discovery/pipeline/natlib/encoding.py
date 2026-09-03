"""Encoding detection and decoding, ported from app.js (CP_TABLES,
sniffEncoding, makeDecoder — natprog-discovery/app.js lines ~24-238).

That code is validated against a real 770 MB scan (README.md "Findings so
far"). This module must stay behaviorally identical to it; pipeline/tests
covers the byte tables, and pipeline/validate.py (WORKPLAN.md stage 1.3)
covers the end-to-end result against the browser scanner on the same file.

Four of the seven candidate encodings are native Python codecs with no
porting needed:
  - utf-8            -> 'utf-8'
  - windows-1255      -> 'cp1255'
  - iso-8859-8        -> 'iso8859_8'
  - latin1            -> 'latin-1'  (Python's is the true ISO-8859-1 identity
                          mapping already; app.js had to hand-build this one
                          because JS's TextDecoder('latin1') is actually an
                          alias for windows-1252 per the WHATWG spec, which
                          remaps 0x80-0x9F. Python has no such alias problem.)

The other three are single-byte mainframe codepages built from Python's
own `codecs` module (README.md: "generated from Python's codecs module
rather than typed by hand" — that description applies here even more
directly than to the JS port, since this IS that generation):
  - cp862  -> byte-identical to Python's builtin 'cp862' codec.
  - cp037  -> byte-identical to Python's builtin 'cp037' codec.
  - cp424  -> Python's builtin 'cp424' codec raises on 38 byte values it
              doesn't define; those map to U+FFFD here, which is a verified
              byte-for-byte match to app.js's embedded CP_TABLES.cp424
              string (extracted and diffed against this module's output
              while writing it — see the git history for the check).
"""
from __future__ import annotations

ENCODINGS = ("utf-8", "windows-1255", "iso-8859-8", "latin1", "cp862", "cp424", "cp037")

_PY_CODEC = {
    "utf-8": "utf-8",
    "windows-1255": "cp1255",
    "iso-8859-8": "iso8859_8",
    "latin1": "latin-1",
}

_TABLE_CODECS = ("cp862", "cp424", "cp037")


def _build_table(codec_name: str) -> str:
    """256-char string: table[byte] = the Unicode character that byte
    decodes to under `codec_name`, or U+FFFD if that byte is undefined
    in the codepage (matches app.js's CP424 table exactly for this case)."""
    chars = []
    for b in range(256):
        try:
            chars.append(bytes((b,)).decode(codec_name))
        except UnicodeDecodeError:
            chars.append("�")
    return "".join(chars)


_TABLES = {name: _build_table(name) for name in _TABLE_CODECS}


def decode(encoding: str, data: bytes) -> str:
    """Decode `data` under `encoding` (one of ENCODINGS)."""
    if encoding in _TABLES:
        table = _TABLES[encoding]
        return "".join(table[b] for b in data)
    codec = _PY_CODEC.get(encoding)
    if codec is None:
        raise ValueError(f"unknown encoding: {encoding!r} (expected one of {ENCODINGS})")
    return data.decode(codec, errors="replace")


def _hebrew_bytes_of(table_name: str) -> list[int]:
    """Byte values that carry Hebrew letters (U+05D0..U+05EA) in a given
    EBCDIC table. Computed from the table so it stays correct if the
    table changes — ported from app.js hebrewBytesOf()."""
    table = _TABLES.get(table_name, "")
    return [b for b, ch in enumerate(table) if 0x05D0 <= ord(ch) <= 0x05EA]


CP424_HEBREW_BYTES = _hebrew_bytes_of("cp424")


def sniff_encoding(sample: bytes) -> dict:
    """Port of app.js sniffEncoding(). Runs on a sample block (the caller
    decides how much — natunload_split.py uses config.yaml scan.sniff_bytes,
    matching app.js's SNIFF=4MiB) and returns a guess plus the evidence
    behind it, not just a bare label."""
    n = len(sample)
    if n == 0:
        return {
            "guess": "utf-8", "why": "empty sample", "sampled_bytes": 0,
            "utf8_valid": True, "replacement_seq_in_sample": 0,
            "ebcdic_hebrew_byte_ratio": 0.0,
            "counts": {"ascii": 0, "high": 0, "ctrl": 0, "nul": 0, "lf": 0, "cr": 0, "crlf": 0},
            "ratios": {"ascii_printable": 0.0, "high_bytes": 0.0, "ascii_space": 0.0, "ebcdic_space": 0.0},
            "line_ending": "none found", "ebcdic_nl_bytes": 0,
        }

    ascii_ = high = ctrl = nul = lf = cr = crlf = 0
    ebcdic_space = ebcdic_nl = ascii_space = 0
    freq = [0] * 256

    prev = -1
    for b in sample:
        freq[b] += 1
        if b == 0:
            nul += 1
        if b == 0x0A:
            lf += 1
            if prev == 0x0D:
                crlf += 1
        if b == 0x0D:
            cr += 1
        if b == 0x20:
            ascii_space += 1
        if b == 0x40:
            ebcdic_space += 1
        if b == 0x15:
            ebcdic_nl += 1
        if b >= 0x80:
            high += 1
        elif 0x20 <= b < 0x7F:
            ascii_ += 1
        elif b not in (9, 10, 13):
            ctrl += 1
        prev = b

    try:
        sample[: min(n, 1 << 20)].decode("utf-8", errors="strict")
        utf8_valid = True
    except UnicodeDecodeError:
        utf8_valid = False

    # U+FFFD already baked into the bytes (EF BF BD) = a prior lossy conversion.
    fffd = 0
    i = 0
    limit = n - 2
    while i < limit:
        if sample[i] == 0xEF and sample[i + 1] == 0xBF and sample[i + 2] == 0xBD:
            fffd += 1
            i += 3
        else:
            i += 1

    space_ratio_ascii = ascii_space / n
    space_ratio_ebcdic = ebcdic_space / n

    looks_ebcdic = (
        space_ratio_ebcdic > 0.05
        and space_ratio_ebcdic > space_ratio_ascii * 5
        and high / n > 0.10
    )

    hebrew_ebcdic = sum(freq[b] for b in CP424_HEBREW_BYTES)
    hebrew_ratio = hebrew_ebcdic / n

    if looks_ebcdic:
        if hebrew_ebcdic >= 200 and hebrew_ratio > 0.0002:
            guess = "cp424"
            why = (
                f"EBCDIC detected: byte 0x40 is {space_ratio_ebcdic * 100:.1f}% of the sample "
                f"vs ASCII space {space_ratio_ascii * 100:.2f}%, {high / n * 100:.1f}% bytes >= 0x80. "
                f"{hebrew_ebcdic:,} bytes ({hebrew_ratio * 100:.3f}%) fall in the CP424 Hebrew range "
                "-> guessing CP424 (EBCDIC Hebrew)."
            )
        else:
            guess = "cp037"
            why = (
                f"EBCDIC detected: byte 0x40 is {space_ratio_ebcdic * 100:.1f}% of the sample "
                f"vs ASCII space {space_ratio_ascii * 100:.2f}%, {high / n * 100:.1f}% bytes >= 0x80. "
                f"Only {hebrew_ebcdic:,} bytes fall in the CP424 Hebrew range -> guessing CP037 "
                "(EBCDIC US). If Hebrew is expected, force cp424 and compare."
            )
    elif utf8_valid:
        guess = "utf-8"
        why = "Byte stream is valid UTF-8."
        if fffd:
            why += f" NOTE: {fffd} pre-existing U+FFFD replacement characters (data already lost upstream)."
    elif high / n > 0.005:
        guess = "windows-1255"
        why = f"Not valid UTF-8, {high / n * 100:.2f}% high bytes -> single-byte Hebrew codepage likely."
    else:
        guess = "latin1"
        why = "Not valid UTF-8 and almost no high bytes; falling back to byte-preserving Latin-1."

    if crlf > 0 and crlf >= lf * 0.9:
        line_ending = "CRLF"
    elif lf > 0:
        line_ending = "mixed (CRLF+LF)" if crlf else "LF"
    elif cr > 0:
        line_ending = "CR only"
    else:
        line_ending = "none found"

    return {
        "guess": guess, "why": why, "sampled_bytes": n,
        "utf8_valid": utf8_valid, "replacement_seq_in_sample": fffd,
        "ebcdic_hebrew_byte_ratio": round(hebrew_ratio, 5),
        "counts": {"ascii": ascii_, "high": high, "ctrl": ctrl, "nul": nul, "lf": lf, "cr": cr, "crlf": crlf},
        "ratios": {
            "ascii_printable": round(ascii_ / n, 4),
            "high_bytes": round(high / n, 4),
            "ascii_space": round(space_ratio_ascii, 4),
            "ebcdic_space": round(space_ratio_ebcdic, 4),
        },
        "line_ending": line_ending,
        "ebcdic_nl_bytes": ebcdic_nl,
    }
