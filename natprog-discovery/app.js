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
         evidence: 'Seen once in a real 770MB scan (NCSTDEMO/NCPDEMO). Needs a source-body sample to classify.' }
  }
};

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

    this.anomalies = new Map();
    this.cur = null;                   // object under construction
    this.lineNo = 0;
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

    if (!this.profileOn || len === 0) return;
    this.feedProfile(line, len, truncated);
  }

  /* ================= profile layer ================= */
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

  /* ---- 3. stream ---- */
  const an = new Analyzer({ ...opts, encoding, lineMode, recLen });
  an.meta = { sniff, encoding, lineMode, recLen, fixedDetection, framing, separator: SEP, preview };

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
  const g = an.g, p = an.p, m = an.meta;
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
    tool: { name: 'NATPROG Discovery', version: '1.0', profile: an.profileOn ? NAT_PROFILE.id : 'none' },
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

    anomalies,
    anomalyKindsTruncated: an.anomalies.size >= CAPS.anomalyKinds,

    caps: CAPS,
    fieldLayoutUsed: an.profileOn ? {
      note: '0-based character offsets after decoding. "inferred" fields are read from data patterns, not vendor documentation — challenge them if the numbers look wrong.',
      recordPadding: NAT_PROFILE.recordPad,
      H: NAT_PROFILE.H, C: NAT_PROFILE.C, D01: NAT_PROFILE.D01,
      D02: NAT_PROFILE.D02, D03: NAT_PROFILE.D03, D04: NAT_PROFILE.D04
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

  if (!r.profile) {
    add('warn', 'Generic scan only',
      'No structural profile was applied. Use the prefix histogram and record-length histogram below to decide what this file is.');
  } else if (r.profile.objects.countInScan === 0) {
    add('err', 'PROFILE DOES NOT MATCH — no objects found',
      'Not a single *C** catalog record was recognised. The file is either a different format, a different encoding, ' +
      'or split into records differently. Do not trust any object numbers in this report.');
  } else if (!r.profile.matched) {
    add('err', 'PROFILE PARTIALLY MATCHES — ' + (r.profile.matchRate * 100).toFixed(2) + '% of records recognised',
      r.profile.unknownRecords.toLocaleString() + ' records did not start with a known 4-character tag. ' +
      'Either the layout drifts partway through the file, or a second format is concatenated into it. ' +
      'See the "unknown_record_prefix" anomaly samples.');
  } else {
    add('ok', 'Structure matches the Natural SYSOBJH profile',
      (r.profile.matchRate * 100).toFixed(3) + '% of records recognised across ' +
      r.generic.records.toLocaleString() + ' records / ' +
      r.profile.objects.countInScan.toLocaleString() + ' objects.');
  }

  if (r.encodingDamage.replacementCharsFound > 0) {
    const cause = r.encodingDamage.likelyCause;
    add(cause === 'pre-existing-in-bytes' ? 'err' : 'warn',
      (cause === 'pre-existing-in-bytes' ? 'Character data already destroyed: ' : 'Possible codepage mismatch: ') +
      r.encodingDamage.replacementCharsFound.toLocaleString() + ' U+FFFD characters (' +
      r.encodingDamage.recordsAffected.toLocaleString() + ' records)',
      r.encodingDamage.meaning);
  }

  if (r.generic.lengthModulo12.length > 1) {
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

  return v;
}

/* ================================================================== *
 * CSV inventory
 * ================================================================== */
const CSV_HEADER = ['library','name','type','type_meaning','nat_version','saved','cataloged',
                    'declared_size','src_lines','src_chars','max_src_len','os','tp_monitor',
                    'codepage','user1','user2','user3'];

function buildCsv(an) {
  const q = s => {
    s = s === null || s === undefined ? '' : String(s);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const out = [CSV_HEADER.join(',')];
  for (const r of an.p.objects) {
    const meaning = (NAT_PROFILE.typeMap[r[2]] || {}).name || 'UNKNOWN';
    out.push([r[0], r[1], r[2], meaning, r[3], r[4], r[5], r[6], r[7], r[8], r[9],
              r[10], r[11], r[12], r[13], r[14], r[15]].map(q).join(','));
  }
  return out.join('\n');
}

/* ================================================================== *
 * Node export (for headless testing)
 * ================================================================== */
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { Analyzer, runScan, buildReport, buildCsv, sniffEncoding,
                     makeDecoder, NAT_PROFILE, parseNatTs, detectRecordLength, CAPS };
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

    try {
      analyzer = await runScan(file, opts, (off, lim, ms, an) => {
        const pct = lim ? (off / lim) * 100 : 100;
        $('bar').style.width = pct.toFixed(1) + '%';
        const mb = off / 1048576, sec = ms / 1000;
        $('progtext').textContent =
          pct.toFixed(1) + '%  ·  ' + fmtBytes(off) + ' / ' + fmtBytes(lim) +
          '  ·  ' + (mb / Math.max(sec, .001)).toFixed(1) + ' MB/s' +
          '  ·  ' + fmtNum(an.g.lines) + ' רשומות' +
          '  ·  ' + fmtNum(an.p.objectsSeen) + ' אובייקטים' +
          '  ·  ' + sec.toFixed(0) + 's';
      }, () => cancelled);

      report = buildReport(analyzer, file);
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
  function render(r) {
    /* verdict */
    $('verdict').innerHTML = r.verdict.map(v =>
      '<div class="v ' + v.level + '"><div class="t">' + esc(v.title) +
      '</div><div class="d">' + esc(v.detail) + '</div></div>').join('');
    $('sec-verdict').classList.remove('hidden');

    /* ---------- overview ---------- */
    const o = [];
    o.push(stats([
      ['גודל קובץ', r.file.sizeHuman],
      ['נסרק', r.file.scannedHuman],
      ['רשומות', fmtNum(r.generic.records)],
      ['אובייקטים', r.profile ? fmtNum(r.profile.objects.countInScan) : '—'],
      ['שורות מקור', r.profile ? fmtNum(r.profile.objects.sourceLines) : '—'],
      ['זמן', r.scan.durationSec + 's · ' + r.scan.throughputMBps + ' MB/s']
    ]));
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
    if (!r.profile) ob.push('<p class="muted">לא הופעל פרופיל מבנה.</p>');
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
    if (!r.lexical) dp.push('<p class="muted">לא הופעל פרופיל מבנה.</p>');
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
    if (!r.sourceSamples) sm.push('<p class="muted">לא הופעל פרופיל מבנה.</p>');
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
    const json = JSON.stringify(r, null, 1);
    $('expinfo').textContent = 'JSON ≈ ' + fmtBytes(json.length) +
      '  ·  CSV ' + fmtNum(analyzer.p.objects.length) + ' שורות';
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
    if (report) download(base() + '-discovery-log.json', JSON.stringify(report, null, 1), 'application/json');
  });
  $('dl-csv').addEventListener('click', () => {
    if (analyzer) download(base() + '-objects.csv', '﻿' + buildCsv(analyzer), 'text/csv');
  });
  $('copy-json').addEventListener('click', async () => {
    if (!report) return;
    const btn = $('copy-json');
    try {
      await navigator.clipboard.writeText(JSON.stringify(report, null, 1));
      btn.textContent = 'הועתק ✓';
    } catch (e) {
      btn.textContent = 'ההעתקה נחסמה — השתמש בהורדה';
    }
    setTimeout(() => { btn.textContent = 'העתק ללוח'; }, 2500);
  });
})();
