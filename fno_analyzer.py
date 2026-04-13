#!/usr/bin/env python3
"""
F&O Accumulation Analyzer
=========================
Ingests a derivative analytics CSV/XLSX (2 dates per symbol), computes the
multi-signal accumulation scoring model, and emits a hybrid Markdown+JSON
artifact for downstream analysis.

Usage:  python fno_analyzer.py
        (prompts for input path, writes output alongside input)

Scoring weights are frozen per the 02-Apr -> 06-Apr auto sector analysis.
All per-symbol signal breakdowns are preserved in the JSON for re-analysis.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — LOGGING SETUP (Rule #1: structured logging)
# ─────────────────────────────────────────────────────────────────────────────

def setup_logger(script_dir: Path) -> logging.Logger:
    logs_dir = script_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_path = logs_dir / f"fno_analyzer_{datetime.now().strftime('%Y-%m-%d')}.log"
    logger = logging.getLogger("fno_analyzer")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            '{"ts":"%(asctime)s","level":"%(levelname)s","msg":%(message)s}'
        ))
        logger.addHandler(fh)
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(ch)
    return logger


def log_event(logger: logging.Logger, level: str, event: str, **kv: Any) -> None:
    payload = {"event": event, **kv}
    getattr(logger, level)(json.dumps(payload, default=str))


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — SAFE VALUE EXTRACTION (Rule #16: IFERROR equivalent)
# ─────────────────────────────────────────────────────────────────────────────

def safe_float(val: Any) -> float | None:
    """Return float or None for blanks/NaN/malformed. Never raises."""
    try:
        if val is None:
            return None
        if isinstance(val, float) and math.isnan(val):
            return None
        s = str(val).strip()
        if s == "" or s.lower() in ("nan", "none", "null", "-"):
            return None
        return float(s.replace(",", "").replace("%", "").replace("+", ""))
    except (ValueError, TypeError):
        return None


def safe_int(val: Any) -> int | None:
    f = safe_float(val)
    return int(f) if f is not None else None


def safe_str(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, float) and math.isnan(val):
        return None
    s = str(val).strip()
    return s if s else None


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — INPUT LOADER (CSV + XLSX, blank-row tolerant)
# ─────────────────────────────────────────────────────────────────────────────

EXPECTED_COLS = {
    "date": "Date",
    "symbol": "Symbol",
    "chg_pct": "Chg %",
    "fut_oi": "Cumulative Future OI",
    "oi_chg": "OI Chg %",
    "volume": "Volume (Times)",
    "delivery": "Delivery (Times)",
    "call_oi": "Cumulative Call OI",
    "put_oi": "Cumulative Put OI",
    "pcr": "Put Call Ratio (PCR)",
    "pcr_chg": "PCR Change 1D",
}


def load_input(path: Path, logger: logging.Logger) -> list[dict]:
    """Load CSV or XLSX into list of normalized row dicts. Blank/invalid rows dropped."""
    try:
        import pandas as pd
    except ImportError:
        print("ERROR: pandas required. Install with: pip install --only-binary :all: pandas openpyxl")
        sys.exit(1)

    ext = path.suffix.lower()
    if ext == ".csv":
        df = pd.read_csv(path, encoding="utf-8-sig")
    elif ext in (".xlsx", ".xls"):
        df = pd.read_excel(path)
    else:
        raise ValueError(f"Unsupported file type: {ext}")

    df.columns = [str(c).strip().lstrip("\ufeff") for c in df.columns]
    missing = [v for v in EXPECTED_COLS.values() if v not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    rows, skipped = [], []
    for idx, r in df.iterrows():
        sym = safe_str(r.get("Symbol"))
        dt = safe_str(r.get("Date"))
        chg = safe_float(r.get("Chg %"))
        pcr = safe_float(r.get("Put Call Ratio (PCR)"))
        fut = safe_float(r.get("Cumulative Future OI"))
        if not sym or not dt or chg is None or pcr is None or fut is None:
            skipped.append({"row": int(idx) + 2, "reason": "missing critical field"})
            continue
        rows.append({
            "date": dt, "symbol": sym,
            "chg_pct": chg,
            "fut_oi": safe_float(r.get("Cumulative Future OI")) or 0.0,
            "oi_chg": safe_float(r.get("OI Chg %")) or 0.0,
            "volume": safe_float(r.get("Volume (Times)")) or 0.0,
            "delivery": safe_float(r.get("Delivery (Times)")) or 0.0,
            "call_oi": safe_float(r.get("Cumulative Call OI")) or 0.0,
            "put_oi": safe_float(r.get("Cumulative Put OI")) or 0.0,
            "pcr": pcr,
            "pcr_chg": safe_float(r.get("PCR Change 1D")) or 0.0,
        })
    log_event(logger, "info", "input_loaded", rows_valid=len(rows), rows_skipped=len(skipped))
    return rows, skipped


def pair_by_symbol(rows: list[dict], logger: logging.Logger) -> tuple[dict, list]:
    """Group rows by symbol; require exactly 2 dates. Returns (pairs, warnings)."""
    by_sym: dict[str, list[dict]] = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(r)

    # Parse dates for ordering (older -> newer)
    def parse_dt(s: str) -> datetime:
        for fmt in ("%d-%b-%y", "%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return datetime.min

    pairs, warnings = {}, []
    for sym, recs in by_sym.items():
        if len(recs) != 2:
            warnings.append({"symbol": sym, "reason": f"expected 2 rows, got {len(recs)}"})
            continue
        recs.sort(key=lambda x: parse_dt(x["date"]))
        pairs[sym] = {"d1": recs[0], "d2": recs[1]}
    log_event(logger, "info", "pairing_complete", paired=len(pairs), warned=len(warnings))
    return pairs, warnings


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — SCORING MODEL (frozen weights from 02-Apr -> 06-Apr analysis)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SymbolResult:
    symbol: str
    score: float
    tier: str
    rank: int
    date_d1: str
    date_d2: str
    price_chg_pct: float
    pcr_d1: float
    pcr_d2: float
    pcr_chg: float
    fut_oi_chg_pct: float
    avg_del: float
    peak_del: float
    del_chg: float
    avg_vol: float
    call_oi_growth_pct: float
    put_oi_growth_pct: float
    opt_oi_growth_pct: float
    interpretation: str
    triple_convergence: dict
    signals: list[str] = field(default_factory=list)


def score_symbol(sym: str, pair: dict) -> SymbolResult:
    d1, d2 = pair["d1"], pair["d2"]
    score, signals = 0.0, []

    # --- Signal 1: PCR Level ---
    pcr_d1, pcr_d2 = d1["pcr"], d2["pcr"]
    if pcr_d2 >= 1.0:
        score += 3; signals.append(f"PCR {pcr_d2:.2f} ABOVE 1.0 — put writers dominant (STRONGEST)")
    elif pcr_d2 >= 0.85:
        score += 2; signals.append(f"PCR {pcr_d2:.2f} — approaching bullish zone")
    elif pcr_d2 >= 0.75:
        score += 1; signals.append(f"PCR {pcr_d2:.2f} — neutral-to-bullish")
    else:
        signals.append(f"PCR {pcr_d2:.2f} — bearish (call writers dominant)")

    # --- Signal 1b: PCR Trajectory (uses pre-computed PCR Change 1D from d2) ---
    pcr_change = d2["pcr_chg"]
    if pcr_change > 0.08:
        score += 2; signals.append(f"PCR surging +{pcr_change:.2f} (1D)")
    elif pcr_change > 0.03:
        score += 1; signals.append(f"PCR rising +{pcr_change:.2f} (1D)")
    elif pcr_change < -0.08:
        score -= 1; signals.append(f"PCR collapsing {pcr_change:+.2f} — bearish shift")
    elif pcr_change < -0.03:
        signals.append(f"PCR easing {pcr_change:+.2f}")

    # --- Signal 2: Futures OI vs Price ---
    price_chg = d2["chg_pct"]
    fut_oi_chg = ((d2["fut_oi"] - d1["fut_oi"]) / d1["fut_oi"] * 100.0) if d1["fut_oi"] else 0.0
    if fut_oi_chg > 2 and price_chg > 0.5:
        score += 3; interp = "★ NEW LONGS (Bullish)"
        signals.append(f"★ NEW LONGS: FutOI {fut_oi_chg:+.1f}% + Price {price_chg:+.1f}%")
    elif fut_oi_chg > 0 and price_chg > 0:
        score += 2; interp = "Mild long build"
        signals.append(f"Mild long build: FutOI {fut_oi_chg:+.1f}% + Price {price_chg:+.1f}%")
    elif fut_oi_chg > 2 and price_chg < -0.5:
        score -= 1; interp = "⚠ NEW SHORTS (Bearish)"
        signals.append(f"⚠ NEW SHORTS: FutOI {fut_oi_chg:+.1f}% + Price {price_chg:+.1f}%")
    elif fut_oi_chg < -2 and price_chg > 0.5:
        score += 1; interp = "↑ SHORT COVERING (Weak Bull)"
        signals.append(f"Short covering: FutOI {fut_oi_chg:+.1f}% + Price {price_chg:+.1f}%")
    elif fut_oi_chg < -2 and price_chg < -0.5:
        score -= 1; interp = "↓ LONG LIQUIDATION (Bearish)"
        signals.append(f"Long liquidation: FutOI {fut_oi_chg:+.1f}% + Price {price_chg:+.1f}%")
    else:
        interp = "→ CONSOLIDATION"
        signals.append(f"Consolidation: FutOI {fut_oi_chg:+.1f}%, Price {price_chg:+.1f}%")

    # --- Signal 3: Delivery ---
    avg_del = (d1["delivery"] + d2["delivery"]) / 2.0
    peak_del = max(d1["delivery"], d2["delivery"])
    del_chg = d2["delivery"] - d1["delivery"]
    if avg_del >= 1.0:
        score += 3; signals.append(f"Delivery AVG {avg_del:.2f}x — INSTITUTIONAL GRADE")
    elif avg_del >= 0.70:
        score += 2; signals.append(f"Delivery AVG {avg_del:.2f}x — above-normal accumulation")
    elif avg_del >= 0.50:
        score += 1; signals.append(f"Delivery AVG {avg_del:.2f}x — moderate")
    else:
        signals.append(f"Delivery AVG {avg_del:.2f}x — speculative/low conviction")

    if del_chg > 0.3:
        score += 1; signals.append(f"Delivery ACCELERATING: {d1['delivery']:.2f}x → {d2['delivery']:.2f}x (+{del_chg:.2f})")
    elif del_chg < -0.3:
        signals.append(f"Delivery decelerating: {d1['delivery']:.2f}x → {d2['delivery']:.2f}x ({del_chg:+.2f})")
    if peak_del >= 1.5:
        score += 1; signals.append(f"Delivery spike: {peak_del:.2f}x peak — heavy accumulation day")

    # --- Signal 4: Volume ---
    avg_vol = (d1["volume"] + d2["volume"]) / 2.0
    if avg_vol >= 1.5:
        score += 1; signals.append(f"High volume: AVG {avg_vol:.2f}x")
    elif avg_vol >= 1.0:
        score += 0.5; signals.append(f"Normal volume: AVG {avg_vol:.2f}x")

    # --- Signal 5: Options OI growth ---
    call_g = ((d2["call_oi"] - d1["call_oi"]) / d1["call_oi"] * 100.0) if d1["call_oi"] else 0.0
    put_g  = ((d2["put_oi"]  - d1["put_oi"])  / d1["put_oi"]  * 100.0) if d1["put_oi"] else 0.0
    tot1, tot2 = d1["call_oi"] + d1["put_oi"], d2["call_oi"] + d2["put_oi"]
    opt_g = ((tot2 - tot1) / tot1 * 100.0) if tot1 else 0.0
    if opt_g > 15:
        score += 1; signals.append(f"Options OI surged {opt_g:+.0f}% — heavy positioning")
    elif opt_g > 5:
        score += 0.5; signals.append(f"Options OI up {opt_g:+.0f}%")

    if put_g > call_g and put_g > 5:
        score += 0.5; signals.append(f"Put OI growing faster ({put_g:+.0f}%) than Call OI ({call_g:+.0f}%) — bullish lean")
    elif call_g > put_g + 10:
        signals.append(f"Call OI growing faster ({call_g:+.0f}%) than Put OI ({put_g:+.0f}%) — hedging/bearish lean")

    # --- Signal 6: Single-day OI surge on d2 ---
    if d2["oi_chg"] > 5:
        score += 1; signals.append(f"{d2['date']} single-day OI surge: +{d2['oi_chg']:.1f}% — aggressive entry")
    elif d2["oi_chg"] > 2:
        score += 0.5; signals.append(f"{d2['date']} OI build: +{d2['oi_chg']:.1f}%")

    # --- Triple Convergence ---
    tc = {
        "price_up": price_chg > 0,
        "oi_up": fut_oi_chg > 0,
        "pcr_up": pcr_change > 0,
    }
    tc["count"] = sum([tc["price_up"], tc["oi_up"], tc["pcr_up"]])
    tc["status"] = (
        "★★★ TRIPLE CONVERGENCE" if tc["count"] == 3 else
        f"★★ DOUBLE" if tc["count"] == 2 else
        f"★ SINGLE" if tc["count"] == 1 else "○ NONE"
    )

    # --- Tier ---
    if score >= 9:   tier = "TIER 1 — STRONG ACCUMULATION"
    elif score >= 7: tier = "TIER 2 — ACCUMULATION"
    elif score >= 5: tier = "TIER 3 — BUILDING POSITION"
    elif score >= 3: tier = "WATCH"
    else:            tier = "NO SIGNAL"

    return SymbolResult(
        symbol=sym, score=round(score, 2), tier=tier, rank=0,
        date_d1=d1["date"], date_d2=d2["date"],
        price_chg_pct=price_chg, pcr_d1=pcr_d1, pcr_d2=pcr_d2, pcr_chg=pcr_change,
        fut_oi_chg_pct=round(fut_oi_chg, 2),
        avg_del=round(avg_del, 3), peak_del=peak_del, del_chg=round(del_chg, 3),
        avg_vol=round(avg_vol, 3),
        call_oi_growth_pct=round(call_g, 2), put_oi_growth_pct=round(put_g, 2),
        opt_oi_growth_pct=round(opt_g, 2),
        interpretation=interp, triple_convergence=tc, signals=signals,
    )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — OUTPUT EMITTER (Hybrid Markdown + JSON)
# ─────────────────────────────────────────────────────────────────────────────

def resolve_output_path(input_path: Path) -> Path:
    """Auto-suffix to never overwrite prior runs (Rule: non-destructive)."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = input_path.parent / f"fno_analysis_{input_path.stem}_{ts}.md"
    if not base.exists():
        return base
    n = 2
    while True:
        candidate = input_path.parent / f"fno_analysis_{input_path.stem}_{ts}_v{n}.md"
        if not candidate.exists():
            return candidate
        n += 1


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def emit_output(results: list[SymbolResult], meta: dict, warnings: list, out_path: Path) -> None:
    results.sort(key=lambda r: r.score, reverse=True)
    for i, r in enumerate(results, 1):
        r.rank = i

    # Phase map
    phase_map = {"pre_phase_1": [], "phase_1": [], "phase_2": [], "distribution": [], "no_signal": []}
    for r in results:
        if "NEW SHORTS" in r.interpretation or "LONG LIQUIDATION" in r.interpretation:
            phase_map["distribution"].append(r.symbol)
        elif "NEW LONGS" in r.interpretation and r.avg_del >= 1.0:
            phase_map["phase_2"].append(r.symbol)
        elif r.avg_del >= 0.70 and r.fut_oi_chg_pct > 0:
            phase_map["phase_1"].append(r.symbol)
        elif r.pcr_chg > 0.05 and r.fut_oi_chg_pct <= 0:
            phase_map["pre_phase_1"].append(r.symbol)
        else:
            phase_map["no_signal"].append(r.symbol)

    payload = {
        "meta": meta,
        "rankings": [asdict(r) for r in results],
        "triple_convergence": [
            {"symbol": r.symbol, **r.triple_convergence} for r in results
        ],
        "phase_map": phase_map,
        "warnings": warnings,
    }

    md = []
    md.append("# F&O Accumulation Analysis")
    md.append("")
    md.append(f"- **Input:** `{meta['input_file']}`")
    md.append(f"- **SHA-256:** `{meta['input_sha256']}`")
    md.append(f"- **Generated:** {meta['generated_at']}")
    md.append(f"- **Dates analyzed:** {meta['dates']}")
    md.append(f"- **Symbols analyzed:** {meta['symbols_analyzed']} | **Skipped:** {len(warnings)}")
    md.append("")
    md.append("## Methodology")
    md.append("")
    md.append("Multi-signal scoring: PCR level & trajectory, Futures OI × Price interpretation, "
              "Delivery grade & acceleration, Volume, Options OI growth, Put vs Call lean, "
              "single-day OI surge. Triple Convergence = Price↑ + OI↑ + PCR↑ simultaneously. "
              "All per-symbol signals preserved in the JSON block below for re-analysis.")
    md.append("")
    md.append("## Summary Rankings")
    md.append("")
    md.append("| Rank | Symbol | Score | Tier | Price% | PCR(d1→d2) | PCRΔ | AvgDel | FutOI% | Interpretation |")
    md.append("|---:|:---|---:|:---|---:|:---:|---:|---:|---:|:---|")
    for r in results:
        md.append(
            f"| {r.rank} | {r.symbol} | {r.score:.1f} | {r.tier} | "
            f"{r.price_chg_pct:+.2f}% | {r.pcr_d1:.2f}→{r.pcr_d2:.2f} | {r.pcr_chg:+.2f} | "
            f"{r.avg_del:.2f}x | {r.fut_oi_chg_pct:+.1f}% | {r.interpretation} |"
        )
    md.append("")
    md.append("## Phase Map")
    md.append("")
    for phase, syms in phase_map.items():
        md.append(f"- **{phase}**: {', '.join(syms) if syms else '(none)'}")
    md.append("")
    if warnings:
        md.append("## Warnings (Skipped Symbols)")
        md.append("")
        for w in warnings:
            md.append(f"- `{w.get('symbol','?')}`: {w.get('reason','?')}")
        md.append("")
    md.append("## Structured Data (re-analysis payload)")
    md.append("")
    md.append("```json")
    md.append(json.dumps(payload, indent=2, default=str))
    md.append("```")
    md.append("")

    out_path.write_text("\n".join(md), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    script_dir = Path(__file__).resolve().parent
    logger = setup_logger(script_dir)

    print("=" * 70)
    print("  F&O Accumulation Analyzer")
    print("=" * 70)
    raw = input("Enter path to Derivative Analytics CSV/XLSX: ").strip().strip('"').strip("'")
    if not raw:
        print("ERROR: No path provided.")
        return 1
    input_path = Path(os.path.expanduser(raw)).resolve()
    if not input_path.exists() or not input_path.is_file():
        print(f"ERROR: File not found: {input_path}")
        return 1

    try:
        log_event(logger, "info", "run_start", input=str(input_path))
        rows, skipped_rows = load_input(input_path, logger)
        pairs, pair_warnings = pair_by_symbol(rows, logger)

        results = [score_symbol(sym, pair) for sym, pair in pairs.items()]
        dates_set = sorted({r["date"] for r in rows})

        meta = {
            "input_file": input_path.name,
            "input_sha256": sha256_file(input_path),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "dates": dates_set,
            "symbols_analyzed": len(results),
            "rows_skipped_blank": len(skipped_rows),
        }
        warnings = skipped_rows + pair_warnings

        out_path = resolve_output_path(input_path)
        emit_output(results, meta, warnings, out_path)

        log_event(logger, "info", "run_complete", output=str(out_path), symbols=len(results))
        print(f"\n✓ Analyzed {len(results)} symbols | Skipped: {len(warnings)}")
        print(f"✓ Output written: {out_path}")
        return 0
    except Exception as e:
        log_event(logger, "error", "run_failed", error=str(e), error_type=type(e).__name__)
        print(f"ERROR: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
