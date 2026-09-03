#!/usr/bin/env python3
"""Stage 4.1 (WORKPLAN.md): extract Natural program entry points from a
JCL folder.

Parsing logic (job-card detection, EXEC PGM=/PROC branching, and the
Natural-batch CMSYNIN convention) is ported 1:1 from app.js parseJcl()/
analyzeJcl() (natprog-discovery/app.js ~1473-1591) -- already confirmed
against 5 real JCL jobs (README.md "JCL -> Natural program links"):

    //STEP2 EXEC NATB240,COND=(0,NE)
    //CMSYNIN DD *
    LOGON RC
    HICNEWN3
    FIN

A Natural-batch step never names the program on the EXEC line -- it runs a
shared PROC and hands the real library+program through in-stream CMSYNIN
input. The first content line names the library, either as "LOGON <lib>"
or a bare "<lib>" (both forms seen in the same real job); every line after
that up to FIN/end-of-data is a program name. An "EXEC PGM=xxx" step is
captured too (SORT/FTP-style utility, or an unrecognized name that's a
likely custom-program candidate), just without a library.

STEPLIB/NATLIB DD concatenation extraction (steplib_chains[]) is NEW --
not present in app.js. It records the raw dataset names z/OS sees, not a
resolved Natural library name: MERGE-PLAN.md section 8 question 2 (is
there a STEPLIB configuration at all?) is still an open question, so this
does not guess a DSN-to-Natural-library mapping.

Output (relative to --out-dir):
  jcl.json   one artifact, schema: SCHEMAS.md section 3
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

UTILITY_PGM_NAMES = {
    "SORT", "DFSORT", "ICETOOL", "IDCAMS", "IEBGENER", "IEBCOPY",
    "IEFBR14", "IKJEFT01", "IKJEFT1B", "FTP", "IEWL", "IEBPTPCH", "ICEMAN",
}

# Case-sensitive on purpose, matching app.js: JCL keywords (JOB, EXEC, PGM=)
# are conventionally all-uppercase and app.js's validated regexes rely on
# that. CMSYNIN/LOGON/FIN use /i in app.js, so those stay case-insensitive.
_JOB_RE = re.compile(r"^//#?(\S+)\s+JOB\b")
_EXEC_RE = re.compile(r"^//(\S+)\s+EXEC\s+(.+)$")
_PGM_RE = re.compile(r"^PGM=(\S+)")
_CMSYNIN_RE = re.compile(r"^//CMSYNIN\s+DD\s+\*", re.IGNORECASE)
_INSTREAM_END_RE = re.compile(r"^/\*\s*$")
_LOGON_RE = re.compile(r"^(?:LOGON\s+)?(\S+)", re.IGNORECASE)
_FIN_RE = re.compile(r"^FIN\b", re.IGNORECASE)

# New (not in app.js): STEPLIB/NATLIB DD concatenation.
_DD_RE = re.compile(r"^//(\S*)\s+DD\s+(.*)$")
_DSN_RE = re.compile(r"DSN(?:AME)?=([^,\s]+)", re.IGNORECASE)
_STEPLIB_DD_NAMES = ("STEPLIB", "NATLIB")


def parse_jcl(text: str, file_name: str) -> dict:
    """Ported from app.js parseJcl(). Returns job_name, steps[], and
    program_refs[] (each {file, step, kind, library, program, raw}) plus
    steplib_groups[] (new -- see module docstring)."""
    lines = re.split(r"\r\n|\n", text)
    job_name = None
    steps: list[dict] = []
    program_refs: list[dict] = []
    steplib_groups: list[dict] = []
    cur_step = None
    in_cmsynin = False
    cmsynin_lib = None
    cur_steplib_chain: list[str] | None = None

    def close_steplib_group() -> None:
        nonlocal cur_steplib_chain
        if cur_steplib_chain:
            steplib_groups.append({"step": cur_step, "library_order": cur_steplib_chain})
        cur_steplib_chain = None

    for line in lines:
        if in_cmsynin:
            if _INSTREAM_END_RE.match(line) or line.startswith("//"):
                in_cmsynin = False
                cmsynin_lib = None
                # fall through: this line may itself be a real JCL control line
            else:
                t = line.strip()
                if t:
                    if cmsynin_lib is None:
                        m = _LOGON_RE.match(t)
                        if m:
                            cmsynin_lib = m.group(1).upper()
                    elif not _FIN_RE.match(t) and t.upper() != cmsynin_lib:
                        program_refs.append({
                            "file": file_name, "step": cur_step, "kind": "natural-batch",
                            "library": cmsynin_lib, "program": t.split()[0], "raw": line,
                        })
                continue

        if not line.startswith("//"):
            close_steplib_group()
            continue
        if line.startswith("//*"):
            continue  # JCL comment (incl. commented-out steps)

        if job_name is None:
            jm = _JOB_RE.match(line)
            if jm:
                job_name = jm.group(1)

        em = _EXEC_RE.match(line)
        if em:
            close_steplib_group()
            cur_step = em.group(1)
            target = em.group(2).split(",")[0].strip()
            pm = _PGM_RE.match(target)
            if pm:
                steps.append({"step": cur_step, "kind": "pgm", "target": pm.group(1)})
                program_refs.append({
                    "file": file_name, "step": cur_step, "kind": "direct-pgm",
                    "library": None, "program": pm.group(1), "raw": line,
                })
            else:
                steps.append({"step": cur_step, "kind": "proc", "target": target})
            continue

        if _CMSYNIN_RE.match(line):
            close_steplib_group()
            in_cmsynin = True
            cmsynin_lib = None
            continue

        ddm = _DD_RE.match(line)
        if ddm:
            name, rest = ddm.group(1), ddm.group(2)
            if name.upper() in _STEPLIB_DD_NAMES:
                close_steplib_group()
                cur_steplib_chain = []
                dsn = _DSN_RE.search(rest)
                if dsn:
                    cur_steplib_chain.append(dsn.group(1))
                continue
            if name == "" and cur_steplib_chain is not None:
                dsn = _DSN_RE.search(rest)
                if dsn:
                    cur_steplib_chain.append(dsn.group(1))
                continue
            close_steplib_group()
            continue

        close_steplib_group()

    close_steplib_group()

    return {
        "file": file_name, "job_name": job_name, "steps": steps,
        "program_refs": program_refs, "steplib_groups": steplib_groups,
    }


def build_jcl_json(parsed: list[dict]) -> dict:
    """The jcl.json artifact itself (SCHEMAS.md section 3). entry_points
    excludes utility refs -- MERGE-PLAN.md 5.1: a recognized utility isn't
    a seed or a custom-code entry point."""
    jobs = []
    entry_points = []
    utility_refs = []
    steplib_chains = []

    for p in parsed:
        jobs.append({"file": p["file"], "job_name": p["job_name"], "steps": p["steps"]})

        for r in p["program_refs"]:
            if r["kind"] == "direct-pgm" and r["program"].upper() in UTILITY_PGM_NAMES:
                utility_refs.append({"jcl_file": r["file"], "step": r["step"], "program": r["program"]})
            else:
                entry_points.append({
                    "library": r["library"], "program": r["program"], "jcl_file": r["file"],
                    "step": r["step"], "kind": r["kind"], "resolved": None,
                })

        job_key = p["job_name"] or p["file"]
        for g in p["steplib_groups"]:
            steplib_chains.append({"job": job_key, "library_order": g["library_order"]})

    return {
        "jobs": jobs,
        "entry_points": entry_points,
        "steplib_chains": steplib_chains,
        "utility_refs": utility_refs,
    }


def _top_n(counter: Counter, n: int) -> list[list]:
    items = sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return [[k, v] for k, v in (items[:n] if n else items)]


def build_summary(parsed: list[dict]) -> dict:
    """Headline counts computed from ALL program refs (utilities included),
    the same way app.js analyzeJcl() computes them -- so this can be
    cross-checked directly against a real discovery-log.json's jcl section
    (WORKPLAN.md 1.3's cross-check pattern, applied here to stage 4.1)."""
    all_refs = [r for p in parsed for r in p["program_refs"]]
    by_kind: Counter = Counter(r["kind"] for r in all_refs)
    by_library: Counter = Counter(r["library"] for r in all_refs if r["kind"] == "natural-batch")
    by_program: Counter = Counter()
    for r in all_refs:
        if r["kind"] == "natural-batch":
            by_program[f"{r['library']}/{r['program']}"] += 1
        else:
            by_program[r["program"]] += 1

    direct_pgm_distinct = {r["program"] for r in all_refs if r["kind"] == "direct-pgm"}
    utility_count = sum(
        1 for r in all_refs if r["kind"] == "direct-pgm" and r["program"].upper() in UTILITY_PGM_NAMES
    )
    non_utility_direct_pgm = sorted(n for n in direct_pgm_distinct if n.upper() not in UTILITY_PGM_NAMES)

    return {
        "files_parsed": len(parsed),
        "total_program_refs": len(all_refs),
        "natural_batch_refs": by_kind.get("natural-batch", 0),
        "direct_pgm_refs": by_kind.get("direct-pgm", 0),
        "distinct_programs": len(by_program),
        "distinct_libraries": len(by_library),
        "by_library_top20": _top_n(by_library, 20),
        "top_programs_top20": _top_n(by_program, 20),
        "utility_direct_pgm_count": utility_count,
        "non_utility_direct_pgm": non_utility_direct_pgm,
        "jobs_with_no_job_card": sum(1 for p in parsed if p["job_name"] is None),
        "steplib_chains_found": sum(len(p["steplib_groups"]) for p in parsed),
    }


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
    parser.add_argument("--jcl-dir", help="Overrides config paths.jcl_dir")
    parser.add_argument("--out-dir", help="Overrides config paths.out_dir")
    parser.add_argument("--encoding", default="auto",
                         help="Encoding for JCL files, or 'auto' to sniff each file (default auto)")
    args = parser.parse_args(argv)

    config_path = pathlib.Path(args.config) if args.config else nat_config.DEFAULT_CONFIG_PATH
    try:
        cfg = nat_config.load(config_path)
    except nat_config.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    base_dir = config_path.resolve().parent

    jcl_dir = _resolve(base_dir, args.jcl_dir) or _resolve(base_dir, cfg["paths"].get("jcl_dir"))
    if not jcl_dir:
        print("error: no JCL directory given (pass --jcl-dir or set paths.jcl_dir in config.yaml)", file=sys.stderr)
        return 1
    if not jcl_dir.is_dir():
        print(f"error: JCL directory not found: {jcl_dir}", file=sys.stderr)
        return 1

    encoding_opt = args.encoding
    if encoding_opt != "auto" and encoding_opt not in natenc.ENCODINGS:
        print(f"error: unknown encoding {encoding_opt!r} (expected 'auto' or one of {natenc.ENCODINGS})", file=sys.stderr)
        return 1

    out_dir = _resolve(base_dir, args.out_dir) or _resolve(base_dir, cfg["paths"].get("out_dir")) or (base_dir / "out")
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    files = sorted(p for p in jcl_dir.rglob("*") if p.is_file())
    parsed = []
    read_errors = []
    for fp in files:
        try:
            text = read_text_auto(fp, encoding_opt)
        except (ValueError, UnicodeDecodeError) as e:
            read_errors.append({"file": fp.name, "error": str(e)})
            continue
        parsed.append(parse_jcl(text, fp.name))

    jcl_json = build_jcl_json(parsed)
    out_path = out_dir / "jcl.json"
    out_path.write_text(json.dumps(jcl_json, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = build_summary(parsed)
    summary["elapsed_seconds"] = round(time.time() - t0, 3)
    summary["out_file"] = str(out_path)
    if read_errors:
        summary["read_errors"] = read_errors
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
