# תוכנית פעולה — איחוד NATPROG Discovery + Natural Viewer

מסמך תכנון בלבד. שום דבר כאן עוד לא מומש.
נכתב אחרי קריאה בפועל של `app.js` (2,379 שורות), `natural-viewer.html` (2,181 שורות),
`index.html`, `style.css` ו-`README.md`.

---

## 1. מה קיים היום — מיפוי מאומת

### 1.1 `index.html` + `app.js` + `style.css` — "הסורק"

כלי דפדפן עצמאי, ללא תלויות, שקורא קובץ unload גולמי של SYSOBJH בזרימה
(`File.slice()` ב-8MB, ~30MB/s, ~101MB heap על קלט 250MB).

מה הוא **כן** יודע:

| יכולת | מיקום בקוד | מצב |
|---|---|---|
| פענוח קידוד (UTF-8, win-1255, ISO-8859-8, CP862, CP424, CP037) | `sniffEncoding` / `CP_TABLES` | עובד, נבדק |
| פרופיל unload גולמי (`*H**`/`*C**`/`*D01-04`/`*S**`/`-S**`) | `NAT_PROFILE` | עובד, נבדק על 770MB |
| פרופיל דוח job-log (132 עמודות קבועות) | `NAT_REPORT_PROFILE` | עובד, נבדק |
| זיהוי פרופיל אוטומטי | `sniffFileProfile` | עובד |
| מיפוי אותיות סוג (F/N/S/M/L/P/C/T/G/H/V) | `NAT_PROFILE.typeMap` | 9 confirmed, 2 inferred, 2 unknown (`7`,`5`) |
| הצלבת unload מול דוח | `buildCrossCheck` | עובד, 215/215 |
| ניתוח JCL (CMSYNIN + `EXEC PGM=`) | `parseJcl` / `analyzeJcl` | עובד, נבדק על 5 ג'obs |
| ניתוח COBOL/CICS (`CALL`/`LINK`/`XCTL`/`START`) | `parseCobol` / `analyzeCobol` | עובד, נבדק על 2 קבצים |
| ייצוא `discovery-log.json` + CSV | `buildReport` / `buildCsv` | עובד |

מה הוא **לא** יודע — וזה הלב של הבעיה:

* **הוא לא שומר את קוד המקור.** `feedSource()` סופר שורות ותווים ומחלץ תלויות,
  ואז זורק את השורה. אין דרך להוציא ממנו את גוף התוכנית.
* **מפות התלויות שלו גלובליות, לא לפי אובייקט.** ב-`feedSource` השורה
  `const d = p.dep` מפנה למפה אחת לכל הקובץ. התוצאה היא היסטוגרמה
  "כמה פעמים נקרא `X` בכל ה-800MB" — **לא** קשת `A → X`.
  אי אפשר לבנות מזה גרף קריאות, ולכן אי אפשר לבנות מזה reachability, ולכן
  אי אפשר לבנות מזה זיהוי קוד מת. זה חסם ארכיטקטוני, לא באג.
* אין רזולוציית ספרייה לתלויות (`CALLNAT 'X'` לא נפתר מול STEPLIB).
* המלאי מוגבל ל-400,000 אובייקטים (`CAPS.objectsRetained`) — מספיק ל-86K, אבל כדאי לדעת.

### 1.2 `natural-viewer.html` — "החוקר"

כלי דפדפן נפרד ועשיר בהרבה, שיושב מעל צינור חיצוני שלא נמצא ב-repo.
הוא **לא קורא את ה-unload בכלל** — הוא קורא 5 קבצים מוכנים + תיקיית מקור.

הקלטים המדויקים שהוא מצפה להם:

| # | קובץ | פורמט | שדות שהקוד באמת קורא |
|---|---|---|---|
| 1 | פעילות | TSV/CSV עם כותרת | `LIBRARY`, `MEMBER`, `DBID`, `FUSER`, `LOADED IN BP`, `LAST USED` |
| 2 | ניתוח LLM | JSONL | `object_id`, `lines`, `chars`, `tokens_in`, `source_path`, `status`, `analysis.{sf_target, confidence, business_purpose, process_area, trigger, inputs[], outputs[], data_entities[], key_rules[], obsolescence_quotes[], calls_out[]}` |
| 3 | כותרות | JSONL | `object_id`, `description`, `system_name`, `routine_name`, `author`, `object_type`, `header_text`, `changes[]`, `change_count`, `last_change`, `first_change`, `authors[]`, `tickets[]`, `calls_out_static[]`, `net_lines`, `total_lines` |
| 4 | `natmap.json` | JSON יחיד | `meta.schema_version`, `objects[]`, `ddm_access[]`, `calls[]`, `dup_pairs[]`, `obsolete[]` |
| 5 | קטגוריזציה | CSV | `שם תוכנית`, `תיאור`, `מה זה עושה`, `main_topic`, `sub_topic` |
| 6 | תיקיית מקור | webkitdirectory / `showDirectoryPicker` | שם קובץ בלי סיומת = `object_id` |

שדות `natmap.objects[]` שהקוד קורא (`applyNatmap`):
`object_id`, `triage`, `domain`, `primary_ddm`, `ui_class`, `object_type`, `max_depth`,
`if_count`, `decide_count`, `compute_count`, `code_lines`, `fan_in`, `fan_out`, `writes`,
`n_obsolete`, `unbalanced`, `self_redundancy`, `family`.
`ddm_access[]` = `{object_id, ddm, op}` (op ב-`STORE|UPDATE|DELETE` ⇒ כתיבה).
`calls[]` = `{kind, scope, target}` — בפועל **משמש רק ל-`external3gl()`**
(סינון `kind==='CALL3GL' && scope==='unresolved'`).
`dup_pairs[]` = `{a, b}`.
`obsolete[]` — **נקרא ב-`parseNatmap` ולא בשימוש בשום מקום**. שדה מת בקוד.

יכולות ייחודיות: פענוח עברית SI-960 (`HEB_TABLE`, `decodeComment`, `splitHebSegments`),
טריאז' ויזואלי, drill-down, IndexedDB persistence, צ'אט Gemini.

---

## 2. הפערים — למה זה לא "פשוט למזג"

### פער 1 (P0, חוסם הכל): אין מייצר ל-4 מתוך 5 קבצי הקלט
הצינור שמייצר `natmap.json`, `headers.jsonl`, `analysis.jsonl` וקובץ הפעילות
לא נמצא כאן, ובכל מקרה רץ רק על 3,000 מתוך 86,000 אובייקטים.
בלי צינור שרץ על כל ה-estate, ה-viewer מציג 3.5% מהמערכת.

### פער 2 (P0): החיבור עיוור לספריות
`joinData` מצליב על `keyOf(a.member)` מול `keyOf(r.object_id)` — **שם בלבד**.
`fileKey()` מוריד תיקייה וסיומת ומשאיר שם בלבד.
לפי ה-README יש 448 ספריות עם שכפול כבד (`RC`, `RC1`…`RC11`, `RCOLD`, `RCSIGAL`,
משפחות `ZM`/`ZGD`/`ASP`). המשמעות: `RC/GO0701P0` ו-`RCOLD/GO0701P0` מתמזגים
לרשומה אחת, והניתוח של אחד מיוחס לשני.
זה בדיוק הכשל שהסורק הפשוט **כן** יודע להימנע ממנו — `analyzeJcl` מצליב
library+name ותועד שהוא בחר נכון את `RC/GO0701P0` על פני העותקים ב-`GOCOPY`/`GOGO`.
**המפתח חייב להיות `LIBRARY/NAME` בכל הצינור.** זו שבירת תאימות מכוונת.

### פער 3: אין גרף קריאות אמיתי
ראה 1.1. חייב להיבנות מחדש ב-Python.

### פער 4: שני כלים, שתי מודלים של נתונים, אפס שיתוף קוד
`typeMap`, פענוח קידוד, פרסור CSV — משוכפלים או חסרים בצד השני.

### פער 5: פרטיות לא אחידה
הסורק מבטיח "הקובץ לא עוזב את המחשב"; ה-viewer שולח ל-Gemini.
במוצר ממוזג זו חייבת להיות הבחנה מפורשת ומוצגת, לא הערת שוליים.

---

## 3. ארכיטקטורת היעד

ההכרעה המרכזית: **Python מחזיק בכבדות, הדפדפן מחזיק בחקירה.**

```
                 ┌──────────────────────────────────────────┐
   unload 800MB →│  צינור Python (offline, פעם אחת)         │
   תיקיית JCL   →│                                          │
   תיקיית COBOL →│  natunload_split → natmap3 → reachability │
   דוח SYSOBJH  →│  natscan_headers → llm_batch              │
   קובץ פעילות  →│                                          │
                 └───────────────────┬──────────────────────┘
                                     │  JSON/JSONL + תיקיית מקור
                                     ▼
                 ┌──────────────────────────────────────────┐
                 │  natprog.html — כלי דפדפן אחד             │
                 │  טאב "קליטה"  = הסורק הקיים (QA/אימות)   │
                 │  טאב "חקירה"  = ה-viewer הקיים, מורחב    │
                 └──────────────────────────────────────────┘
```

**למה לשמור את הסורק בדפדפן ולא לזרוק אותו:** הוא כבר בנוי, מאומת מול 770MB,
ולא דורש Python מותקן על המכונה שיושבת ליד ה-mainframe. הוא הופך לכלי אימות
("האם ה-JSON שה-Python ייצר תואם למה שבקובץ?") ולמסלול חירום.

**מפתח אחיד לכל הצינור:** `LIBRARY/NAME` (uppercase). כל קובץ JSON יישא
גם `library` וגם `name` בנפרד, וגם `object_id` = `LIBRARY/NAME`.
תאימות לאחור: אם `object_id` לא מכיל `/`, ה-viewer יחזור להצלבה לפי שם בלבד
ויסמן את זה כ-`ambiguous_match` במקום להסתיר.

---

## 4. תוכניות ה-Python שצריך לכתוב

סדר תלויות. כל תוכנית = CLI עצמאי, קלט קבצים, פלט JSON/JSONL, דטרמיניסטית.

### 4.1 `natunload_split.py` — הבסיס (P0)
**קלט:** ה-unload הגולמי (800MB), פרמטר קידוד.
**פלט:**
* `source/<LIBRARY>/<NAME>.<ext>` — גוף המקור של כל אובייקט (86K קבצים).
* `objects.jsonl` — רשומה לאובייקט: `object_id`, `library`, `name`, `type`,
  `type_meaning`, `nat_version`, `saved`, `cataloged`, `size`, `lines`, `chars`,
  `os`, `tp`, `codepage`, `users[]`, `source_path`, `sha256_raw`, `sha256_norm`.

**הערות מימוש:**
* לוגיקת הפרסור מועתקת 1:1 מ-`app.js` (`NAT_PROFILE`, `fld`, `parseNatTs`,
  `feedProfile`) — היא כבר מאומתת מול 770MB. לא ממציאים מחדש.
* לטפל ב-`-S**` בדיוק כמו `*S**` (1.1% מהרשומות).
* `sha256_norm` = hash של המקור אחרי נרמול (trim ימני, איחוד רווחים,
  הסרת שורות ריקות). זה מה שיזהה עותקים כפולים בין ספריות — **הכלי המרכזי**
  לחיסכון בתקציב ה-LLM ולזיהוי ספריות-קברות.
* טבלאות EBCDIC — לייצר מ-`codecs` של Python, בדיוק כמו שה-README מתאר.
* 86K קבצים בתיקייה אחת זה בעייתי ב-Windows; לכן sharding לפי ספרייה.

**סיכון:** אם ה-unload מכיל אובייקטים בפורמט שהסורק מסמן `unknown_object_type_letter`
(אותיות `7`, `5`), התוכנית תשמור אותם עם `type_meaning: null` ולא תיפול.

### 4.2 `natmap3.py` — הגרף (P0)
**קלט:** `source/` + `objects.jsonl`.
**פלט:** `natmap.json` בסכימה שה-viewer כבר קורא, **בתוספת שדות חדשים**.

מה מחלצים לכל אובייקט (רגקסים קיימים ב-`RE` ב-`app.js`, יורחבו):
`CALLNAT '<x>'`, `FETCH [RETURN|REPEAT] '<x>'`, `PERFORM <x>`, `INCLUDE <x>`,
`<LOCAL|GLOBAL|PARAMETER|CONTEXT|INDEPENDENT> USING <x>`, `USING MAP '<x>'`,
`CALL '<x>'` (3GL), וגם — **חסר היום** — `READ/FIND/HISTOGRAM <view>` לגישת DDM,
`STORE/UPDATE/DELETE` לזיהוי כתיבה, `DEFINE SUBROUTINE` לזיהוי יעדי PERFORM פנימיים.

**רזולוציית ספרייה — הנקודה העדינה:**
Natural פותר `CALLNAT` לפי סדר: הספרייה הנוכחית → שרשרת STEPLIB → `SYSTEM`.
בלי קונפיגורציית ה-STEPLIB אי אפשר לפתור נכון. לכן כל קשת תישא:
```json
{"from":"RC/GO0701P0","kind":"CALLNAT","target":"HICNEWN3",
 "resolved_to":"RC/HICNEWN3","scope":"same_library",
 "candidates":["RC/HICNEWN3","RCOLD/HICNEWN3","RC3/HICNEWN3"]}
```
`scope` ∈ `same_library` | `steplib` | `system` | `ambiguous` | `unresolved` | `external_3gl`.
`ambiguous` = יותר ממועמד אחד ואין STEPLIB להכריע. **לא בוחרים שרירותית.**

**שדות נגזרים** (מה שה-viewer כבר מצפה): `fan_in`, `fan_out`, `code_lines`,
`if_count`, `decide_count`, `compute_count`, `max_depth`, `unbalanced`,
`self_redundancy`, `family` (לפי `sha256_norm`), `primary_ddm`, `writes`, `ui_class`.
`triage` — **לא כאן**. מיוצר ב-4.6, כי הוא דורש את ה-JCL ואת הפעילות.

**ביצועים:** 86K קבצים, single-pass, `multiprocessing.Pool`. יעד: < 10 דקות.

### 4.3 `natscan_headers.py` — קיים אצלך (P1)
צריך רק להרחיב ל-`LIBRARY/NAME` ולהריץ על כל 86K.
לוודא שהפלט עדיין תואם ל-`applyHeaders`.

### 4.4 `jclmap.py` — נקודות הכניסה (P0)
**קלט:** תיקיית JCL.
**פלט:** `jcl.json` — ג'ובים, steps, ולכל אחד `entry_points[]` = `{library, program, jcl_file, step, schedule?}`.
הלוגיקה קיימת ומאומתת ב-`parseJcl` — מעתיקים.
**להוסיף:** פרסור `STEPLIB`/`//NATLIB DD` אם קיים ב-JCL — זה **המקור הכי טוב**
לשרשרת ה-STEPLIB שחסרה ב-4.2.

### 4.5 `cobolmap.py` — הגשר ל-3GL (P1)
**קלט:** תיקיית COBOL/CICS.
**פלט:** `cobol.json` — `PROGRAM-ID`, דגל CICS, קשתות `CALL`/`LINK`/`XCTL`/`START`.
הלוגיקה קיימת ב-`parseCobol`.
**הערך המוסף:** לגשר בין `CALL '<x>'` של Natural ל-`PROGRAM-ID` של COBOL.
היום `external3gl()` רק מונה יעדים לא פתורים; אחרי הגישור הם ייפתרו.
זה גם מייצר נקודות כניסה חדשות: כל תוכנית CICS = entry point online.

### 4.6 `reachability.py` — הלב של זיהוי קוד מת (P0)
פרק 5 מפרט. **קלט:** כל הפלטים למעלה + קובץ פעילות + דוח SYSOBJH.
**פלט:** `triage.json` — לכל אובייקט `triage`, `triage_confidence`, `triage_evidence[]`.

### 4.7 `llm_batch.py` — קריאות Gemini (P1)
פרק 6 מפרט. **פלט:** `analysis.jsonl` בסכימה שה-viewer קורא.

### 4.8 `validate.py` — בקרת איכות (P2)
משווה את ספירות ה-Python מול `discovery-log.json` של הסורק בדפדפן.
אם `objectsSeen` לא זהה — משהו שבור. זה מה שהופך את הכפילות בין הכלים ליתרון.

---

## 5. זיהוי קוד מת — האסטרטגיה

**העיקרון:** לא מנחשים "נראה ישן". בונים **סגור הישֽיגות (reachability closure)**
מנקודות כניסה אמיתיות, ואז מדרגים כל אובייקט לפי כמה ראיות עומדות מאחורי המסקנה.

### שכבה 0 — נקודות כניסה (ground truth)
| מקור | מה זה נותן | חוזק |
|---|---|---|
| JCL (`LOGON <lib>` + program ב-CMSYNIN) | כל תוכנית batch שג'וב מריץ | **חזק מאוד** — זו הרצה בפועל |
| JCL (`EXEC PGM=` שאינו utility) | תוכניות 3GL ישירות | חזק |
| COBOL/CICS (`EXEC CICS START TRANSID`, תוכניות CICS) | נקודות כניסה online | חזק |
| קובץ פעילות (`LAST USED` מה-buffer pool) | מה שבאמת רץ בחלון הדגימה | **חזק, אך חלקי** |
| דוח SYSOBJH, עמודת `S/C` | האם האובייקט מקוטלג בכלל | בינוני — צריך אימות |
| שכבת Java/חיצונית | **לא ידוע — חור בכיסוי** | ⚠ ראה שאלה פתוחה |

### שכבה 1 — סגור על הגרף
BFS מכל נקודות הכניסה על קשתות `natmap3` (`CALLNAT`/`FETCH`/`INCLUDE`/`USING`/`MAP`/`CALL`).
כל אובייקט מקבל `reachable: true/false` + `distance_from_entry`.

**זהירות:** `PERFORM` ברובו המכריע פונה ל-`DEFINE SUBROUTINE` פנימי, לא לאובייקט חיצוני.
ה-README כבר תיעד את זה (295,907 מתוך 296,015 "לא פתורים" — וזה תקין).
לכן `PERFORM` ייפתר קודם מול תוויות פנימיות של אותו אובייקט, ורק מה שנשאר
ייחשב קשת חיצונית מול אובייקט מסוג `S`.

### שכבה 2 — דירוג
| קטגוריה | תנאי | מה עושים |
|---|---|---|
| `live` | reachable, או מופיע בפעילות | לא נוגעים |
| `dead_shadow_copy` | `sha256_norm` זהה לאובייקט `live`, בספרייה שאינה שלו | **המחיקה הבטוחה ביותר** |
| `dead_unreachable` | לא reachable, אין פעילות, אין entry | מועמד למחיקה |
| `dead_stale_library` | כל הספרייה ללא אף entry point ו-`saved` ישן | מחיקה ברמת ספרייה — הכי משתלם |
| `source_only` | יש SRC, אין CATALOG (מהדוח) | ⚠ לאמת — Natural יכול להריץ מקור בקונפיגורציות מסוימות |
| `external_entry_suspect` | לא reachable, **אבל** שונה לאחרונה או בספרייה חיה | **לא למחוק** — כנראה נקרא מ-Java |
| `unknown` | חסרים נתונים | להשלים נתונים |

**מדוע אני מאמין שזה יעבוד כאן דווקא:** ה-README מתעד שכפול כבד
(`RC`, `RC1`…`RC11`, `RCOLD`, `RCSIGAL`). אם דפוס ה-copy-before-you-change
אמיתי, `dead_shadow_copy` + `dead_stale_library` לבדם יסבירו נתח גדול מ-86K,
**בלי צורך לקרוא שורת קוד אחת ובלי אף קריאת LLM** — רק hash והשוואת ספריות.
זו ההשערה המרכזית של התוכנית, והצעד הראשון שכדאי לבדוק כי הוא זול.

### מה שהשיטה **לא** יכולה להבטיח
* אובייקט שנקרא רק מ-Java/REST/EntireX ייראה מת. זה למה `external_entry_suspect`
  קיים כקטגוריה נפרדת ולא מקוטלג כמת. ה-viewer כבר מנסח את זה נכון בטקסט שלו.
* קריאה דינמית (`CALLNAT` עם שם במשתנה) לא נתפסת ברגקס.
  **חובה למדוד כמה כאלה יש** — אם זה אחוז גבוה, כל השיטה נחלשת ויש לומר זאת.
* בלי STEPLIB, קשתות `ambiguous` מנפחות reachability (שמרני — עדיף מלמחוק בטעות).

---

## 6. אסטרטגיית ה-LLM — מ-3,000 ל-86,000 בלי לשלם על 86,000

סדר הפעולות, מהזול ליקר:

1. **דדופליקציה לפי `sha256_norm`.** שולחים נציג אחד לכל משפחת תוכן,
   ומפיצים את התוצאה לכל האחים. אם השכפול באמת כבד — זה לבדו חותך את הרוב.
2. **לא שולחים `dead_*` בכלל.** אין טעם לנתח קוד שהולך להימחק.
3. **דירוג עדיפות** למה שנשאר: `fan_in × code_lines × recency × (reachable)`.
   קודם הליבה, אחר כך השוליים.
4. **סוגי אובייקט שלא צריכים LLM:** `M` (מסך), `L`/`P`/`C` (data areas), `T` (טקסט)
   ניתנים לתיאור אוטומטי מהמבנה. רק `F`/`N`/`S`/`G`/`H` באמת דורשים ניתוח סמנטי.
5. **מיפוי מה שכבר יש.** 3,000 הרשומות הקיימות — לוודא איזה `object_id` הן
   מכסות ולא לשלם עליהן שוב. (בכפוף לפער 2: ייתכן שהן ממופות לשם בלבד
   ויידרש מיפוי מחדש ל-`LIBRARY/NAME`.)

**הערה על מודל ה-Gemini:** הקוד מקובע ל-`gemini-3.7-flash`
(`natural-viewer.html:1800`). חיפוש שעשיתי מצא `gemini-3.5-flash`
ו-`gemini-3.1-flash-lite` כדגמי Flash עדכניים, ולא מצא `gemini-3.7-flash`.
**לא קובע שהוא שגוי** — צריך לאמת מול `GET /v1beta/models` עם המפתח האמיתי
לפני שמריצים אצווה גדולה. `llm_batch.py` יקרא את רשימת המודלים בתחילת ריצה
ויכשל מוקדם עם הודעה ברורה במקום לצבור 86K שגיאות 404.

---

## 7. חבילות עבודה — מה אפשר לעשות במקביל

```
WP-A (חוסם):  natunload_split.py            ← חייב להיות ראשון
                     │
      ┌──────────────┼──────────────┬─────────────────┐
      ▼              ▼              ▼                 ▼
WP-B: natmap3   WP-C: jclmap   WP-D: cobolmap   WP-E: headers
      │              │              │                 │
      └──────────────┴──────┬───────┴─────────────────┘
                            ▼
                 WP-F: reachability.py  ← זיהוי קוד מת
                            │
                            ▼
                 WP-G: llm_batch.py
```

במקביל וללא תלות ב-WP-A:
* **WP-H:** מיזוג ה-HTML — שינוי מפתח ל-`LIBRARY/NAME`, איחוד ה-`typeMap`,
  איחוד פענוח הקידוד, טאב "קליטה" מול טאב "חקירה".
* **WP-I:** מסמך סכימה (`SCHEMAS.md`) — חוזה אחד לכל קובץ JSON, שגם ה-Python
  וגם ה-JS מאמתים מולו. בלי זה השניים יתפצלו שוב.

**סדר מומלץ להתחיל:** WP-A ← ואז מיד מדידת `sha256_norm` בלבד.
זו בדיקה של יום אחד שתגיד אם השערת השכפול נכונה, ולפי התשובה
משתנה סדר העדיפויות של כל השאר.

---

## 8. שאלות פתוחות — צריך תשובה ממך

1. **שכבת Java/חיצונית** — יש מערכת שקוראת ל-Natural מבחוץ (EntireX, Broker, REST)?
   אם כן, יש דרך להוציא ממנה רשימת תוכניות נקראות? זה החור הגדול ביותר בכיסוי.
2. **STEPLIB** — יש קונפיגורציה (NATPARM/JCL) עם שרשרת ה-STEPLIB?
   בלעדיה חלק מהקשתות יישארו `ambiguous`.
3. **"PCL"** — כתבת JCL, CICS ו-"PCL". התכוונת ל-COBOL, או שיש גם PL/I ב-estate?
   אם יש PL/I צריך parser נוסף.
4. **קובץ הפעילות** — מאיזה חלון זמן? יום? חודש? שנה?
   זה קובע כמה משקל לתת ל-"לא רץ ⇒ מת".
5. **3,000 הניתוחים הקיימים** — ממופים לפי שם בלבד או `LIBRARY/NAME`?
   קובע אם אפשר למחזר אותם as-is.
6. **מחיקה בפועל** — היעד הוא רשימת מועמדים לאישור אנושי, או שיש כוונה
   למחוק אוטומטית? זה משנה כמה שמרני להיות בדירוג.
7. **הרצת Python** — על איזו מכונה? יש גישה ל-800MB שם? יש הגבלת רשת
   ליציאה ל-Gemini?

---

## 9. מה מכוון נשאר בחוץ

* לא בונים DB (SQLite/Postgres). JSON על הדיסק מספיק ל-86K ושומר על
  העיקרון שהכל עובד מ-`file://`.
* לא בונים שרת. הדפדפן נשאר קורא-קבצים.
* לא מוחקים את הסורק בדפדפן — הוא הופך לכלי אימות (4.8).
* לא נוגעים בפענוח העברית SI-960 — הוא עובד ומאומת.
