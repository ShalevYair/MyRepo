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
* **L2 profile** — applies the Natural record layout below and reports **every** place reality
  disagrees with it. If the match rate drops, the report says so instead of quietly emitting
  confident wrong numbers.

## Output

* **`discovery-log.json`** — the analysis log, capped and aggregated (~60 KB even for a 250 MB scan).
  This is the artifact to send on for further analysis: histograms, samples, dependency graphs,
  and every anomaly with record numbers and raw text.
* **`objects.csv`** — the full object inventory (library, name, type, version, dates, size, line counts).

## Record layout used

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

### Object type letters

Derived by reading the actual source bodies, **not** from vendor documentation — and **not** the
standard Natural user-facing letter set (where `P`=Program, `A`=PDA, `G`=GDA). Each mapping ships
with its evidence and a confidence flag, and the scanner re-derives the shape of every body at
runtime; a body that contradicts its declared type is reported as `declared_type_vs_body_mismatch`.

| Letter | Read as | Confidence | Evidence |
|---|---|---|---|
| `F` | Program | inferred | `DEFINE DATA` + executable statements; no `DEFINE FUNCTION` anywhere |
| `N` | Subprogram | high | bodies self-describe as `SUBPROGRAM : <name>` |
| `S` | Subroutine | high | bodies self-describe as `SUBROUTINE` |
| `M` | Map | high | map prototype header + `DEFINE DATA PARAMETER` |
| `L` | Local Data Area | high | internal data-area format (`**DF`/`**DR`/`**C`) |
| `P` | Parameter Data Area | inferred | data-area format; bodies say `PARAMETER : <name>` |
| `C` | Global Data Area | inferred | data-area format; every observed name ends in `G` |
| `T` | Text | high | free-form text, no Natural syntax |

If the scan reports `unknown_object_type_letter`, the map needs extending — the log carries the samples.

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

## Findings on the supplied 1 MB sample

* Clean match: 21,203 records, 216 objects, 20,126 source lines, 100.000% of records recognised.
* Export written by Natural 8.2.07 on MVS/ESA, 2026-08-13. Objects themselves date 1989–2015 and
  were catalogued under Natural 2.1.05–8.2.03.
* Libraries: `ADLDMF` (197), `ADLIVP` (16), `A` (2) — mostly the Adabas/DL-I Bridge product library.
* **2,179 `U+FFFD` characters across 271 records.** Every Hebrew comment in the export is already
  destroyed. `*D04` declares codepage `IBM01140`, a Latin-1 EBCDIC page that cannot represent Hebrew,
  so the conversion mapped Hebrew bytes to the replacement character. **This is not recoverable from
  this file** — it needs re-exporting from the mainframe with a Hebrew codepage (CP424/CP803/CP12712)
  or as raw untranslated bytes.
* The sample ends mid-record, as expected for the first 1 MB of a larger file.
