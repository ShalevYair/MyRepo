/* =====================================================================
 * NATPROG Discovery — streaming structural discovery for very large
 * mainframe source-unload files (tested target: 800 MB, browser-local).
 *
 * Design notes:
 *  - Nothing is ever fully loaded into memory. The file is read with
 *    File.slice() in chunks and decoded incrementally.
 *  - No Web Worker: Blob-workers are blocked under file:// in Chrome,
 *    so we yield to the event loop between chunks instead.
 *  - Two analysis layers:
 *      L1 "generic"  — assumes nothing, answers "what IS this file?"
 *      L2 "profile"  — applies a known record layout and reports every
 *                      place reality disagrees with it.
 *    If L2 disagrees too often, the report says so loudly instead of
 *    quietly producing wrong numbers.
 * ===================================================================== */
'use strict';

/* ---------------------------------------------------------------- *
 * Single-byte codepage tables.
 * Generated from Python's `codecs` module (authoritative), not typed
 * by hand.  Index = byte value, value = Unicode character.
 * ---------------------------------------------------------------- */
const CP_TABLES = {
  cp037: "\u0000\u0001\u0002\u0003\t\u000b\f\r\u000e\u000f\u0010\u0011\u0012\u0013\b\u0018\u0019\u001c\u001d\u001e\u001f\n\u0017\u001b\u0005\u0006\u0007\u0016\u0004\u0014\u0015\u001a  âäàáãåçñ¢.<(+|&éêëèíîïìß!$*);¬-/ÂÄÀÁÃÅÇÑ¦,%_>?øÉÊËÈÍÎÏÌ`:#@'=\"Øabcdefghi«»ðýþ±°jklmnopqrªºæ¸Æ¤µ~stuvwxyz¡¿ÐÝÞ®^£¥·©§¶¼½¾[]¯¨´×{ABCDEFGHI­ôöòóõ}JKLMNOPQR¹ûüùúÿ\\÷STUVWXYZ²ÔÖÒÓÕ0123456789³ÛÜÙÚ",
  cp424: "\u0000\u0001\u0002\u0003\t\u000b\f\r\u000e\u000f\u0010\u0011\u0012\u0013\b\u0018\u0019\u001c\u001d\u001e\u001f\n\u0017\u001b\u0005\u0006\u0007\u0016\u0004\u0014\u0015\u001a אבגדהוזחט¢.<(+|&יךכלםמןנס!$*);¬-/עףפץצקרש¦,%_>?�ת�� ���‗`:#@'=\"�abcdefghi«»���±°jklmnopqr���¸�¤µ~stuvwxyz�����®^£¥·©§¶¼½¾[]¯¨´×{ABCDEFGHI­�����}JKLMNOPQR¹�����\\÷STUVWXYZ²�����0123456789³����",
  cp862: "\u0000\u0001\u0002\u0003\u0004\u0005\u0006\u0007\b\t\n\u000b\f\r\u000e\u000f\u0010\u0011\u0012\u0013\u0014\u0015\u0016\u0017\u0018\u0019\u001a\u001b\u001c\u001d\u001e\u001f !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~אבגדהוזחטיךכלםמןנסעףפץצקרשת¢£¥₧ƒáíóúñÑªº¿⌐¬½¼¡«»░▒▓│┤╡╢╖╕╣║╗╝╜╛┐└┴┬├─┼╞╟╚╔╩╦╠═╬╧╨╤╥╙╘╒╓╫╪┘┌█▄▌▐▀αßΓπΣσµτΦΘΩδ∞φε∩≡±≥≤⌠⌡÷≈°∙·√ⁿ²■ ",
};

/* ================================================================== *
 * Small utilities
 * ================================================================== */

/** Increment map[key], funnelling excess distinct keys into an overflow
 *  bucket so a pathological file cannot exhaust memory. */
function bump(map, key, cap) {
  const v = map.get(key);
  if (v !== undefined) { map.set(key, v + 1); return; }
  if (cap && map.size >= cap) {
    map.set('«overflow»', (map.get('«overflow»') || 0) + 1);
    return;
  }
  map.set(key, 1);
}

/** Map -> array of [key,count] sorted by count desc, capped to `n`. */
function topN(map, n) {
  const a = [...map.entries()];
  a.sort((x, y) => y[1] - x[1] || String(x[0]).localeCompare(String(y[0])));
  return n ? a.slice(0, n) : a;
}

/** Fast right-trim of ASCII spaces only (hot path). */
function rtrim(s) {
  let e = s.length;
  while (e > 0 && s.charCodeAt(e - 1) === 32) e--;
  return e === s.length ? s : s.slice(0, e);
}

function fmtBytes(n) {
  if (n < 1024) return n + ' B';
  const u = ['KB', 'MB', 'GB', 'TB'];
  let i = -1, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(v < 10 ? 2 : 1) + ' ' + u[i];
}
function fmtNum(n) { return (n === null || n === undefined) ? '' : n.toLocaleString('en-US'); }

/** Make control characters visible so log samples stay readable. */
function visible(s, max) {
  max = max || 200;
  let out = '';
  for (let i = 0; i < s.length && i < max; i++) {
    const c = s.charCodeAt(i);
    if (c === 9) out += '\\t';
    else if (c === 13) out += '\\r';
    else if (c === 10) out += '\\n';
    else if (c < 32 || c === 127) out += '\\x' + c.toString(16).padStart(2, '0');
    else out += s[i];
  }
  if (s.length > max) out += '…(+' + (s.length - max) + ')';
  return out;
}

/* ================================================================== *
 * Decoders
 * ================================================================== */

/** Streaming decoder for a single-byte codepage table. Stateless across
 *  chunks, which is exactly what we want for fixed-byte encodings. */
function tableDecoder(tbl) {
  const codes = new Uint16Array(256);
  for (let i = 0; i < 256; i++) codes[i] = tbl.charCodeAt(i);
  return {
    decode(u8) {
      const n = u8.length;
      const buf = new Uint16Array(n);
      for (let i = 0; i < n; i++) buf[i] = codes[u8[i]];
      let out = '';
      const STEP = 32768;                       // avoid apply() arg limits
      for (let i = 0; i < n; i += STEP) {
        out += String.fromCharCode.apply(null, buf.subarray(i, Math.min(i + STEP, n)));
      }
      return out;
    }
  };
}

/** Byte values that carry Hebrew letters in a given EBCDIC table.
 *  Computed from the table, so it stays correct if a table is swapped. */
function hebrewBytesOf(tableName) {
  const t = CP_TABLES[tableName];
  const set = [];
  if (!t) return set;
  for (let b = 0; b < 256; b++) {
    const c = t.charCodeAt(b);
    if (c >= 0x05d0 && c <= 0x05ea) set.push(b);
  }
  return set;
}
const CP424_HEBREW_BYTES = hebrewBytesOf('cp424');

const TEXT_DECODER_NAMES = {
  'utf-8': 'utf-8',
  'windows-1255': 'windows-1255',
  'iso-8859-8': 'iso-8859-8',
  'latin1': 'windows-1252'   // closest byte-preserving label TextDecoder offers
};

function makeDecoder(enc) {
  if (CP_TABLES[enc]) return tableDecoder(CP_TABLES[enc]);
  if (enc === 'latin1') {
    // True Latin-1: byte value === code point. windows-1252 remaps 0x80-0x9F,
    // so build the identity table ourselves.
    let t = '';
    for (let i = 0; i < 256; i++) t += String.fromCharCode(i);
    return tableDecoder(t);
  }
  const name = TEXT_DECODER_NAMES[enc] || 'utf-8';
  const d = new TextDecoder(name, { fatal: false });
  return { decode: (u8, more) => d.decode(u8, { stream: !!more }) };
}

/* ================================================================== *
 * Encoding sniffer — runs on the first sample block, before parsing.
 * ================================================================== */
function sniffEncoding(u8) {
  const n = u8.length;
  let ascii = 0, high = 0, ctrl = 0, nul = 0, lf = 0, cr = 0, crlf = 0;
  let ebcdicSpace = 0, ebcdicNL = 0, asciiSpace = 0;
  const freq = new Uint32Array(256);
  for (let i = 0; i < n; i++) {
    const b = u8[i];
    freq[b]++;
    if (b === 0) nul++;
    if (b === 0x0a) { lf++; if (i > 0 && u8[i - 1] === 0x0d) crlf++; }
    if (b === 0x0d) cr++;
    if (b === 0x20) asciiSpace++;
    if (b === 0x40) ebcdicSpace++;
    if (b === 0x15) ebcdicNL++;
    if (b >= 0x80) high++;
    else if (b >= 0x20 && b < 0x7f) ascii++;
    else if (b !== 9 && b !== 10 && b !== 13) ctrl++;
  }

  // Is the byte stream valid UTF-8?
  let utf8Valid = true;
  try { new TextDecoder('utf-8', { fatal: true }).decode(u8.subarray(0, Math.min(n, 1 << 20))); }
  catch (e) { utf8Valid = false; }

  // U+FFFD already baked into the bytes (EF BF BD) = prior lossy conversion.
  let fffd = 0;
  for (let i = 0; i + 2 < n; i++) {
    if (u8[i] === 0xef && u8[i + 1] === 0xbf && u8[i + 2] === 0xbd) { fffd++; i += 2; }
  }

  const spaceRatioAscii = asciiSpace / n;
  const spaceRatioEbcdic = ebcdicSpace / n;

  // Byte 0x40 is the EBCDIC space. Padded mainframe records are mostly padding,
  // so in EBCDIC 0x40 dominates while ASCII 0x20 is essentially absent.
  // (Do NOT test "ascii printable ratio" here: 0x40 falls inside 0x20-0x7E.)
  const looksEbcdic = spaceRatioEbcdic > 0.05 &&
                      spaceRatioEbcdic > spaceRatioAscii * 5 &&
                      high / n > 0.10;

  let hebrewEbcdic = 0;
  for (const b of CP424_HEBREW_BYTES) hebrewEbcdic += freq[b];
  const hebrewRatio = hebrewEbcdic / n;

  let guess = 'utf-8', why;
  if (looksEbcdic) {
    // Hebrew comments in an otherwise-English source base are a small share of
    // the bytes, so the bar has to be low. Accented Latin (what these same bytes
    // mean under CP037) is vanishingly rare in mainframe source, so a few hundred
    // hits is already strong evidence for CP424.
    guess = (hebrewEbcdic >= 200 && hebrewRatio > 0.0002) ? 'cp424' : 'cp037';
    why = 'EBCDIC detected: byte 0x40 (EBCDIC space) is ' + (spaceRatioEbcdic * 100).toFixed(1) +
          '% of the sample while ASCII space 0x20 is ' + (spaceRatioAscii * 100).toFixed(2) +
          '%, and ' + (high / n * 100).toFixed(1) + '% of bytes are >= 0x80. ' +
          (guess === 'cp424'
            ? hebrewEbcdic.toLocaleString() + ' bytes (' + (hebrewRatio * 100).toFixed(3) +
              '%) fall in the CP424 Hebrew range -> guessing CP424 (EBCDIC Hebrew).'
            : 'Only ' + hebrewEbcdic.toLocaleString() + ' bytes fall in the CP424 Hebrew range ' +
              '-> guessing CP037 (EBCDIC US). If Hebrew is expected, switch the codepage manually ' +
              'and compare against the preview below.');
  } else if (utf8Valid) {
    guess = 'utf-8';
    why = 'Byte stream is valid UTF-8.' + (fffd ? ' NOTE: contains ' + fffd +
          ' pre-existing U+FFFD replacement characters (data was already lost upstream).' : '');
  } else if (high / n > 0.005) {
    guess = 'windows-1255';
    why = 'Not valid UTF-8, ' + (high / n * 100).toFixed(2) + '% high bytes -> single-byte Hebrew codepage likely.';
  } else {
    guess = 'latin1';
    why = 'Not valid UTF-8 and almost no high bytes; falling back to byte-preserving Latin-1.';
  }

  return {
    guess, why, sampledBytes: n,
    utf8Valid, replacementSeqInSample: fffd,
    ebcdicHebrewByteRatio: +hebrewRatio.toFixed(5),
    counts: { ascii, high, ctrl, nul, lf, cr, crlf },
    ratios: {
      asciiPrintable: +(ascii / n).toFixed(4),
      highBytes: +(high / n).toFixed(4),
      asciiSpace: +spaceRatioAscii.toFixed(4),
      ebcdicSpace: +spaceRatioEbcdic.toFixed(4)
    },
    lineEnding: crlf > 0 && crlf >= lf * 0.9 ? 'CRLF'
              : lf > 0 ? (crlf ? 'mixed (CRLF+LF)' : 'LF')
              : cr > 0 ? 'CR only'
              : 'none found',
    ebcdicNLBytes: ebcdicNL
  };
}

/** Decode the same few records under several codepages so a human can see at a
 *  glance which one produces real text. Runs only on the sniff sample. */
function decodePreview(u8, encodings, nLines) {
  nLines = nLines || 4;
  const out = {};
  const slice = u8.subarray(0, Math.min(u8.length, 65536));
  for (const enc of encodings) {
    try {
      const text = makeDecoder(enc).decode(slice);
      const parts = text.split(/\n|\u0085/).map(l => rtrim(l.replace(/\r$/, '')));
      const picked = [];
      for (const l of parts) {
        if (l.trim().length > 12) picked.push(l.slice(0, 110));
        if (picked.length >= nLines) break;
      }
      out[enc] = picked.length ? picked : [text.slice(0, 110)];
    } catch (e) { out[enc] = ['<decode failed: ' + e.message + '>']; }
  }
  return out;
}

/* ================================================================== *
 * PROFILE: Software AG Natural object unload (SYSOBJH / SYSTRANS work
 * file), as observed in NATPROG exports.
 *
 * Column offsets below were derived empirically from a real export and
 * are 0-based character offsets *after* decoding.  Every field carries a
 * confidence flag; anything marked 'inferred' is a best-fit reading of
 * the data, not a quote from Software AG documentation. The scanner
 * validates them at runtime and logs disagreements rather than trusting
 * them blindly.
 * ================================================================== */

const NAT_PROFILE = {
  id: 'natural-sysobjh',
  recordPad: 12,              // every record length observed as a multiple of 12 chars
  prefixLen: 4,
  known: ['*H**', '*C**', '*D01', '*D02', '*D03', '*D04', '*S**'],

  // [offset, length, confidence]
  H:   { flag:[4,1,'inferred'], prod:[5,3,'high'], version:[8,4,'high'],
         timestamp:[12,15,'high'], os:[27,8,'high'], userOrNode:[60,8,'inferred'],
         versionText:[70,4,'inferred'] },
  C:   { library:[36,8,'high'], name:[44,32,'high'], type:[76,1,'high'],
         srcObj:[78,1,'inferred'], flagN:[82,1,'inferred'], num:[84,4,'inferred'] },
  D01: { prod:[4,3,'high'], version:[7,4,'high'], type:[11,1,'high'],
         library:[13,8,'high'], name:[21,32,'high'],
         user1:[53,8,'inferred'], user2:[61,8,'inferred'], user3:[69,8,'inferred'],
         flagS:[77,1,'inferred'] },
  D02: { savedTs:[16,15,'high'], catalogedTs:[31,15,'high'], size:[46,10,'high'] },
  D03: { os:[4,8,'high'], tpMonitor:[12,8,'high'], tpExtra:[20,8,'inferred'] },
  D04: { codepage:[21,8,'high'] },

  /* Object-type letters.  Derived by reading the actual source bodies,
     NOT from the standard Natural user-facing letter set (which differs:
     there P=Program, A=PDA, G=GDA).  Confidence is per-letter. */
  typeMap: {
    // "confirmed" = cross-referenced by name+library+timestamp against a real
    // SYSOBJH job-log report, which spells the type out as a full word
    // (PROGRAM/MAP/LOCAL/GLOBAL/SUBPROGRAM/PARAMETER/SUBROUTINE/TEXT/COPYCODE/
    // HELPROUTINE). That report is not vendor documentation either, but it is
    // Software AG's own tool naming its own objects, so it outranks our
    // source-body heuristics.
    F: { name: 'Program',             kind: 'exec', conf: 'confirmed',
         evidence: 'Bodies contain DEFINE DATA + executable statements, no DEFINE FUNCTION; ' +
                    'confirmed via SYSOBJH report (A/ASKZBTP1, 2015-03-08 15:00:15 -> "PROGRAM").' },
    N: { name: 'Subprogram',          kind: 'exec', conf: 'confirmed',
         evidence: 'Bodies self-describe as "SUBPROGRAM : <name>"; confirmed via SYSOBJH report ' +
                    '(ADLDMF/ADLCSTO -> "SUBPROGRAM").' },
    S: { name: 'Subroutine',          kind: 'exec', conf: 'confirmed',
         evidence: 'Bodies self-describe as "SUBROUTINE"; confirmed via SYSOBJH report ' +
                    '(ADLIVP/ADBXPAS1 -> "SUBROUTINE").' },
    M: { name: 'Map',                 kind: 'map',  conf: 'confirmed',
         evidence: 'Bodies carry the map prototype header + DEFINE DATA PARAMETER; confirmed via ' +
                    'SYSOBJH report (ADLDMF/#L9902D -> "MAP").' },
    L: { name: 'Local Data Area',     kind: 'data', conf: 'confirmed',
         evidence: 'Bodies are internal data-area format (**DF/**DR/**C lines); confirmed via ' +
                    'SYSOBJH report (ADLDMF/ADBLOC-D -> "LOCAL").' },
    P: { name: 'Parameter Data Area', kind: 'data', conf: 'confirmed',
         evidence: 'Data-area format; bodies self-describe as "PARAMETER : <name>"; confirmed via ' +
                    'SYSOBJH report (ADLDMF/ADLECB-A -> "PARAMETER").' },
    C: { name: 'Global Data Area',    kind: 'data', conf: 'confirmed',
         evidence: 'Data-area format; confirmed via SYSOBJH report (ADLDMF/ADBGLOBA -> "GLOBAL").' },
    T: { name: 'Text',                kind: 'text', conf: 'confirmed',
         evidence: 'Free-form text, no Natural syntax; confirmed via SYSOBJH report ' +
                    '(ADLDMF/USR0010T -> "TEXT").' },
    G: { name: 'Copycode',            kind: 'exec', conf: 'inferred',
         evidence: 'NOT "Global Data Area" as earlier guessed. Two independent signals: (1) in a ' +
                    'SYSOBJH report sample, COPYCODE:GLOBAL rows were 154:35 (ratio 4.4), matching ' +
                    'this scan\'s G:C ratio of 1066:240 (ratio 4.44) almost exactly, while C is now ' +
                    'confirmed as GLOBAL; (2) 24 of 25 declared_type_vs_body_mismatch samples in this ' +
                    'very scan are type G with a body that "looks like exec" — expected for reusable ' +
                    'code fragments (copycode), not for a data area.' },
    H: { name: 'Helproutine',         kind: 'exec', conf: 'inferred',
         evidence: 'Confirmed as a real type via SYSOBJH report ("HELPROUTINE", e.g. ALEX/AL0002H0); ' +
                    'kind=exec not independently verified against a body sample yet.' },
    A: { name: 'Parameter Data Area?',kind: 'data', conf: 'guess', evidence: 'not seen in sample' },
    4: { name: 'Class?',              kind: 'exec', conf: 'guess', evidence: 'not seen in sample' },
    8: { name: 'Adapter?',            kind: 'exec', conf: 'guess', evidence: 'not seen in sample' },
    7: { name: 'UNKNOWN',             kind: '?',    conf: 'none',
         evidence: 'Seen 18x in a real 770MB scan (e.g. SYSEXPG/FUNCAX02, SYSEXV/V82FUNCA) but no ' +
                    'matching row found in the SYSOBJH report sample. Needs a source-body sample to classify.' },
    5: { name: 'UNKNOWN',             kind: '?',    conf: 'none',
         evidence: 'Seen once in a real 770MB scan (NCSTDEMO/NCPDEMO). Needs a source-body sample to classify.' },
    V: { name: 'DDM (Adabas field layout)', kind: 'data', conf: 'confirmed',
         evidence: 'Confirmed via a SYSOBJH job-log report cross-reference (SYSTEM/ACCOUNTING, saved ' +
                    '2015-08-05 09:55:09, appears as type V here and as "DDM" there). A DDM describes the ' +
                    "field layout of one physical Adabas file — not a program; its body is a field list " +
                    "(name, format N/A/B/D/T, length, description), not executable Natural." }
  }
};

/* ================================================================== *
 * PROFILE 2: SYSOBJH job-log / print report (SYSPRINT of the SAME
 * SYSOBJH utility, not the raw *H** / *C** / *D0x / *S** unload above).
 *
 * Fixed 132-char lines, CRLF, column 1 is an IBM/ASA print-carriage-
 * control character (' '=single space, '0'=double space, '-'=triple
 * space, '1'=new page) — not part of the payload. One data row per
 * unloaded object, with the object type spelled out as a full word
 * rather than the single letter the raw unload uses.
 *
 * Column offsets (0-based, after decoding) were measured directly off
 * a real report sample using its own header/dashes line, not guessed.
 * ================================================================== */
const REPORT_TYPE_TO_LETTER = {
  PROGRAM: 'F', SUBPROGRAM: 'N', MAP: 'M', LOCAL: 'L', PARAMETER: 'P',
  GLOBAL: 'C', TEXT: 'T', COPYCODE: 'G', SUBROUTINE: 'S', HELPROUTINE: 'H', DDM: 'V'
};

const NAT_REPORT_PROFILE = {
  id: 'natural-sysobjh-report',
  row: {                    // [offset, length]
    status:  [1, 30], library: [32, 8], name: [41, 32], type: [74, 11],
    sc: [86, 3], dbidfnr: [90, 11], date: [102, 10], time: [113, 8],
    user: [122, 8], flag: [131, 1]
  }
};

/** Structural fingerprint of a data row: works regardless of STATUS text
 *  (UNLOADED/ERROR/whatever), so a failed row is still recognised. */
function isReportDataRow(l) {
  if (l.length < 112 || l.charCodeAt(0) !== 32) return false;   // ASA "single space" control
  return /^\d{4}-\d{2}-\d{2}$/.test(l.substr(102, 10));
}

/** Cheap sniff over a decoded text sample: does this look like the raw
 *  *H** / *C** / *D0x / *S** unload, the job-log report, or neither? */
function sniffFileProfile(text) {
  const lines = text.split(/\r\n|\n/);
  let rawTags = 0, reportRows = 0, hasBanner = false;
  const sample = lines.slice(0, 2000);
  for (const l of sample) {
    if (l.startsWith('*H**') || l.startsWith('*C**')) rawTags++;
    else if (isReportDataRow(l)) reportRows++;
    if (l.indexOf('NATURAL OBJECT HANDLER') >= 0) hasBanner = true;
  }
  if (rawTags >= 1) return 'natural-sysobjh';
  if (reportRows >= 2 || hasBanner) return 'natural-sysobjh-report';
  return 'none';
}

function fld(line, spec) {
  const s = line.substr(spec[0], spec[1]);
  return rtrim(s);
}

/** Natural timestamp: YYYYMMDDHHMMSSt (t = tenths of a second). */
function parseNatTs(s) {
  if (!/^\d{15}$/.test(s)) return { ok: false, empty: false, raw: s };
  if (s === '000000000000000') return { ok: false, empty: true, raw: s };
  const y = +s.slice(0, 4), mo = +s.slice(4, 6), d = +s.slice(6, 8);
  const h = +s.slice(8, 10), mi = +s.slice(10, 12), se = +s.slice(12, 14);
  if (y < 1960 || y > 2100 || mo < 1 || mo > 12 || d < 1 || d > 31 ||
      h > 23 || mi > 59 || se > 59) return { ok: false, empty: false, raw: s };
  return {
    ok: true, empty: false, raw: s, year: y,
    iso: s.slice(0, 4) + '-' + s.slice(4, 6) + '-' + s.slice(6, 8) + 'T' +
         s.slice(8, 10) + ':' + s.slice(10, 12) + ':' + s.slice(12, 14)
  };
}

/* ---------------- dependency extraction --------------------------- *
 * Only applied to non-comment source lines, gated behind cheap
 * indexOf() checks so the regex engine is not run 16M times.
 * ------------------------------------------------------------------ */
const RE = {
  using:   /\b(LOCAL|GLOBAL|PARAMETER|CONTEXT|INDEPENDENT)\s+USING\s+([A-Z0-9#$&@_.\-]+)/g,
  callnat: /\bCALLNAT\s+'([^']+)'/g,
  fetch:   /\bFETCH\s+(?:RETURN\s+|REPEAT\s+)?'([^']+)'/g,
  perform: /\bPERFORM\s+(?!BREAK\b)([A-Z0-9#$&@_.\-]+)/g,
  map:     /\bUSING\s+MAP\s+'([^']+)'/g,
  include: /\bINCLUDE\s+([A-Z0-9#$&@_.\-]+)/g,
  call3gl: /\bCALL\s+'([^']+)'/g
};

function collect(map, re, s, cap) {
  re.lastIndex = 0;
  let m;
  while ((m = re.exec(s)) !== null) bump(map, m[m.length - 1], cap);
}

/* ================================================================== *
 * Analyzer — the streaming state machine
 * ================================================================== */

const CAPS = {
  anomalyKinds:      80,
  anomalySamples:    25,
  prefixKeys:      4000,
  lengthKeys:      4096,
  libKeys:        20000,
  depKeys:       200000,
  stmtKeys:        5000,
  objectsRetained:400000,
  sampleObjsPerType:  3,
  sampleLines:       18
};

class Analyzer {
  constructor(opts) {
    this.opts = opts;
    this.profileOn = opts.profile === 'natural-sysobjh';
    this.reportOn = opts.profile === 'natural-sysobjh-report';

    /* ---- L1 generic ---- */
    this.g = {
      lines: 0, chars: 0, bytes: 0,
      emptyLines: 0, crlf: 0, lfOnly: 0,
      lenHist: new Map(), prefix4: new Map(), prefix1: new Map(),
      maxLen: 0, maxLenSample: '', minLen: Infinity,
      padMultiple: new Map(),          // len % recordPad
      replacementChars: 0,             // U+FFFD seen after decoding
      linesWithReplacement: 0,
      nonAsciiLines: 0
    };

    /* ---- L2 profile ---- */
    this.p = {
      rec: new Map(),                  // record-prefix counts
      unknown: 0,
      header: null,
      objects: [],                     // retained inventory rows
      objectsSeen: 0, objectsDropped: 0,
      byType: new Map(), byLib: new Map(), byVersion: new Map(),
      byOsTp: new Map(), byCodepage: new Map(), bySavedYear: new Map(),
      srcLines: 0, srcChars: 0,
      sizeSum: 0, sizeMin: Infinity, sizeMax: 0, sizeBad: 0,
      dupKeys: new Map(),
      nameIndex: new Map(),            // "TYPE|NAME" -> true, for link resolution
      stmt: new Map(),
      dep: {
        using: new Map(), callnat: new Map(), perform: new Map(),
        fetch: new Map(), map: new Map(), include: new Map(), call3gl: new Map()
      },
      samples: new Map(),              // type -> [{name, lines[]}]
      contentKind: new Map()           // "declaredType>observedKind" -> count
    };

    /* ---- L2 report (job-log profile) ---- */
    this.rep = {
      dataRows: 0, rows: [], rowsDropped: 0, seen: new Map(), dupRows: new Map(),
      byStatus: new Map(), byType: new Map(), byLibrary: new Map(), byUser: new Map(),
      bySC: new Map(), byYear: new Map(),
      banner: null, runContext: null, reportMember: null, commandLine: null
    };

    this.anomalies = new Map();
    this.cur = null;                   // object under construction
    this.lineNo = 0;
    this.lastLine = null;              // for end-of-scan truncation checks
  }

  /* -------- anomaly recorder -------- */
  note(kind, lineNo, text, extra) {
    let a = this.anomalies.get(kind);
    if (!a) {
      if (this.anomalies.size >= CAPS.anomalyKinds) return;
      a = { kind, count: 0, samples: [] };
      this.anomalies.set(kind, a);
    }
    a.count++;
    if (a.samples.length < CAPS.anomalySamples) {
      const s = { line: lineNo };
      if (text !== undefined) s.text = visible(text, 160);
      if (extra) Object.assign(s, extra);
      a.samples.push(s);
    }
  }

  /* ================= hot path: one logical record ================= */
  feedLine(raw, truncated) {
    this.lineNo++;
    let line = raw;
    if (line.length && line.charCodeAt(line.length - 1) === 13) {
      line = line.slice(0, -1);
      this.g.crlf++;
    } else if (this.opts.lineMode !== 'fixed') {
      this.g.lfOnly++;
    }
    this.lastLine = line;

    const len = line.length;
    const g = this.g;
    g.lines++; g.chars += len;
    if (len === 0) { g.emptyLines++; }
    if (len > g.maxLen) { g.maxLen = len; g.maxLenSample = visible(line, 120); }
    if (len < g.minLen) g.minLen = len;
    bump(g.lenHist, len, CAPS.lengthKeys);
    bump(g.padMultiple, len % NAT_PROFILE.recordPad, 32);

    if (len) {
      bump(g.prefix1, line[0], 300);
      bump(g.prefix4, line.slice(0, 4), CAPS.prefixKeys);
    }

    // Encoding-damage detector: U+FFFD survived into the decoded text.
    if (line.indexOf('�') >= 0) {
      g.linesWithReplacement++;
      let c = 0, i = -1;
      while ((i = line.indexOf('�', i + 1)) >= 0) c++;
      g.replacementChars += c;
      this.note('encoding_replacement_chars', this.lineNo, line,
        { replacementCharsInLine: c });
    }

    if (len === 0) return;
    if (this.profileOn) { this.feedProfile(line, len, truncated); return; }
    if (this.reportOn) { this.feedReportLine(line); return; }
  }

  /* ================= profile layer: raw *H** / *C** / *D0x / *S** unload ================= */
  feedProfile(line, len, truncated) {
    const p = this.p;
    const tag = line.slice(0, 4);

    if (len % NAT_PROFILE.recordPad !== 0 && !truncated) {
      this.note('record_length_not_multiple_of_' + NAT_PROFILE.recordPad,
        this.lineNo, line, { length: len });
    }

    switch (tag) {
      case '*S**': case '-S**': {
        // '-S**' is a real, recurring variant (confirmed on a 770 MB scan: it was
        // 100% of that run's "unknown record" anomaly, ~1.1% of all records,
        // mechanically identical to '*S**' — same width, same offset-4 payload,
        // valid Natural content). Observed payloads look like internal DDM/view
        // structure directives (e.g. "/*DS ... 1AL0002A1", "/*DV ... GN-GLUFA-VIEW")
        // rather than plain statement text. Parsed the same way as '*S**' so source
        // counts and dependency extraction aren't silently short, but tracked under
        // its own tag (not folded into '*S**') and flagged so it stays auditable —
        // the exact reason Natural marks these with '-' instead of '*' is not
        // confirmed against vendor documentation.
        bump(p.rec, tag);
        if (tag === '-S**') this.note('source_line_dash_variant', this.lineNo, line);
        if (!this.cur) { this.note('orphan_source_line', this.lineNo, line); return; }
        this.feedSource(line);
        return;
      }
      case '*C**': {
        bump(p.rec, tag);
        this.finalizeObject();
        this.startObject(line, truncated);
        return;
      }
      case '*D01': case '*D02': case '*D03': case '*D04': {
        bump(p.rec, tag);
        if (!this.cur) { this.note('directory_record_without_object', this.lineNo, line); return; }
        this.feedDirectory(tag, line);
        return;
      }
      case '*H**': {
        bump(p.rec, tag);
        if (p.header) this.note('multiple_header_records', this.lineNo, line);
        else p.header = this.decodeHeader(line);
        return;
      }
      default:
        p.unknown++;
        bump(p.rec, '«unknown»');
        this.note('unknown_record_prefix', this.lineNo, line, { prefix: visible(tag, 8) });
    }
  }

  decodeHeader(line) {
    const H = NAT_PROFILE.H;
    const ts = parseNatTs(fld(line, H.timestamp).padEnd(15, '0'));
    return {
      raw: visible(line, 120),
      flag: fld(line, H.flag),
      product: fld(line, H.prod),
      version: fld(line, H.version),
      versionText: fld(line, H.versionText),
      unloadTimestamp: ts.ok ? ts.iso : null,
      unloadTimestampRaw: fld(line, H.timestamp),
      os: fld(line, H.os),
      userOrNode: fld(line, H.userOrNode)
    };
  }

  startObject(line, truncated) {
    const C = NAT_PROFILE.C;
    this.cur = {
      lineNo: this.lineNo, truncated: !!truncated,
      library: fld(line, C.library),
      name: fld(line, C.name),
      type: line.substr(C.type[0], 1),
      srcObj: fld(line, C.srcObj),
      num: fld(line, C.num),
      version: null, ts1: null, ts2: null, size: null,
      os: null, tp: null, tpExtra: null, codepage: null,
      d: { D01: false, D02: false, D03: false, D04: false },
      srcLines: 0, srcChars: 0, maxSrcLen: 0,
      daLines: 0, execLines: 0, sawMapProto: false,
      sample: null
    };
    const t = this.cur.type;
    if (truncated) {
      this.note('final_record_truncated', this.lineNo, line,
        { hint: 'The last record is cut mid-way. Expected when scanning only part of a file; ' +
                'if the whole file was scanned it means the export itself is incomplete.' });
    } else if (!NAT_PROFILE.typeMap[t]) {
      this.note('unknown_object_type_letter', this.lineNo, line, { typeLetter: visible(t, 4) });
    }
    // keep a few full source heads per type for eyeballing
    const arr = this.p.samples.get(t) || [];
    if (arr.length < CAPS.sampleObjsPerType) {
      this.cur.sample = { name: this.cur.name, library: this.cur.library, lines: [] };
      arr.push(this.cur.sample);
      this.p.samples.set(t, arr);
    }
  }

  feedDirectory(tag, line) {
    const c = this.cur;
    if (c.d[tag.slice(1)]) this.note('duplicate_' + tag + '_for_object', this.lineNo, line, { object: c.library + '/' + c.name });
    c.d[tag.slice(1)] = true;

    if (tag === '*D01') {
      const D = NAT_PROFILE.D01;
      c.version = fld(line, D.version);
      c.user1 = fld(line, D.user1); c.user2 = fld(line, D.user2); c.user3 = fld(line, D.user3);
      const t2 = line.substr(D.type[0], 1);
      const n2 = fld(line, D.name), l2 = fld(line, D.library);
      if (t2 !== c.type) this.note('type_mismatch_C_vs_D01', this.lineNo, line, { C: c.type, D01: t2, object: c.library + '/' + c.name });
      if (n2 !== c.name)  this.note('name_mismatch_C_vs_D01', this.lineNo, line, { C: c.name, D01: n2 });
      if (l2 !== c.library) this.note('library_mismatch_C_vs_D01', this.lineNo, line, { C: c.library, D01: l2 });
      if (fld(line, D.prod) !== 'NAT') this.note('unexpected_product_code_in_D01', this.lineNo, line);

    } else if (tag === '*D02') {
      const D = NAT_PROFILE.D02;
      const a = parseNatTs(line.substr(D.savedTs[0], 15));
      const b = parseNatTs(line.substr(D.catalogedTs[0], 15));
      c.ts1 = a.ok ? a.iso : null; c.ts2 = b.ok ? b.iso : null;
      c.year = a.ok ? a.year : null;
      if (!a.ok && !a.empty) this.note('bad_saved_timestamp', this.lineNo, line, { raw: a.raw });
      if (!b.ok && !b.empty) this.note('bad_cataloged_timestamp', this.lineNo, line, { raw: b.raw });
      const sz = line.substr(D.size[0], D.size[1]);
      if (/^\d{10}$/.test(sz)) c.size = +sz;
      else { this.p.sizeBad++; this.note('bad_size_field', this.lineNo, line, { raw: visible(sz, 20) }); }

    } else if (tag === '*D03') {
      const D = NAT_PROFILE.D03;
      c.os = fld(line, D.os); c.tp = fld(line, D.tpMonitor); c.tpExtra = fld(line, D.tpExtra);

    } else if (tag === '*D04') {
      c.codepage = fld(line, NAT_PROFILE.D04.codepage);
    }
  }

  feedSource(line) {
    const c = this.cur, p = this.p;
    const s = rtrim(line.slice(4));
    c.srcLines++; p.srcLines++;
    c.srcChars += s.length; p.srcChars += s.length;
    if (s.length > c.maxSrcLen) c.maxSrcLen = s.length;
    if (c.sample && c.sample.lines.length < CAPS.sampleLines) c.sample.lines.push(s);

    if (!s.length) return;
    const c0 = s.charCodeAt(0);

    // Internal data-area format: "**" + a directive LETTER (**DF, **DR, **C, **G...).
    // A banner line of asterisks ("*****") has '*' in position 3 and must not count,
    // otherwise every commented program is misread as a data area.
    if (c0 === 42) {                                   // '*'
      if (s.charCodeAt(1) === 42) {
        const c2 = s.charCodeAt(2);
        if (c2 >= 65 && c2 <= 90) c.daLines++;         // A-Z
      }
      return;                                          // comment / directive: no statements
    }

    // Strip trailing Natural comment before pattern matching.
    let code = s;
    const ci = code.indexOf('/*');
    if (ci >= 0) code = rtrim(code.slice(0, ci));
    if (!code) return;

    // leading statement keyword
    const lt = code.replace(/^\s+/, '');
    if (lt) {
      let e = 0;
      while (e < lt.length) {
        const ch = lt.charCodeAt(e);
        if (ch === 32 || ch === 40 || ch === 46 || ch === 39) break;  // space ( . '
        e++;
      }
      const kw = lt.slice(0, e).toUpperCase();
      if (kw && kw.length <= 24 && /^[A-Z][A-Z0-9\-]*$/.test(kw)) {
        bump(p.stmt, kw, CAPS.stmtKeys);
        if (kw === 'DEFINE' || kw === 'END-DEFINE' || kw === 'READ' || kw === 'FIND' ||
            kw === 'WRITE' || kw === 'DISPLAY' || kw === 'CALLNAT' || kw === 'PERFORM' ||
            kw === 'IF' || kw === 'DECIDE' || kw === 'FOR' || kw === 'REPEAT' ||
            kw === 'MOVE' || kw === 'COMPUTE' || kw === 'ASSIGN' || kw === 'FETCH') c.execLines++;
      }
    }

    const up = code.toUpperCase();
    const d = p.dep;
    if (up.indexOf('USING') >= 0) {
      collect(d.using, RE.using, up, CAPS.depKeys);
      if (up.indexOf('MAP') >= 0) { collect(d.map, RE.map, up, CAPS.depKeys); c.sawMapProto = true; }
    }
    if (up.indexOf('CALLNAT') >= 0) collect(d.callnat, RE.callnat, up, CAPS.depKeys);
    if (up.indexOf('PERFORM') >= 0) collect(d.perform, RE.perform, up, CAPS.depKeys);
    if (up.indexOf('FETCH') >= 0)   collect(d.fetch, RE.fetch, up, CAPS.depKeys);
    if (up.indexOf('INCLUDE') >= 0) collect(d.include, RE.include, up, CAPS.depKeys);
    if (up.indexOf("CALL '") >= 0)  collect(d.call3gl, RE.call3gl, up, CAPS.depKeys);
  }

  finalizeObject(isEof) {
    const c = this.cur;
    if (!c) return;
    this.cur = null;
    const p = this.p;
    p.objectsSeen++;

    for (const k of ['D01', 'D02', 'D03', 'D04']) {
      if (!c.d[k]) {
        // A truncated scan legitimately cuts the last object short.
        this.note((isEof || c.truncated) ? 'last_object_truncated_missing_' + k : 'object_missing_' + k,
          c.lineNo, undefined, { object: c.library + '/' + c.name, type: c.type });
      }
    }
    if (c.srcLines === 0) this.note('object_with_zero_source_lines', c.lineNo, undefined,
      { object: c.library + '/' + c.name, type: c.type });

    bump(p.byType, c.type, 200);
    bump(p.byLib, c.library, CAPS.libKeys);
    if (c.version) bump(p.byVersion, c.version, 500);
    if (c.os || c.tp) bump(p.byOsTp, (c.os || '?') + ' / ' + (c.tp || '?'), 500);
    if (c.codepage) bump(p.byCodepage, c.codepage, 200);
    if (c.year) bump(p.bySavedYear, c.year, 200);

    if (c.size !== null) {
      p.sizeSum += c.size;
      if (c.size < p.sizeMin) p.sizeMin = c.size;
      if (c.size > p.sizeMax) p.sizeMax = c.size;
    }

    // declared type vs what the body actually looks like
    const meta = NAT_PROFILE.typeMap[c.type];
    const observed = (c.daLines >= 3 && c.daLines > c.execLines) ? 'data'
                   : c.execLines > 0 ? 'exec'
                   : c.srcLines ? 'other' : 'empty';
    bump(p.contentKind, c.type + '>' + observed, 400);
    if (meta && c.srcLines > 0 && observed !== 'other' &&
        meta.kind !== 'map' && meta.kind !== 'text' && meta.kind !== observed) {
      this.note('declared_type_vs_body_mismatch', c.lineNo, undefined,
        { object: c.library + '/' + c.name, declaredType: c.type,
          declaredMeaning: meta.name, bodyLooksLike: observed });
    }

    const key = c.type + '|' + c.library + '|' + c.name;
    if (p.nameIndex.has(key)) {
      bump(p.dupKeys, key, 5000);
      this.note('duplicate_object', c.lineNo, undefined, { object: key });
    } else if (p.nameIndex.size < 1500000) {
      p.nameIndex.set(key, 1);
    }

    if (p.objects.length < CAPS.objectsRetained) {
      p.objects.push([c.library, c.name, c.type, c.version, c.ts1, c.ts2,
                      c.size, c.srcLines, c.srcChars, c.maxSrcLen,
                      c.os, c.tp, c.codepage, c.user1 || '', c.user2 || '', c.user3 || '']);
    } else p.objectsDropped++;
  }

  /* ================= profile layer: SYSOBJH job-log report ================= */
  feedReportLine(line) {
    const rep = this.rep;

    if (!rep.banner && line.indexOf('NATURAL OBJECT HANDLER') >= 0) {
      const m = line.match(/(\d{2}:\d{2}:\d{2}).*NATURAL OBJECT HANDLER.*?(\d{4}-\d{2}-\d{2})/);
      rep.banner = { raw: visible(line, 120), time: m ? m[1] : null, date: m ? m[2] : null };
    }
    if (!rep.runContext) {
      const m = line.match(/^\s*USER\s+(\S+)\s+.*LIBRARY\s+(\S+)/);
      if (m) rep.runContext = { user: m[1], library: m[2] };
    }
    if (!rep.reportMember) {
      const m = line.match(/^\s*REPORT TEXT MEMBER\s+(\S+)/);
      if (m) rep.reportMember = m[1];
    }
    if (!rep.commandLine && /^\s*DATA UNLOAD/.test(line)) rep.commandLine = visible(line.trim(), 140);

    if (!isReportDataRow(line)) return;
    rep.dataRows++;

    const R = NAT_REPORT_PROFILE.row;
    const status = fld(line, R.status), library = fld(line, R.library), name = fld(line, R.name),
          type = fld(line, R.type), sc = fld(line, R.sc), dbidfnr = fld(line, R.dbidfnr),
          date = fld(line, R.date), time = fld(line, R.time), user = fld(line, R.user);

    bump(rep.byStatus, status || '(empty)', 50);
    bump(rep.byLibrary, library, CAPS.libKeys);
    bump(rep.byType, type || '(empty)', 60);
    bump(rep.byUser, user, 20000);
    bump(rep.bySC, sc || '(empty)', 10);
    const year = +date.slice(0, 4);
    if (date.length === 10 && year > 1900) bump(rep.byYear, year, 200);

    if (status !== 'UNLOADED') {
      this.note('report_row_status_not_unloaded', this.lineNo, line,
        { status, object: library + '/' + name, hint: 'Object did NOT come through as UNLOADED — check whether it is missing from the raw unload file.' });
    }
    if (type && !REPORT_TYPE_TO_LETTER[type]) {
      this.note('report_unmapped_type_word', this.lineNo, line, { type });
    }

    const key = library + '|' + name + '|' + type;
    if (rep.seen.has(key)) { bump(rep.dupRows, key, 5000); this.note('report_duplicate_row', this.lineNo, line, { object: key }); }
    else if (rep.seen.size < 1500000) rep.seen.set(key, 1);

    if (rep.rows.length < CAPS.objectsRetained) {
      rep.rows.push([library, name, type, REPORT_TYPE_TO_LETTER[type] || '', sc, dbidfnr, date, time, user, status]);
    } else rep.rowsDropped++;
  }

  /** Called once after the stream ends. A partial scan (byte limit, or a
   *  sample file that just stops mid-file) can cut the very last physical
   *  line short, right where isReportDataRow() needs the DATE column — that
   *  looks identical to a malformed row unless we know it's specifically
   *  the end of the file. Scoped to the single last line, so a genuine
   *  short banner line elsewhere in the file is never at risk of matching. */
  finalizeReport() {
    if (!this.reportOn || this.lastLine === null) return;
    const l = this.lastLine;
    if (isReportDataRow(l)) return;                       // already counted normally
    if (l.charCodeAt(0) !== 32 || l.length < 40 || l.length >= 112) return;
    if (!/^[A-Z][A-Z0-9-]{2,20}\b/.test(l.slice(1))) return;
    this.note('report_final_row_truncated', this.lineNo, l,
      { hint: 'Looks like the start of a data row cut off before the DATE column. Expected at the end of a ' +
               'partial scan or a sample file; if the whole file was scanned, the export itself is incomplete.' });
  }
}

/* ================================================================== *
 * Streaming driver
 * ================================================================== */

const CHUNK = 8 * 1024 * 1024;          // 8 MB: ~100 ms of work per turn
const SNIFF = 4 * 1024 * 1024;

function yieldToUI() { return new Promise(r => setTimeout(r, 0)); }

/** Decide how records are separated, using the DECODED sample rather than raw
 *  bytes. EBCDIC decodes 0x25 to LF and 0x15 to NEL (U+0085); a raw byte scan
 *  for 0x0A would see neither and wrongly conclude "fixed length". */
function detectFraming(text) {
  let lf = 0, nel = 0, cr = 0;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if (c === 10) lf++; else if (c === 0x85) nel++; else if (c === 13) cr++;
  }
  const sep = (lf > 0 && lf >= nel) ? '\n' : (nel > 0 ? '\u0085' : null);
  return {
    lf, nel, cr,
    separator: sep,
    separatorName: sep === '\n' ? 'LF (U+000A)' : sep ? 'NEL (U+0085)' : 'none found',
    mode: sep ? 'lf' : 'fixed'
  };
}

async function runScan(file, opts, onProgress, isCancelled) {
  const t0 = Date.now();
  const limit = opts.limitBytes > 0 ? Math.min(opts.limitBytes, file.size) : file.size;

  /* ---- 1. sniff ---- */
  const head = new Uint8Array(await file.slice(0, Math.min(SNIFF, limit)).arrayBuffer());
  const sniff = sniffEncoding(head);
  const encoding = opts.encoding === 'auto' ? sniff.guess : opts.encoding;

  /* ---- 2. record framing (decided on decoded text) ---- */
  const probe = makeDecoder(encoding).decode(head.subarray(0, Math.min(head.length, 1 << 20)));
  const framing = detectFraming(probe);
  const candidates = [...new Set([encoding, 'utf-8', 'windows-1255', 'cp424', 'cp037', 'cp862', 'latin1'])];
  const preview = decodePreview(head, candidates);

  let lineMode = opts.lineMode, recLen = opts.recLen | 0;
  let fixedDetection = null;
  if (lineMode === 'auto') lineMode = framing.mode;
  if (lineMode === 'fixed' && !(recLen > 0)) {
    fixedDetection = detectRecordLength(head, encoding);
    recLen = fixedDetection.best || 80;
  }
  const SEP = framing.separator || '\n';

  /* ---- 2b. which of the two known Natural formats is this? ---- */
  let profile = opts.profile;
  const profileDetection = profile === 'auto' ? sniffFileProfile(probe) : null;
  if (profile === 'auto') profile = profileDetection;

  /* ---- 3. stream ---- */
  const an = new Analyzer({ ...opts, encoding, lineMode, recLen, profile });
  an.meta = { sniff, encoding, lineMode, recLen, fixedDetection, framing, separator: SEP, preview,
              profileRequested: opts.profile, profileUsed: profile, profileDetection };

  const dec = makeDecoder(encoding);
  let offset = 0, tail = '';

  while (offset < limit) {
    if (isCancelled && isCancelled()) { an.cancelled = true; break; }
    const end = Math.min(offset + CHUNK, limit);
    const u8 = new Uint8Array(await file.slice(offset, end).arrayBuffer());
    an.g.bytes += u8.length;
    const more = end < limit;
    const text = tail + dec.decode(u8, more);
    offset = end;

    if (lineMode === 'fixed') {
      let i = 0;
      const stop = more ? text.length - (text.length % recLen) : text.length;
      for (; i + recLen <= stop; i += recLen) an.feedLine(text.substr(i, recLen));
      tail = text.slice(i);
      if (!more && tail.length) { an.feedLine(tail, tail.length % recLen !== 0); tail = ''; }
    } else {
      let start = 0, nl;
      while ((nl = text.indexOf(SEP, start)) >= 0) {
        an.feedLine(text.slice(start, nl));
        start = nl + 1;
      }
      tail = text.slice(start);
      if (!more && tail.length) {
        an.feedLine(tail, tail.length % NAT_PROFILE.recordPad !== 0);
        tail = '';
      }
    }

    if (onProgress) onProgress(offset, limit, Date.now() - t0, an);
    await yieldToUI();
  }

  an.finalizeObject(true);
  an.finalizeReport();
  an.durationMs = Date.now() - t0;
  an.scannedBytes = offset;
  an.limit = limit;
  return an;
}

/** When the file has no line terminators, guess the fixed record length
 *  by finding the length that makes the leading 4 characters most
 *  repetitive (real fixed-format files have a small tag alphabet). */
function detectRecordLength(u8, encoding) {
  const dec = makeDecoder(encoding);
  const text = dec.decode(u8.subarray(0, Math.min(u8.length, 1 << 20)));
  const candidates = [];
  for (let L = 20; L <= 512; L++) {
    if (text.length < L * 20) continue;
    const seen = new Map();
    const n = Math.min(2000, Math.floor(text.length / L));
    for (let i = 0; i < n; i++) bump(seen, text.substr(i * L, 4));
    // score = share of records covered by the single most common tag
    let top = 0; for (const v of seen.values()) if (v > top) top = v;
    candidates.push({ length: L, distinctTags: seen.size, topShare: +(top / n).toFixed(3) });
  }
  candidates.sort((a, b) => b.topShare - a.topShare || a.distinctTags - b.distinctTags || a.length - b.length);
  const best = candidates[0];
  return {
    best: best && best.topShare > 0.25 ? best.length : null,
    top5: candidates.slice(0, 5),
    note: 'Heuristic. Verify against the real record layout before trusting object counts.'
  };
}

/* ================================================================== *
 * Link resolution + report building
 * ================================================================== */

function resolveTargets(an, depMap, allowedTypes, cap) {
  const idx = an.p.nameIndex;
  const names = new Set();
  for (const k of idx.keys()) {
    const t = k.charAt(0);
    if (allowedTypes.indexOf(t) >= 0) names.add(k.slice(k.indexOf('|', 2) + 1));
  }
  let resolved = 0, unresolved = 0;
  const missing = new Map();
  for (const [target, count] of depMap) {
    if (target === '«overflow»') continue;
    if (names.has(target)) resolved += count;
    else { unresolved += count; bump(missing, target, 50000); }
  }
  return {
    distinctTargets: depMap.size,
    referencesResolved: resolved,
    referencesUnresolved: unresolved,
    topUnresolved: topN(missing, cap || 40)
  };
}

function buildReport(an, file) {
  const g = an.g, p = an.p, m = an.meta, rep2 = an.rep;
  const secs = an.durationMs / 1000;

  const anomalies = [...an.anomalies.values()]
    .sort((a, b) => b.count - a.count)
    .map(a => ({ kind: a.kind, count: a.count, samples: a.samples }));

  const totalRec = [...p.rec.values()].reduce((s, v) => s + v, 0);
  const matchRate = totalRec ? 1 - (p.unknown / totalRec) : 0;

  const objCount = p.objectsSeen;
  const est = an.scannedBytes > 0 && file.size > an.scannedBytes
    ? { note: 'Linear extrapolation from the scanned portion; assumes uniform composition.',
        scale: +(file.size / an.scannedBytes).toFixed(2),
        estimatedObjects: Math.round(objCount * file.size / an.scannedBytes),
        estimatedSourceLines: Math.round(p.srcLines * file.size / an.scannedBytes) }
    : null;

  const typeTable = topN(p.byType, 60).map(([t, n]) => {
    const meta = NAT_PROFILE.typeMap[t] || { name: 'UNKNOWN', kind: '?', conf: 'none', evidence: 'letter not in profile' };
    return { type: t, count: n, meaning: meta.name, kind: meta.kind, confidence: meta.conf, evidence: meta.evidence };
  });

  const report = {
    tool: { name: 'NATPROG Discovery', version: '1.0',
            profile: an.profileOn ? NAT_PROFILE.id : an.reportOn ? NAT_REPORT_PROFILE.id : 'none',
            profileRequested: m.profileRequested, profileAutoDetected: m.profileDetection },
    generatedAt: new Date().toISOString(),

    file: {
      name: file.name, sizeBytes: file.size, sizeHuman: fmtBytes(file.size),
      scannedBytes: an.scannedBytes, scannedHuman: fmtBytes(an.scannedBytes),
      fullFileScanned: an.scannedBytes >= file.size,
      cancelled: !!an.cancelled
    },

    scan: {
      encodingUsed: m.encoding,
      encodingRequested: an.opts.encodingRequested,
      lineMode: m.lineMode,
      recordSeparator: m.lineMode === 'lf' ? m.framing.separatorName : null,
      framingProbe: m.framing,
      fixedRecordLength: m.lineMode === 'fixed' ? m.recLen : null,
      fixedRecordDetection: m.fixedDetection,
      durationSec: +secs.toFixed(1),
      throughputMBps: +((an.scannedBytes / 1048576) / Math.max(secs, .001)).toFixed(1)
    },

    encodingSniff: m.sniff,

    encodingCandidatePreview: {
      note: 'The same first records decoded under each codepage. The one that produces readable ' +
            'text is the correct one; if none does, the file needs a codepage this tool does not carry.',
      chosen: m.encoding,
      decoded: m.preview
    },

    generic: {
      records: g.lines,
      emptyRecords: g.emptyLines,
      recordsEndingCRLF: g.crlf,
      recordsEndingLFonly: g.lfOnly,
      minRecordLenChars: g.minLen === Infinity ? null : g.minLen,
      maxRecordLenChars: g.maxLen,
      longestRecordSample: g.maxLenSample,
      recordLengthHistogramTop: topN(g.lenHist, 40),
      distinctRecordLengths: g.lenHist.size,
      lengthModulo12: topN(g.padMultiple, 12),
      leadingCharHistogram: topN(g.prefix1, 30),
      prefix4Histogram: topN(g.prefix4, 60),
      distinctPrefix4: g.prefix4.size
    },

    encodingDamage: (() => {
      if (g.replacementChars === 0) return { replacementCharsFound: 0, recordsAffected: 0, likelyCause: null,
        meaning: 'No replacement characters detected.' };
      // Two very different situations produce the same symptom:
      //  (a) U+FFFD was already baked into the bytes (EF BF BD) before this tool ever saw them
      //      -> genuinely gone, no encoding choice here can bring it back.
      //  (b) the chosen codepage is a single-byte table with a handful of UNASSIGNED byte
      //      values (e.g. windows-1255 leaves several code points undefined) -> the bytes are
      //      still there, this tool's codepage guess just isn't the right one for them.
      // sniff.replacementSeqInSample only covers the sniffed head (see SNIFF constant), so a
      // "0" there is evidence, not proof, for the whole file.
      const preExisting = m.sniff.replacementSeqInSample > 0;
      const singleByte = m.encoding !== 'utf-8';
      let cause, meaning;
      if (preExisting) {
        cause = 'pre-existing-in-bytes';
        meaning = 'U+FFFD was already present in the raw bytes (found ' + m.sniff.replacementSeqInSample +
          ' EF BF BD sequences in the sniffed sample) — this predates this tool and is NOT recoverable from ' +
          'this file. The data must be re-exported from source with a codepage that can represent it.';
      } else if (singleByte) {
        cause = 'possible-codepage-mismatch';
        meaning = 'No pre-existing U+FFFD was found in the sniffed sample, and "' + m.encoding + '" is a ' +
          'single-byte codepage with some unassigned byte values — so these replacement characters were most ' +
          'likely PRODUCED BY THIS DECODE, not by prior data loss. Before concluding anything is lost, re-run ' +
          'with a sibling codepage (try ISO-8859-8, CP862, or CP424/CP037 if the source is EBCDIC) and compare ' +
          'the U+FFFD count and the side-by-side preview on the Overview tab.';
      } else {
        cause = 'unclear';
        meaning = 'No pre-existing U+FFFD was found in the sniffed sample, but the file is large enough that ' +
          'the sniff (first few MB) may not be representative of where the damage occurs. Treat as unconfirmed.';
      }
      return { replacementCharsFound: g.replacementChars, recordsAffected: g.linesWithReplacement,
               likelyCause: cause, meaning };
    })(),

    profile: an.profileOn ? {
      matched: matchRate > 0.98 && p.objectsSeen > 0,
      matchRate: +matchRate.toFixed(5),
      recordCounts: topN(p.rec, 40),
      unknownRecords: p.unknown,
      header: p.header,
      objects: {
        countInScan: objCount,
        retainedInMemory: p.objects.length,
        droppedFromInventory: p.objectsDropped,
        sourceLines: p.srcLines,
        sourceChars: p.srcChars,
        avgSourceLinesPerObject: objCount ? +(p.srcLines / objCount).toFixed(1) : 0,
        declaredSizeBytes: { sum: p.sizeSum, min: p.sizeMin === Infinity ? null : p.sizeMin, max: p.sizeMax, unparsable: p.sizeBad },
        byType: typeTable,
        byLibrary: topN(p.byLib, 100),
        distinctLibraries: p.byLib.size,
        byNaturalVersion: topN(p.byVersion, 40),
        byOsAndTpMonitor: topN(p.byOsTp, 40),
        byCodepageField: topN(p.byCodepage, 20),
        bySavedYear: topN(p.bySavedYear, 80).sort((a, b) => a[0] - b[0]),
        duplicateObjectKeys: topN(p.dupKeys, 40),
        declaredTypeVsBodyShape: topN(p.contentKind, 60),
        top30BySourceLines: [...p.objects].sort((a, b) => b[7] - a[7]).slice(0, 30)
          .map(r => ({ library: r[0], name: r[1], type: r[2], srcLines: r[7], declaredSize: r[6], natVersion: r[3], saved: r[4] }))
      },
      extrapolationToFullFile: est
    } : null,

    lexical: an.profileOn ? {
      topStatementKeywords: topN(p.stmt, 70),
      distinctStatementKeywords: p.stmt.size,
      dependencies: {
        usingTargets:   { distinct: p.dep.using.size,   top: topN(p.dep.using, 80) },
        callnatTargets: { distinct: p.dep.callnat.size, top: topN(p.dep.callnat, 80) },
        performTargets: { distinct: p.dep.perform.size, top: topN(p.dep.perform, 60) },
        fetchTargets:   { distinct: p.dep.fetch.size,   top: topN(p.dep.fetch, 40) },
        mapTargets:     { distinct: p.dep.map.size,     top: topN(p.dep.map, 40) },
        includeTargets: { distinct: p.dep.include.size, top: topN(p.dep.include, 40) },
        externalCall3GL:{ distinct: p.dep.call3gl.size, top: topN(p.dep.call3gl, 60) }
      },
      resolution: {
        note: 'Resolved against objects seen IN THIS SCAN only. On a partial scan, "unresolved" is expected and not an error.',
        performNote: 'Most PERFORM targets are subroutine labels DEFINEd INSIDE the same source object ' +
          '(DEFINE SUBROUTINE ... END-SUBROUTINE), not separate catalogued objects — this check can only ' +
          'resolve PERFORMs of externally catalogued type-S Subroutines. A high unresolved count here is ' +
          'expected and is not evidence of broken calls; compare the DEFINE/END-SUBROUTINE counts in the ' +
          'statement-keyword list to sanity-check that most PERFORMs are accounted for internally.',
        using:   resolveTargets(an, p.dep.using,   ['L', 'C', 'P', 'G', 'A'], 60),
        callnat: resolveTargets(an, p.dep.callnat, ['N'], 60),
        perform: resolveTargets(an, p.dep.perform, ['S'], 40),
        map:     resolveTargets(an, p.dep.map,     ['M'], 40)
      }
    } : null,

    sourceSamples: an.profileOn
      ? Object.fromEntries([...p.samples.entries()].map(([t, arr]) => [
          t + ' (' + ((NAT_PROFILE.typeMap[t] || {}).name || 'UNKNOWN') + ')',
          arr.map(o => ({ library: o.library, name: o.name, firstLines: o.lines }))
        ]))
      : null,

    jobLog: an.reportOn ? {
      matched: rep2.dataRows > 0,
      runInfo: { banner: rep2.banner, context: rep2.runContext, reportMember: rep2.reportMember,
                 commandLine: rep2.commandLine },
      rows: { seen: rep2.dataRows, retained: rep2.rows.length, dropped: rep2.rowsDropped },
      byStatus: topN(rep2.byStatus, 30),
      byType: topN(rep2.byType, 30).map(([w, n]) => ({ word: w, count: n, letter: REPORT_TYPE_TO_LETTER[w] || null })),
      byLibrary: topN(rep2.byLibrary, 100), distinctLibraries: rep2.byLibrary.size,
      byUser: topN(rep2.byUser, 100),
      bySC: topN(rep2.bySC, 10),
      byYear: topN(rep2.byYear, 80).sort((a, b) => a[0] - b[0]),
      duplicateRows: topN(rep2.dupRows, 40),
      top500Rows: rep2.rows.slice(0, 500).map(r => ({
        library: r[0], name: r[1], type: r[2], letter: r[3], sc: r[4],
        dbidFnr: r[5], date: r[6], time: r[7], user: r[8], status: r[9] }))
    } : null,

    anomalies,
    anomalyKindsTruncated: an.anomalies.size >= CAPS.anomalyKinds,

    caps: CAPS,
    fieldLayoutUsed: an.profileOn ? {
      note: '0-based character offsets after decoding. "inferred" fields are read from data patterns, not vendor documentation — challenge them if the numbers look wrong.',
      recordPadding: NAT_PROFILE.recordPad,
      H: NAT_PROFILE.H, C: NAT_PROFILE.C, D01: NAT_PROFILE.D01,
      D02: NAT_PROFILE.D02, D03: NAT_PROFILE.D03, D04: NAT_PROFILE.D04
    } : an.reportOn ? {
      note: 'Column offsets measured directly off a real report sample\'s own header/dashes line.',
      row: NAT_REPORT_PROFILE.row
    } : null
  };

  report.verdict = buildVerdict(report, an);
  return report;
}

/* ================================================================== *
 * Verdict — the part that says "we are parsing this wrong"
 * ================================================================== */
function buildVerdict(r, an) {
  const v = [];
  const add = (level, title, detail) => v.push({ level, title, detail });
  const A = k => (an.anomalies.get(k) || { count: 0 }).count;

  if (r.file.sizeBytes === 0) {
    add('err', 'The file is empty', 'Zero bytes. Nothing to analyse.');
    return v;
  }

  if (r.profile) {
    if (r.profile.objects.countInScan === 0) {
      add('err', 'PROFILE DOES NOT MATCH — no objects found',
        'Not a single *C** catalog record was recognised. The file is either a different format, a different encoding, ' +
        'or split into records differently. Do not trust any object numbers in this report.');
    } else if (!r.profile.matched) {
      add('err', 'PROFILE PARTIALLY MATCHES — ' + (r.profile.matchRate * 100).toFixed(2) + '% of records recognised',
        r.profile.unknownRecords.toLocaleString() + ' records did not start with a known 4-character tag. ' +
        'Either the layout drifts partway through the file, or a second format is concatenated into it. ' +
        'See the "unknown_record_prefix" anomaly samples.');
    } else {
      add('ok', 'Structure matches the Natural SYSOBJH raw-unload profile',
        (r.profile.matchRate * 100).toFixed(3) + '% of records recognised across ' +
        r.generic.records.toLocaleString() + ' records / ' +
        r.profile.objects.countInScan.toLocaleString() + ' objects.');
    }
  } else if (r.jobLog) {
    if (!r.jobLog.matched) {
      add('err', 'PROFILE DOES NOT MATCH — no report rows found',
        'This was detected as a SYSOBJH job-log report, but no row matched the expected column layout ' +
        '(ASA single-space control + a YYYY-MM-DD date at the expected offset). The layout may have shifted.');
    } else {
      add('ok', 'Structure matches the SYSOBJH job-log report profile',
        fmtNum(r.jobLog.rows.seen) + ' rows recognised across ' + r.generic.records.toLocaleString() + ' lines. ' +
        'This is a run report (who ran it, when, and per-object status) — it carries no source code.');
    }
    const notUnloaded = A('report_row_status_not_unloaded');
    if (notUnloaded > 0) {
      const statuses = r.jobLog.byStatus.filter(([s]) => s !== 'UNLOADED').map(([s, n]) => s + '=' + n).join(', ');
      add('err', notUnloaded.toLocaleString() + ' rows did NOT come through as UNLOADED (' + statuses + ')',
        'These objects are listed in this report but may be missing or different in the raw unload file — cross-check ' +
        'them by library/name against the raw-unload scan before assuming the transfer is complete.');
    }
    if (A('report_unmapped_type_word') > 0)
      add('warn', A('report_unmapped_type_word').toLocaleString() + " rows use a TYPE word not in this tool's letter map",
        'A Natural object type appears here that has no known single-letter equivalent yet. See the anomaly samples.');
    if (A('report_final_row_truncated') > 0)
      add('warn', 'The file ends mid-row',
        'The last row is cut off before its date/time/user/status columns. Expected if this is a sample/excerpt ' +
        'or a partial scan; if the whole file was scanned, the export itself is incomplete.');
  } else {
    add('warn', 'Generic scan only',
      'Neither known Natural profile matched (raw *H**/*C**/*D0x/*S** unload, or the SYSOBJH job-log report). ' +
      'Use the prefix histogram and record-length histogram below to decide what this file is.');
  }

  if (r.encodingDamage.replacementCharsFound > 0) {
    const cause = r.encodingDamage.likelyCause;
    add(cause === 'pre-existing-in-bytes' ? 'err' : 'warn',
      (cause === 'pre-existing-in-bytes' ? 'Character data already destroyed: ' : 'Possible codepage mismatch: ') +
      r.encodingDamage.replacementCharsFound.toLocaleString() + ' U+FFFD characters (' +
      r.encodingDamage.recordsAffected.toLocaleString() + ' records)',
      r.encodingDamage.meaning);
  }

  if (r.profile && r.generic.lengthModulo12.length > 1) {
    let bad = r.generic.lengthModulo12.filter(x => x[0] !== 0).reduce((s, x) => s + x[1], 0);
    bad -= A('final_record_truncated');            // a cut-off tail record is not a layout problem
    if (bad > 0) add('warn', bad.toLocaleString() + ' records are not a multiple of 12 characters',
      'The format pads every record to a 12-character boundary. Records that break this are usually a sign the ' +
      'decoding is off (wrong codepage collapsing or expanding characters) rather than corrupt data.');
  }

  const missD = ['D01', 'D02', 'D03', 'D04'].reduce((s, k) => s + A('object_missing_' + k), 0);
  if (missD > 0) add('warn', missD.toLocaleString() + ' missing directory records',
    'Objects without a full *D01–*D04 set. A handful at the very end of a partial scan is normal; many is not.');

  if (A('declared_type_vs_body_mismatch') > 0)
    add('warn', A('declared_type_vs_body_mismatch').toLocaleString() + ' objects whose body contradicts their declared type',
      'The object-type letter map is inferred, not documented. This anomaly is the strongest signal that a letter ' +
      'is mapped to the wrong meaning — check the samples before relying on the type breakdown.');

  if (A('unknown_object_type_letter') > 0)
    add('warn', A('unknown_object_type_letter').toLocaleString() + ' objects with an unmapped type letter',
      'New type letters appear beyond the sample used to build the profile. Send the log so the map can be extended.');

  if (A('duplicate_object') > 0)
    add('warn', A('duplicate_object').toLocaleString() + ' duplicate library/name/type keys',
      'Either the export contains several versions of the same object, or several libraries were concatenated.');

  if (A('orphan_source_line') > 0)
    add('err', A('orphan_source_line').toLocaleString() + ' source records before any object header',
      'Strongly suggests the record framing is wrong — likely the wrong record length or a mid-file format change.');

  if (A('source_line_dash_variant') > 0)
    add('ok', A('source_line_dash_variant').toLocaleString() + " source records use the '-S**' tag instead of '*S**'",
      "Parsed the same as '*S**' (confirmed on a real 770MB scan: same width, same offset-4 payload, valid " +
      'Natural content — commonly internal DDM/view structure lines). Counted in source lines and dependency ' +
      "extraction; the exact reason Natural uses '-' here is not confirmed against vendor documentation.");

  if (A('final_record_truncated') > 0 && r.file.fullFileScanned && !r.file.cancelled)
    add('warn', 'The file ends mid-record',
      'The last record is cut off. The whole file was read, so the export itself is incomplete — ' +
      'the transfer was probably truncated. Re-transfer before drawing conclusions about the tail.');

  if (r.scan.lineMode === 'fixed')
    add('warn', 'Fixed-length record mode (' + r.scan.fixedRecordLength + ' chars)',
      'No line terminators were found, so the record length was guessed heuristically. Confirm it against the real layout.');

  if (!r.file.fullFileScanned)
    add('warn', 'Partial scan — ' + r.file.scannedHuman + ' of ' + r.file.sizeHuman +
      ' (' + ((r.file.scannedBytes / r.file.sizeBytes) * 100).toFixed(1) + '%)',
      r.file.cancelled ? 'Scan was cancelled by the user.'
        : 'Scan limit was applied. Totals below describe only the scanned portion; the extrapolation is a linear guess.');

  if (r.profile && r.profile.objects.droppedFromInventory > 0)
    add('warn', r.profile.objects.droppedFromInventory.toLocaleString() + ' objects omitted from the CSV inventory',
      'The in-memory inventory cap (' + CAPS.objectsRetained.toLocaleString() + ') was reached. Aggregate counts are still complete.');

  if (r.jobLog && r.jobLog.rows.dropped > 0)
    add('warn', r.jobLog.rows.dropped.toLocaleString() + ' rows omitted from the CSV inventory',
      'The in-memory inventory cap (' + CAPS.objectsRetained.toLocaleString() + ') was reached. Aggregate counts are still complete.');

  return v;
}

/* ================================================================== *
 * Cross-check: one raw-unload scan + one job-log report scan, compared
 *
 * The report's job-log tells you what SHOULD be in the raw unload (every
 * row with STATUS=UNLOADED). This confirms it actually is, by looking each
 * one up in the raw file's object index. A row already flagged with a
 * non-UNLOADED status by the report's own verdict is skipped here — it
 * already told you it didn't make it, no need to re-discover that.
 * ================================================================== */
function buildCrossCheck(anA, rA, anB, rB) {
  let raw, rawR, rep, repR;
  if (anA.profileOn && anB.reportOn) { raw = anA; rawR = rA; rep = anB; repR = rB; }
  else if (anB.profileOn && anA.reportOn) { raw = anB; rawR = rB; rep = anA; repR = rA; }
  else {
    return {
      compatible: false,
      fileA: { name: rA.file.name, profile: rA.tool.profile },
      fileB: { name: rB.file.name, profile: rB.tool.profile },
      note: 'Cross-check needs one raw-unload file and one job-log report file. Got "' +
        rA.tool.profile + '" and "' + rB.tool.profile + '" — nothing to compare.'
    };
  }

  const idx = raw.p.nameIndex;                    // "LETTER|LIBRARY|NAME" -> true
  const libNameOnly = new Set();                  // "LIBRARY|NAME" -> exists (any type)
  for (const k of idx.keys()) libNameOnly.add(k.slice(k.indexOf('|') + 1));

  let checked = 0, matched = 0;
  const missing = [];
  for (const row of rep.rep.rows) {
    const [library, name, typeWord, letter, , , date, time, user, status] = row;
    if (status !== 'UNLOADED') continue;
    checked++;
    const found = letter ? idx.has(letter + '|' + library + '|' + name)
                          : libNameOnly.has(library + '|' + name);
    if (found) matched++;
    else missing.push({ library, name, type: typeWord, letter: letter || null,
                        date, time, user, typeUnmapped: !letter });
  }

  const reportKeys = new Set(rep.rep.rows.map(r => (r[3] || '?') + '|' + r[0] + '|' + r[1]));
  let extraInRawNotInReport = 0;
  for (const k of idx.keys()) if (!reportKeys.has(k)) extraInRawNotInReport++;

  const bothFullyScanned = rawR.file.fullFileScanned && repR.file.fullFileScanned;

  return {
    compatible: true,
    bothFullyScanned,
    rawFile: { name: rawR.file.name, objects: rawR.profile.objects.countInScan, fullyScanned: rawR.file.fullFileScanned },
    reportFile: { name: repR.file.name, rows: repR.jobLog.rows.seen, fullyScanned: repR.file.fullFileScanned },
    reportRowsWithStatusUnloaded: checked,
    matchedInRawUnload: matched,
    missingFromRawUnload: missing.length,
    missingSamples: missing.slice(0, 500),
    missingTruncated: missing.length > 500,
    partialScanCaveat: bothFullyScanned ? null :
      'At least one side was a PARTIAL scan (byte limit, or these are excerpts of larger files). A high ' +
      '"missing" count here most likely just means the two files\' scanned windows do not cover the same ' +
      'objects — it is NOT evidence of a real problem. Re-run with the full files (or the same scan limit on ' +
      'both) before treating any of this as a genuine gap.',
    extraInRawNotInReport,
    extraInRawNote: 'Objects present in the raw unload but not mentioned in the report at all. ' +
      'NOT necessarily a problem — a report is often filtered by object type or library on purpose ' +
      '(this tool has already seen reports filtered to just Programs, or just DDMs).'
  };
}

/* ================================================================== *
 * JCL: which programs does a job actually run?
 *
 * Verified against 5 real JCL jobs: a Natural batch step doesn't name the
 * program on the EXEC line at all — it runs a shared PROC (seen here as
 * "NATB240") and hands the real library+program to that PROC through
 * in-stream CMSYNIN input:
 *     //STEP2 EXEC NATB240,COND=(0,NE)
 *     //CMSYNIN DD *
 *     LOGON RC
 *     HICNEWN3
 *     FIN
 * The first content line names the library — sometimes as "LOGON <lib>",
 * sometimes as a bare "<lib>" with no LOGON keyword (both seen in the same
 * job). Whatever isn't the library and isn't FIN is a program name. A step
 * that instead reads "EXEC PGM=xxx" (SORT, FTP, or — not yet seen in any
 * sample — a custom COBOL load module) is captured too, just without a
 * library, since PGM= steps don't go through a library LOGON.
 * ================================================================== */
function parseJcl(text, fileName) {
  const lines = text.split(/\r\n|\n/);
  let jobName = null;
  const steps = [];
  const programRefs = [];
  let curStep = null;
  let inCmsynin = false;
  let cmsyninLib = null;

  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];

    if (inCmsynin) {
      if (/^\/\*\s*$/.test(l) || /^\/\//.test(l)) {
        inCmsynin = false;
        cmsyninLib = null;
        // fall through: this line may itself be a real JCL control line
      } else {
        const t = l.trim();
        if (t) {
          if (cmsyninLib === null) {
            const m = /^(?:LOGON\s+)?(\S+)/i.exec(t);
            if (m) cmsyninLib = m[1].toUpperCase();
          } else if (!/^FIN\b/i.test(t) && t.toUpperCase() !== cmsyninLib) {
            programRefs.push({ file: fileName, step: curStep, kind: 'natural-batch',
                               library: cmsyninLib, program: t.split(/\s+/)[0], raw: l });
          }
        }
        continue;
      }
    }

    if (!/^\/\//.test(l)) continue;          // not a JCL control line
    if (/^\/\/\*/.test(l)) continue;         // JCL comment (incl. commented-out steps)

    if (!jobName) {
      const jm = /^\/\/#?(\S+)\s+JOB\b/.exec(l);
      if (jm) jobName = jm[1];
    }

    const em = /^\/\/(\S+)\s+EXEC\s+(.+)$/.exec(l);
    if (em) {
      curStep = em[1];
      const target = em[2].split(',')[0].trim();
      const pm = /^PGM=(\S+)/.exec(target);
      if (pm) {
        steps.push({ step: curStep, kind: 'pgm', target: pm[1] });
        programRefs.push({ file: fileName, step: curStep, kind: 'direct-pgm',
                           library: null, program: pm[1], raw: l });
      } else {
        steps.push({ step: curStep, kind: 'proc', target });
      }
      continue;
    }

    if (/^\/\/CMSYNIN\s+DD\s+\*/i.test(l)) { inCmsynin = true; cmsyninLib = null; continue; }
  }

  return { file: fileName, jobName, steps, programRefs };
}

const UTILITY_PGM_NAMES = new Set(['SORT', 'DFSORT', 'ICETOOL', 'IDCAMS', 'IEBGENER', 'IEBCOPY',
  'IEFBR14', 'IKJEFT01', 'IKJEFT1B', 'FTP', 'IEWL', 'IEBPTPCH', 'ICEMAN']);

/** Aggregate parseJcl() results across many files, and — if a raw-unload
 *  Analyzer is available — cross-check every natural-batch reference
 *  against its object index (library+name, letter-agnostic since JCL
 *  never says which Natural object-type letter it means). */
function analyzeJcl(jclFiles, rawAnalyzer) {
  const parsed = jclFiles.map(f => parseJcl(f.text, f.name));
  const allRefs = [];
  for (const p of parsed) for (const r of p.programRefs) allRefs.push(r);

  const byProgram = new Map(), byLibrary = new Map(), byKind = new Map();
  for (const r of allRefs) {
    bump(byKind, r.kind, 10);
    if (r.kind === 'natural-batch') {
      bump(byLibrary, r.library, 5000);
      bump(byProgram, r.library + '/' + r.program, 200000);
    } else {
      bump(byProgram, r.program, 200000);
    }
  }

  const directPgmDistinct = new Set(allRefs.filter(r => r.kind === 'direct-pgm').map(r => r.program));
  const utilityCount = allRefs.filter(r => r.kind === 'direct-pgm' && UTILITY_PGM_NAMES.has(r.program.toUpperCase())).length;
  const nonUtilityDirectPgm = [...directPgmDistinct].filter(n => !UTILITY_PGM_NAMES.has(n.toUpperCase()));

  let resolution = null;
  if (rawAnalyzer && rawAnalyzer.profileOn) {
    const idx = rawAnalyzer.p.nameIndex;
    const libNameOnly = new Set();
    for (const k of idx.keys()) libNameOnly.add(k.slice(k.indexOf('|') + 1));
    let resolved = 0, unresolved = 0;
    const rows = allRefs.filter(r => r.kind === 'natural-batch').map(r => {
      const found = libNameOnly.has(r.library + '|' + r.program);
      if (found) resolved++; else unresolved++;
      return { file: r.file, step: r.step, library: r.library, program: r.program, foundInRaw: found };
    });
    resolution = { resolved, unresolved, rows,
      rawFile: rawAnalyzer.opts && rawAnalyzer.opts.fileNameForDisplay || null };
  }

  return {
    filesParsed: parsed.length,
    jobs: parsed.map(p => ({ file: p.file, jobName: p.jobName, steps: p.steps.length, programRefs: p.programRefs.length })),
    totalProgramRefs: allRefs.length,
    naturalBatchRefs: (byKind.get('natural-batch') || 0),
    directPgmRefs: (byKind.get('direct-pgm') || 0),
    distinctPrograms: byProgram.size,
    distinctLibraries: byLibrary.size,
    byLibrary: topN(byLibrary, 100),
    topPrograms: topN(byProgram, 200),
    utilityDirectPgmCount: utilityCount,
    nonUtilityDirectPgm,
    resolution,
    allRefs
  };
}

/* ================================================================== *
 * CSV inventory
 * ================================================================== */
const CSV_HEADER = ['library','name','type','type_meaning','nat_version','saved','cataloged',
                    'declared_size','src_lines','src_chars','max_src_len','os','tp_monitor',
                    'codepage','user1','user2','user3'];

function csvQuote(s) {
  s = s === null || s === undefined ? '' : String(s);
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

function buildCsv(an) {
  const out = [CSV_HEADER.join(',')];
  for (const r of an.p.objects) {
    const meaning = (NAT_PROFILE.typeMap[r[2]] || {}).name || 'UNKNOWN';
    out.push([r[0], r[1], r[2], meaning, r[3], r[4], r[5], r[6], r[7], r[8], r[9],
              r[10], r[11], r[12], r[13], r[14], r[15]].map(csvQuote).join(','));
  }
  return out.join('\n');
}

const REPORT_CSV_HEADER = ['library','name','type_word','type_letter','s_c','dbid_fnr','date','time','user_id','status'];

function buildReportCsv(an) {
  const out = [REPORT_CSV_HEADER.join(',')];
  for (const r of an.rep.rows) out.push(r.map(csvQuote).join(','));
  return out.join('\n');
}

const JCL_CSV_HEADER = ['jcl_file', 'step', 'kind', 'library', 'program', 'found_in_raw_unload'];

function buildJclCsv(jcl) {
  const out = [JCL_CSV_HEADER.join(',')];
  const foundByKey = new Map();
  if (jcl.resolution) for (const r of jcl.resolution.rows) foundByKey.set(r.file + '|' + r.step + '|' + r.program, r.foundInRaw);
  for (const r of jcl.allRefs) {
    const found = jcl.resolution ? (r.kind === 'natural-batch' ? String(foundByKey.get(r.file + '|' + r.step + '|' + r.program)) : 'n/a') : '';
    out.push([r.file, r.step || '', r.kind, r.library || '', r.program, found].map(csvQuote).join(','));
  }
  return out.join('\n');
}

/* ================================================================== *
 * Node export (for headless testing)
 * ================================================================== */
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { Analyzer, runScan, buildReport, buildCsv, buildReportCsv, buildCrossCheck,
                     parseJcl, analyzeJcl, buildJclCsv,
                     sniffEncoding, makeDecoder, NAT_PROFILE, NAT_REPORT_PROFILE, REPORT_TYPE_TO_LETTER,
                     sniffFileProfile, parseNatTs, detectRecordLength, CAPS };
}

/* ================================================================== *
 * UI  (skipped entirely when loaded under Node for testing)
 * ================================================================== */
if (typeof document !== 'undefined') (function () {
  const $ = id => document.getElementById(id);
  const el = (tag, cls, txt) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (txt !== undefined) n.textContent = txt;
    return n;
  };
  const esc = s => String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  /** Build a <table> from a header list and row arrays. `numCols` = indices
   *  that should be right-aligned. */
  function table(headers, rows, numCols) {
    numCols = numCols || [];
    let h = '<table><thead><tr>';
    headers.forEach((x, i) => { h += '<th' + (numCols.includes(i) ? ' class="num"' : '') + '>' + esc(x) + '</th>'; });
    h += '</tr></thead><tbody>';
    for (const r of rows) {
      h += '<tr>';
      r.forEach((c, i) => {
        const isNum = numCols.includes(i);
        h += '<td' + (isNum ? ' class="num"' : '') + '>' +
             esc(isNum && typeof c === 'number' ? fmtNum(c) : c) + '</td>';
      });
      h += '</tr>';
    }
    return h + '</tbody></table>';
  }
  const scroll = html => '<div class="scroll">' + html + '</div>';
  const pairs = (list, h1, h2) => table([h1, h2], list.map(([k, v]) => [k, v]), [1]);

  function stats(items) {
    return '<div class="stats">' + items.map(([l, v]) =>
      '<div class="stat"><div class="l">' + esc(l) + '</div><div class="bignum">' + esc(v) + '</div></div>'
    ).join('') + '</div>';
  }

  /* ------------------------- state ------------------------- */
  let file = null, report = null, analyzer = null, cancelled = false;
  let file2 = null, report2 = null, analyzer2 = null, crossCheck = null;
  let jclFiles = [], jclResult = null;

  /* ------------------------- inputs ------------------------ */
  $('linemode').addEventListener('change', e => {
    $('reclen-wrap').classList.toggle('hidden', e.target.value !== 'fixed');
  });

  $('file').addEventListener('change', e => {
    file = e.target.files[0] || null;
    $('run').disabled = !file;
    $('fileinfo').innerHTML = file
      ? '<b>' + esc(file.name) + '</b> — ' + fmtBytes(file.size) +
        ' <span class="muted">(' + fmtNum(file.size) + ' bytes)</span>'
      : 'לא נבחר קובץ';
  });

  $('file2').addEventListener('change', e => {
    file2 = e.target.files[0] || null;
    $('clear-file2').classList.toggle('hidden', !file2);
    $('fileinfo2').innerHTML = file2
      ? '<b>' + esc(file2.name) + '</b> — ' + fmtBytes(file2.size)
      : '';
  });
  $('clear-file2').addEventListener('click', () => {
    file2 = null; $('file2').value = '';
    $('clear-file2').classList.add('hidden'); $('fileinfo2').innerHTML = '';
  });

  $('filesJcl').addEventListener('change', e => {
    jclFiles = [...e.target.files];
    $('clear-jcl').classList.toggle('hidden', !jclFiles.length);
    $('jclinfo').innerHTML = jclFiles.length
      ? '<b>' + fmtNum(jclFiles.length) + ' קבצי JCL</b> — ' +
        fmtBytes(jclFiles.reduce((s, f) => s + f.size, 0))
      : '';
  });
  $('clear-jcl').addEventListener('click', () => {
    jclFiles = []; $('filesJcl').value = '';
    $('clear-jcl').classList.add('hidden'); $('jclinfo').innerHTML = '';
  });

  $('cancel').addEventListener('click', () => { cancelled = true; });

  $('run').addEventListener('click', async () => {
    if (!file) return;
    cancelled = false;
    $('run').disabled = true;
    $('cancel').classList.remove('hidden');
    $('progwrap').classList.remove('hidden');
    ['sec-verdict', 'sec-results', 'sec-export'].forEach(i => $(i).classList.add('hidden'));

    const opts = {
      encoding: $('encoding').value,
      encodingRequested: $('encoding').value,
      lineMode: $('linemode').value,
      recLen: +$('reclen').value || 0,
      limitBytes: (+$('limit').value || 0) * 1024 * 1024,
      profile: $('profile').value
    };

    const makeProgress = tag => (off, lim, ms, an) => {
      const pct = lim ? (off / lim) * 100 : 100;
      $('bar').style.width = pct.toFixed(1) + '%';
      const mb = off / 1048576, sec = ms / 1000;
      $('progtext').textContent =
        tag + pct.toFixed(1) + '%  ·  ' + fmtBytes(off) + ' / ' + fmtBytes(lim) +
        '  ·  ' + (mb / Math.max(sec, .001)).toFixed(1) + ' MB/s' +
        '  ·  ' + fmtNum(an.g.lines) + ' רשומות' +
        '  ·  ' + sec.toFixed(0) + 's';
    };

    try {
      analyzer = await runScan(file, opts, makeProgress(file2 ? 'קובץ 1/2 · ' : ''), () => cancelled);
      report = buildReport(analyzer, file);

      analyzer2 = null; report2 = null; crossCheck = null;
      if (file2 && !cancelled) {
        analyzer2 = await runScan(file2, opts, makeProgress('קובץ 2/2 · '), () => cancelled);
        report2 = buildReport(analyzer2, file2);
        crossCheck = buildCrossCheck(analyzer, report, analyzer2, report2);
      }

      jclResult = null;
      if (jclFiles.length && !cancelled) {
        $('progtext').textContent = 'קורא ' + fmtNum(jclFiles.length) + ' קבצי JCL…';
        const texts = await Promise.all(jclFiles.map(async f => ({ name: f.name, text: await f.text() })));
        const rawAn = analyzer && analyzer.profileOn ? analyzer : (analyzer2 && analyzer2.profileOn ? analyzer2 : null);
        jclResult = analyzeJcl(texts, rawAn);
      }

      render(report);
    } catch (err) {
      $('sec-verdict').classList.remove('hidden');
      $('verdict').innerHTML =
        '<div class="v err"><div class="t">הסריקה נכשלה</div><div class="d">' +
        esc(err && err.message ? err.message : String(err)) +
        '<br><br><code>' + esc((err && err.stack ? err.stack : '').split('\n').slice(0, 4).join('\n')) +
        '</code></div></div>';
      console.error(err);
    } finally {
      $('run').disabled = false;
      $('cancel').classList.add('hidden');
    }
  });

  /* ------------------------- render ------------------------ */
  function renderJcl(jcl) {
    const out = [];
    out.push(stats([
      ['קבצי JCL', fmtNum(jcl.filesParsed)],
      ['הפניות לתוכניות', fmtNum(jcl.totalProgramRefs)],
      ['דרך Natural batch', fmtNum(jcl.naturalBatchRefs)],
      ['PGM= ישיר', fmtNum(jcl.directPgmRefs)],
      ['תוכניות שונות', fmtNum(jcl.distinctPrograms)],
      ['ספריות Natural', fmtNum(jcl.distinctLibraries)]
    ]));

    if (jcl.resolution) {
      const pct = jcl.resolution.resolved + jcl.resolution.unresolved
        ? (jcl.resolution.resolved / (jcl.resolution.resolved + jcl.resolution.unresolved) * 100) : 0;
      out.push('<div class="v ' + (jcl.resolution.unresolved === 0 ? 'ok' : 'warn') + '">' +
        '<div class="t">' + fmtNum(jcl.resolution.resolved) + ' מתוך ' +
        fmtNum(jcl.resolution.resolved + jcl.resolution.unresolved) +
        ' הפניות ל-Natural אומתו מול קובץ ה-unload (' + pct.toFixed(1) + '%)</div>' +
        '<div class="d">אם הרוב "לא נמצא" — כנראה קובץ ה-unload לא מכסה את הספרייה/הקובץ הזה ' +
        '(סריקה חלקית או קובץ אחר). לא בהכרח בעיה אמיתית.</div></div>');
      out.push('<h3>כל ההפניות ל-Natural, עם סטטוס אימות</h3>');
      out.push(scroll(table(['קובץ JCL', 'step', 'ספרייה', 'תוכנית', 'נמצא ב-unload'],
        jcl.resolution.rows.map(r => [r.file, r.step, r.library, r.program, r.foundInRaw ? '✓' : '—']))));
    } else {
      out.push('<p class="muted">לא נטען קובץ unload גולמי — מוצגות רק ההפניות שחולצו מה-JCL, בלי אימות.</p>');
      out.push('<h3>כל ההפניות שחולצו</h3>');
      out.push(scroll(table(['קובץ JCL', 'step', 'סוג', 'ספרייה', 'תוכנית'],
        jcl.allRefs.map(r => [r.file, r.step, r.kind, r.library || '—', r.program]))));
    }

    if (jcl.nonUtilityDirectPgm.length) {
      out.push('<h3>PGM= שאינם utility מוכר (' + fmtNum(jcl.nonUtilityDirectPgm.length) + ')</h3>');
      out.push('<p class="muted">אלה מועמדים לתוכניות COBOL/Assembler מותאמות אישית שה-JCL מריץ ישירות.</p>');
      out.push(jcl.nonUtilityDirectPgm.map(n => '<span class="chip">' + esc(n) + '</span>').join(''));
    }

    out.push('<h3>לפי קובץ JCL</h3>');
    out.push(scroll(table(['קובץ', 'JOB', 'steps', 'הפניות'],
      jcl.jobs.map(j => [j.file, j.jobName || '—', j.steps, j.programRefs]))));

    return out.join('');
  }

  function renderCrossCheck(cc) {
    const out = [];
    if (!cc.compatible) {
      out.push('<div class="v warn"><div class="t">לא ניתן להצליב</div><div class="d">' + esc(cc.note) + '</div></div>');
      return out.join('');
    }

    const pct = cc.reportRowsWithStatusUnloaded
      ? (cc.matchedInRawUnload / cc.reportRowsWithStatusUnloaded * 100) : 100;
    const level = cc.missingFromRawUnload === 0 ? 'ok' : !cc.bothFullyScanned ? 'warn' : 'err';
    out.push('<div class="v ' + level + '"><div class="t">' +
      fmtNum(cc.matchedInRawUnload) + ' מתוך ' + fmtNum(cc.reportRowsWithStatusUnloaded) +
      ' שורות "UNLOADED" בדוח נמצאו ב-unload הגולמי (' + pct.toFixed(2) + '%)' +
      '</div><div class="d" style="direction:rtl">' +
      (cc.missingFromRawUnload === 0
        ? 'כל האובייקטים שהדוח מסמן כהצליחו אכן נמצאים בקובץ הגולמי.'
        : fmtNum(cc.missingFromRawUnload) + ' אובייקטים שהדוח אומר שהצליחו — לא נמצאו בקובץ הגולמי.') +
      '</div></div>');

    if (cc.partialScanCaveat) out.push('<div class="v warn"><div class="t">סריקה חלקית</div><div class="d">' +
      esc(cc.partialScanCaveat) + '</div></div>');

    out.push(stats([
      ['קובץ unload גולמי', esc(cc.rawFile.name)],
      ['אובייקטים ב-unload', fmtNum(cc.rawFile.objects)],
      ['קובץ דוח', esc(cc.reportFile.name)],
      ['שורות בדוח', fmtNum(cc.reportFile.rows)],
      ['נבדקו (UNLOADED)', fmtNum(cc.reportRowsWithStatusUnloaded)],
      ['נמצאו חסרים', fmtNum(cc.missingFromRawUnload)]
    ]));

    if (cc.missingSamples.length) {
      out.push('<h3>אובייקטים שהדוח אומר UNLOADED אבל לא נמצאו ב-unload הגולמי' +
        (cc.missingTruncated ? ' (500 ראשונים מתוך ' + fmtNum(cc.missingFromRawUnload) + ')' : '') + '</h3>');
      out.push(scroll(table(['library', 'name', 'type', '≈letter', 'date', 'time', 'user'],
        cc.missingSamples.map(m => [m.library, m.name, m.type, m.letter || '(לא ממופה)', m.date, m.time, m.user]))));
    }

    out.push('<h3>אובייקטים ב-unload שלא הוזכרו בדוח כלל: ' + fmtNum(cc.extraInRawNotInReport) + '</h3>');
    out.push('<p class="muted">' + esc(cc.extraInRawNote) + '</p>');

    return out.join('');
  }

  function render(r) {
    /* verdict */
    $('verdict').innerHTML = r.verdict.map(v =>
      '<div class="v ' + v.level + '"><div class="t">' + esc(v.title) +
      '</div><div class="d">' + esc(v.detail) + '</div></div>').join('');
    $('sec-verdict').classList.remove('hidden');

    /* ---------- cross-check ---------- */
    $('tab-cross').classList.toggle('hidden', !crossCheck);
    if (crossCheck) $('t-cross').innerHTML = renderCrossCheck(crossCheck);

    /* ---------- JCL ---------- */
    $('tab-jcl').classList.toggle('hidden', !jclResult);
    $('dl-jcl-csv').classList.toggle('hidden', !jclResult);
    if (jclResult) $('t-jcl').innerHTML = renderJcl(jclResult);

    /* ---------- overview ---------- */
    const o = [];
    o.push(stats([
      ['גודל קובץ', r.file.sizeHuman],
      ['נסרק', r.file.scannedHuman],
      ['רשומות', fmtNum(r.generic.records)],
      ['אובייקטים', r.profile ? fmtNum(r.profile.objects.countInScan)
                   : r.jobLog ? fmtNum(r.jobLog.rows.seen) + ' שורות' : '—'],
      ['שורות מקור', r.profile ? fmtNum(r.profile.objects.sourceLines)
                    : r.jobLog ? fmtNum(r.jobLog.distinctLibraries) + ' ספריות' : '—'],
      ['זמן', r.scan.durationSec + 's · ' + r.scan.throughputMBps + ' MB/s']
    ]));
    o.push('<h3>סוג קובץ שזוהה</h3>');
    o.push('<dl class="kv"><dt>פרופיל</dt><dd>' + esc(r.tool.profile) +
      (r.tool.profileRequested === 'auto' ? ' (זוהה אוטומטית)' : ' (נבחר ידנית)') + '</dd></dl>');
    o.push('<h3>קידוד וזיהוי</h3>');
    o.push('<dl class="kv">' +
      ['<dt>קידוד בשימוש</dt><dd>' + esc(r.scan.encodingUsed) + '</dd>',
       '<dt>ניחוש אוטומטי</dt><dd>' + esc(r.encodingSniff.guess) + '</dd>',
       '<dt>נימוק</dt><dd>' + esc(r.encodingSniff.why) + '</dd>',
       '<dt>סיום שורה (בייטים)</dt><dd>' + esc(r.encodingSniff.lineEnding) + '</dd>',
       '<dt>מפריד רשומות</dt><dd>' + esc(r.scan.recordSeparator || ('אורך קבוע ' + r.scan.fixedRecordLength)) + '</dd>',
       '<dt>UTF-8 תקין</dt><dd>' + (r.encodingSniff.utf8Valid ? 'כן' : 'לא') + '</dd>',
       '<dt>תווי U+FFFD</dt><dd>' + fmtNum(r.encodingDamage.replacementCharsFound) + '</dd>'
      ].join('') + '</dl>');

    o.push('<h3>אותו קטע מפוענח בכל קידוד — בחר את זה שנקרא</h3>');
    for (const [enc, lines] of Object.entries(r.encodingCandidatePreview.decoded)) {
      o.push('<div class="chip">' + esc(enc) + (enc === r.scan.encodingUsed ? ' ✓ נבחר' : '') + '</div>');
      o.push('<pre>' + esc(lines.join('\n')) + '</pre>');
    }

    if (r.profile && r.profile.header) {
      o.push('<h3>כותרת הייצוא (רשומת *H**)</h3>');
      o.push('<dl class="kv">' + Object.entries(r.profile.header)
        .map(([k, v]) => '<dt>' + esc(k) + '</dt><dd>' + esc(v) + '</dd>').join('') + '</dl>');
    }
    if (r.profile && r.profile.extrapolationToFullFile) {
      const e = r.profile.extrapolationToFullFile;
      o.push('<h3>הערכה לקובץ המלא (אקסטרפולציה לינארית)</h3>');
      o.push('<dl class="kv"><dt>אובייקטים משוערים</dt><dd>' + fmtNum(e.estimatedObjects) +
        '</dd><dt>שורות מקור משוערות</dt><dd>' + fmtNum(e.estimatedSourceLines) +
        '</dd><dt>הערה</dt><dd>' + esc(e.note) + '</dd></dl>');
    }
    if (r.jobLog) {
      const ri = r.jobLog.runInfo;
      o.push('<h3>פרטי ההרצה (מתוך כותרת הדוח)</h3>');
      const kv = [];
      if (ri.banner) kv.push('<dt>תאריך/שעת הרצה</dt><dd>' + esc(ri.banner.date) + ' ' + esc(ri.banner.time) + '</dd>');
      if (ri.context) kv.push('<dt>משתמש</dt><dd>' + esc(ri.context.user) + '</dd><dt>ספריית הרצה</dt><dd>' + esc(ri.context.library) + '</dd>');
      if (ri.reportMember) kv.push('<dt>הדוח נשמר כאובייקט Text</dt><dd>' + esc(ri.reportMember) + '</dd>');
      if (ri.commandLine) kv.push('<dt>פקודת ה-SYSOBJH</dt><dd>' + esc(ri.commandLine) + '</dd>');
      o.push('<dl class="kv">' + kv.join('') + '</dl>');
      o.push('<p class="muted">קובץ זה הוא דוח ריצה קריא לבני אדם — אין בו קוד מקור. ' +
        'טבלה מלאה של השורות בטאב "אובייקטים".</p>');
    }
    $('t-overview').innerHTML = o.join('');

    /* ---------- structure ---------- */
    const st = [];
    st.push('<h3>תדירות תגית 4 תווים ראשונים (' + fmtNum(r.generic.distinctPrefix4) + ' ערכים שונים)</h3>');
    st.push(scroll(pairs(r.generic.prefix4Histogram, 'prefix', 'count')));
    st.push('<h3>אורכי רשומה (' + fmtNum(r.generic.distinctRecordLengths) + ' ערכים שונים)</h3>');
    st.push(scroll(pairs(r.generic.recordLengthHistogramTop, 'length (chars)', 'count')));
    st.push('<h3>שארית אורך מודולו 12</h3>');
    st.push(pairs(r.generic.lengthModulo12, 'len % 12', 'count'));
    st.push('<h3>תו ראשון</h3>');
    st.push(scroll(pairs(r.generic.leadingCharHistogram, 'char', 'count')));
    if (r.profile) {
      st.push('<h3>סוגי רשומות לפי הפרופיל</h3>');
      st.push(pairs(r.profile.recordCounts, 'record', 'count'));
    }
    st.push('<h3>הרשומה הארוכה ביותר (' + fmtNum(r.generic.maxRecordLenChars) + ' תווים)</h3>');
    st.push('<pre>' + esc(r.generic.longestRecordSample) + '</pre>');
    $('t-structure').innerHTML = st.join('');

    /* ---------- objects ---------- */
    const ob = [];
    if (r.jobLog) {
      const J = r.jobLog;
      ob.push('<h3>סוגי אובייקטים (מילה מלאה בדוח + האות המקבילה בקובץ הגולמי)</h3>');
      ob.push(table(['type word', 'count', '≈ letter'], J.byType.map(t => [t.word, t.count, t.letter || '?']), [1]));
      ob.push('<h3>סטטוס שורה</h3>' + pairs(J.byStatus, 'status', 'rows'));
      ob.push('<h3>ספריות (' + fmtNum(J.distinctLibraries) + ')</h3>');
      ob.push(scroll(pairs(J.byLibrary, 'library', 'rows')));
      ob.push('<h3>משתמשים (מי שמר את האובייקט)</h3>');
      ob.push(scroll(pairs(J.byUser, 'user', 'rows')));
      ob.push('<h3>S/C</h3>' + pairs(J.bySC, 'S/C', 'rows'));
      ob.push('<h3>שנת שמירה</h3>' + scroll(pairs(J.byYear, 'year', 'rows')));
      if (J.duplicateRows.length) ob.push('<h3>שורות כפולות</h3>' + scroll(pairs(J.duplicateRows, 'library|name|type', 'count')));
      const rows = analyzer.rep.rows.slice(0, 500);
      ob.push('<h3>מלאי שורות (500 ראשונות מתוך ' + fmtNum(analyzer.rep.rows.length) + ' — המלאי המלא ב-CSV)</h3>');
      ob.push(scroll(table(['library', 'name', 'type', '≈letter', 'S/C', 'dbid/fnr', 'date', 'time', 'user', 'status'], rows)));
    } else if (!r.profile) ob.push('<p class="muted">לא הופעל פרופיל מבנה.</p>');
    else {
      const P = r.profile.objects;
      ob.push('<h3>סוגי אובייקטים — מיפוי האותיות הוסק מהנתונים, לא מתיעוד</h3>');
      ob.push(table(['type', 'count', 'meaning', 'kind', 'confidence', 'evidence'],
        P.byType.map(t => [t.type.trim() || '(empty)', t.count, t.meaning, t.kind, t.confidence, t.evidence]), [1]));
      ob.push('<h3>ספריות (' + fmtNum(P.distinctLibraries) + ')</h3>');
      ob.push(scroll(pairs(P.byLibrary, 'library', 'objects')));
      ob.push('<h3>גרסת Natural</h3>' + pairs(P.byNaturalVersion, 'NAT version', 'objects'));
      ob.push('<h3>מערכת הפעלה / TP monitor</h3>' + pairs(P.byOsAndTpMonitor, 'OS / TP', 'objects'));
      ob.push('<h3>Codepage מוצהר (*D04)</h3>' + pairs(P.byCodepageField, 'codepage', 'objects'));
      ob.push('<h3>שנת שמירה אחרונה</h3>' + scroll(pairs(P.bySavedYear, 'year', 'objects')));
      ob.push('<h3>גוף האובייקט מול הסוג המוצהר</h3>' + scroll(pairs(P.declaredTypeVsBodyShape, 'declared>observed', 'count')));
      ob.push('<h3>30 האובייקטים הגדולים ביותר</h3>');
      ob.push(scroll(table(['library', 'name', 'type', 'srcLines', 'declaredSize', 'natVer', 'saved'],
        P.top30BySourceLines.map(x => [x.library, x.name, x.type, x.srcLines, x.declaredSize, x.natVersion, x.saved]), [3, 4])));

      const rows = analyzer.p.objects.slice(0, 500).map(x =>
        [x[0], x[1], x[2], (NAT_PROFILE.typeMap[x[2]] || {}).name || '?', x[3], x[4], x[6], x[7]]);
      ob.push('<h3>מלאי אובייקטים (500 ראשונים מתוך ' + fmtNum(analyzer.p.objects.length) +
              ' — המלאי המלא ב-CSV)</h3>');
      ob.push(scroll(table(['library', 'name', 'type', 'meaning', 'natVer', 'saved', 'size', 'srcLines'],
        rows, [6, 7])));
    }
    $('t-objects').innerHTML = ob.join('');

    /* ---------- dependencies ---------- */
    const dp = [];
    if (r.jobLog) dp.push('<p class="muted">לא רלוונטי — קובץ מסוג דוח SYSOBJH אינו מכיל קוד מקור, ולכן אין תלויות לחלץ.</p>');
    else if (!r.lexical) dp.push('<p class="muted">לא הופעל פרופיל מבנה.</p>');
    else {
      const L = r.lexical;
      dp.push('<h3>פקודות Natural נפוצות (' + fmtNum(L.distinctStatementKeywords) + ' שונות)</h3>');
      dp.push(scroll(pairs(L.topStatementKeywords, 'statement', 'count')));
      const D = L.dependencies;
      const blocks = [
        ['USING (data areas / views)', D.usingTargets, L.resolution.using],
        ['CALLNAT (subprograms)', D.callnatTargets, L.resolution.callnat],
        ['PERFORM (subroutines)', D.performTargets, L.resolution.perform],
        ['MAP', D.mapTargets, L.resolution.map],
        ['FETCH (programs)', D.fetchTargets, null],
        ['INCLUDE (copycode)', D.includeTargets, null],
        ["CALL '…' (3GL חיצוני)", D.externalCall3GL, null]
      ];
      for (const [title, dd, res] of blocks) {
        dp.push('<h3>' + esc(title) + ' — ' + fmtNum(dd.distinct) + ' יעדים שונים' +
          (res ? ' · <span class="muted">' + fmtNum(res.referencesUnresolved) +
                 ' הפניות לא נפתרו מתוך ' + fmtNum(res.referencesResolved + res.referencesUnresolved) + '</span>' : '') +
          '</h3>');
        if (title.indexOf('PERFORM') === 0 && L.resolution.performNote) {
          dp.push('<p class="muted" style="direction:ltr;text-align:left">' + esc(L.resolution.performNote) + '</p>');
        }
        dp.push(scroll(pairs(dd.top, 'target', 'refs')));
        if (res && res.topUnresolved.length) {
          dp.push('<div class="muted" style="font-size:12px;margin:4px 0 10px">יעדים שלא נמצאו בסריקה: ' +
            res.topUnresolved.slice(0, 25).map(x => '<span class="chip">' + esc(x[0]) + '</span>').join('') + '</div>');
        }
      }
      dp.push('<p class="muted">' + esc(L.resolution.note) + '</p>');
    }
    $('t-deps').innerHTML = dp.join('');

    /* ---------- samples ---------- */
    const sm = [];
    if (r.jobLog) sm.push('<p class="muted">לא רלוונטי — קובץ מסוג דוח SYSOBJH אינו מכיל קוד מקור.</p>');
    else if (!r.sourceSamples) sm.push('<p class="muted">לא הופעל פרופיל מבנה.</p>');
    else for (const [type, objs] of Object.entries(r.sourceSamples)) {
      sm.push('<h3>' + esc(type) + '</h3>');
      for (const ob2 of objs) {
        sm.push('<div class="chip">' + esc(ob2.library) + ' / ' + esc(ob2.name) + '</div>');
        sm.push('<pre>' + esc(ob2.firstLines.join('\n')) + '</pre>');
      }
    }
    $('t-samples').innerHTML = sm.join('');

    /* ---------- anomalies ---------- */
    const an2 = [];
    if (!r.anomalies.length) an2.push('<p class="muted">לא נמצאו חריגות.</p>');
    else {
      an2.push('<p class="muted">כל חריגה מוגבלת ל-' + CAPS.anomalySamples + ' דגימות. הכול נכלל ב-JSON.</p>');
      for (const a of r.anomalies) {
        an2.push('<h3>' + esc(a.kind) + ' — ' + fmtNum(a.count) + '</h3>');
        an2.push(scroll(table(['record #', 'details'],
          a.samples.map(s => {
            const rest = Object.entries(s).filter(([k]) => k !== 'line' && k !== 'text')
              .map(([k, v]) => k + '=' + v).join('  ');
            return [s.line, (rest ? rest + '   ' : '') + (s.text || '')];
          }), [0])));
      }
    }
    $('t-anom').innerHTML = an2.join('');

    $('sec-results').classList.remove('hidden');
    $('sec-export').classList.remove('hidden');
    const json = JSON.stringify(exportPayload(), null, 1);
    const csvRowCount = analyzer.reportOn ? analyzer.rep.rows.length : analyzer.p.objects.length;
    $('expinfo').textContent = 'JSON ≈ ' + fmtBytes(json.length) + '  ·  CSV ' + fmtNum(csvRowCount) + ' שורות' +
      (crossCheck ? '  ·  כולל הצלבה' : '') + (jclResult ? '  ·  כולל JCL' : '');
  }

  function exportPayload() {
    if (!crossCheck && !jclResult) return report;
    const p = crossCheck ? { fileA: report, fileB: report2, crossCheck } : { file: report };
    if (jclResult) p.jcl = jclResult;
    return p;
  }

  /* ------------------------- tabs -------------------------- */
  $('tabs').addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b) return;
    [...$('tabs').children].forEach(x => x.classList.toggle('active', x === b));
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.id === b.dataset.tab));
  });

  /* ------------------------- export ------------------------ */
  function download(name, text, mime) {
    const url = URL.createObjectURL(new Blob([text], { type: mime + ';charset=utf-8' }));
    const a = document.createElement('a');
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }
  const base = () => (file ? file.name.replace(/\.[^.]*$/, '') : 'scan') + '-' +
                     new Date().toISOString().slice(0, 19).replace(/[:T]/g, '');

  $('dl-json').addEventListener('click', () => {
    if (report) download(base() + '-discovery-log.json', JSON.stringify(exportPayload(), null, 1), 'application/json');
  });
  $('dl-csv').addEventListener('click', () => {
    if (!analyzer) return;
    const csv = analyzer.reportOn ? buildReportCsv(analyzer) : buildCsv(analyzer);
    const name = analyzer.reportOn ? '-rows.csv' : '-objects.csv';
    download(base() + name, '﻿' + csv, 'text/csv');
  });
  $('dl-jcl-csv').addEventListener('click', () => {
    if (jclResult) download(base() + '-jcl-refs.csv', '﻿' + buildJclCsv(jclResult), 'text/csv');
  });
  $('copy-json').addEventListener('click', async () => {
    if (!report) return;
    const btn = $('copy-json');
    try {
      await navigator.clipboard.writeText(JSON.stringify(exportPayload(), null, 1));
      btn.textContent = 'הועתק ✓';
    } catch (e) {
      btn.textContent = 'ההעתקה נחסמה — השתמש בהורדה';
    }
    setTimeout(() => { btn.textContent = 'העתק ללוח'; }, 2500);
  });
})();
