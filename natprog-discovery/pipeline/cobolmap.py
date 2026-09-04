#!/usr/bin/env python3
"""Stage 4.2 (WORKPLAN.md): extract the COBOL/CICS call graph from a COBOL
folder, and bridge it to Natural's `CALL '<x>'` (3GL) call sites.

Parsing logic (PROGRAM-ID, the CICS declaration flag, and the four ways one
COBOL program references another) is ported 1:1 from app.js parseCobol()/
analyzeCobol() (natprog-discovery/app.js ~1656-1717) -- already confirmed
against two real files, one plain batch and one CICS transaction
(README.md "COBOL/CICS call graph"):

    01 SAP-OPTIONS TPMONITOR UTP-CICS.        -> this file is a CICS program
    CALL 'NAME' USING ...                     -> kind 'call'
    EXEC CICS LINK PROGRAM('NAME') ...        -> kind 'cics-link'
    EXEC CICS XCTL PROGRAM('NAME') ...        -> kind 'cics-xctl'
    EXEC CICS START TRANSID('NAME') ...       -> kind 'cics-start' (a TRANSID,
                                                  not a PROGRAM-ID -- reported
                                                  separately, never "unresolved")

Like app.js, resolution cross-checks against the folder itself: every file's
PROGRAM-ID becomes the ground truth, and every call/link/xctl target is
looked up against that same set.

natural_bridge[] (SCHEMAS.md section 4) is NEW -- not present in app.js.
MERGE-PLAN.md section 4.5 calls this cobolmap.py's whole added value: today
app.js's external3gl() can only count CALL3GL targets that stayed
unresolved (no Natural source file has that name); nothing checks whether
an "unresolved" target is actually a COBOL program sitting right here. That
check needs one thing app.js's parseCobol() never sees: actual Natural
source text. natmap3.py (WORKPLAN.md stage 3, not built yet) is what will
extract every CALL3GL *edge* (which Natural object calls what); until then,
this program does its own narrow, single-pattern scan over
out/source/**/*.nat (objects.jsonl's source_path, stage 1.1's already-run
output) for the one pattern relevant to this bridge -- literal `CALL '<x>'`
-- and records every case where <x> matches a PROGRAM-ID found in the COBOL
folder. This is intentionally not a general call-graph extraction (that
stays natmap3.py's job): it reuses the exact same comment-skipping rule
app.js's Analyzer.feedSource() applies to Natural source (a line starting
with '*' in column 1 is a comment -- see app.js ~708-727) and the exact
CALL3GL regex (app.js RE.call3gl, ~424-431), applied case-insensitively so
both fields are meaningful: `cobol_program` is the canonical (uppercase)
PROGRAM-ID it resolved to, `natural_call_target` is the literal text
between quotes as it appeared in the Natural source (letting a same-name
different-case mismatch still show up instead of being silently erased).

SCHEMAS.md section 4 documents natural_bridge[] only as {cobol_program,
natural_call_target} -- it does not say how those two fields are derived,
because nothing implements it yet. This is this program's interpretation;
two extra fields (natural_object_id, natural_source_path) are added beyond
the documented minimum for auditability, the same way jclmap.py added
steplib_chains[] beyond app.js's original shape -- cobol.json is a brand
new file nothing downstream reads yet, so this is additive, not breaking.
Flag this interpretation for confirmation rather than silently trusting it.

The bridge pass is optional and additive: if objects.jsonl isn't found (or
--skip-bridge is passed), cobol.json is still written with programs[],
calls[], and cics_starts[] -- just with natural_bridge: [] and the reason
recorded in the printed summary, never a hard failure.

Output (relative to --out-dir):
  cobol.json   one artifact, schema: SCHEMAS.md section 4
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time
from collections import Counter

from natlib import config as nat_config
from natlib import encoding as natenc

# Case-insensitive, matching app.js parseCobol()'s own /i-flagged regexes --
# applied directly to the raw (not upper-cased) line, target upper()'d
# individually. This differs from the Natural-source side further below,
# which app.js upper-cases a whole line at a time before matching.
_COBOL_COMMENT_RE = re.compile(r"^\s*\*")  # COBOL column-7 '*' (leading spaces allowed)
_PROGRAM_ID_RE = re.compile(r"PROGRAM-ID\.\s*([A-Z0-9#$@\-]+)", re.IGNORECASE)
_CICS_FLAG_RE = re.compile(r"TPMONITOR\s+UTP-CICS", re.IGNORECASE)
_CALL_RE = re.compile(r"\bCALL\s+'([^']+)'", re.IGNORECASE)
_LINK_RE = re.compile(r"EXEC\s+CICS\s+LINK\s+PROGRAM\(['\"]?([A-Z0-9#$@\-]+)['\"]?\)", re.IGNORECASE)
_XCTL_RE = re.compile(r"EXEC\s+CICS\s+XCTL\s+PROGRAM\(['\"]?([A-Z0-9#$@\-]+)['\"]?\)", re.IGNORECASE)
_START_RE = re.compile(r"EXEC\s+CICS\s+START\s+TRANSID\s*\(\s*'([^']+)'", re.IGNORECASE)

# Natural-source side of the bridge only: app.js RE.call3gl (app.js ~431),
# applied case-insensitively here (app.js relies on upper-casing the whole
# line first; this program keeps the raw match instead -- see module
# docstring on natural_call_target vs cobol_program).
_NATURAL_CALL3GL_RE = re.compile(r"\bCALL\s+'([^']+)'", re.IGNORECASE)


def parse_cobol(text: str, file_name: str) -> dict:
    """Ported from app.js parseCobol(). Returns {file, program_id,
    uses_cics, calls: [{kind, target}]}."""
    lines = re.split(r"\r\n|\n", text)
    program_id = None
    uses_cics = False
    calls: list[dict] = []
    for line in lines:
        if _COBOL_COMMENT_RE.match(line):
            continue
        if program_id is None:
            m = _PROGRAM_ID_RE.search(line)
            if m:
                program_id = m.group(1).upper()
        if _CICS_FLAG_RE.search(line):
            uses_cics = True

        for m in _CALL_RE.finditer(line):
            calls.append({"kind": "call", "target": m.group(1).upper()})
        for m in _LINK_RE.finditer(line):
            calls.append({"kind": "cics-link", "target": m.group(1).upper()})
        for m in _XCTL_RE.finditer(line):
            calls.append({"kind": "cics-xctl", "target": m.group(1).upper()})
        for m in _START_RE.finditer(line):
            calls.append({"kind": "cics-start", "target": m.group(1).upper()})

    return {"file": file_name, "program_id": program_id, "uses_cics": uses_cics, "calls": calls}


def analyze_cobol(parsed: list[dict]) -> dict:
    """Ported from app.js analyzeCobol(). Cross-checks against the folder
    itself: every file's program_id is the ground truth for every other
    file's call/cics-link/cics-xctl target. cics-start targets are TRANSIDs,
    not PROGRAM-IDs, so they're reported separately and never counted as
    unresolved (matches app.js: `resolvable = allCalls.filter(kind !==
    'cics-start')`)."""
    program_index = {p["program_id"] for p in parsed if p["program_id"]}

    all_calls = []
    for p in parsed:
        for c in p["calls"]:
            all_calls.append({"file": p["file"], "from": p["program_id"], "kind": c["kind"], "target": c["target"]})

    resolvable = [c for c in all_calls if c["kind"] != "cics-start"]
    rows = []
    resolved = 0
    for c in resolvable:
        found = c["target"] in program_index
        if found:
            resolved += 1
        rows.append({**c, "found_in_folder": found})

    cics_start_rows = [dict(c) for c in all_calls if c["kind"] == "cics-start"]

    return {
        "program_index": program_index,
        "all_calls": all_calls,
        "resolvable_rows": rows,
        "cics_start_rows": cics_start_rows,
        "resolution": {"resolved": resolved, "unresolved": len(resolvable) - resolved, "total": len(resolvable)},
    }


def build_cobol_json(parsed: list[dict], analysis: dict, natural_bridge: list[dict]) -> dict:
    """The cobol.json artifact itself (SCHEMAS.md section 4)."""
    programs = [{"file": p["file"], "program_id": p["program_id"], "uses_cics": p["uses_cics"]} for p in parsed]
    calls = [
        {"file": r["file"], "from": r["from"], "kind": r["kind"], "target": r["target"], "found_in_folder": r["found_in_folder"]}
        for r in analysis["resolvable_rows"]
    ]
    cics_starts = [{"file": r["file"], "from": r["from"], "target": r["target"]} for r in analysis["cics_start_rows"]]
    return {
        "programs": programs,
        "calls": calls,
        "cics_starts": cics_starts,
        "natural_bridge": natural_bridge,
    }


def _top_n(counter: Counter, n: int) -> list[list]:
    items = sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return [[k, v] for k, v in (items[:n] if n else items)]


def build_summary(parsed: list[dict], analysis: dict) -> dict:
    """Headline counts computed the same way app.js analyzeCobol() computes
    them, so this can be cross-checked directly against a real
    discovery-log.json's `cobol` section (written by app.js buildReport()
    whenever a COBOL folder was loaded alongside -- app.js ~2330) -- the
    same cross-check pattern WORKPLAN.md 1.3 established for
    natunload_split.py and 4.1 used for jclmap.py, applied here.

    programs_with_id vs distinct_program_ids -- found the hard way cross-
    checking against a real 323-file folder (WORKPLAN.md 4.2): app.js's
    `programsWithId` is `programIndex.size`, a Set of PROGRAM-ID *values* --
    i.e. how many distinct program names exist, not how many files declare
    one. When several files declare the same PROGRAM-ID (a real, common
    case: copy-before-you-change duplicates, same pattern README.md already
    documents for the Natural side), that Set undercounts files. Both
    numbers are real and useful; only distinct_program_ids is the one
    comparable to app.js's field of the (misleading) same-ish name."""
    all_calls = analysis["all_calls"]
    by_kind: Counter = Counter(c["kind"] for c in all_calls)
    by_target: Counter = Counter(c["target"] for c in all_calls)
    cics_programs = sum(1 for p in parsed if p["uses_cics"])
    program_ids = [p["program_id"] for p in parsed if p["program_id"]]

    return {
        "files_parsed": len(parsed),
        "programs_with_id": len(program_ids),
        "distinct_program_ids": len(set(program_ids)),
        "cics_programs": cics_programs,
        "total_calls": len(all_calls),
        "by_kind": _top_n(by_kind, 10),
        "distinct_targets": len(by_target),
        "top_targets_top20": _top_n(by_target, 20),
        "resolution": analysis["resolution"],
        "cics_starts_count": len(analysis["cics_start_rows"]),
    }


def extract_natural_bridge(
    objects_path: pathlib.Path,
    out_dir: pathlib.Path,
    program_index: set,
    progress_every: int = 2000,
    progress_stream=None,
) -> dict:
    """The bridge itself -- see module docstring. Scans every Natural source
    file listed in objects.jsonl for literal `CALL '<x>'` sites and keeps
    the ones where <x> resolves (case-insensitively) to a COBOL program
    found in this run's --cobol-dir. Never raises: a missing objects.jsonl,
    or a missing/unreadable individual source file, is reported in the
    result rather than failing the whole run -- this pass is additive.

    This is one open()+read() per object -- for a real ~80K-object estate
    that's tens of thousands of individual file operations, which on
    Windows (small-file open/close overhead, often worse under real-time
    antivirus scanning) can genuinely take several minutes even though
    nothing is wrong. progress_stream exists specifically so that time is
    never silent: pass sys.stderr (main() does) to get a line up front and
    one every `progress_every` objects -- a real, if slow, run must never
    look indistinguishable from a hang."""
    result = {
        "enabled": False,
        "skipped_reason": None,
        "objects_scanned": 0,
        "read_errors": [],
        "matches": [],
    }
    if not program_index:
        result["skipped_reason"] = "no COBOL PROGRAM-ID found in --cobol-dir; nothing to bridge to"
        return result
    if not objects_path.is_file():
        result["skipped_reason"] = f"objects.jsonl not found: {objects_path}"
        return result

    result["enabled"] = True

    def log(msg: str) -> None:
        if progress_stream is not None:
            print(msg, file=progress_stream, flush=True)

    with objects_path.open(encoding="utf-8") as fh:
        total = sum(1 for _ in fh)
    log(f"[cobolmap] natural_bridge: scanning {total} objects from {objects_path} (this reads one Natural "
        f"source file per object -- can take a while on Windows with many objects) ...")

    t0 = time.time()
    with objects_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            result["objects_scanned"] += 1
            src_rel = row.get("source_path")
            if src_rel:
                src_path = out_dir / src_rel
                try:
                    text = src_path.read_text(encoding="utf-8")
                except OSError as e:
                    result["read_errors"].append({"object_id": row.get("object_id"), "error": str(e)})
                    text = None
                if text is not None:
                    for raw_line in text.split("\n"):
                        if raw_line.startswith("*"):  # Natural comment: column-1 '*', app.js ~722
                            continue
                        code = raw_line
                        ci = code.find("/*")
                        if ci >= 0:
                            code = code[:ci].rstrip()
                        if not code:
                            continue
                        if "CALL '" not in code.upper():  # cheap gate before running the regex
                            continue
                        for m in _NATURAL_CALL3GL_RE.finditer(code):
                            raw_target = m.group(1)
                            canon = raw_target.upper()
                            if canon in program_index:
                                result["matches"].append({
                                    "cobol_program": canon,
                                    "natural_call_target": raw_target,
                                    "natural_object_id": row.get("object_id"),
                                    "natural_source_path": src_rel,
                                })
            if result["objects_scanned"] % progress_every == 0:
                log(f"[cobolmap] natural_bridge: {result['objects_scanned']}/{total} objects "
                    f"({time.time() - t0:.1f}s elapsed, {len(result['matches'])} matches so far)")

    log(f"[cobolmap] natural_bridge: done -- {result['objects_scanned']}/{total} objects in "
        f"{time.time() - t0:.1f}s, {len(result['matches'])} matches, {len(result['read_errors'])} read errors")
    return result


def read_text_auto(path: pathlib.Path, encoding_opt: str) -> str:
    raw = path.read_bytes()
    if encoding_opt == "auto":
        sniff = natenc.sniff_encoding(raw)
        enc = sniff["guess"]
    else:
        enc = encoding_opt
    return natenc.decode(enc, raw)


def _resolve(base_dir: pathlib.Path, value: str | None) -> pathlib.Path | None:
    if not value:
        return None
    p = pathlib.Path(value)
    return p if p.is_absolute() else (base_dir / p)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to config.yaml (default: pipeline/config.yaml)")
    parser.add_argument("--cobol-dir", help="Overrides config paths.cobol_dir")
    parser.add_argument("--out-dir", help="Overrides config paths.out_dir")
    parser.add_argument("--encoding", default="auto",
                         help="Encoding for COBOL files, or 'auto' to sniff each file (default auto)")
    parser.add_argument("--objects-file", help="Overrides <out-dir>/objects.jsonl for the natural_bridge pass")
    parser.add_argument("--skip-bridge", action="store_true",
                         help="Skip the natural_bridge pass even if objects.jsonl is found (faster iteration)")
    args = parser.parse_args(argv)

    config_path = pathlib.Path(args.config) if args.config else nat_config.DEFAULT_CONFIG_PATH
    try:
        cfg = nat_config.load(config_path)
    except nat_config.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    base_dir = config_path.resolve().parent

    cobol_dir = _resolve(base_dir, args.cobol_dir) or _resolve(base_dir, cfg["paths"].get("cobol_dir"))
    if not cobol_dir:
        print("error: no COBOL directory given (pass --cobol-dir or set paths.cobol_dir in config.yaml)", file=sys.stderr)
        return 1
    if not cobol_dir.is_dir():
        print(f"error: COBOL directory not found: {cobol_dir}", file=sys.stderr)
        return 1

    encoding_opt = args.encoding
    if encoding_opt != "auto" and encoding_opt not in natenc.ENCODINGS:
        print(f"error: unknown encoding {encoding_opt!r} (expected 'auto' or one of {natenc.ENCODINGS})", file=sys.stderr)
        return 1

    out_dir = _resolve(base_dir, args.out_dir) or _resolve(base_dir, cfg["paths"].get("out_dir")) or (base_dir / "out")
    out_dir.mkdir(parents=True, exist_ok=True)
    objects_path = _resolve(base_dir, args.objects_file) or (out_dir / "objects.jsonl")

    t0 = time.time()
    files = sorted(p for p in cobol_dir.rglob("*") if p.is_file())
    print(f"[cobolmap] found {len(files)} files under {cobol_dir}, parsing...", file=sys.stderr, flush=True)
    parsed = []
    read_errors = []
    for i, fp in enumerate(files, 1):
        try:
            text = read_text_auto(fp, encoding_opt)
        except (ValueError, UnicodeDecodeError) as e:
            read_errors.append({"file": fp.name, "error": str(e)})
            continue
        parsed.append(parse_cobol(text, fp.name))
        if i % 2000 == 0:
            print(f"[cobolmap] parsed {i}/{len(files)} COBOL files...", file=sys.stderr, flush=True)
    print(f"[cobolmap] COBOL folder done: {len(parsed)} files parsed, {len(read_errors)} read errors",
          file=sys.stderr, flush=True)

    analysis = analyze_cobol(parsed)

    bridge = {"enabled": False, "skipped_reason": "--skip-bridge passed", "objects_scanned": 0, "read_errors": [], "matches": []}
    if not args.skip_bridge:
        bridge = extract_natural_bridge(objects_path, out_dir, analysis["program_index"], progress_stream=sys.stderr)

    cobol_json = build_cobol_json(parsed, analysis, bridge["matches"])
    out_path = out_dir / "cobol.json"
    out_path.write_text(json.dumps(cobol_json, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = build_summary(parsed, analysis)
    summary["elapsed_seconds"] = round(time.time() - t0, 3)
    summary["out_file"] = str(out_path)
    summary["natural_bridge"] = {
        "enabled": bridge["enabled"],
        "skipped_reason": bridge["skipped_reason"],
        "objects_scanned": bridge["objects_scanned"],
        "matches_found": len(bridge["matches"]),
        "read_errors": len(bridge["read_errors"]),
    }
    if read_errors:
        summary["read_errors"] = read_errors
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
