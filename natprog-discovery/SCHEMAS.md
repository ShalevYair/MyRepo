# סכימות — חוזה הנתונים בין Python ל-HTML

מסמך יחיד לכל קובץ JSON/JSONL בצינור. שני צדדים קוראים אותו: כל תוכנית Python
שכותבת קובץ, וכל קוד JS שקורא אותו. שינוי שם שדה כאן = שינוי בשני מקומות בבת אחת.

כל שדה מסומן:
* **קיים** — `natural-viewer.html` כבר קורא אותו היום (עם מספר שורה). **אסור לשנות שם
  בלי לעדכן את שם הקריאה שם.**
  Bash: `grep -n '<שם השדה>' natprog-discovery/natural-viewer.html`
* **חדש** — נוסף כחלק מהצינור הזה. תוסף, לא שובר קריאות קיימות.

---

## 0. העיקרון המחייב: `object_id`

מ-שלב 1 ואילך, `object_id` בכל קובץ חדש הוא **`LIBRARY/NAME`** (uppercase, ללא רווחים).
זו שבירת תאימות מכוונת מול הקבצים הידניים הקיימים (ניתוח/כותרות/קטגוריזציה),
שהיום ממופים לפי שם בלבד — ראו `MERGE-PLAN.md` פער 2.

* קובץ שמגיע עם `object_id` בלי `/` (כמו קבצי הניתוח הישנים בני 3,000 הרשומות)
  נחשב **legacy**: הצלבה חוזרת לפי שם בלבד, מסומנת `ambiguous_match: true`
  בתוצאה — **מוצג בממשק, לא מוסתר**. מימוש ההצלבה עצמה הוא שלב 7.1.
* `keyOf()` ב-JS (`natural-viewer.html:317`) הוא `String(...).trim().toUpperCase()` —
  נשאר כפי שהוא; ה-`/` פשוט הופך לחלק מהמפתח.

---

## 1. `objects.jsonl` — פלט `natunload_split.py` (שלב 1) · חדש

שורה לאובייקט. כל שדה חדש (אין קובץ קודם מסוג זה).

| שדה | טיפוס | חובה | הערה |
|---|---|---|---|
| `object_id` | string | כן | `LIBRARY/NAME` |
| `library` | string | כן | |
| `name` | string | כן | |
| `type` | string(1) | כן | אות הסוג הגולמית (`F`,`N`,`S`,…) |
| `type_meaning` | string\|null | לא | מ-`NAT_PROFILE.typeMap`; `null` אם אות לא מוכרת (`7`,`5`) |
| `kind` | string\|null | לא | `exec`\|`map`\|`data`\|`text`\|`null` |
| `type_confidence` | string\|null | לא | `confirmed`\|`inferred`\|`guess`\|`none` |
| `nat_version` | string | לא | מ-`*D01` |
| `saved` | string(ISO)\|null | לא | מ-`*D02` |
| `cataloged` | string(ISO)\|null | לא | מ-`*D02` |
| `size` | int\|null | לא | מ-`*D02` |
| `lines` | int | כן | ספירת `*S**`+`-S**` |
| `chars` | int | כן | |
| `max_line_len` | int | לא | |
| `os` | string | לא | מ-`*D03` |
| `tp` | string | לא | מ-`*D03` |
| `codepage` | string | לא | מ-`*D04` |
| `users` | [string] | לא | עד 3, מ-`*D01` |
| `source_path` | string | כן | יחסי ל-`out/`, למשל `source/RC/GO0701P0.nat` |
| `sha256_raw` | string(64hex) | כן | hash של המקור כפי שנקרא |
| `sha256_norm` | string(64hex) | כן | אחרי נרמול — ראו §6 |
| `truncated` | bool | לא | האם זה הרשומה האחרונה בסריקה חלקית |

---

## 2. `natmap.json` — פלט `natmap3.py` (שלב 3)

**כבר נקרא ב-`applyNatmap`, `natural-viewer.html:667-715`.** כל שדה קיים חייב להישאר.

### 2.1 `meta` (קיים חלקית)
| שדה | קיים/חדש | הערה |
|---|---|---|
| `schema_version` | קיים | `natural-viewer.html:1341` |
| `generated_at` | חדש | |
| `object_count` | חדש | |
| `dynamic_callnat_ratio` | חדש | תוצר מדידה 3.4 — **חובה להציג בממשק**, לא רק לכתוב |

### 2.2 `objects[]` — כל שדה קיים, `applyNatmap:688-712`
`object_id`, `triage`, `domain`, `primary_ddm`, `ui_class`, `object_type`, `max_depth`,
`if_count`, `decide_count`, `compute_count`, `code_lines`, `fan_in`, `fan_out`, `writes`
(bool→0/1), `n_obsolete`, `unbalanced`, `self_redundancy`, `family`.

**הערה:** `triage` בשדה הזה **לא** נכתב על ידי `natmap3.py` — הוא תוצר שלב 5
(`propagate.py`, כותב ל-`triage.json`). `natmap3.py` משאיר את זה ריק/לא כותב את המפתח,
ו-`propagate.py` הוא זה שממזג אותו פנימה לפני שהקובץ נטען לממשק — או שהממשק טוען
את שני הקבצים ומאחד אותם ב-`applyNatmap`+`applyTriage` (החלטת מימוש, שלב 7.3).

### 2.3 `ddm_access[]` — קיים, `applyNatmap:671-678`
| שדה | הערה |
|---|---|
| `object_id` | |
| `ddm` | |
| `op` | `STORE`\|`UPDATE`\|`DELETE`\|אחר. הראשונים ⇒ `write=true` (`WRITE_OPS`, `app.js` המקורי) |

### 2.4 `dup_pairs[]` — קיים, `applyNatmap:680-685`
`{a, b}` — שני `object_id` בעלי `sha256_norm` זהה.

### 2.5 `calls[]` — קיים חלקית, **מורחב**
היום (`external3gl`, `natural-viewer.html:718-724`) רק `kind`,`scope`,`target` נקראים,
ורק לצורך `kind==='CALL3GL' && scope==='unresolved'`. הקשתות עצמן (`from`) **לא נכתבות
בקוד המקורי בכלל** — זה בדיוק פער 3 מ-`MERGE-PLAN.md`. השדות החדשים נחוצים לגרף:

| שדה | קיים/חדש | הערה |
|---|---|---|
| `kind` | קיים | `CALLNAT`\|`FETCH`\|`INCLUDE`\|`USING`\|`MAP`\|`PERFORM`\|`CALL3GL` |
| `scope` | קיים | `same_library`\|`steplib`\|`system`\|`ambiguous`\|`unresolved`\|`external_3gl` |
| `target` | קיים | השם הגולמי כפי שמופיע במקור |
| `from` | **חדש** | `object_id` הקורא — בלעדיו אין גרף, רק היסטוגרמה |
| `resolved_to` | **חדש** | `object_id` היעד אחרי רזולוציה, `null` אם `unresolved`/`ambiguous` |
| `candidates` | **חדש** | `[object_id]` — כל המועמדים כש-`scope=ambiguous` |
| `dynamic` | **חדש** | `bool` — `true` אם היעד היה משתנה לא ליטרל (נספר לתוך 3.4, לא מקבל קשת) |

### 2.6 `obsolete[]` — קיים בפרסור, לא בשימוש
`parseNatmap` (`natural-viewer.html:661`) קורא את זה, שום מקום אחר לא משתמש בו.
**החלטה (`WORKPLAN.md` 7.4):** או להשמיט מהפלט של `natmap3.py`, או לחבר לשימוש אמיתי
(למשל: אובייקטים עם `n_obsolete>0` שגם `dead≥0.85`). לא משאירים "כמו שהיה" בלי סיבה.

---

## 3. `jcl.json` — פלט `jclmap.py` (שלב 4.1) · חדש

| שדה | הערה |
|---|---|
| `jobs[]` | `{file, job_name, steps[]}` |
| `steps[]` | `{step, kind: 'pgm'\|'proc', target}` |
| `entry_points[]` | `{library, program, jcl_file, step, kind: 'natural-batch'\|'direct-pgm', resolved: bool}` |
| `steplib_chains[]` | **חדש**, לא היה ב-`app.js` המקורי — `{job, library_order: [string]}`, מ-`STEPLIB`/`NATLIB DD`. ריק אם JCL לא מכיל את זה |
| `utility_refs[]` | `EXEC PGM=` שזוהה כ-utility (`UTILITY_PGM_NAMES`) — לא entry point |

`resolved` נכתב על ידי `natmap3.py` אחרי שהוא רץ (או `null` אם `jclmap.py` רץ לפני
שיש `objects.jsonl`) — לא ערך שרירותי.

---

## 4. `cobol.json` — פלט `cobolmap.py` (שלב 4.2) · חדש

| שדה | הערה |
|---|---|
| `programs[]` | `{file, program_id, uses_cics}` |
| `calls[]` | `{file, from, kind: 'call'\|'cics-link'\|'cics-xctl', target, found_in_folder}` |
| `cics_starts[]` | `{file, from, target}` — TRANSID, לא `PROGRAM-ID`, לא נספר כ-unresolved |
| `natural_bridge[]` | **חדש** — `{cobol_program, natural_call_target}` כש-`CALL 'x'` ב-Natural תואם `PROGRAM-ID` כאן |

---

## 5. `triage.json` — פלט `propagate.py` (שלב 5) · חדש

| שדה | הערה |
|---|---|
| `object_id` | |
| `alive` | float [0,1] |
| `dead` | float [0,1] |
| `triage` | `live`\|`dead_shadow_copy`\|`dead_unreachable`\|`dead_stale_library`\|`source_only`\|`external_entry_suspect`\|`unknown` |
| `alive_evidence[]` | `[{from, via_kind, contributed}]` — שרשרת מלאה, לא רק המספר הסופי |
| `dead_evidence[]` | כנ"ל, לכיוון ההפוך |

ערכי `triage` תואמים ל-`TRIAGE_HE` הקיים ב-`natural-viewer.html:871-877`
(`dead_likely`, `external_entry_suspect`, `rules_core`, `platform_replaces`, `support`)
**רק חלקית** — המיפוי המלא בין השמות החדשים לתוויות העבריות הקיימות הוא החלטת מימוש
של שלב 7.3, לא כאן.

---

## 6. `sha256_norm` — הגדרת הנרמול (משותפת לכל התוכניות)

לפני חישוב ה-hash:
1. לכל שורה: trim משני הצדדים (גם הזחה בהתחלה, לא רק רווחים בסוף) —
   לצורך hash כפילויות אין סיבה עקרונית להתייחס להזחה כ"אמיתית" ולרווח
   סוגר כ"רעש"; שניהם רעש עיצוב אפשרי מ-copy-before-you-change.
2. איחוד רצף רווחים פנימי לרווח בודד.
3. השמטת שורות ריקות (אחרי 1-2).
4. חיבור ב-`\n`, קידוד UTF-8, `sha256`.

מומש פעם אחת ב-`natlib/objid.py:normalize_source()` — **אף תוכנית לא מממשת את זה בעצמה**.

---

## 7. קבצים חיצוניים (קלט אנושי, לא פלט צינור) — ללא שינוי

אלה כבר נקראים ב-`natural-viewer.html` ונשארים כפי שהם; מובאים כאן לשלמות ההפניה.

### 7.1 קובץ פעילות — `parseActivity`, שורה 320
עמודות (זיהוי לפי כותרת, לא מיקום): `LIBRARY`, `MEMBER`, `DBID`, `FUSER`,
`LOADED IN BP`, `LAST USED`.

### 7.2 כותרות — `headers.jsonl`, `applyHeaders`, שורה 623
`object_id`, `description`, `system_name`, `routine_name`, `author`, `object_type`,
`header_text`, `changes[]`, `change_count`, `last_change`, `first_change`, `authors[]`,
`tickets[]`, `calls_out_static[]`, `net_lines`, `total_lines`.

### 7.3 ניתוח LLM — `analysis.jsonl`, `parseAnalysis`+`joinData`, שורות 362,395
`object_id`, `lines`, `chars`, `tokens_in`, `source_path`, `status`, `analysis.{`
`sf_target, confidence, business_purpose, process_area, trigger,`
`inputs[], outputs[], data_entities[], key_rules[], obsolescence_quotes[], calls_out[]}`.

### 7.4 קטגוריזציה — CSV, `parseCategories`, שורה 751
עמודות (עברית או אנגלית, זיהוי לפי כותרת): `שם תוכנית`, `תיאור`, `מה זה עושה`,
`main_topic`, `sub_topic`.

### 7.5 `discovery-log.json` — פלט `app.js buildReport`, שורה 1050
לא נצרך על ידי שום תוכנית Python כקלט. **נצרך על ידי `validate.py` (שלב 1.3)**
כמד-אמת בלתי תלוי להשוואה מול `objects.jsonl`. שדות רלוונטיים להשוואה:
`report.file.sizeBytes`, `report.scan.*`, ספירות `byType`/`byLibrary` (בתוך המבנה
שנבנה ב-`buildReport`, לא מפורט כאן שדה-שדה — `validate.py` קורא את ה-JSON כמו שהוא).
