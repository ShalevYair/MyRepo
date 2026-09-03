#!/usr/bin/env python3
"""Stage 1.1 (WORKPLAN.md): split a raw SYSOBJH unload into per-object source
files plus objects.jsonl.

Parsing logic (record tags, field offsets, type-letter meanings) is the
natlib.natprofile port of app.js NAT_PROFILE/feedProfile — already validated
against a real 770 MB scan (README.md "Findings so far"). This program does
not re-derive that logic; it only adds the streaming driver, object-boundary
bookkeeping, and file/JSONL output that app.js has no equivalent of (app.js
counts source lines and throws them away — MERGE-PLAN.md section 1.1).

Output (relative to --out-dir):
  source/<LIBRARY>/<NAME>.nat   one file per object (sharded by library)
  objects.jsonl                 one row per object, schema: SCHEMAS.md section 1
"""
from __future__ import annotations

import argparse
import codecs
import json
import pathlib
import re
import sys
import time
from collections import Counter

from natlib import config as nat_config
from natlib import encoding as natenc
from natlib import natprofile as prof
from natlib import objid

_UNSAFE_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_component(raw: str, placeholder: str) -> str:
    """Make a library or object name safe as a single path component on
    both Windows and POSIX filesystems (WORKPLAN.md 1.1: sharding exists
    specifically because Windows chokes on one directory of 86K files;
    the same "don't fall over on Windows" concern applies to the names
    themselves — reserved characters, trailing dots/spaces)."""
    s = (raw or "").strip()
    if not s:
        return placeholder
    s = _UNSAFE_FS_CHARS.sub("_", s)
    s = s.rstrip(". ")
    return s or placeholder


def detect_framing(text: str) -> str:
    """Which character actually separates records in the decoded text.
    Ported from app.js detectFraming() (natprog-discovery/app.js ~904-919):
    EBCDIC decodes byte 0x25 to LF and 0x15 to NEL (U+0085), so this must
    run on decoded text, never on raw bytes. Falls back to '\\n' if neither
    is present (matches app.js: an all-fixed-width file with no embedded
    separator is not expected for either known Natural profile, but must
    not crash if seen)."""
    lf = text.count("\n")
    nel = text.count("")
    if lf > 0 and lf >= nel:
        return "\n"
    if nel > 0:
        return ""
    return "\n"


class Stats:
    """Run summary — printed at the end, not part of objects.jsonl. Deliberately
    small: full anomaly tracking (histograms, capped samples, etc.) is the
    browser tool's L1 layer; this is stage 1.3's validate.py job to cross-check
    against, not this program's job to reproduce."""

    def __init__(self) -> None:
        self.tag_counts: Counter[str] = Counter()
        self.objects_seen = 0
        self.by_type: Counter[str] = Counter()
        self.by_library: Counter[str] = Counter()
        self.unknown_type_letters: Counter[str] = Counter()
        self.orphan_source_lines = 0
        self.directory_without_object = 0
        self.record_pad_bad = 0
        self.bad_saved_ts = 0
        self.bad_cataloged_ts = 0
        self.bad_size = 0
        self.duplicate_object_keys: Counter[str] = Counter()
        self.empty_library = 0
        self.empty_name = 0
        self.header = None
        self.encoding_requested = None
        self.encoding_used = None
        self.sniff = None

    def as_dict(self) -> dict:
        star_s = self.tag_counts.get("*S**", 0)
        dash_s = self.tag_counts.get("-S**", 0)
        total_s = star_s + dash_s
        return {
            "encoding_requested": self.encoding_requested,
            "encoding_used": self.encoding_used,
            "encoding_sniff": self.sniff,
            "objects_seen": self.objects_seen,
            "tag_counts": dict(self.tag_counts),
            "dash_s_ratio": (dash_s / total_s) if total_s else 0.0,
            "by_type": dict(self.by_type),
            "by_library_top20": dict(self.by_library.most_common(20)),
            "unknown_type_letters": dict(self.unknown_type_letters),
            "orphan_source_lines": self.orphan_source_lines,
            "directory_without_object": self.directory_without_object,
            "record_length_not_multiple_of_12": self.record_pad_bad,
            "bad_saved_timestamp": self.bad_saved_ts,
            "bad_cataloged_timestamp": self.bad_cataloged_ts,
            "bad_size_field": self.bad_size,
            "duplicate_object_keys": dict(self.duplicate_object_keys),
            "empty_library": self.empty_library,
            "empty_name": self.empty_name,
            "header": self.header,
        }


class _PendingObject:
    __slots__ = (
        "library", "name", "type", "truncated_start",
        "seen_d01", "seen_d02", "seen_d03", "seen_d04",
        "nat_version", "saved", "cataloged", "size",
        "os", "tp", "codepage", "users",
        "source_lines", "chars", "max_line_len",
    )

    def __init__(self, library: str, name: str, type_letter: str, truncated_start: bool) -> None:
        self.library = library
        self.name = name
        self.type = type_letter
        self.truncated_start = truncated_start
        self.seen_d01 = self.seen_d02 = self.seen_d03 = self.seen_d04 = False
        self.nat_version = None
        self.saved = None
        self.cataloged = None
        self.size = None
        self.os = None
        self.tp = None
        self.codepage = None
        self.users: list[str] = []
        self.source_lines: list[str] = []
        self.chars = 0
        self.max_line_len = 0

    def add_source_line(self, payload: str) -> None:
        s = prof.rtrim(payload)
        self.source_lines.append(s)
        self.chars += len(s)
        if len(s) > self.max_line_len:
            self.max_line_len = len(s)


class NatunloadSplitter:
    def __init__(self, out_dir: pathlib.Path, stats: Stats) -> None:
        self.out_dir = out_dir
        self.source_dir = out_dir / "source"
        self.stats = stats
        self.cur: _PendingObject | None = None
        self._seen_keys: Counter[tuple[str, str]] = Counter()
        self._objects_path = out_dir / "objects.jsonl"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._objects_fh = self._objects_path.open("w", encoding="utf-8")

    def close(self) -> None:
        self._objects_fh.close()

    # ---- per-record dispatch, mirrors app.js Analyzer.feedProfile ----
    def feed_line(self, line: str, truncated: bool) -> None:
        if line.endswith("\r"):
            line = line[:-1]
        if not line:
            return
        if len(line) % prof.RECORD_PAD != 0 and not truncated:
            self.stats.record_pad_bad += 1

        tag = line[:4]
        if tag in ("*S**", "-S**"):
            self.stats.tag_counts[tag] += 1
            if self.cur is None:
                self.stats.orphan_source_lines += 1
                return
            self.cur.add_source_line(line[prof.PREFIX_LEN:])
            return
        if tag == "*C**":
            self.stats.tag_counts[tag] += 1
            self._finalize_current(truncated=False)
            self._start_object(line, truncated)
            return
        if tag in ("*D01", "*D02", "*D03", "*D04"):
            self.stats.tag_counts[tag] += 1
            if self.cur is None:
                self.stats.directory_without_object += 1
                return
            self._feed_directory(tag, line)
            return
        if tag == "*H**":
            self.stats.tag_counts[tag] += 1
            if self.stats.header is None:
                self.stats.header = self._decode_header(line)
            return
        self.stats.tag_counts["«unknown»"] += 1

    def _decode_header(self, line: str) -> dict:
        H = prof.H
        ts = prof.parse_nat_ts(prof.fld(line, H["timestamp"]).ljust(15, "0"))
        return {
            "product": prof.fld(line, H["prod"]),
            "version": prof.fld(line, H["version"]),
            "os": prof.fld(line, H["os"]),
            "unload_timestamp": ts["iso"] if ts["ok"] else None,
        }

    def _start_object(self, line: str, truncated: bool) -> None:
        C = prof.C
        library = prof.fld(line, C["library"])
        name = prof.fld(line, C["name"])
        type_letter = line[C["type"][0]:C["type"][0] + 1]
        self.cur = _PendingObject(library, name, type_letter, truncated)

    def _feed_directory(self, tag: str, line: str) -> None:
        c = self.cur
        assert c is not None
        if tag == "*D01":
            c.seen_d01 = True
            D = prof.D01
            c.nat_version = prof.fld(line, D["version"])
            users = [prof.fld(line, D["user1"]), prof.fld(line, D["user2"]), prof.fld(line, D["user3"])]
            c.users = [u for u in users if u]
        elif tag == "*D02":
            c.seen_d02 = True
            D = prof.D02
            saved = prof.parse_nat_ts(line[D["saved_ts"][0]:D["saved_ts"][0] + 15])
            cataloged = prof.parse_nat_ts(line[D["cataloged_ts"][0]:D["cataloged_ts"][0] + 15])
            c.saved = saved["iso"] if saved["ok"] else None
            if not saved["ok"] and not saved["empty"]:
                self.stats.bad_saved_ts += 1
            c.cataloged = cataloged["iso"] if cataloged["ok"] else None
            if not cataloged["ok"] and not cataloged["empty"]:
                self.stats.bad_cataloged_ts += 1
            size_off, size_len = D["size"]
            size_raw = line[size_off:size_off + size_len]
            if re.fullmatch(r"\d{10}", size_raw):
                c.size = int(size_raw)
            else:
                self.stats.bad_size += 1
        elif tag == "*D03":
            c.seen_d03 = True
            D = prof.D03
            c.os = prof.fld(line, D["os"])
            c.tp = prof.fld(line, D["tp_monitor"])
        elif tag == "*D04":
            c.seen_d04 = True
            c.codepage = prof.fld(line, prof.D04["codepage"])

    def _finalize_current(self, truncated: bool) -> None:
        c = self.cur
        if c is None:
            return
        self.cur = None
        self.stats.objects_seen += 1
        self.stats.by_type[c.type] += 1

        library = c.library or ""
        name = c.name or ""
        if not library:
            self.stats.empty_library += 1
        if not name:
            self.stats.empty_name += 1
        self.stats.by_library[library or "<empty>"] += 1

        object_id = objid.normalize_object_id(library, name)
        meta = prof.TYPE_MAP.get(c.type)
        if meta is None:
            self.stats.unknown_type_letters[c.type] += 1
            type_meaning, kind, type_confidence = None, None, "none"
        else:
            type_confidence = meta["confidence"]
            kind = meta["kind"]
            type_meaning = None if type_confidence == "none" else meta["name"]
            if type_confidence == "none":
                self.stats.unknown_type_letters[c.type] += 1

        source_path, source_text = self._write_source(c, library, name)
        sha_raw = objid.sha256_raw(source_text)
        sha_norm = objid.sha256_norm(source_text)

        row = {
            "object_id": object_id,
            "library": library,
            "name": name,
            "type": c.type,
            "type_meaning": type_meaning,
            "kind": kind,
            "type_confidence": type_confidence,
            "nat_version": c.nat_version,
            "saved": c.saved,
            "cataloged": c.cataloged,
            "size": c.size,
            "lines": len(c.source_lines),
            "chars": c.chars,
            "max_line_len": c.max_line_len,
            "os": c.os,
            "tp": c.tp,
            "codepage": c.codepage,
            "users": c.users,
            "source_path": source_path,
            "sha256_raw": sha_raw,
            "sha256_norm": sha_norm,
            "truncated": bool(truncated or c.truncated_start),
        }
        self._objects_fh.write(json.dumps(row, ensure_ascii=False))
        self._objects_fh.write("\n")

    def _write_source(self, c: _PendingObject, library: str, name: str) -> tuple[str, str]:
        shard = sanitize_component(library.upper(), "_EMPTY_LIBRARY_")
        base = sanitize_component(name.upper(), "_EMPTY_NAME_")

        # Keyed on (shard, base) -- the actual resolved path components --
        # not the raw (library, name), so two objects that collide on disk
        # for any reason (case differences, or sanitize_component() mapping
        # two different raw names to the same safe filename) are always
        # detected and disambiguated. Keying on the raw strings would miss
        # that collision and let the second object silently overwrite the
        # first object's file while objects.jsonl still points both rows at
        # the same source_path.
        key = (shard, base)
        n = self._seen_keys[key]
        self._seen_keys[key] += 1
        if n > 0:
            self.stats.duplicate_object_keys[f"{library}/{name}"] += 1
            filename = f"{base}~{n + 1}.nat"
        else:
            filename = f"{base}.nat"

        shard_dir = self.source_dir / shard
        shard_dir.mkdir(parents=True, exist_ok=True)
        file_path = shard_dir / filename

        # Trailing newline included in both the written bytes and the hashed
        # text, so sha256_raw always matches what's actually on disk — see
        # pipeline/tests/test_natunload_split.py for the check that enforces this.
        source_text = ("\n".join(c.source_lines) + "\n") if c.source_lines else ""
        file_path.write_text(source_text, encoding="utf-8")

        rel = pathlib.PurePosixPath("source") / shard / filename
        return str(rel), source_text

    def finish(self, hit_byte_limit: bool, last_tail_partial: bool) -> None:
        self._finalize_current(truncated=hit_byte_limit or last_tail_partial)


def _make_chunk_decoder(encoding: str):
    """Returns decode(chunk_bytes, is_final) -> str.

    Only utf-8 is variable-width; a multi-byte sequence can straddle an
    8 MiB chunk boundary, so it needs a real stateful incremental decoder
    (mirrors JS TextDecoder(..., {stream:true})). The other six candidates
    (natlib.encoding.ENCODINGS) are all single-byte codepages — one byte is
    always exactly one character, so no cross-chunk decoder state is needed
    and natlib.encoding.decode() can run independently per chunk.
    """
    if encoding == "utf-8":
        decoder = codecs.getincrementaldecoder("utf-8")()

        def decode_utf8(chunk: bytes, is_final: bool) -> str:
            return decoder.decode(chunk, is_final)

        return decode_utf8

    def decode_single_byte(chunk: bytes, is_final: bool) -> str:  # noqa: ARG001
        return natenc.decode(encoding, chunk)

    return decode_single_byte


def run_split(
    unload_path: pathlib.Path,
    out_dir: pathlib.Path,
    encoding_opt: str,
    chunk_bytes: int,
    sniff_bytes: int,
    limit_bytes: int | None,
) -> Stats:
    file_size = unload_path.stat().st_size
    limit = file_size if not limit_bytes else min(limit_bytes, file_size)
    hit_byte_limit = limit < file_size

    with unload_path.open("rb") as f:
        head = f.read(min(sniff_bytes, limit))
        sniff = natenc.sniff_encoding(head)
        encoding = sniff["guess"] if encoding_opt == "auto" else encoding_opt

        probe_decoder = _make_chunk_decoder(encoding)
        probe_text = probe_decoder(head[: min(len(head), 1 << 20)], False)
        sep = detect_framing(probe_text)

        f.seek(0)
        stats = Stats()
        stats.encoding_requested = encoding_opt
        stats.encoding_used = encoding
        stats.sniff = sniff
        splitter = NatunloadSplitter(out_dir, stats)
        decode_chunk = _make_chunk_decoder(encoding)

        offset = 0
        tail = ""
        last_tail_partial = False
        while offset < limit:
            to_read = min(chunk_bytes, limit - offset)
            chunk = f.read(to_read)
            if not chunk:
                break
            offset += len(chunk)
            more = offset < limit
            text = tail + decode_chunk(chunk, not more)

            start = 0
            while True:
                idx = text.find(sep, start)
                if idx < 0:
                    break
                splitter.feed_line(text[start:idx], truncated=False)
                start = idx + 1
            tail = text[start:]

            if not more and tail:
                last_tail_partial = len(tail) % prof.RECORD_PAD != 0
                splitter.feed_line(tail, truncated=last_tail_partial)
                tail = ""

        splitter.finish(hit_byte_limit=hit_byte_limit, last_tail_partial=last_tail_partial)
        splitter.close()

    return stats


def _resolve(base_dir: pathlib.Path, value: str | None) -> pathlib.Path | None:
    if not value:
        return None
    p = pathlib.Path(value)
    return p if p.is_absolute() else (base_dir / p)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to config.yaml (default: pipeline/config.yaml)")
    parser.add_argument("--unload-file", help="Overrides config paths.unload_file")
    parser.add_argument("--encoding", help="Overrides config paths.unload_encoding (default 'auto')")
    parser.add_argument("--out-dir", help="Overrides config paths.out_dir")
    parser.add_argument("--limit-bytes", type=int, default=0,
                         help="Only read the first N bytes (0 = whole file). For sample runs before the full 800MB pass.")
    args = parser.parse_args(argv)

    config_path = pathlib.Path(args.config) if args.config else nat_config.DEFAULT_CONFIG_PATH
    try:
        cfg = nat_config.load(config_path)
    except nat_config.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    base_dir = config_path.resolve().parent

    unload_file = _resolve(base_dir, args.unload_file) or _resolve(base_dir, cfg["paths"].get("unload_file"))
    if not unload_file:
        print("error: no unload file given (pass --unload-file or set paths.unload_file in config.yaml)", file=sys.stderr)
        return 1
    if not unload_file.is_file():
        print(f"error: unload file not found: {unload_file}", file=sys.stderr)
        return 1

    out_dir = _resolve(base_dir, args.out_dir) or _resolve(base_dir, cfg["paths"].get("out_dir")) or (base_dir / "out")
    encoding_opt = args.encoding or cfg["paths"].get("unload_encoding") or "auto"
    if encoding_opt != "auto" and encoding_opt not in natenc.ENCODINGS:
        print(f"error: unknown encoding {encoding_opt!r} (expected 'auto' or one of {natenc.ENCODINGS})", file=sys.stderr)
        return 1

    scan_cfg = cfg.get("scan", {})
    chunk_bytes = int(scan_cfg.get("chunk_bytes", 8 * 1024 * 1024))
    sniff_bytes = int(scan_cfg.get("sniff_bytes", 4 * 1024 * 1024))

    t0 = time.time()
    stats = run_split(
        unload_path=unload_file,
        out_dir=out_dir,
        encoding_opt=encoding_opt,
        chunk_bytes=chunk_bytes,
        sniff_bytes=sniff_bytes,
        limit_bytes=args.limit_bytes or None,
    )
    elapsed = time.time() - t0

    summary = stats.as_dict()
    summary["elapsed_seconds"] = round(elapsed, 3)
    summary["out_dir"] = str(out_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
