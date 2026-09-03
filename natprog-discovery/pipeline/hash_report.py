#!/usr/bin/env python3
"""Stage 2.1 (WORKPLAN.md): duplication report over objects.jsonl.

Reads ONLY objects.jsonl (WORKPLAN.md 2.1: "קורא רק את objects.jsonl") --
no source files, no unload file. Groups objects by sha256_norm (natlib.objid,
SCHEMAS.md section 6) and reports:

  * how many distinct content families exist vs. total object count --
    the duplication hypothesis README.md's "heavy copy-before-you-change
    duplication" finding is about.
  * "shadow copy" families: identical content living in >=2 different
    libraries -- the RC/RCOLD/RC1..RC11 kind of duplication.
  * libraries where EVERY object's content also exists in some other
    library -- wholesale-deletion candidates, cross-referenced against
    config.yaml's graveyard_library_patterns (a naming-based signal) as
    corroborating, not decisive, evidence.

This is a report, not a decision: WORKPLAN.md 2.2 says the number decides
how the rest of the pipeline is prioritized, not this script.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from collections import defaultdict

from natlib import config as nat_config

DEFAULT_TOP_N = 20


def load_objects(path: pathlib.Path) -> list[dict]:
    """Reads objects.jsonl. Raises ValueError with the offending line number
    on malformed JSON -- this file is our own pipeline's output, so a bad
    line means real corruption, not something to silently skip past."""
    objects = []
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                objects.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{line_no}: invalid JSON ({e})") from e
    return objects


def _compile_graveyard_patterns(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


def _matches_any(name: str, compiled_patterns: list[re.Pattern]) -> bool:
    # config.yaml: "matched case-insensitively against the LIBRARY name
    # (regex, must match the whole name)" -- fullmatch, not search.
    return any(p.fullmatch(name) for p in compiled_patterns)


def build_report(objects: list[dict], graveyard_patterns: list[str], top_n: int = DEFAULT_TOP_N) -> dict:
    total = len(objects)

    families: dict[str, list[dict]] = defaultdict(list)
    for obj in objects:
        h = obj.get("sha256_norm")
        if not h:
            continue
        families[h].append(obj)

    family_libraries: dict[str, set] = {
        h: {o.get("library", "") for o in members} for h, members in families.items()
    }

    distinct_families = len(families)
    singleton_families = sum(1 for members in families.values() if len(members) == 1)
    duplicate_objects = total - distinct_families

    # Shadow copy: identical content living in >=2 distinct libraries.
    shadow = [(h, members) for h, members in families.items() if len(family_libraries[h]) >= 2]
    shadow.sort(key=lambda item: len(item[1]), reverse=True)
    shadow_object_count = sum(len(members) for _, members in shadow)

    top_shadow = []
    for h, members in shadow[:top_n]:
        top_shadow.append({
            "sha256_norm": h,
            "family_size": len(members),
            "library_count": len(family_libraries[h]),
            "libraries": sorted(family_libraries[h]),
            "sample_object_ids": [o.get("object_id") for o in members[:top_n]],
        })

    # Wholesale-candidate libraries: every object's content also exists
    # under some OTHER library. len(family_libraries[h]) >= 2 already means
    # "this object's family spans more than one library", and since the
    # object's own library is necessarily one member of that set, >=2
    # implies at least one *other* library holds the same content.
    by_library: dict[str, list[dict]] = defaultdict(list)
    for obj in objects:
        by_library[obj.get("library", "")].append(obj)

    compiled_patterns = _compile_graveyard_patterns(graveyard_patterns)
    graveyard_candidates = []
    for library, members in by_library.items():
        target_libraries: set = set()
        all_duplicated = True
        for obj in members:
            libs = family_libraries.get(obj.get("sha256_norm"), set())
            others = libs - {library}
            if not others:
                all_duplicated = False
                break
            target_libraries |= others
        if all_duplicated:
            graveyard_candidates.append({
                "library": library,
                "object_count": len(members),
                "matches_graveyard_name_pattern": _matches_any(library, compiled_patterns),
                "duplicate_target_libraries": sorted(target_libraries),
            })
    graveyard_candidates.sort(key=lambda c: c["object_count"], reverse=True)

    return {
        "total_objects": total,
        "distinct_content_families": distinct_families,
        "singleton_families": singleton_families,
        "duplicate_objects": duplicate_objects,
        "duplicate_object_ratio": round(duplicate_objects / total, 4) if total else None,
        "shadow_copy": {
            "family_count": len(shadow),
            "object_count": shadow_object_count,
            "top_families": top_shadow,
            "families_truncated": len(shadow) > top_n,
        },
        "graveyard_candidate_libraries": {
            "count": len(graveyard_candidates),
            "object_count": sum(c["object_count"] for c in graveyard_candidates),
            "libraries": graveyard_candidates[:top_n],
            "libraries_truncated": len(graveyard_candidates) > top_n,
        },
    }


def _resolve(base_dir: pathlib.Path, value: str | None) -> pathlib.Path | None:
    if not value:
        return None
    p = pathlib.Path(value)
    return p if p.is_absolute() else (base_dir / p)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to config.yaml (default: pipeline/config.yaml)")
    parser.add_argument("--objects", help="Path to objects.jsonl (default: <out_dir>/objects.jsonl from config)")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N,
                         help=f"How many entries to list per section (default {DEFAULT_TOP_N})")
    parser.add_argument("--out", help="Write the JSON report to this file (UTF-8) instead of printing "
                                       "it to the console -- use this for large --top values, and to "
                                       "avoid the Windows console mangling non-ASCII characters.")
    args = parser.parse_args(argv)

    config_path = pathlib.Path(args.config) if args.config else nat_config.DEFAULT_CONFIG_PATH
    try:
        cfg = nat_config.load(config_path)
    except nat_config.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    base_dir = config_path.resolve().parent

    objects_path = _resolve(base_dir, args.objects)
    if objects_path is None:
        out_dir = _resolve(base_dir, cfg["paths"].get("out_dir")) or (base_dir / "out")
        objects_path = out_dir / "objects.jsonl"

    if not objects_path.is_file():
        print(f"error: objects.jsonl not found: {objects_path}", file=sys.stderr)
        return 1

    graveyard_patterns = cfg.get("graveyard_library_patterns", [])

    try:
        objects = load_objects(objects_path)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    report = build_report(objects, graveyard_patterns, top_n=args.top)
    report_json = json.dumps(report, ensure_ascii=False, indent=2)

    if args.out:
        out_path = _resolve(base_dir, args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report_json, encoding="utf-8")
        print(f"Report written to {out_path}")
    else:
        print(report_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
