#!/usr/bin/env python3
"""Stage 3 (WORKPLAN.md): the real Natural call graph.

MERGE-PLAN.md section 1.1 names the exact gap this closes: app.js's
feedSource() extracts dependencies into one GLOBAL histogram per kind
("how many times is X called across the whole 800MB scan"), never a
per-object edge -- so no call graph, no reachability, no dead-code
detection can be built from it. This program builds real A -> B edges,
one object at a time, then resolves each target against every object in
the estate (library-aware: CALLNAT/FETCH/PERFORM never carry a library
qualifier in Natural syntax, so "which HICNEWN3" is only answerable by
checking who else has that name -- exactly the ambiguity jclmap.py's
README-documented cross-check proved matters, WORKPLAN.md 3.3/MERGE-PLAN.md
section 3).

Dependency extraction and the "leading keyword" trick for classifying a
line are ported from app.js RE / Analyzer.feedSource (app.js ~424-431,
~708-766), already validated against a real 770MB scan. DDM access
(READ/FIND/HISTOGRAM/STORE/UPDATE/DELETE) and library resolution are NEW
-- app.js does not have them (MERGE-PLAN.md section 4.2 explicitly lists
DDM access as "missing today").

Output (relative to --out-dir):
  natmap.json   one artifact, schema: SCHEMAS.md section 2

Known gap, flagged rather than guessed: SCHEMAS.md section 2.2 lists
`domain`, `self_redundancy`, and `n_obsolete` as fields natural-viewer.html
already reads -- but nowhere in README.md/MERGE-PLAN.md/WORKPLAN.md/
SCHEMAS.md is there any stated formula for what they mean, and grepping
natural-viewer.html's own usage of them (natural-viewer.html ~1657,
~1663-1665, ~1366-1370) confirms they're read but never gives a
computable definition -- unlike natural_bridge[] (cobolmap.py), where the
*intent* was at least stated. Rather than invent an algorithm for a
foundational, hard-to-unwind component on zero grounding, this program
emits neutral placeholders (domain=None, self_redundancy=0.0,
n_obsolete=0) so natural-viewer.html's `|| ''`/`|| 0` fallbacks degrade
exactly the way they already do for a natmap.json that doesn't have these
keys at all -- and asks rather than guesses. `ui_class` and `max_depth`/
`unbalanced` ARE grounded (see below) and are computed for real.

`ui_class`: natural-viewer.html's own filter placeholder text is "online /
batch" (natural-viewer.html:1215) -- the only concrete hint available.
Implemented here as 'online' for Map objects (type M -- the only Natural
object kind that is inherently a screen/UI artifact) and 'batch'
otherwise. This is this program's inference, not a documented rule --
flagged, not hidden.

`max_depth`/`unbalanced`: confirmed by natural-viewer.html's own warning
text, "הפרסר לא הצליח לאזן את הבלוקים בקובץ הזה -- אל תסמוך על העומק" (the
parser couldn't balance this file's blocks -- don't trust the depth)
(natural-viewer.html:1315,1665) -- i.e. max_depth is IF/DECIDE/FOR/REPEAT
block-nesting depth within one object's own source (not call-graph
depth), and unbalanced flags when openers and END-* closers don't match.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time
from collections import Counter, defaultdict

from natlib import config as nat_config

SCHEMA_VERSION = "1.0"

# ---- dependency regexes: ported/extended from app.js RE (app.js ~424-431) ----
# Unlike app.js, CALLNAT/FETCH/CALL3GL capture EITHER a quoted literal OR a
# bare token, so a dynamic target (CALLNAT #PGM-NAME) is captured instead of
# silently missed -- WORKPLAN.md 3.4 exists specifically to measure how often
# that happens; a regex that only matches literals could never measure it.
_USING_RE = re.compile(r"\b(LOCAL|GLOBAL|PARAMETER|CONTEXT|INDEPENDENT)\s+USING\s+([A-Z0-9#$&@_.\-]+)")
_CALLNAT_RE = re.compile(r"\bCALLNAT\s+('[^']*'|\S+)")
_FETCH_RE = re.compile(r"\bFETCH\s+(?:RETURN\s+|REPEAT\s+)?('[^']*'|\S+)")
_PERFORM_RE = re.compile(r"\bPERFORM\s+(?!BREAK\b)([A-Z0-9#$&@_.\-]+)")
_MAP_RE = re.compile(r"\bUSING\s+MAP\s+'([^']+)'")
_INCLUDE_RE = re.compile(r"\bINCLUDE\s+([A-Z0-9#$&@_.\-]+)")
_CALL3GL_RE = re.compile(r"\bCALL\s+('[^']*'|\S+)")
_DEFINE_SUBROUTINE_RE = re.compile(r"\bDEFINE\s+SUBROUTINE\s+([A-Z0-9#$&@_.\-]+)")

# New (not in app.js -- MERGE-PLAN.md 4.2 lists this as missing today).
# FIND allows an optional "(n)" record-limit clause before the view name
# (e.g. "FIND (10) VIEW-NAME WITH ..."); READ/HISTOGRAM don't use that form
# but the pattern is harmless to allow for all three.
_DDM_READ_RE = re.compile(r"\b(READ|FIND|HISTOGRAM)\s+(?:\(\S+\)\s+)?([A-Z0-9#$&@_.\-]+)")
_DDM_STORE_RE = re.compile(r"\bSTORE\s+(?:RECORD\s+)?([A-Z0-9#$&@_.\-]+)")
_DDM_BARE_WRITE_RE = re.compile(r"\b(UPDATE|DELETE)\b")

_BLOCK_OPEN = {"IF", "DECIDE", "FOR", "REPEAT"}
_BLOCK_CLOSE = {"END-IF": "IF", "END-DECIDE": "DECIDE", "END-FOR": "FOR", "END-REPEAT": "REPEAT"}
# app.js ~748-751 -- the exact set of leading keywords counted as "real"
# executable statements (as opposed to every recognised keyword, which is a
# larger set app.js also tracks separately in its stmt Counter).
_EXEC_KEYWORDS = {
    "DEFINE", "END-DEFINE", "READ", "FIND", "WRITE", "DISPLAY", "CALLNAT", "PERFORM",
    "IF", "DECIDE", "FOR", "REPEAT", "MOVE", "COMPUTE", "ASSIGN", "FETCH",
}
_KW_STOP_CHARS = frozenset(" (.'")  # app.js ~740-744
_KW_RE = re.compile(r"^[A-Z][A-Z0-9\-]*$")

_LITERAL_TARGET_KINDS = {"CALLNAT", "FETCH", "CALL3GL"}


def leading_keyword(code: str) -> str | None:
    """Port of app.js's leading-statement-keyword extraction (app.js
    ~736-746): strip leading spaces, take characters up to the first
    space/(/./', upper-case, and keep it only if it looks like a real
    keyword token (starts with a letter, <=24 chars, only A-Z0-9-)."""
    lt = code.lstrip()
    if not lt:
        return None
    end = 0
    for ch in lt:
        if ch in _KW_STOP_CHARS:
            break
        end += 1
    kw = lt[:end].upper()
    if kw and len(kw) <= 24 and _KW_RE.match(kw):
        return kw
    return None


def strip_inline_comment(code: str) -> str:
    """Port of app.js ~730-733: a Natural inline comment starts at the
    first `/*` on the line; the code is whatever precedes it, right-trimmed."""
    ci = code.find("/*")
    if ci >= 0:
        code = code[:ci].rstrip()
    return code


def _literal_or_dynamic(kind: str, raw: str) -> dict:
    if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
        return {"kind": kind, "raw_target": raw[1:-1], "dynamic": False}
    return {"kind": kind, "raw_target": raw, "dynamic": True}


def parse_object(text: str) -> dict:
    """Single pass over one already-split Natural object's source (one
    payload line per line -- natunload_split.py already stripped the *S**
    prefix and right-trimmed each line, matching what app.js's `s` variable
    holds in feedSource()). Returns raw per-object findings; resolving an
    edge's target against the rest of the estate happens in a later pass
    (resolve_edges) because that needs every object's library, which a
    single object's own source can't tell you.
    """
    code_lines = 0
    if_count = decide_count = compute_count = 0
    depth = 0
    max_depth = 0
    went_negative = False
    subroutines: set[str] = set()
    current_view: str | None = None
    edges: list[dict] = []
    ddm_access: list[dict] = []

    for raw_line in text.split("\n"):
        if raw_line.startswith("*"):  # Natural comment: column-1 '*', app.js ~722
            continue
        code = strip_inline_comment(raw_line)
        if not code:
            continue

        kw = leading_keyword(code)
        if kw:
            if kw in _EXEC_KEYWORDS:
                code_lines += 1
            if kw == "IF":
                if_count += 1
            elif kw == "DECIDE":
                decide_count += 1
            elif kw == "COMPUTE":
                compute_count += 1
            if kw in _BLOCK_OPEN:
                depth += 1
                max_depth = max(max_depth, depth)
            elif kw in _BLOCK_CLOSE:
                depth -= 1
                if depth < 0:
                    went_negative = True

        up = code.upper()

        if "SUBROUTINE" in up:
            m = _DEFINE_SUBROUTINE_RE.search(up)
            if m:
                subroutines.add(m.group(1))

        if "USING" in up:
            for m in _USING_RE.finditer(up):
                edges.append({"kind": "USING", "raw_target": m.group(2), "dynamic": False})
            if "MAP" in up:
                for m in _MAP_RE.finditer(up):
                    edges.append({"kind": "USING_MAP", "raw_target": m.group(1), "dynamic": False})
        if "CALLNAT" in up:
            for m in _CALLNAT_RE.finditer(up):
                edges.append(_literal_or_dynamic("CALLNAT", m.group(1)))
        if "PERFORM" in up:
            for m in _PERFORM_RE.finditer(up):
                edges.append({"kind": "PERFORM", "raw_target": m.group(1), "dynamic": False})
        if "FETCH" in up:
            for m in _FETCH_RE.finditer(up):
                edges.append(_literal_or_dynamic("FETCH", m.group(1)))
        if "INCLUDE" in up:
            for m in _INCLUDE_RE.finditer(up):
                edges.append({"kind": "INCLUDE", "raw_target": m.group(1), "dynamic": False})
        if "CALL" in up:  # catches both CALLNAT and 'CALL ' -- see _CALL3GL_RE note
            for m in _CALL3GL_RE.finditer(up):
                edges.append(_literal_or_dynamic("CALL3GL", m.group(1)))

        if "READ" in up or "FIND" in up or "HISTOGRAM" in up:
            m = _DDM_READ_RE.search(up)
            if m:
                current_view = m.group(2)
                ddm_access.append({"ddm": current_view, "op": m.group(1)})
        if "STORE" in up:
            m = _DDM_STORE_RE.search(up)
            if m:
                current_view = m.group(1)
                ddm_access.append({"ddm": current_view, "op": "STORE"})
        if current_view and ("UPDATE" in up or "DELETE" in up):
            m = _DDM_BARE_WRITE_RE.search(up)
            if m:
                ddm_access.append({"ddm": current_view, "op": m.group(1)})

    return {
        "code_lines": code_lines,
        "if_count": if_count,
        "decide_count": decide_count,
        "compute_count": compute_count,
        "max_depth": max_depth,
        "unbalanced": bool(went_negative or depth != 0),
        "subroutines": subroutines,
        "edges": edges,
        "ddm_access": ddm_access,
    }


# ---- second pass: resolve edges against the whole estate -----------------

_WRITE_OPS = {"STORE", "UPDATE", "DELETE"}


def build_name_index(objects: list[dict]) -> dict[str, list[tuple[str, str, str]]]:
    """name (upper) -> [(object_id, library, type), ...]. Natural syntax
    never lets CALLNAT/FETCH/PERFORM/INCLUDE name a library -- only the
    bare object name -- so resolving "which library" requires knowing
    every object sharing that name (README.md's own documented ambiguity:
    RC/GO0701P0 vs GOCOPY/GOGO copies of the same name)."""
    idx: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for o in objects:
        idx[o["name"].upper()].append((o["object_id"], o["library"], o.get("type") or ""))
    return idx


def resolve_target(
    name_index: dict[str, list[tuple[str, str, str]]],
    calling_library: str,
    target_name: str,
    steplib_chain: list[str],
    system_library: str,
    require_type: str | None = None,
) -> tuple[str | None, str, list[str]]:
    """WORKPLAN.md 3.3's exact order: same library -> steplib chain (from
    config.yaml, filled in from jcl.json's steplib_chains if any JCL
    declares one) -> SYSTEM -> ambiguous (every candidate, not a guess) or
    unresolved (no candidate anywhere). Returns (resolved_to, scope,
    candidates) -- candidates is only populated when scope == 'ambiguous'.

    require_type narrows candidates to one object type first (MERGE-PLAN.md
    5.2: an external PERFORM target is a type-S Subroutine by Natural's own
    rules) -- but only when at least one such candidate exists; if none do,
    falls back to the unfiltered set rather than manufacturing an
    unresolved result out of a type mismatch that might just be a
    mis-declared or unusual object.
    """
    all_candidates = name_index.get(target_name.upper(), [])
    if require_type:
        typed = [c for c in all_candidates if c[2] == require_type]
        if typed:
            all_candidates = typed

    def _pick(pool: list[tuple[str, str, str]]) -> tuple[str | None, str, list[str]] | None:
        if len(pool) == 1:
            return pool[0][0], "", []
        if len(pool) > 1:
            return None, "ambiguous", [c[0] for c in pool]
        return None

    same_lib = [c for c in all_candidates if c[1] == calling_library]
    picked = _pick(same_lib)
    if picked:
        oid, scope, cands = picked
        return (oid, "same_library", []) if oid else (None, scope, cands)

    for lib in steplib_chain:
        chain_hits = [c for c in all_candidates if c[1] == lib]
        picked = _pick(chain_hits)
        if picked:
            oid, scope, cands = picked
            return (oid, "steplib", []) if oid else (None, scope, cands)

    sys_hits = [c for c in all_candidates if c[1] == system_library]
    picked = _pick(sys_hits)
    if picked:
        oid, scope, cands = picked
        return (oid, "system", []) if oid else (None, scope, cands)

    if all_candidates:
        return None, "ambiguous", [c[0] for c in all_candidates]
    return None, "unresolved", []


def load_cobol_bridge(cobol_json_path: pathlib.Path | None) -> dict[tuple[str, str], str]:
    """(object_id, CALL-target-upper) -> COBOL PROGRAM-ID it resolved to,
    from cobolmap.py's natural_bridge[] (SCHEMAS.md section 4). Optional:
    a CALL3GL edge that isn't in this map just stays scope=external_3gl
    with resolved_to=None, same as if cobol.json were never given."""
    if not cobol_json_path or not cobol_json_path.is_file():
        return {}
    data = json.loads(cobol_json_path.read_text(encoding="utf-8"))
    bridge: dict[tuple[str, str], str] = {}
    for row in data.get("natural_bridge", []):
        oid = row.get("natural_object_id")
        target = row.get("natural_call_target")
        program = row.get("cobol_program")
        if oid and target and program:
            bridge[(oid, target.upper())] = program
    return bridge


def resolve_edges(
    objects: list[dict],
    parsed_by_id: dict[str, dict],
    steplib_chain: list[str],
    system_library: str,
    cobol_bridge: dict[tuple[str, str], str],
) -> list[dict]:
    name_index = build_name_index(objects)
    calls: list[dict] = []

    for o in objects:
        oid = o["object_id"]
        library = o["library"]
        p = parsed_by_id.get(oid)
        if not p:
            continue
        subroutines = p["subroutines"]

        for e in p["edges"]:
            kind = e["kind"]
            target = e["raw_target"]
            dynamic = e["dynamic"]

            if kind == "PERFORM" and target in subroutines:
                continue  # WORKPLAN.md 3.2: internal target, not a real edge

            if dynamic:
                calls.append({
                    "from": oid, "kind": kind, "target": target,
                    "resolved_to": None, "scope": "unresolved", "candidates": [], "dynamic": True,
                })
                continue

            if kind == "CALL3GL":
                resolved_to = cobol_bridge.get((oid, target.upper()))
                calls.append({
                    "from": oid, "kind": kind, "target": target,
                    "resolved_to": f"COBOL:{resolved_to}" if resolved_to else None,
                    "scope": "external_3gl", "candidates": [], "dynamic": False,
                })
                continue

            require_type = "S" if kind == "PERFORM" else None
            resolved_to, scope, candidates = resolve_target(
                name_index, library, target, steplib_chain, system_library, require_type)
            calls.append({
                "from": oid, "kind": kind, "target": target,
                "resolved_to": resolved_to, "scope": scope, "candidates": candidates, "dynamic": False,
            })

    return calls


def compute_families(objects: list[dict]) -> tuple[dict[str, str], list[dict]]:
    """family = shared sha256_norm value, only for objects that actually
    have company (a singleton isn't a "family"). Same grouping key
    hash_report.py already uses (stage 2.1), computed independently here
    because natmap.json needs it inline on every object plus the
    SCHEMAS.md-required dup_pairs[] (every pairwise combination within a
    family), which hash_report.py's own report shape doesn't emit."""
    by_hash: dict[str, list[str]] = defaultdict(list)
    for o in objects:
        h = o.get("sha256_norm")
        if h:
            by_hash[h].append(o["object_id"])

    family_of: dict[str, str] = {}
    dup_pairs: list[dict] = []
    for h, members in by_hash.items():
        if len(members) < 2:
            continue
        members = sorted(members)
        for m in members:
            family_of[m] = h
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                dup_pairs.append({"a": members[i], "b": members[j]})
    return family_of, dup_pairs


def build_natmap(
    objects: list[dict],
    parsed_by_id: dict[str, dict],
    calls: list[dict],
) -> dict:
    family_of, dup_pairs = compute_families(objects)

    fan_out: Counter = Counter()
    fan_in_sources: dict[str, set] = defaultdict(set)
    for c in calls:
        if c["resolved_to"]:
            fan_out[c["from"]] += 1
            fan_in_sources[c["resolved_to"]].add(c["from"])

    ddm_access_out: list[dict] = []
    primary_ddm_of: dict[str, str] = {}
    writes_of: dict[str, bool] = {}
    for oid, p in parsed_by_id.items():
        ddm_counts: Counter = Counter()
        has_write = False
        for da in p["ddm_access"]:
            ddm_access_out.append({"object_id": oid, "ddm": da["ddm"], "op": da["op"]})
            ddm_counts[da["ddm"]] += 1
            if da["op"] in _WRITE_OPS:
                has_write = True
        writes_of[oid] = has_write
        if ddm_counts:
            primary_ddm_of[oid] = max(sorted(ddm_counts), key=lambda k: ddm_counts[k])

    # Only objects actually parsed get a row -- with --limit-objects (a
    # quick sampling/timing flag, main()), objects.jsonl can list far more
    # objects than were read; a zero-stats placeholder row for the rest
    # would misreport "no code" instead of "not sampled this run". The name
    # index used for resolution (build_name_index, inside resolve_edges)
    # still sees every object regardless, so resolution rates for the
    # sampled objects stay realistic even in a limited run.
    objects_by_id = {o["object_id"]: o for o in objects}
    objects_out = []
    for oid, p in parsed_by_id.items():
        o = objects_by_id.get(oid, {})
        objects_out.append({
            "object_id": oid,
            "domain": None,             # not computed -- see module docstring
            "primary_ddm": primary_ddm_of.get(oid),
            "ui_class": "online" if o.get("type") == "M" else "batch",
            "object_type": o.get("type_meaning"),
            "max_depth": p.get("max_depth", 0),
            "if_count": p.get("if_count", 0),
            "decide_count": p.get("decide_count", 0),
            "compute_count": p.get("compute_count", 0),
            "code_lines": p.get("code_lines", 0),
            "fan_in": len(fan_in_sources.get(oid, ())),
            "fan_out": fan_out.get(oid, 0),
            "writes": writes_of.get(oid, False),
            "n_obsolete": 0,             # not computed -- see module docstring
            "unbalanced": p.get("unbalanced", False),
            "self_redundancy": 0.0,      # not computed -- see module docstring
            "family": family_of.get(oid),
        })

    total_callnat = sum(1 for c in calls if c["kind"] == "CALLNAT")
    dynamic_callnat = sum(1 for c in calls if c["kind"] == "CALLNAT" and c["dynamic"])
    total_call3gl = sum(1 for c in calls if c["kind"] == "CALL3GL")
    dynamic_call3gl = sum(1 for c in calls if c["kind"] == "CALL3GL" and c["dynamic"])

    return {
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "generated_at": None,  # filled in by main()
            "object_count": len(objects_out),
            "dynamic_callnat_ratio": (dynamic_callnat / total_callnat) if total_callnat else 0.0,
            "dynamic_call3gl_ratio": (dynamic_call3gl / total_call3gl) if total_call3gl else 0.0,
        },
        "objects": objects_out,
        "ddm_access": ddm_access_out,
        "calls": calls,
        "dup_pairs": dup_pairs,
    }


def build_summary(natmap: dict) -> dict:
    calls = natmap["calls"]
    by_kind: Counter = Counter(c["kind"] for c in calls)
    by_scope: Counter = Counter(c["scope"] for c in calls)
    return {
        "object_count": natmap["meta"]["object_count"],
        "total_edges": len(calls),
        "by_kind": sorted(by_kind.items(), key=lambda kv: (-kv[1], kv[0])),
        "by_scope": sorted(by_scope.items(), key=lambda kv: (-kv[1], kv[0])),
        "dynamic_callnat_ratio": round(natmap["meta"]["dynamic_callnat_ratio"], 4),
        "dynamic_call3gl_ratio": round(natmap["meta"]["dynamic_call3gl_ratio"], 4),
        "ddm_access_count": len(natmap["ddm_access"]),
        "dup_pairs_count": len(natmap["dup_pairs"]),
        "families_count": len({o["family"] for o in natmap["objects"] if o["family"]}),
    }


def load_objects_jsonl(path: pathlib.Path) -> list[dict]:
    objects = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                objects.append(json.loads(line))
    return objects


def _resolve(base_dir: pathlib.Path, value: str | None) -> pathlib.Path | None:
    if not value:
        return None
    p = pathlib.Path(value)
    return p if p.is_absolute() else (base_dir / p)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to config.yaml (default: pipeline/config.yaml)")
    parser.add_argument("--out-dir", help="Overrides config paths.out_dir (reads objects.jsonl/source/ from here too)")
    parser.add_argument("--objects-file", help="Overrides <out-dir>/objects.jsonl")
    parser.add_argument("--cobol-json", help="Optional path to cobol.json, to resolve CALL3GL edges via its natural_bridge[]")
    parser.add_argument("--progress-every", type=int, default=5000)
    parser.add_argument("--limit-objects", type=int, default=0,
                         help="Only parse the first N objects' source (0 = all). For a quick "
                              "timing/sanity-check run before the full estate -- resolution still "
                              "sees every object in objects.jsonl as a possible target, only the "
                              "sampled objects get real stats in the output.")
    args = parser.parse_args(argv)

    config_path = pathlib.Path(args.config) if args.config else nat_config.DEFAULT_CONFIG_PATH
    try:
        cfg = nat_config.load(config_path)
    except nat_config.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    base_dir = config_path.resolve().parent

    out_dir = _resolve(base_dir, args.out_dir) or _resolve(base_dir, cfg["paths"].get("out_dir")) or (base_dir / "out")
    objects_path = _resolve(base_dir, args.objects_file) or (out_dir / "objects.jsonl")
    if not objects_path.is_file():
        print(f"error: objects.jsonl not found: {objects_path} (run natunload_split.py first)", file=sys.stderr)
        return 1

    steplib_cfg = cfg.get("steplib", {})
    steplib_chain = list(steplib_cfg.get("default_chain") or [])
    system_library = steplib_cfg.get("system_library") or "SYSTEM"

    cobol_json_path = _resolve(base_dir, args.cobol_json) if args.cobol_json else (out_dir / "cobol.json")
    cobol_bridge = load_cobol_bridge(cobol_json_path if cobol_json_path.is_file() else None)

    t0 = time.time()
    objects = load_objects_jsonl(objects_path)
    to_parse = objects[: args.limit_objects] if args.limit_objects else objects
    limited_note = f" (limited to first {len(to_parse)} for this run)" if args.limit_objects else ""
    print(f"[natmap3] loaded {len(objects)} objects from {objects_path}, parsing source{limited_note}...",
          file=sys.stderr, flush=True)

    parsed_by_id: dict[str, dict] = {}
    read_errors = []
    for i, o in enumerate(to_parse, 1):
        src_path = out_dir / o["source_path"]
        try:
            text = src_path.read_text(encoding="utf-8")
        except OSError as e:
            read_errors.append({"object_id": o["object_id"], "error": str(e)})
            continue
        parsed_by_id[o["object_id"]] = parse_object(text)
        if i % args.progress_every == 0:
            print(f"[natmap3] parsed {i}/{len(to_parse)} objects ({time.time() - t0:.1f}s elapsed)",
                  file=sys.stderr, flush=True)

    print(f"[natmap3] source parsed: {len(parsed_by_id)}/{len(to_parse)} objects "
          f"({len(read_errors)} read errors), resolving edges...", file=sys.stderr, flush=True)

    calls = resolve_edges(objects, parsed_by_id, steplib_chain, system_library, cobol_bridge)
    natmap = build_natmap(objects, parsed_by_id, calls)
    natmap["meta"]["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    out_path = out_dir / "natmap.json"
    out_path.write_text(json.dumps(natmap, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = build_summary(natmap)
    summary["elapsed_seconds"] = round(time.time() - t0, 3)
    summary["out_file"] = str(out_path)
    summary["cobol_bridge_loaded"] = bool(cobol_bridge)
    if read_errors:
        summary["read_errors"] = read_errors
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
