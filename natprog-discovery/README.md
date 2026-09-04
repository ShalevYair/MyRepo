# NATPROG Discovery

A local, browser-only discovery tool for very large mainframe source-unload files
(built and tested against a Software AG **Natural** SYSOBJH/SYSTRANS export; target size 800 MB).

Three files, no build step, no dependencies, no network. **The file never leaves the machine.**

```
natprog-discovery/
├── index.html
├── style.css
├── app.js
├── natural-viewer.html   ← a separate, more advanced tool (see below), not part of this one
└── pipeline/             ← an in-progress Python pipeline merging this tool with natural-viewer.html
                             (see "Merging with a Python pipeline" below) — not needed to run either
                             of the two tools above.
```

`natural-viewer.html` is a **different, richer tool** that sits on top of an external pipeline of the
same shape this repo is now building for itself (see below): a static call-graph/complexity/DDM-access
analysis, header extraction, an LLM-generated business analysis (`sf_target`, `business_purpose`,
`key_rules`, one JSON record per program), and a usage/activity log. It also has a Gemini-API chat
feature — meaning, unlike everything below, **it does send data externally** when that feature is used.
This tool (the three files above) doesn't try to reproduce or consume any of that on its own; the merge
effort below is what connects the two.

## Running it

Open `index.html` in Chrome or Edge (double-click works — `file://` is fine), pick the file, press
**הרץ סריקה**. For an 800 MB file expect roughly one to three minutes.

Optionally serve it instead (`npx http-server`), but nothing requires it.

## What it does

The file is read with `File.slice()` in 8 MB chunks and decoded incrementally, so memory stays flat
regardless of file size (measured: **101 MB peak heap on a 250 MB input, ~30 MB/s**). There is
deliberately no Web Worker — Blob workers are blocked under `file://` in Chrome — so the scan yields
to the event loop between chunks instead, keeping the progress bar live.

Analysis runs in two layers:

* **L1 generic** — assumes nothing. Encoding sniff, record-length histogram, record-tag histogram,
  line-ending analysis, fixed-length-record detection. This layer answers *"what actually is this file?"*
* **L2 profile** — applies one of two known Natural record layouts (below) and reports **every**
  place reality disagrees with it. If the match rate drops, the report says so instead of quietly
  emitting confident wrong numbers.

### Two file types, one tool

Software AG's SYSOBJH utility produces two very different files that describe the same repository:

1. **Raw unload** (`*H**`/`*C**`/`*D01-04`/`*S**` tags) — the actual source code transfer.
2. **Job-log report** — SYSOBJH's own SYSPRINT: a human-readable, fixed-132-column table (one row
   per object: library, object name, TYPE spelled out as a full word, date, time, user, status) with
   no source code at all. Confirmed to describe the same repository as the raw unload by joining
   identical library+name+timestamp rows across a real pair of these files (e.g. `A/ASKZBTP1` saved
   `2015-03-08 15:00:15` appears in both).

The "פרופיל מבנה" selector defaults to **אוטומטי**, which sniffs the file (looks for `*H**`/`*C**`
tags vs. the report's fixed-column date fingerprint) and picks the right profile — upload either file
type and it's handled correctly, no manual switch needed. Both profiles can also be forced manually,
and picking the wrong one on purpose is a good way to see the tool correctly refuse to match.

### Cross-checking a pair (the "הצלבה" tab)

A second, optional file input lets you load a raw unload and its matching job-log report **together**.
Once both finish scanning, every report row with `STATUS=UNLOADED` is looked up in the raw file's
object index; anything the report claims succeeded but that isn't actually in the raw unload is
surfaced as a concrete list (library, name, type, date, time, user) — not just a count. A row the
report itself flagged as failed is skipped here (its own status already says so).

Verified against a synthetic full-coverage pair built from the real 216-object sample: 215/215 matched
with zero false positives, and a deliberately deleted object was caught as exactly one miss, nothing
else. **Important**: if either file was only partially scanned (a byte limit, or these are excerpts of
larger files — like the two ~1 MB samples this tool was validated against), a large "missing" count is
expected and does **not** mean data was lost — it means the two scanned windows don't cover the same
objects. The tool detects this and downgrades its own verdict accordingly; treat cross-check numbers as
meaningful only when both sides were fully scanned.

### Loading many files: folder pickers

JCL jobs and COBOL programs are one file each — hundreds or thousands of them, not one big dump —
so both inputs are folder pickers (`webkitdirectory`): one click selects the whole directory instead
of hand-picking files one at a time. A secondary "or individual files" button next to each is a plain
multi-select fallback for when only a handful of files are wanted. Neither streams — these files are
small (a few KB each), so each one is read in full with `File.text()`.

### JCL → Natural program links (the "JCL" tab)

Each JCL file is parsed for the two ways a job invokes a program, confirmed against 5 real jobs:

* **Natural batch** — the EXEC line doesn't name the program at all; it runs a shared PROC (seen as
  `NATB240`) and hands the real library + program name through in-stream `CMSYNIN` input:
  `LOGON RC` (or a bare `RC` with no `LOGON` keyword — both forms appear in the same job) followed by
  the program name, terminated by `FIN`.
* **Direct** — `EXEC PGM=xxx`. In every sample seen so far this is always a vendor utility (`SORT`,
  `FTP`) rather than a custom program; anything that isn't a recognised utility name is called out
  separately as a likely custom-program candidate.

If a raw-unload file is also loaded (either slot), every Natural-batch reference is looked up in its
object index by library+name. Verified two ways: a synthetic full-coverage raw file built from 5 real
object rows the user confirmed independently (`RC/DOHUZDP2`, `RC/GO0701P0`, `RC/HICNEWN3`) resolved
3/3 with no false positives — including correctly picking the `RC`-library copy of `GO0701P0` over two
older duplicate copies of the same program sitting in different libraries (`GOCOPY`, `GOGO`), proving
the match is library-aware, not just name-aware.

### COBOL/CICS call graph (the "COBOL/CICS" tab)

A folder of COBOL programs (one object per file, same convention as everything else here). Each file
is parsed for its `PROGRAM-ID`, whether it declares itself CICS (`01 SAP-OPTIONS TPMONITOR UTP-CICS.`),
and every way it can reference another program — confirmed against two real files, one plain batch and
one CICS transaction:

* `CALL 'name' USING ...` — an ordinary COBOL subroutine call.
* `EXEC CICS LINK PROGRAM('name') ...` — synchronous call to another CICS program (COMMAREA as the
  argument buffer).
* `EXEC CICS XCTL PROGRAM('name') ...` — transfer control to another program. Not seen in either
  sample yet, but a standard CICS verb, so it's parsed for.
* `EXEC CICS START TRANSID('name') ...` — starts a separate transaction asynchronously. Its target is
  a TRANSID, not necessarily a `PROGRAM-ID`, so it's reported in its own table and never counted as
  "unresolved" alongside the other three.

Unlike the JCL check, this cross-checks against **itself**: every file's `PROGRAM-ID` becomes the
ground truth, and every `CALL`/`LINK`/`XCTL` target is looked up against that same set — so it only
resolves well when the whole folder (or at least everything these programs call) was actually
selected. Comment lines (COBOL's column-7 `*`) are skipped before extraction, so old commented-out
calls aren't reported as live dependencies.

Verified against the two real sample files (0/18 resolved, correctly — neither file calls the other,
so every call is expected to miss) and, to prove the resolver itself works, a synthetic third file
declaring `PROGRAM-ID. INVERSE.` added to the same set: resolution correctly jumped to 14/18, with
every `INVERSE` call (the program's own RTL-Hebrew helper, called 14 times) now marked found.

### One screen for everything (the "תמונת מצב כוללת" / dashboard tab)

Appears automatically — and becomes the active tab — the moment more than the single base file is
loaded (any of: a second file for cross-checking, a JCL folder, a COBOL folder). With just one file,
it stays hidden and the existing Overview tab already covers it, so there's nothing to switch to.
Pulls the headline number and verdict out of every loaded source into one table, instead of clicking
through each tab to piece it together. Explicitly says when JCL and COBOL haven't been linked to each
other yet (true as of this writing — no sample JCL has shown an `EXEC PGM=` naming a custom COBOL
program, only vendor utilities), rather than implying a connection that hasn't actually been found.

## Output

* **`discovery-log.json`** — the analysis log, capped and aggregated (~60–90 KB even for a 250 MB
  scan). This is the artifact to send on for further analysis: histograms, samples, dependency
  graphs, and every anomaly with record numbers and raw text.
* **CSV inventory** — `*-objects.csv` (raw-unload profile: library, name, type, version, dates, size,
  line counts) or `*-rows.csv` (job-log profile: library, name, type word + letter, S/C, DBID/FNR,
  date, time, user, status) — whichever profile actually ran.

## Record layout used: raw unload

Character offsets are 0-based, applied **after** decoding. Every record is padded to a multiple of
**12 characters**.

| Record | Meaning | Fields |
|---|---|---|
| `*H**` | File header, once | product, Natural version, unload timestamp, OS |
| `*C**` | Object catalog entry | library@36:8, name@44:32, type@76:1 |
| `*D01` | Directory | version@7:4, type@11:1, library@13:8, name@21:32, three 8-char user fields |
| `*D02` | Timestamps | saved@16:15, cataloged@31:15, size@46:10 (`YYYYMMDDHHMMSSt`) |
| `*D03` | Environment | OS@4:8, TP monitor@12:8 |
| `*D04` | Codepage | @21:8 |
| `*S**` | Source line | content from offset 4, space-padded |
| `-S**` | Source line variant | same layout as `*S**`; seen on a real 770 MB scan as ~1.1% of all records — mechanically identical (same width, same offset-4 payload, valid Natural content, often internal DDM/view structure directives), parsed the same way but tracked under its own tag. Why Natural uses `-` here isn't confirmed against vendor docs. |

### Object type letters

Originally read from source bodies alone; now cross-referenced against a real job-log report, which
spells types out as full words. Every letter but `G` is **confirmed** this way. A body that
contradicts its declared type is still reported as `declared_type_vs_body_mismatch` at runtime —
that anomaly is what caught `G` being wrong in the first place.

| Letter | Read as | Confidence | Evidence |
|---|---|---|---|
| `F` | Program | confirmed | `DEFINE DATA` + executable statements; report confirms `A/ASKZBTP1` → PROGRAM |
| `N` | Subprogram | confirmed | bodies self-describe as `SUBPROGRAM : <name>`; report confirms |
| `S` | Subroutine | confirmed | bodies self-describe as `SUBROUTINE`; report confirms |
| `M` | Map | confirmed | map prototype header + `DEFINE DATA PARAMETER`; report confirms |
| `L` | Local Data Area | confirmed | internal data-area format (`**DF`/`**DR`/`**C`); report confirms |
| `P` | Parameter Data Area | confirmed | data-area format; bodies say `PARAMETER : <name>`; report confirms |
| `C` | Global Data Area | confirmed | data-area format; report confirms (`ADLDMF/ADBGLOBA` → GLOBAL) |
| `T` | Text | confirmed | free-form text, no Natural syntax; report confirms |
| `G` | **Copycode** | inferred | **not** "Global Data Area" as first guessed — that guess was never seen in the original small sample. On a real scan, COPYCODE:GLOBAL rows in the report (154:35) match this scan's G:C ratio (1066:240) almost exactly once C is pinned to GLOBAL, and 24 of 25 `declared_type_vs_body_mismatch` samples were type G with an executable-looking body — expected for copycode, not a data area |
| `H` | Helproutine | inferred | confirmed as a real type via the report; body shape not independently verified |
| `V` | **DDM** (Adabas field layout) | confirmed | report confirms (`SYSTEM/ACCOUNTING`, saved 2015-08-05 09:55:09, is type V here and "DDM" there). A DDM describes one physical Adabas file's field layout — name/format/length/description rows — not executable Natural, and not the same internal shape as an `L`/`P`/`C` data area either; it just lands in the same `kind: data` bucket |

Letters `7` and `5` are seen in real scans (18× and 1×) but absent from the report sample — still
unclassified. If the scan reports `unknown_object_type_letter`, send the log so the map can extend.

**DDM has the same raw+report pairing as the programs above** — a DDM unload uses the identical
`*H**`/`*C**`/`*D01-04`/`*S**` structure (this profile parses it with no changes needed), and Software
AG's own DDM job-log report uses the identical 132-column table, just with `TYPE=DDM` and (in the one
real pair examined) a different Adabas file number (`DBID/FNR` `240/10` for DDMs vs `240/9` for
programs) — cross-check works across this pair exactly the same way.

## Record layout used: job-log report

Fixed 132-char lines, CRLF. Column 1 is an IBM/ASA print-carriage-control character (`' '`=single
space, `'0'`=double space, `'-'`=triple space, `'1'`=new page), not part of the payload. Offsets
below are 0-based, measured directly off a real report's own header/dashes line.

| Field | Offset:length | Example |
|---|---|---|
| STATUS | 1:30 | `UNLOADED` (anything else is flagged — it means the object may be missing from the raw unload) |
| LIBRARY | 32:8 | `ADLDMF` |
| OBJECT NAME | 41:32 | `ADBGLOBA` |
| TYPE | 74:11 | `GLOBAL` (full word — see the letter table above for the mapping back) |
| S/C | 86:3 | `SRC` |
| DBID/FNR | 90:11 | `240/9` |
| DATE | 102:10 | `1993-05-20` |
| TIME | 113:8 | `10:14:42` |
| USER ID | 122:8 | `L9902D` |

A data row is recognised structurally (leading single-space control char + a `YYYY-MM-DD` date at
the DATE offset), not by matching the literal text "UNLOADED" — so a row with any other status is
still picked up and surfaced, not silently skipped.

## Encodings

Auto-detected, with manual override: UTF-8, Windows-1255, ISO-8859-8, Latin-1, CP862,
**CP424 (EBCDIC Hebrew)**, **CP037 (EBCDIC US)**. The EBCDIC tables are generated from Python's
`codecs` module rather than typed by hand.

EBCDIC is detected by byte 0x40 (EBCDIC space) dominating while ASCII 0x20 is absent; CP424 vs CP037
is then decided by how many bytes fall in the CP424 Hebrew range. Record framing is decided on the
**decoded** text, not raw bytes, because EBCDIC decodes 0x25 to LF and 0x15 to NEL — a raw scan for
0x0A would see neither and wrongly conclude "fixed length".

The **Overview** tab shows the same records decoded under every candidate codepage side by side, so
the correct one can be picked by eye.

**On `U+FFFD` (replacement character) findings**: the same symptom has two very different causes,
and the tool tells them apart rather than always crying data loss. If `U+FFFD` was already baked into
the raw bytes (checked via the sniffed sample's raw `EF BF BD` byte sequences, independent of which
codepage got picked), it predates this tool and is genuinely gone — flagged `err`. If the chosen
codepage is a single-byte table with unassigned byte values (e.g. windows-1255 leaves several code
points undefined) and no pre-existing `U+FFFD` was found, the replacement characters were most likely
produced by *this decode*, not by prior loss — flagged `warn`, with a suggestion to try a sibling
codepage (ISO-8859-8, CP862, or the EBCDIC pair) and compare.

## Findings so far

**1 MB sample (raw unload, first MB of a larger file):** clean match, 21,203 records, 216 objects,
20,126 source lines, 100.000% recognised. Export written by Natural 8.2.07 on MVS/ESA, 2026-08-13;
objects themselves date 1989–2015, catalogued under Natural 2.1.05–8.2.03. Libraries: `ADLDMF` (197),
`ADLIVP` (16), `A` (2) — mostly the Adabas/DL-I Bridge product library. 2,179 `U+FFFD` characters
across 271 records, confirmed pre-existing in the raw UTF-8 bytes — genuinely unrecoverable from this
file, needs re-export with a Hebrew codepage or as raw bytes.

**770.5 MB real scan (raw unload, full file):** 18,505,357 records, 80,476 objects, ~17.9M source
lines, 98.877% recognised — and the entire gap turned out to be the `-S**` tag variant above (100% of
the "unknown record" anomaly), so real coverage is effectively ~100% once that's accounted for. Spans
Natural versions 2.1.00–8.2.08 across MVS/ESA, VSE/ESA, DOS/VSE and OS, objects dated 1987–2026 (a
~39-year-old estate). 448 distinct libraries with heavy copy-before-you-change duplication (`RC`,
`RC1`…`RC11`, `RCOLD`, `RCSIGAL`, `ZM`/`ZGD`/`ASP` families). Only 2,114 `U+FFFD` characters (0.01% of
records) — and the sniff sample showed **no** pre-existing replacement bytes, meaning this damage most
likely came from the windows-1255 auto-pick hitting unassigned byte values, not from prior data loss;
worth re-checking against ISO-8859-8/CP862 before concluding anything is lost. `PERFORM` shows a huge
"unresolved" count (295,907 of 296,015) that is **not** a defect — most PERFORM targets are
`DEFINE SUBROUTINE`/`END-SUBROUTINE` labels internal to their own object, not catalogued objects, so
this check can only ever resolve the small minority that call an external type-S Subroutine.

**1 MB sample (job-log report, first MB of a 10 MB file):** 7,159 rows recognised across 25 libraries,
all status `UNLOADED`. Run by user `S13` against library `SYSTEM` on 2026-08-13 14:24:57; the report
itself was saved back into Natural as Text member `WORKPLAN/4CEX3AM0` — which also explains an odd
finding from the 770 MB scan: the two largest objects by source-line count (`WORKPLAN/4CEX3AM0` and
`WORKPLAN/4CEWYH-6`, ~63,650 lines each) are captured job-log reports like this one, saved as Text —
so the declared-size field apparently doesn't mean "source byte count" for Text-type objects the way
it does for the rest (worth confirming directly rather than assuming).

## Merging with a Python pipeline (in progress)

A separate effort is underway to merge this scanner with `natural-viewer.html` into one tool, driven
by an offline Python pipeline that does what a browser tab can't: hold the full source of 86K+ objects,
build a real call graph, and compute a dead-code likelihood score. The rationale, architecture, and
full task breakdown live in three documents, read in this order:

1. **`MERGE-PLAN.md`** — why this is needed, the gaps between the two existing tools, and the
   dead-code scoring design (propagated `alive`/`dead` likelihood, not binary reachability).
2. **`WORKPLAN.md`** — the 9-stage execution plan, with a **status table at the top** tracking exactly
   what's done and what's next. Check this first before assuming anything below is current.
3. **`SCHEMAS.md`** — the field-by-field contract between every file the pipeline produces and what
   `natural-viewer.html` already reads.

### What exists so far (`pipeline/`)

CLI tools, stdlib `unittest` tests only (`python3 -m unittest discover -s pipeline/tests`), thresholds
and paths read from `pipeline/config.yaml` rather than hardcoded. Each one below has been run against
the real ~800 MB estate this project targets, not just samples — `WORKPLAN.md`'s status table has the
exact validation (usually: an independent cross-check against this same browser tool's own
`discovery-log.json`, on the same real file).

| Tool | Reads | Writes | What it does |
|---|---|---|---|
| `natunload_split.py` | the raw SYSOBJH unload | `out/source/<LIBRARY>/<NAME>.nat` + `objects.jsonl` | Splits the unload into one source file per object plus a metadata row each (type, timestamps, size, content hashes for dedup) |
| `hash_report.py` | `objects.jsonl` | a JSON report (stdout, or `--out <file>`) | Groups objects by normalized content hash: overall duplication ratio, cross-library "shadow copy" families, whole libraries fully duplicated elsewhere |
| `jclmap.py` | a JCL folder | `jcl.json` | Extracts which Natural program (and library) each JCL job actually runs, plus any `STEPLIB`/`NATLIB` DD chain found |
| `cobolmap.py` | a COBOL/CICS folder + (optionally) `objects.jsonl`/`out/source/` | `cobol.json` | Extracts each COBOL program's `PROGRAM-ID`, CICS flag, and `CALL`/`LINK`/`XCTL`/`START` targets; if Natural source is available, also bridges any Natural `CALL '<x>'` whose target matches a COBOL `PROGRAM-ID` |

Run any of them with `--help` for its exact options; each defaults its input/output paths from
`pipeline/config.yaml` when `--*-dir`/`--*-file` isn't passed explicitly.

`cobol.json`'s `natural_bridge[]` field is `cobolmap.py`'s own interpretation of an underspecified
part of `SCHEMAS.md` (which doesn't say how that field is derived) — see `WORKPLAN.md`'s status table
(stage 4.2) for the reasoning. Everything else in `cobol.json` matched a real discovery-log.json
cross-check exactly.

**Not built yet:** the call graph (`natmap3.py`), activity/report ingestion, the dead-code scoring
engine, and the merged HTML tool itself. `WORKPLAN.md`'s status table is the source of truth for
what's next — this list is not.
