"""Analysis / export layer.

Turns the dashboard's raw activity data into a clean, machine-readable
dataset designed for longitudinal analysis (6-12 months of runs):

  - analysis.json   : source of truth, one normalized record per run, with
                      MEASURED / DERIVED / INTERPRETATION separated and a
                      per-metric validity (data quality) layer.
  - activities.csv  : one row per run, flat + analysis-ready (Excel/pandas/R).
  - intervals.csv   : one row per lap/interval split (fan-out of the run row).
  - pain_log.csv    : one row per check-in (subjective pain response).

The dataset is written at build time (inside health_dashboard.py / CI) and
committed to the repo so GitHub Pages hosts it next to the dashboard.

Deliberate design choices:
  - Nothing is deleted when a metric looks wrong -- instead it is kept raw
    and a data_quality flag explains why it should not be trusted (e.g. HR
    drift computed over an interval session is dominated by rep/recovery
    swings). This matches the "much better than deleting the metric"
    philosophy: the value stays, its validity is explicit.
  - Aggregates (GCT balance, cadence, stride) are kept as measured values;
    *change* metrics (early -> late drift) are only present when the source
    stream analysis produced them (None otherwise), never invented here.
"""

import csv
import io
import json
import statistics


SCHEMA_VERSION = "1.0"

SESSION_STEADY = "steady"
SESSION_FARTLEK = "fartlek"
SESSION_INTERVAL = "interval"
SESSION_HILL = "hill"
SESSION_THRESHOLD = "threshold"


# ── Helpers ─────────────────────────────────────────────────────────────────

def _fmt(v):
    if isinstance(v, float):
        return round(v, 2)
    return v


def _csv_string(rows, columns):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


# ── Session heuristic ───────────────────────────────────────────────────────

def guess_session(run):
    """Cheap classification of what kind of session a run was, from lap pace
    variance + elevation + HR. Honest uncertainty: returns confidence + reason.
    Real classifier output (run_model.classify_workout) is preferred when it
    exists and overrides this heuristic upstream."""
    laps = run.get("laps") or []
    paces = [l.get("pace") for l in laps if l.get("pace")]
    if not paces:
        return SESSION_STEADY, 0.2, "no lap data available"

    avg = sum(paces) / len(paces)
    cv = statistics.pstdev(paces) / avg if len(paces) > 1 else 0.0

    elev = float(run.get("elevation") or 0)
    dist = float(run.get("distance") or 0)
    elev_per_10km = (elev / (dist / 10.0)) if dist else 0.0

    if elev_per_10km >= 100:
        return SESSION_HILL, 0.7, f"elevation {elev:.0f}m over {dist:.1f}km (>{elev_per_10km:.0f}m/10km)"
    if cv >= 0.15:
        if elev > 60:
            return SESSION_HILL, 0.55, f"lap pace CV {cv:.2f} + {elev:.0f}m climbing"
        return SESSION_INTERVAL, 0.6, f"repeated hard/easy segments (lap pace CV {cv:.2f})"
    if cv >= 0.08:
        return SESSION_FARTLEK, 0.5, f"moderate lap pace variance (CV {cv:.2f})"

    hr = run.get("avg_hr")
    if hr and hr >= 160:
        return SESSION_THRESHOLD, 0.45, f"high avg HR ({hr:.0f} bpm) with steady pacing"
    return SESSION_STEADY, 0.6, f"low lap pace variance (CV {cv:.2f})"


# ── Validity layer ──────────────────────────────────────────────────────────

_NON_STEADY = (SESSION_INTERVAL, SESSION_HILL, SESSION_FARTLEK, SESSION_THRESHOLD)


def _metric_quality(session_type, derived):
    """Return {metric: {value, valid, confidence, reason}} for derived metrics
    that lose meaning on non-steady-state sessions."""
    flawed = session_type in _NON_STEADY
    out = {}
    for key, label in (("hr_drift_pct", "HR drift"),
                       ("decoupling_pct", "Aerobic decoupling"),
                       ("efficiency_factor", "Efficiency factor")):
        out[key] = {
            "value": _fmt(derived.get(key)),
            "valid": not flawed,
            "confidence": round(0.35 if flawed else 0.85, 2),
            "reason": ("Non-steady-state session — aggregate drift is dominated by "
                       "rep/recovery swings") if flawed else "Steady-state session",
        }
    return out


# ── Per-run normalization ───────────────────────────────────────────────────

def _ictl_for_date(load_data, date_iso):
    for row in load_data or []:
        if row.get("date") == date_iso:
            return row
    return {}


def _checkin_for_date(checkins, date_iso):
    for c in checkins or []:
        if c.get("date") == date_iso:
            return c
    return {}


def normalize_activity(run, ictl=None, checkin=None):
    """One raw run dict -> normalized record. ictl = matching intervals.icu
    load_data row (by date), checkin = matching check-in row (by date)."""
    run = run or {}
    ictl = ictl or {}
    checkin = checkin or {}

    session_type, session_conf, session_reason = guess_session(run)

    hr_drift = ictl.get("decoupling")
    derived = {
        "avg_pace_sec_km": round((run.get("avg_pace") or 0) * 60, 1) if run.get("avg_pace") else None,
        "gct_balance_symmetry_pp": round(abs((run.get("gct_balance") or 50) - 50), 1)
        if run.get("gct_balance") is not None else None,
        "hr_drift_pct": hr_drift,
        "decoupling_pct": hr_drift,
        "efficiency_factor": ictl.get("efficiency"),
        "training_load": ictl.get("load"),
        "ctl": ictl.get("ctl"),
        "atl": ictl.get("atl"),
    }

    measured = {
        "distance_km": _fmt(run.get("distance")),
        "duration_min": _fmt(run.get("duration")),
        "moving_duration_min": _fmt(run.get("moving_duration")),
        "avg_pace_min_km": _fmt(run.get("avg_pace")),
        "max_pace_min_km": _fmt(run.get("max_pace")),
        "avg_hr": run.get("avg_hr"),
        "max_hr": run.get("max_hr"),
        "calories": run.get("calories"),
        "elevation_gain_m": run.get("elevation"),
        "avg_temperature_c": run.get("avg_temperature"),
        "avg_gct_ms": run.get("avg_gct"),
        "gct_balance_left_pct": run.get("gct_balance"),
        "avg_cadence_spm": run.get("cadence"),
        "avg_stride_cm": run.get("avg_stride_length"),
        "avg_power_w": run.get("avg_power"),
        "max_power_w": run.get("max_power"),
        "aerobic_te": run.get("training_effect_aerobic"),
        "anaerobic_te": run.get("training_effect_anaerobic"),
    }

    return {
        "activity": {
            "activity_id": run.get("activity_id") or run.get("date"),
            "date": run.get("date"),
            "garmin_type": run.get("type"),
            "derived": derived,
        },
        "measured": measured,
        "cardio": {
            "avg_hr": measured["avg_hr"],
            "max_hr": measured["max_hr"],
            "hr_drift_pct": hr_drift,
            "decoupling_pct": hr_drift,
            "aerobic_te": measured["aerobic_te"],
            "anaerobic_te": measured["anaerobic_te"],
        },
        "mechanics": {
            "avg_gct_ms": measured["avg_gct_ms"],
            "gct_change_ms": None,
            "gct_balance_left_pct": measured["gct_balance_left_pct"],
            "balance_symmetry_pp": derived["gct_balance_symmetry_pp"],
            "avg_cadence_spm": measured["avg_cadence_spm"],
            "avg_stride_cm": measured["avg_stride_cm"],
            "stride_change_cm": None,
        },
        "power": {
            "avg_power_w": measured["avg_power_w"],
            "max_power_w": measured["max_power_w"],
            "power_cv_pct": None,
        },
        "load": {
            "elevation_gain_m": measured["elevation_gain_m"],
            "training_load": derived["training_load"],
            "ctl": derived["ctl"],
            "atl": derived["atl"],
            "calories": measured["calories"],
        },
        "injury": {
            "pain_before": checkin.get("pain_before"),
            "pain_stiffness": checkin.get("stiffness"),
            "pain_first_steps": checkin.get("first_steps_pain"),
            "pain_post_run": checkin.get("post_run_pain"),
            "pain_during": checkin.get("pain_during"),
            "location": checkin.get("location"),
            "side": checkin.get("side"),
            "neurological_symptoms": checkin.get("neurological"),
            "calf_raises_done": checkin.get("calf_raises"),
            "has_checkin": bool(checkin),
        },
        "session_type": {
            "label": session_type,
            "confidence": session_conf,
            "reason": session_reason,
        },
        "measurement_quality": {
            "session_classification": {
                "value": session_type,
                "confidence": session_conf,
                "reason": session_reason,
            },
            "mechanics_change": {
                "value": None,
                "valid": False,
                "confidence": 0.0,
                "reason": "Per-lap stream analysis not yet run for this activity",
            },
            "power_cv": {
                "value": None,
                "valid": False,
                "confidence": 0.0,
                "reason": "Lap-level power not available for this activity",
            },
        },
        "measurement_quality_derived": _metric_quality(session_type, derived),
        "interpretation": {
            "note": (
                "Drop in stride + rise in GCT = mechanical fatigue accumulation; "
                "balance drifting from 50% = side compensation. See dashboard."
            ),
            "needs_review": bool(checkin and (checkin.get("post_run_pain") or 0) > 2),
        },
    }


# ── Record building ─────────────────────────────────────────────────────────

def build_records(runs, ictl_load, checkins):
    records = []
    seen = set()
    for run in runs:
        base = run.get("date")
        aid = base
        n = 2
        while aid in seen:
            aid = f"{base}-{n}"
            n += 1
        seen.add(aid)
        run = dict(run)
        run["activity_id"] = aid

        ictl = _ictl_for_date(ictl_load, base)
        checkin = _checkin_for_date(checkins, base)
        rec = normalize_activity(run, ictl, checkin)
        rec["_laps"] = run.get("laps") or []
        records.append(rec)
    return records


# ── Flat CSV rows ───────────────────────────────────────────────────────────

ACTIVITIES_COLUMNS = [
    "activity_id", "date", "garmin_type", "session_type", "session_confidence",
    "distance_km", "duration_min", "moving_duration_min",
    "avg_pace_min_km", "avg_pace_sec_km", "max_pace_min_km",
    "avg_hr", "max_hr", "elevation_gain_m",
    "avg_gct_ms", "gct_balance_left_pct", "balance_symmetry_pp",
    "avg_cadence_spm", "avg_stride_cm",
    "avg_power_w", "max_power_w",
    "aerobic_te", "anaerobic_te",
    "avg_temperature_c", "calories",
    "training_load", "ctl", "atl",
    "hr_drift_pct", "hr_drift_valid", "efficiency_factor",
    "pain_before", "pain_stiffness", "pain_first_steps", "pain_post_run", "pain_during",
]


def activities_rows(records):
    rows = []
    for r in records:
        measured = r["measured"]
        q = r["measurement_quality_derived"]["hr_drift_pct"]
        rows.append({
            "activity_id": r["activity"]["activity_id"],
            "date": r["activity"]["date"],
            "garmin_type": r["activity"]["garmin_type"],
            "session_type": r["session_type"]["label"],
            "session_confidence": _fmt(r["session_type"]["confidence"]),
            "distance_km": measured["distance_km"],
            "duration_min": measured["duration_min"],
            "moving_duration_min": measured["moving_duration_min"],
            "avg_pace_min_km": measured["avg_pace_min_km"],
            "avg_pace_sec_km": r["activity"]["derived"]["avg_pace_sec_km"],
            "max_pace_min_km": measured["max_pace_min_km"],
            "avg_hr": measured["avg_hr"],
            "max_hr": measured["max_hr"],
            "elevation_gain_m": measured["elevation_gain_m"],
            "avg_gct_ms": measured["avg_gct_ms"],
            "gct_balance_left_pct": measured["gct_balance_left_pct"],
            "balance_symmetry_pp": r["mechanics"]["balance_symmetry_pp"],
            "avg_cadence_spm": measured["avg_cadence_spm"],
            "avg_stride_cm": measured["avg_stride_cm"],
            "avg_power_w": measured["avg_power_w"],
            "max_power_w": measured["max_power_w"],
            "aerobic_te": measured["aerobic_te"],
            "anaerobic_te": measured["anaerobic_te"],
            "avg_temperature_c": measured["avg_temperature_c"],
            "calories": measured["calories"],
            "training_load": r["load"]["training_load"],
            "ctl": r["load"]["ctl"],
            "atl": r["load"]["atl"],
            "hr_drift_pct": q["value"],
            "hr_drift_valid": q["valid"],
            "efficiency_factor": r["measurement_quality_derived"]["efficiency_factor"]["value"],
            "pain_stiffness": r["injury"]["pain_stiffness"],
            "pain_before": r["injury"]["pain_before"],
            "pain_first_steps": r["injury"]["pain_first_steps"],
            "pain_post_run": r["injury"]["pain_post_run"],
            "pain_during": r["injury"]["pain_during"],
        })
    return rows


INTERVALS_COLUMNS = [
    "activity_id", "date", "garmin_type", "session_type",
    "interval_number", "distance_km", "pace_min_km",
    "avg_hr", "avg_power", "gct_ms", "gct_balance_left_pct",
    "cadence_spm", "stride_cm", "vert_osc_mm",
]


def intervals_rows(records):
    rows = []
    for r in records:
        for i, lap in enumerate(r["_laps"] or [], 1):
            rows.append({
                "activity_id": r["activity"]["activity_id"],
                "date": r["activity"]["date"],
                "garmin_type": r["activity"]["garmin_type"],
                "session_type": r["session_type"]["label"],
                "interval_number": i,
                "distance_km": _fmt(round(lap.get("km", 0), 1)),
                "pace_min_km": _fmt(lap.get("pace")),
                "avg_hr": lap.get("hr"),
                "avg_power": lap.get("power"),
                "gct_ms": _fmt(lap.get("gct")),
                "gct_balance_left_pct": _fmt(lap.get("gct_balance")),
                "cadence_spm": lap.get("cadence"),
                "stride_cm": _fmt(lap.get("stride_length")),
                "vert_osc_mm": _fmt(lap.get("vert_osc")),
            })
    return rows


PAIN_LOG_COLUMNS = [
    "date", "pain_before", "stiffness", "first_steps_pain", "post_run_pain", "pain_during",
    "location", "side", "neurological_symptoms", "calf_raises", "source",
]


def pain_log_rows(checkins):
    rows = []
    for c in checkins or []:
        rows.append({
            "date": c.get("date"),
            "pain_before": c.get("pain_before"),
            "stiffness": c.get("stiffness"),
            "first_steps_pain": c.get("first_steps_pain"),
            "post_run_pain": c.get("post_run_pain"),
            "pain_during": c.get("pain_during"),
            "location": c.get("location"),
            "side": c.get("side"),
            "neurological_symptoms": bool(c.get("neurological")),
            "calf_raises": bool(c.get("calf_raises")),
            "source": c.get("source"),
        })
    return rows


# ── Dataset builder ─────────────────────────────────────────────────────────

def build_and_write(runs, ictl_load, checkins, out_dir="."):
    """Build normalized records, write the four dataset files next to the
    dashboard, and return a summary dict for the build log."""
    import os
    from datetime import datetime

    records = build_records(runs, ictl_load, checkins)

    analysis_doc = {
        "schema_version": SCHEMA_VERSION,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "runs": [
            {k: v for k, v in rec.items() if k not in ("_laps",)}
            for rec in records
        ],
    }

    activities_csv = _csv_string(activities_rows(records), ACTIVITIES_COLUMNS)
    intervals_csv = _csv_string(intervals_rows(records), INTERVALS_COLUMNS)
    pain_csv = _csv_string(pain_log_rows(checkins), PAIN_LOG_COLUMNS)

    files = {
        "analysis.json": json.dumps(analysis_doc, indent=2, default=str),
        "activities.csv": activities_csv,
        "intervals.csv": intervals_csv,
        "pain_log.csv": pain_csv,
    }
    written = {}
    for name, content in files.items():
        with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="") as f:
            f.write(content)
        written[name] = len(content.splitlines())

    return {
        "n_runs": len(records),
        "n_intervals": sum(len(r["_laps"]) for r in records),
        "n_checkins": len(checkins or []),
        "files": written,
    }