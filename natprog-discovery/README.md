# NATPROG Discovery

A local, browser-only discovery tool for very large mainframe source-unload files
(built and tested against a Software AG **Natural** SYSOBJH/SYSTRANS export; target size 800 MB).

Three files, no build step, no dependencies, no network. **The file never leaves the machine.**

```
natprog-discovery/
├── index.html
├── style.css
└── app.js
```

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

Letters `7` and `5` are seen in real scans (18× and 1×) but absent from the report sample — still
unclassified. If the scan reports `unknown_object_type_letter`, send the log so the map can extend.

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
