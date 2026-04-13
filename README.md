# F&O Accumulation Analyzer

Offline Python CLI tool that ingests NSE F&O derivative analytics CSV/XLSX sheets (two dates per symbol) and emits a hybrid **Markdown + JSON** analysis file containing multi-signal accumulation scores, Triple Convergence detection, and phase classification for every F&O stock in the input.

The output is designed to be pasted directly back into an LLM for re-analysis without the LLM needing to re-do any arithmetic — the Markdown carries human context while the embedded JSON block carries the full structured payload.

---
The CSV files are available on Stockedge.com >> FnO Section.
---
## 1. Project Layout

```
fno_accumulation_analyzer/
├── fno_analyzer.py        # single-file runnable script
├── pyproject.toml         # explicit [build-system] (Rule #22)
├── requirements.in        # top-level dependency source
├── requirements.txt       # hash-pinned lockfile (generated, Rule #20)
├── README.md              # this file
└── logs/
    └── fno_analyzer_YYYY-MM-DD.log   # structured JSON audit log
```

The script is deliberately a single file — this is a focused analysis tool, not a library. No package structure.

---

## 2. Setup

### 2.1 Virtual environment (Rule #12 — mandatory per-project isolation)

```bash
python -m venv .venv
source .venv/bin/activate          # Linux/macOS
# .venv\Scripts\activate.bat       # Windows
```

### 2.2 Generate the hash-pinned lockfile (Rule #20)

```bash
pip install --only-binary :all: pip-tools
pip-compile --generate-hashes --output-file=requirements.txt requirements.in
```

### 2.3 Install dependencies (Rule #19 — wheels only, no sdist execution)

```bash
pip install --only-binary :all: --require-hashes -r requirements.txt
```

If you need to install without the lockfile (development only, not recommended for reproducible runs):

```bash
pip install --only-binary :all: pandas openpyxl
```

---

## 3. Usage

```bash
python fno_analyzer.py
```

The script prompts once for the input path, then runs to completion:

```
======================================================================
  F&O Accumulation Analyzer
======================================================================
Enter path to Derivative Analytics CSV/XLSX: /path/to/Derivative_Analytics.csv

✓ Analyzed 213 symbols | Skipped: 0
✓ Output written: /path/to/fno_analysis_Derivative_Analytics_20260412_143210.md
```

The output file is always written to the **same folder as the input**. If a file with the identical timestamp already exists, an auto-suffix (`_v2`, `_v3`, …) is appended — the script never overwrites a prior analysis.

---

## 4. Input Schema (CSV or XLSX)

The loader uses **name-based column access**, so extra columns are ignored without error. Required columns (exact header names):

| Column | Type | Description |
|---|---|---|
| `Date` | `DD-MMM-YY` | Trading date (e.g., `09-Apr-26`) |
| `Symbol` | string | NSE F&O symbol |
| `Chg %` | float | **Pre-computed** price change % for that date |
| `Cumulative Future OI` | int | Total futures open interest across all expiries |
| `OI Chg %` | float | Day-level futures OI change % |
| `Volume (Times)` | float | Volume ratio vs average |
| `Delivery (Times)` | float | Delivery ratio vs average |
| `Cumulative Call OI` | int | Total call option OI |
| `Cumulative Put OI` | int | Total put option OI |
| `Put Call Ratio (PCR)` | float | PCR on that date |
| `PCR Change 1D` | float | **Pre-computed** absolute PCR delta (1-day) |

**Optional columns (safely ignored):** `Lot Size`, `Close`, or any other field you want to carry for your own reference.

**Row expectations:** Exactly **2 rows per symbol**, one row per date. Symbols with 0, 1, or 3+ rows are logged as warnings and excluded from scoring. Blank rows and rows with missing critical fields (`Symbol`, `Date`, `Chg %`, `PCR`, `Cumulative Future OI`) are dropped silently at load time — the entire file will never crash because of a trailing blank row or a malformed cell (Rule #16).

---

## 5. Scoring Methodology (Frozen Weights)

The scoring model is a direct port of the 02-Apr → 06-Apr auto sector analysis. All weights are frozen — do not change them without a conscious decision. Every signal contributes a numeric delta to the total score plus a human-readable bullet that is preserved in the JSON for re-analysis.

### 5.1 Signal 1 — PCR Level (on day 2)

| Condition | Score | Meaning |
|---|---:|---|
| PCR ≥ 1.0 | **+3** | Put writers dominant — strongest bullish positioning |
| PCR 0.85 – 0.999 | **+2** | Approaching bullish zone |
| PCR 0.75 – 0.849 | **+1** | Neutral-to-bullish |
| PCR < 0.75 | **0** | Bearish (call writers dominant) |

### 5.2 Signal 1b — PCR Trajectory (from `PCR Change 1D`)

| Condition | Score |
|---|---:|
| PCR Chg > +0.08 | **+2** (surging) |
| PCR Chg > +0.03 | **+1** (rising) |
| PCR Chg between −0.03 and +0.03 | **0** |
| PCR Chg < −0.03 | **0** (easing) |
| PCR Chg < −0.08 | **−1** (collapsing, bearish shift) |

### 5.3 Signal 2 — Futures OI × Price Interpretation

`fut_oi_chg_pct` is computed from the two `Cumulative Future OI` snapshots: `(oi_d2 − oi_d1) / oi_d1 × 100`.

| OI Direction | Price Direction | Score | Classification |
|---|---|---:|---|
| > +2% | > +0.5% | **+3** | ★ NEW LONGS (Bullish) |
| > 0 | > 0 | **+2** | Mild long build |
| > +2% | < −0.5% | **−1** | ⚠ NEW SHORTS (Bearish) |
| < −2% | > +0.5% | **+1** | ↑ Short covering (Weak Bull) |
| < −2% | < −0.5% | **−1** | ↓ Long liquidation (Bearish) |
| otherwise | otherwise | **0** | → Consolidation |

### 5.4 Signal 3 — Delivery Ratio

| Condition | Score | Meaning |
|---|---:|---|
| Average delivery ≥ 1.0x | **+3** | Institutional grade |
| Average delivery 0.70 – 0.99x | **+2** | Above-normal accumulation |
| Average delivery 0.50 – 0.69x | **+1** | Moderate |
| Average delivery < 0.50x | **0** | Speculative / low conviction |

Additional delivery bonuses:

- Delivery change > +0.3 between d1 and d2 → **+1** (accelerating)
- Peak delivery ≥ 1.5x on any day → **+1** (spike / heavy accumulation day)

### 5.5 Signal 4 — Volume

| Condition | Score |
|---|---:|
| Average volume ≥ 1.5x | **+1** |
| Average volume 1.0 – 1.49x | **+0.5** |
| Average volume < 1.0x | **0** |

### 5.6 Signal 5 — Options OI Growth & Lean

`call_g` and `put_g` are computed from cumulative call/put OI snapshots.

| Condition | Score |
|---|---:|
| Total options OI growth > +15% | **+1** |
| Total options OI growth +5% to +15% | **+0.5** |
| Put OI growing > 5% **and** faster than Call OI | **+0.5** (bullish lean) |
| Call OI growing > Put OI + 10% | **0** (hedging / bearish lean flag only) |

### 5.7 Signal 6 — Single-Day OI Surge on Day 2

| Condition | Score |
|---|---:|
| Day-2 `OI Chg %` > +5% | **+1** (aggressive entry) |
| Day-2 `OI Chg %` +2% to +5% | **+0.5** |

### 5.8 Tier Thresholds

| Total Score | Tier |
|---|---|
| ≥ 9.0 | **TIER 1 — STRONG ACCUMULATION** |
| 7.0 – 8.9 | **TIER 2 — ACCUMULATION** |
| 5.0 – 6.9 | **TIER 3 — BUILDING POSITION** |
| 3.0 – 4.9 | **WATCH** |
| < 3.0 | **NO SIGNAL** |

### 5.9 Triple Convergence Detection

A separate boolean check independent of the numeric score. A symbol is flagged `★★★ TRIPLE CONVERGENCE` when all three of the following are true simultaneously:

1. `price_chg_pct > 0`
2. `fut_oi_chg_pct > 0`
3. `pcr_chg > 0`

Triple Convergence is the strongest institutional signal — new longs being built **while** put writers simultaneously build a floor **and** price confirms.

### 5.10 Phase Classification

| Phase | Condition |
|---|---|
| `distribution` | Interpretation contains "NEW SHORTS" or "LONG LIQUIDATION" |
| `phase_2` | Interpretation contains "NEW LONGS" AND average delivery ≥ 1.0x (futures leverage stage) |
| `phase_1` | Average delivery ≥ 0.70x AND `fut_oi_chg_pct` > 0 (cash accumulation stage) |
| `pre_phase_1` | `pcr_chg` > +0.05 AND `fut_oi_chg_pct` ≤ 0 (derivatives leading cash) |
| `no_signal` | None of the above |

---

## 6. Output File Structure

### 6.1 Filename

```
fno_analysis_<input_stem>_<YYYYMMDD_HHMMSS>.md
```

### 6.2 Markdown sections

1. **Header** — input filename, SHA-256 of input, generation timestamp, dates analyzed, symbol counts
2. **Methodology** — one-paragraph recap for the human reader
3. **Summary Rankings** — Markdown table of all symbols sorted by score
4. **Phase Map** — symbols grouped by accumulation phase
5. **Warnings** — skipped symbols with reason (if any)
6. **Structured Data** — fenced `json` block with the full payload

### 6.3 JSON schema

```json
{
  "meta": {
    "input_file": "...",
    "input_sha256": "...",
    "generated_at": "2026-04-12T14:32:10",
    "dates": ["09-Apr-26", "10-Apr-26"],
    "symbols_analyzed": 213,
    "rows_skipped_blank": 0
  },
  "rankings": [
    {
      "rank": 1,
      "symbol": "NAM-INDIA",
      "score": 16.0,
      "tier": "TIER 1 — STRONG ACCUMULATION",
      "date_d1": "09-Apr-26",
      "date_d2": "10-Apr-26",
      "price_chg_pct": 5.13,
      "pcr_d1": 0.52,
      "pcr_d2": 1.42,
      "pcr_chg": 0.90,
      "fut_oi_chg_pct": 16.64,
      "avg_del": 1.20,
      "peak_del": 1.58,
      "del_chg": 0.76,
      "avg_vol": 1.20,
      "call_oi_growth_pct": 43.81,
      "put_oi_growth_pct": 291.33,
      "opt_oi_growth_pct": 128.64,
      "interpretation": "★ NEW LONGS (Bullish)",
      "triple_convergence": {
        "price_up": true,
        "oi_up": true,
        "pcr_up": true,
        "count": 3,
        "status": "★★★ TRIPLE CONVERGENCE"
      },
      "signals": [
        "PCR 1.42 ABOVE 1.0 — put writers dominant (STRONGEST)",
        "PCR surging +0.90 (1D)",
        "★ NEW LONGS: FutOI +16.6% + Price +5.1%",
        "..."
      ]
    }
  ],
  "triple_convergence": [ { "symbol": "...", "count": 3, "status": "..." } ],
  "phase_map": {
    "pre_phase_1": [...],
    "phase_1": [...],
    "phase_2": [...],
    "distribution": [...],
    "no_signal": [...]
  },
  "warnings": [ { "symbol": "...", "reason": "..." } ]
}
```

Every computed value is preserved in the JSON so a downstream reader (LLM or human) can re-challenge any conclusion without re-reading the source CSV.

---

## 7. Logging & Audit Trail (Rule #1)

Every run writes structured JSON log lines to `logs/fno_analyzer_<YYYY-MM-DD>.log`:

```json
{"ts":"2026-04-12 14:32:10,123","level":"INFO","msg":{"event":"run_start","input":"/path/to/file.csv"}}
{"ts":"2026-04-12 14:32:10,456","level":"INFO","msg":{"event":"input_loaded","rows_valid":426,"rows_skipped":0}}
{"ts":"2026-04-12 14:32:10,512","level":"INFO","msg":{"event":"pairing_complete","paired":213,"warned":0}}
{"ts":"2026-04-12 14:32:10,789","level":"INFO","msg":{"event":"run_complete","output":"/path/to/fno_analysis_...md","symbols":213}}
```

Each output file also includes a **SHA-256 hash of the input CSV** in its meta block, providing a tamper-evident linkage between the input and the analysis. If you ever need to verify that an analysis came from a specific source file, compare the hash in the output against a fresh `sha256sum` of the candidate input.

**Note:** this is not a full append-only hash-chained audit table (Rule #24) — that level of rigor is appropriate for production compliance systems, not for a local analysis tool. If you need it, say so and I'll add it.

---

## 8. Security Notes

This tool does not and will not:

- Make any network calls (fully offline; Rules #26, #28, #30 moot)
- Read any secrets from environment variables at build or install time (Rule #21)
- Hardcode any credentials, keys, or endpoints (Rule #10)
- Execute arbitrary code from the input CSV (name-based column access, `safe_float` only parses numerics, no `eval`)

The only file-system access is: (a) read the input CSV/XLSX, (b) write the output Markdown alongside it, (c) append to `logs/fno_analyzer_<date>.log`.

---

## 9. Operational Rules Honoured

| Rule | Where |
|---|---|
| #1 Structured logging & audit logs | `setup_logger()`, JSON-formatted file handler, SHA-256 of input in meta |
| #2 Secure data ingestion patterns | `safe_float`, `safe_int`, `safe_str` helpers; blank-row tolerance |
| #7 Test run after each code file | Validated against 213-symbol production dataset |
| #12 Virtual environment mandatory | Documented in Section 2 |
| #15 Retry/timeout/failure handling | N/A — no external calls. Local I/O uses fail-fast (correct for local tools) |
| #16 Robust error handling (IFERROR equivalent) | `safe_*` helpers + critical-field guards in `load_input` |
| #18 Enterprise file structure | `logs/` folder, clearly sectioned script, pyproject + requirements |
| #19 `--only-binary :all:` for pip | Documented in setup steps |
| #20 Hash-pinned dependencies | `requirements.txt` via `pip-compile --generate-hashes` |
| #21 No secrets in build/install env | No secrets used anywhere |
| #22 Explicit `[build-system]` in pyproject.toml | Present |
| #23 Prefer packaged wheels | pandas + openpyxl both publish wheels |
| #35 Treat complexity as cost | Single-file design; no package scaffolding |
| #36 Code for the next human | Clear variable names, 6 labeled sections, inline comments on scoring weights |

---

## 10. Known Limitations

1. **Exactly 2 rows per symbol required.** Multi-day windows (3+ dates) are out of scope. If you want a rolling window analyzer, that's a separate tool.
2. **Scoring weights are frozen.** Adjusting them requires editing `score_symbol()` directly. This is intentional — consistency across analyses matters more than per-run tuning.
3. **No GUI.** CLI only. If you want a web dashboard, that's a separate project.
4. **Date parsing tolerates only a few formats** (`DD-MMM-YY`, `DD-MMM-YYYY`, `YYYY-MM-DD`, `DD/MM/YYYY`). Exotic formats will sort to `datetime.min` and may break d1/d2 ordering silently.

---

## 11. Version

**v1.0.0** — 2026-04-12. Tested against a 213-symbol NSE F&O dataset (09-Apr-26 / 10-Apr-26). All 213 symbols scored correctly; zero warnings; spot-check math validated manually for NAM-INDIA (score 16.0) and 360ONE (score 12.0).
