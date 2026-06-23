"""
run_model.py
-------------
Physiology + running mechanics analysis with breakpoint detection.

New in this version:
  - Running dynamics layer (GCT, GCT balance, cadence, vertical oscillation)
    from lap splits, merged with the second-by-second stream analysis
  - GCT balance drift flag — tracks left/right loading asymmetry across
    the run, critical for Achilles tendinopathy monitoring
  - Resting HR context pulled from score_history.json for each run date
  - HR reserve at breakpoint (using resting HR + configurable max HR)
  - Mechanical fatigue flag when GCT balance drifts > threshold

Usage:
    python run_model.py today today-1y
    python run_model.py 2026-06-21 2025-06-21 2025-09-14
    python run_model.py today --debug

Date formats: YYYY-MM-DD, today, yesterday, today-Ny
"""

import argparse
import json
import os
import re
import sys
from collections import deque
from datetime import date, timedelta

from health_dashboard import get_garmin, HISTORY_FILE

# ── CONFIG ────────────────────────────────────────────────────────────────────
CONFIG = {
    "hr_threshold":           150,   # bpm — aerobic ceiling
    "hr_duration_sec":         45,   # seconds above threshold to confirm breakpoint
    "hr_smoothing_sec":         8,   # rolling window for HR
    "pace_smoothing_sec":      15,   # rolling window for pace
    "alignment_window_km":    3.0,   # ±km around breakpoint in Layer C
    "sample_every_sec":         5,   # downsample for chart rendering
    "max_hr":                 190,   # used for HR reserve — adjust if you know your real max
    "gct_balance_drift_flag": 2.0,   # % left/right drift threshold for mechanical fatigue flag
}

COLORS = ["#58a6ff", "#f78166", "#34d399", "#fbbf24", "#bc8cff", "#ff7b72"]
INJURY_SIDE = "left"  # your Achilles is left insertional — flags drift away from left loading


# ── DATE PARSING ──────────────────────────────────────────────────────────────
def find_latest_run_date(garmin=None, lookback_days=21):
    """Find the actual most recent run date by scanning score_history.json
    backward from today for the last entry with ran_today=True.

    This deliberately does NOT use find_run_on_date()'s forward-only search
    anchored at "today" -- that search only looks at today, today+1, ...
    today+7, which are FUTURE dates relative to "today" if you haven't run
    yet. On a rest day (or run live before today's run happens), that
    forward search finds nothing and silently skips the comparison instead
    of finding your real most recent run from a few days ago. Scanning
    backward through already-recorded history is the correct direction for
    "what's my latest run", as opposed to "is there a run near THIS date"
    (which forward search is fine for, since historical 1y/2y-ago anchors
    are always in the past already).
    """
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                history = json.load(f)
            for h in sorted(history, key=lambda x: x.get("date", ""), reverse=True):
                if h.get("inputs", {}).get("ran_today"):
                    return date.fromisoformat(h["date"])
        except Exception as e:
            print(f"  Could not read score history for latest-run lookup: {e}")

    # Fallback: score_history.json missing/stale -- ask Garmin directly,
    # day by day, backward from today.
    print("  Falling back to direct Garmin lookup for latest run date...")
    if garmin is None:
        garmin = get_garmin()
    today = date.today()
    for offset in range(lookback_days + 1):
        d = today - timedelta(days=offset)
        activities = garmin.get_activities_by_date(d.isoformat(), d.isoformat(), "running")
        if activities:
            return d
    raise SystemExit(f"No runs found in the last {lookback_days} days via score_history or Garmin.")


def parse_date_arg(s, latest_date=None):
    s = s.strip().lower()
    if s == "today":
        return date.today()
    if s == "yesterday":
        return date.today() - timedelta(days=1)
    if s == "latest":
        if latest_date is None:
            raise SystemExit("'latest' used but no latest_date was resolved.")
        return latest_date
    m = re.match(r"^latest-(\d+)y$", s)
    if m:
        if latest_date is None:
            raise SystemExit("'latest-Ny' used but no latest_date was resolved.")
        years = int(m.group(1))
        try:
            return latest_date.replace(year=latest_date.year - years)
        except ValueError:
            return latest_date.replace(month=2, day=28, year=latest_date.year - years)
    m = re.match(r"^today-(\d+)y$", s)
    if m:
        years = int(m.group(1))
        today = date.today()
        try:
            return today.replace(year=today.year - years)
        except ValueError:
            return today.replace(month=2, day=28, year=today.year - years)
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise SystemExit(f"Can't parse date '{s}'. Use YYYY-MM-DD, 'today', 'yesterday', 'latest', 'latest-Ny', or 'today-Ny'.")


# ── SCORE HISTORY LOOKUP ──────────────────────────────────────────────────────
def get_score_history_entry(d_str):
    """Pull today's resting HR (and any other stored inputs) from score_history.json."""
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            history = json.load(f)
        entry = next((h for h in history if h.get("date") == d_str), None)
        return entry.get("inputs", {}) if entry else {}
    except Exception:
        return {}


# ── GARMIN FETCH ──────────────────────────────────────────────────────────────
def find_similar_run_near(garmin, anchor_date, target_weekday, target_distance_km, window_days=21):
    """
    Find the best comparison run near anchor_date (e.g. ~1 year before the
    latest run), prioritizing in this order:
      1. Same day-of-week as the latest run (e.g. always Monday vs Monday
         vs Monday) -- a literal year-ago date usually ISN'T the same
         weekday (365 % 7 == 1, so it drifts by a day or two per year),
         so this is a deliberate search, not just a date shift.
      2. Among same-weekday candidates, closest distance to the latest
         run's distance -- comparing a 5km recovery jog against a 16km
         long run a year apart isn't a meaningful comparison.
      3. Date proximity to anchor_date as the final tiebreaker.

    Falls back to the best available date+distance match (ignoring
    weekday) if nothing in the window shares the target weekday.
    """
    start = (anchor_date - timedelta(days=window_days)).isoformat()
    end = (anchor_date + timedelta(days=window_days)).isoformat()
    activities = garmin.get_activities_by_date(start, end, "running")
    if not activities:
        return None, None

    candidates = []
    for a in activities:
        d_str = a.get("startTimeLocal", "")[:10]
        if not d_str:
            continue
        try:
            d = date.fromisoformat(d_str)
        except ValueError:
            continue
        dist_km = round(a.get("distance", 0) / 1000, 1)
        candidates.append((a, d, dist_km))

    if not candidates:
        return None, None

    same_weekday = [c for c in candidates if c[1].weekday() == target_weekday]
    pool = same_weekday if same_weekday else candidates
    if not same_weekday:
        print(f"    ⚠️  No run on the matching weekday within ±{window_days}d of "
              f"{anchor_date.isoformat()} -- falling back to best date+distance match")

    def score(c):
        _, d, dist_km = c
        dist_penalty = abs(dist_km - target_distance_km) if target_distance_km else 0
        date_penalty = abs((d - anchor_date).days)
        # Distance similarity dominates; date proximity only breaks ties
        # between otherwise-similar-distance candidates.
        return dist_penalty * 10 + date_penalty

    activity, actual_date, dist_km = min(pool, key=score)
    return activity, actual_date


def find_run_on_date(garmin, d, max_lookahead=7):
    for offset in range(max_lookahead + 1):
        d_str = (d + timedelta(days=offset)).isoformat()
        activities = garmin.get_activities_by_date(d_str, d_str, "running")
        if activities:
            run = max(activities, key=lambda a: a.get("distance", 0))
            actual = d + timedelta(days=offset)
            if offset > 0:
                print(f"    ⏭  No run on {d.isoformat()} → shifted to {actual.isoformat()}")
            return run, actual
    return None, d


def fetch_activity_stream(garmin, activity_id, debug=False):
    """Second-by-second HR + pace from get_activity_details()."""
    details = garmin.get_activity_details(activity_id)
    descriptors = details.get("metricDescriptors", [])

    if debug:
        print("\n  DEBUG — available metric keys (stream):")
        for d in descriptors:
            print(f"    [{d['metricsIndex']}] {d['key']}")
        print()

    idx = {d["key"]: d["metricsIndex"] for d in descriptors}
    time_key = next((k for k in ["sumDuration", "sumElapsedDuration"] if k in idx), None)
    hr_key   = next((k for k in ["directHeartRate", "heartRate"] if k in idx), None)
    spd_key  = next((k for k in ["directGradeAdjustedSpeed", "directSpeed"] if k in idx), None)
    dist_key = next((k for k in ["sumDistance", "directDistance"] if k in idx), None)

    if not hr_key:
        raise ValueError(f"No HR metric found. Available: {list(idx.keys())}")

    points = []
    for m in details.get("activityDetailMetrics", []):
        vals = m.get("metrics", [])
        if not vals:
            continue

        def get(key):
            if key is None:
                return None
            i = idx.get(key)
            if i is None or i >= len(vals):
                return None
            v = vals[i]
            return v if v is not None and v > 0 else None

        time_sec = get(time_key)
        hr       = get(hr_key)
        speed    = get(spd_key)
        dist_m   = get(dist_key)

        if time_sec is None:
            continue
        if hr is None and speed is None:
            continue

        pace = round(1000 / speed / 60, 3) if speed and speed > 0.5 else None
        points.append({
            "t": float(time_sec),
            "hr": float(hr) if hr else None,
            "pace": pace,
            "dist_km": round(dist_m / 1000, 3) if dist_m else None,
        })

    if points and all(p["dist_km"] is None for p in points):
        cum = 0.0
        for i, p in enumerate(points):
            if i > 0 and p["pace"] and p["pace"] > 0:
                dt = p["t"] - points[i-1]["t"]
                cum += (1000 / (p["pace"] * 60)) * dt / 1000
            p["dist_km"] = round(cum, 3)

    points.sort(key=lambda p: p["t"])
    return points


def fetch_lap_dynamics(garmin, activity_id, debug=False):
    """
    Pull per-lap running dynamics from get_activity_splits().
    Returns list of laps with cumulative_km, gct_ms, gct_balance_left,
    cadence, vertical_oscillation, stride_length.

    Garmin field names used here are the standard lapDTOs keys — if any
    come back None for your watch, run with --debug to see what's available.
    """
    try:
        splits = garmin.get_activity_splits(activity_id)
    except Exception as e:
        print(f"    ⚠️  Could not fetch lap splits: {e}")
        return []

    laps = splits.get("lapDTOs", [])

    if debug:
        print("\n  DEBUG — lap keys (first lap sample):")
        if laps:
            for k, v in laps[0].items():
                print(f"    {k}: {v}")
        print()

    result = []
    cum_km = 0.0
    for i, lap in enumerate(laps):
        dist_m = lap.get("distance", 0)
        cum_km += dist_m / 1000

        # GCT: groundContactTime in ms
        gct = lap.get("groundContactTime")
        # Balance: groundContactBalanceLeft as % (e.g. 49.5 = 49.5% left)
        gct_balance_left = lap.get("groundContactBalanceLeft")
        gct_balance_right = round(100 - gct_balance_left, 1) if gct_balance_left is not None else None

        # Cadence: stepsPerMinute or averageRunCadence (steps/min, not strides)
        cadence = lap.get("averageRunningCadenceInStepsPerMinute") or lap.get("averageRunCadence")

        # Vertical oscillation in mm
        vert_osc = lap.get("avgVerticalOscillation")

        # Stride length in m
        stride = lap.get("avgStrideLength")

        result.append({
            "lap": i + 1,
            "dist_km": round(dist_m / 1000, 2),
            "cum_km": round(cum_km, 2),
            "gct_ms": round(gct, 1) if gct else None,
            "gct_balance_left": round(gct_balance_left, 1) if gct_balance_left is not None else None,
            "gct_balance_right": gct_balance_right,
            "cadence": round(cadence, 1) if cadence else None,
            "vert_osc_mm": round(vert_osc, 1) if vert_osc else None,
            "stride_m": round(stride, 2) if stride else None,
            "avg_hr": lap.get("averageHR"),
            "avg_pace": round(1000 / lap.get("averageSpeed", 1) / 60, 2) if lap.get("averageSpeed") else None,
        })

    return result


# ── SMOOTHING ─────────────────────────────────────────────────────────────────
def rolling_avg(series, window_sec, times):
    result = []
    buf = deque()
    for t, v in zip(times, series):
        if v is not None:
            buf.append((t, v))
        while buf and t - buf[0][0] > window_sec:
            buf.popleft()
        result.append(round(sum(x[1] for x in buf) / len(buf), 1) if buf else None)
    return result


def smooth_stream(points):
    times = [p["t"] for p in points]
    smooth_hr   = rolling_avg([p["hr"]   for p in points], CONFIG["hr_smoothing_sec"],   times)
    smooth_pace = rolling_avg([p["pace"] for p in points], CONFIG["pace_smoothing_sec"],  times)
    for i, p in enumerate(points):
        p["hr_smooth"]   = smooth_hr[i]
        p["pace_smooth"] = smooth_pace[i]
    return points


# ── BREAKPOINT DETECTION ──────────────────────────────────────────────────────
def detect_breakpoint(points):
    threshold = CONFIG["hr_threshold"]
    duration  = CONFIG["hr_duration_sec"]
    above_since = None

    for i, p in enumerate(points):
        hr = p.get("hr_smooth") or p.get("hr")
        if hr is None:
            above_since = None
            continue
        if hr > threshold:
            if above_since is None:
                above_since = p["t"]
            elif p["t"] - above_since >= duration:
                for j in range(i, -1, -1):
                    h = points[j].get("hr_smooth") or points[j].get("hr")
                    if h is not None and h <= threshold:
                        return j + 1
                return i
        else:
            above_since = None
    return len(points)


# ── RUNNING DYNAMICS ANALYSIS ─────────────────────────────────────────────────
def analyse_dynamics(laps, bp_idx_lap=None):
    """
    Analyse GCT, balance drift, cadence, and vertical oscillation across laps.
    bp_idx_lap: approximate lap index of the breakpoint (for A/B split).

    Returns a dict with:
      - early/late averages for each metric (first third vs last third)
      - GCT balance drift (start → end)
      - mechanical fatigue flag if drift > threshold
      - per-lap data for charting
    """
    if not laps:
        return {}

    def lap_avg(laps_subset, key):
        vals = [l[key] for l in laps_subset if l.get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    n = len(laps)
    third = max(1, n // 3)
    early = laps[:third]
    late  = laps[-third:]

    # GCT drift
    gct_early  = lap_avg(early, "gct_ms")
    gct_late   = lap_avg(late,  "gct_ms")
    gct_drift  = round(gct_late - gct_early, 1) if gct_early and gct_late else None

    # Balance drift — track left side loading
    bal_early = lap_avg(early, "gct_balance_left")
    bal_late  = lap_avg(late,  "gct_balance_left")
    bal_drift = round(bal_late - bal_early, 2) if bal_early is not None and bal_late is not None else None

    # Mechanical fatigue flag
    # For left Achilles: flag if late-run balance shifts LEFT loading UP
    # (body compensating by loading injured side more when fatigued)
    # or shifts DOWN significantly (offloading the injured side, also compensation)
    mech_fatigue_flag = None
    mech_fatigue_detail = None
    if bal_drift is not None:
        drift_abs = abs(bal_drift)
        if drift_abs >= CONFIG["gct_balance_drift_flag"]:
            direction = "↑" if bal_drift > 0 else "↓"
            side = "more" if bal_drift > 0 else "less"
            mech_fatigue_flag = True
            mech_fatigue_detail = (
                f"Left loading drifted {direction} {drift_abs:.1f}% "
                f"({bal_early:.1f}% → {bal_late:.1f}%) — "
                f"body loading left side {side} when fatigued. "
                f"{'⚠️ Achilles compensation pattern.' if drift_abs >= CONFIG['gct_balance_drift_flag'] * 1.5 else 'Monitor closely.'}"
            )
        else:
            mech_fatigue_flag = False
            mech_fatigue_detail = f"Balance stable ({bal_early:.1f}% → {bal_late:.1f}%, drift {drift_abs:.1f}%)"

    # Cadence drop
    cad_early = lap_avg(early, "cadence")
    cad_late  = lap_avg(late,  "cadence")
    cad_drop  = round(cad_late - cad_early, 1) if cad_early and cad_late else None

    # Vertical oscillation change
    vo_early = lap_avg(early, "vert_osc_mm")
    vo_late  = lap_avg(late,  "vert_osc_mm")
    vo_change = round(vo_late - vo_early, 1) if vo_early and vo_late else None

    return {
        "gct_early_ms": gct_early,
        "gct_late_ms":  gct_late,
        "gct_drift_ms": gct_drift,
        "bal_early_pct": bal_early,
        "bal_late_pct":  bal_late,
        "bal_drift_pct": bal_drift,
        "mech_fatigue_flag": mech_fatigue_flag,
        "mech_fatigue_detail": mech_fatigue_detail,
        "cadence_early": cad_early,
        "cadence_late":  cad_late,
        "cadence_drop":  cad_drop,
        "vert_osc_early_mm": vo_early,
        "vert_osc_late_mm":  vo_late,
        "vert_osc_change_mm": vo_change,
        "laps": laps,
        "has_dynamics": any(l.get("gct_ms") for l in laps),
    }


# ── METRICS ENGINE ────────────────────────────────────────────────────────────
def compute_metrics(points, bp_idx, activity, resting_hr=None):
    seg_a = [p for p in points[:bp_idx] if p.get("hr_smooth") and p.get("pace_smooth")]
    seg_b = [p for p in points[bp_idx:] if p.get("hr_smooth") and p.get("pace_smooth")]
    total = [p for p in points          if p.get("hr_smooth") and p.get("pace_smooth")]

    def avg(lst, key):
        vals = [p[key] for p in lst if p.get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    def ei(lst):
        vals = [1000 / p["pace_smooth"] / p["hr_smooth"]
                for p in lst if p.get("pace_smooth") and p.get("hr_smooth")
                and p["pace_smooth"] > 0 and p["hr_smooth"] > 0]
        return round(sum(vals) / len(vals), 3) if vals else None

    hr_a   = avg(seg_a, "hr_smooth")
    hr_b   = avg(seg_b, "hr_smooth")
    pace_a = avg(seg_a, "pace_smooth")
    pace_b = avg(seg_b, "pace_smooth")
    ei_a   = ei(seg_a)
    ei_b   = ei(seg_b)

    mid = len(total) // 2
    ei_first  = ei(total[:mid])
    ei_second = ei(total[mid:])
    decoupling = round((ei_first - ei_second) / ei_first * 100, 1) \
                 if ei_first and ei_second and ei_first > 0 else None

    total_dist = points[-1].get("dist_km") if points else None
    bp_dist    = points[bp_idx - 1].get("dist_km") if 0 < bp_idx <= len(points) else None
    fully_controlled = bp_idx >= len(points)
    fatigue_onset_pct = 100.0 if fully_controlled else (
        round(bp_dist / total_dist * 100, 1) if bp_dist and total_dist else None
    )

    # HR crossings count
    threshold, duration = CONFIG["hr_threshold"], CONFIG["hr_duration_sec"]
    crossings, above_since = 0, None
    for p in points:
        hr = p.get("hr_smooth")
        if hr is None:
            continue
        if hr > threshold:
            if above_since is None:
                above_since = p["t"]
        elif above_since is not None:
            if p["t"] - above_since >= duration:
                crossings += 1
            above_since = None
    if above_since is not None:
        crossings += 1

    # HR reserve at breakpoint
    # (breakpoint HR - resting HR) / (max HR - resting HR) × 100
    bp_hr = avg([p for p in points[max(0, bp_idx-30):bp_idx+30]
                 if p.get("hr_smooth")], "hr_smooth") if not fully_controlled else None
    hr_reserve_pct = None
    if resting_hr and bp_hr and CONFIG["max_hr"] > resting_hr:
        hr_reserve_pct = round(
            (bp_hr - resting_hr) / (CONFIG["max_hr"] - resting_hr) * 100, 1
        )

    return {
        "hr_a": hr_a, "hr_b": hr_b,
        "pace_a": pace_a, "pace_b": pace_b,
        "hr_rise":   round(hr_b - hr_a, 1)   if hr_a and hr_b     else None,
        "pace_drop": round(pace_b - pace_a, 2) if pace_a and pace_b else None,
        "ei_a": ei_a, "ei_b": ei_b,
        "decoupling": decoupling,
        "fatigue_onset_pct": fatigue_onset_pct,
        "bp_dist_km": bp_dist,
        "total_dist_km": total_dist,
        "crossings": crossings,
        "fully_controlled": fully_controlled,
        "resting_hr": resting_hr,
        "bp_hr": bp_hr,
        "hr_reserve_pct": hr_reserve_pct,
    }


# ── HELPERS ───────────────────────────────────────────────────────────────────
def downsample(points, every_sec=None):
    every, sampled, last_t = every_sec or CONFIG["sample_every_sec"], [], -9999
    for p in points:
        if p["t"] - last_t >= every:
            sampled.append(p)
            last_t = p["t"]
    return sampled


def normalize_dist(points):
    total = points[-1].get("dist_km") if points else None
    if not total:
        return points
    for p in points:
        p["dist_pct"] = round((p.get("dist_km") or 0) / total * 100, 1)
    return points


def align_to_breakpoint(points, bp_idx, window_km):
    bp_dist = points[bp_idx - 1].get("dist_km") if 0 < bp_idx <= len(points) else None
    if bp_dist is None:
        return []
    return [{**p, "dist_rel": round((p.get("dist_km") or 0) - bp_dist, 3)}
            for p in points
            if -window_km <= round((p.get("dist_km") or 0) - bp_dist, 3) <= window_km]


def fmt_pace(p):
    if p is None:
        return "--"
    mins = int(p)
    secs = int(round((p - mins) * 60))
    return f"{mins}:{secs:02d}/km"


# ── HTML GENERATION ───────────────────────────────────────────────────────────
def build_comparison_section(runs):
    """Render just the inner content (cards + charts + script) for the
    run comparison, with no <html>/<head>/<body> wrapper -- meant to be
    embedded directly into another page (health_dashboard.py's dashboard).
    Standalone CLI use still gets a full document via build_html() below,
    which just wraps this."""
    def mk_ds(label, color, pts, x_key, y_key):
        data = [{"x": round(p[x_key], 3), "y": round(p[y_key], 2)}
                for p in pts if p.get(x_key) is not None and p.get(y_key) is not None]
        return {"label": label, "data": data, "borderColor": color,
                "tension": 0.2, "pointRadius": 0, "borderWidth": 1.8}

    # Stream-based chart data
    hr_by_dist   = [mk_ds(r["label"], r["color"], downsample(r["points"]), "dist_km", "hr_smooth") for r in runs]
    pace_by_dist = [mk_ds(r["label"], r["color"], downsample(r["points"]), "dist_km", "pace_smooth") for r in runs]
    for r in runs:
        normalize_dist(r["points"])
    hr_norm   = [mk_ds(r["label"], r["color"], downsample(r["points"]), "dist_pct", "hr_smooth") for r in runs]
    pace_norm = [mk_ds(r["label"], r["color"], downsample(r["points"]), "dist_pct", "pace_smooth") for r in runs]

    bp_runs = [r for r in runs if not r["metrics"]["fully_controlled"]]
    window  = CONFIG["alignment_window_km"]
    hr_aligned, pace_aligned = [], []
    for r in bp_runs:
        aligned = align_to_breakpoint(r["points"], r["bp_idx"], window)
        if aligned:
            hr_aligned.append(  mk_ds(r["label"], r["color"], aligned, "dist_rel", "hr_smooth"))
            pace_aligned.append(mk_ds(r["label"], r["color"], aligned, "dist_rel", "pace_smooth"))

    # Running dynamics chart data (lap-based)
    def lap_ds(label, color, laps, y_key):
        data = [{"x": l["cum_km"], "y": l[y_key]}
                for l in laps if l.get(y_key) is not None]
        return {"label": label, "data": data, "borderColor": color,
                "tension": 0.3, "pointRadius": 3, "borderWidth": 1.8}

    gct_ds     = [lap_ds(r["label"], r["color"], r["dynamics"].get("laps", []), "gct_ms")              for r in runs if r["dynamics"].get("has_dynamics")]
    bal_ds     = [lap_ds(r["label"], r["color"], r["dynamics"].get("laps", []), "gct_balance_left")     for r in runs if r["dynamics"].get("has_dynamics")]
    cadence_ds = [lap_ds(r["label"], r["color"], r["dynamics"].get("laps", []), "cadence")              for r in runs if r["dynamics"].get("has_dynamics")]
    vo_ds      = [lap_ds(r["label"], r["color"], r["dynamics"].get("laps", []), "vert_osc_mm")          for r in runs if r["dynamics"].get("has_dynamics")]

    scatter_data = [{"x": r["metrics"]["ei_a"], "y": r["metrics"]["decoupling"],
                     "label": r["label"], "color": r["color"]}
                    for r in runs if r["metrics"].get("ei_a") and r["metrics"].get("decoupling") is not None]

    # Metrics table
    headers = "".join(f"<th style='color:{r['color']}'>{r['label']}</th>" for r in runs)

    def row(label, vals_fn, cls=""):
        cells = "".join(f"<td>{vals_fn(r)}</td>" for r in runs)
        return f"<tr><td class='ml{' ' + cls if cls else ''}'>{label}</td>{cells}</tr>"

    threshold = CONFIG["hr_threshold"]
    mhr       = CONFIG["max_hr"]

    
    section = f"""<div class="rc-section">
<h1>Run Model — Physiology + Mechanics</h1>
<div class="sub">HR threshold: {threshold} bpm · Max HR: {mhr} bpm · GCT balance drift flag: ±{CONFIG["gct_balance_drift_flag"]}% · Injury side: {INJURY_SIDE}</div>

<!-- MECHANICAL FATIGUE FLAGS -->
{"".join(f'''
<div class="{"mech-flag" if r["dynamics"].get("mech_fatigue_flag") else "mech-ok" if r["dynamics"].get("mech_fatigue_flag") is False else ""}">
  <strong style="color:{"#fbbf24" if r["dynamics"].get("mech_fatigue_flag") else "#34d399"}">{r["label"]}</strong>
  {"⚠️ Mechanical fatigue flagged: " if r["dynamics"].get("mech_fatigue_flag") else "✓ "}
  {r["dynamics"].get("mech_fatigue_detail") or "No GCT balance data available for this run."}
</div>''' for r in runs if r["dynamics"].get("mech_fatigue_detail") is not None)}

<!-- METRICS TABLE -->
<div class="card">
  <h2>Performance + Physiology Matrix</h2>
  <table>
    <thead><tr><th>Metric</th>{headers}</tr></thead>
    <tbody>
      <tr class="rc-section-header"><td colspan="{1 + len(runs)}">Context</td></tr>
      {row("Resting HR (morning)", lambda r: f"{r['metrics']['resting_hr']} bpm" if r['metrics'].get('resting_hr') else "--")}
      {row("HR reserve at breakpoint", lambda r: f"{r['metrics']['hr_reserve_pct']}% of max" if r['metrics'].get('hr_reserve_pct') else "--")}
      <tr class="rc-section-header"><td colspan="{1 + len(runs)}">Physiology</td></tr>
      {row("Total distance", lambda r: f"{r['metrics']['total_dist_km']}km" if r['metrics'].get('total_dist_km') else "--")}
      {row("Breakpoint", lambda r: f"🔴 {r['metrics']['bp_dist_km']}km ({r['metrics']['fatigue_onset_pct']}%)" if not r['metrics']['fully_controlled'] else "🟢 Fully controlled")}
      {row("Avg pace (A — controlled)", lambda r: fmt_pace(r['metrics'].get('pace_a')))}
      {row("Avg pace (B — fatigue)",    lambda r: fmt_pace(r['metrics'].get('pace_b')))}
      {row("Avg HR (A — controlled)",   lambda r: f"{r['metrics']['hr_a']} bpm" if r['metrics'].get('hr_a') else "--")}
      {row("Avg HR (B — fatigue)",      lambda r: f"{r['metrics']['hr_b']} bpm" if r['metrics'].get('hr_b') else "--")}
      {row("HR rise (B−A)",             lambda r: f"+{r['metrics']['hr_rise']} bpm" if r['metrics'].get('hr_rise') else "--")}
      {row("EI — Segment A",            lambda r: str(r['metrics']['ei_a']) if r['metrics'].get('ei_a') else "--")}
      {row("EI — Segment B",            lambda r: str(r['metrics']['ei_b']) if r['metrics'].get('ei_b') else "--")}
      {row("Decoupling (EI drift)",     lambda r: f"{r['metrics']['decoupling']}%" if r['metrics'].get('decoupling') is not None else "--")}
      {row("HR crossings",              lambda r: str(r['metrics']['crossings']))}
      <tr class="rc-section-header"><td colspan="{1 + len(runs)}">Running Mechanics</td></tr>
      {row("GCT — early laps (ms)",        lambda r: str(r['dynamics'].get('gct_early_ms') or '--'))}
      {row("GCT — late laps (ms)",         lambda r: str(r['dynamics'].get('gct_late_ms') or '--'))}
      {row("GCT drift (late − early)",     lambda r: f"+{r['dynamics']['gct_drift_ms']}ms" if (r['dynamics'].get('gct_drift_ms') or 0) > 0 else (f"{r['dynamics']['gct_drift_ms']}ms" if r['dynamics'].get('gct_drift_ms') is not None else "--"))}
      {row("GCT balance — early (L%)",     lambda r: f"{r['dynamics'].get('bal_early_pct')}% L" if r['dynamics'].get('bal_early_pct') is not None else "--")}
      {row("GCT balance — late (L%)",      lambda r: f"{r['dynamics'].get('bal_late_pct')}% L" if r['dynamics'].get('bal_late_pct') is not None else "--")}
      {row("Balance drift",                lambda r: f"{r['dynamics']['bal_drift_pct']:+.1f}%" if r['dynamics'].get('bal_drift_pct') is not None else "--")}
      {row("Cadence — early (spm)",        lambda r: str(r['dynamics'].get('cadence_early') or '--'))}
      {row("Cadence — late (spm)",         lambda r: str(r['dynamics'].get('cadence_late') or '--'))}
      {row("Cadence drop",                 lambda r: f"{r['dynamics']['cadence_drop']:+.1f} spm" if r['dynamics'].get('cadence_drop') is not None else "--")}
      {row("Vert. osc. — early (mm)",      lambda r: str(r['dynamics'].get('vert_osc_early_mm') or '--'))}
      {row("Vert. osc. — late (mm)",       lambda r: str(r['dynamics'].get('vert_osc_late_mm') or '--'))}
      {row("Vert. osc. change",            lambda r: f"{r['dynamics']['vert_osc_change_mm']:+.1f}mm" if r['dynamics'].get('vert_osc_change_mm') is not None else "--")}
    </tbody>
  </table>
  <p class="note">EI = m/min per bpm. Higher = more efficient. Decoupling = EI drift first → second half. HR reserve = (BP HR − resting HR) / ({mhr} − resting HR). GCT balance: 50% = perfect symmetry.</p>
</div>

<!-- RUNNING MECHANICS CHARTS -->
{"" if not any(r["dynamics"].get("has_dynamics") for r in runs) else f'''
<div class="card">
  <h2>Running Mechanics — by km</h2>
  <div class="insight">GCT rising = less elastic return, more braking, fatigue compensation. Balance drifting away from 50% = body protecting one side. Left side is your Achilles side.</div>
  <div class="grid2">
    <div><canvas id="gctChart"></canvas></div>
    <div><canvas id="balChart"></canvas></div>
  </div>
  <div class="grid2" style="margin-top:16px">
    <div><canvas id="cadChart"></canvas></div>
    <div><canvas id="voChart"></canvas></div>
  </div>
</div>
'''}

<!-- LAYER A: Raw distance -->
<div class="card">
  <h2>HR + Pace by distance</h2>
  <div class="grid2">
    <div><canvas id="hrDistChart"></canvas></div>
    <div><canvas id="paceDistChart"></canvas></div>
  </div>
</div>

<!-- LAYER B: Normalized -->
<div class="card">
  <h2>Normalized overlay (0–100% of run)</h2>
  <div class="grid2">
    <div><canvas id="hrNormChart"></canvas></div>
    <div><canvas id="paceNormChart"></canvas></div>
  </div>
</div>

<!-- LAYER C: Breakpoint aligned -->
{"" if not bp_runs else f'''
<div class="card">
  <h2>Breakpoint-aligned (0 = fatigue onset, ±{window}km)</h2>
  <div class="grid2">
    <div><canvas id="hrAlignChart"></canvas></div>
    <div><canvas id="paceAlignChart"></canvas></div>
  </div>
</div>
'''}

<!-- SCATTER -->
{"" if len(scatter_data) < 2 else '''
<div class="card">
  <h2>EI_A vs Decoupling — run scatter</h2>
  <div class="insight">Top-left = high efficiency, low drift = ideal. Track this across runs to see fitness progression.</div>
  <canvas id="scatterChart" style="max-height:240px"></canvas>
</div>
'''}

<script>
(function() {{
const D = {json.dumps({
    "hrByDist": hr_by_dist, "paceByDist": pace_by_dist,
    "hrNorm": hr_norm, "paceNorm": pace_norm,
    "hrAligned": hr_aligned, "paceAligned": pace_aligned,
    "gct": gct_ds, "bal": bal_ds, "cadence": cadence_ds, "vo": vo_ds,
    "scatter": scatter_data,
})};
const threshold = {threshold};
const balTarget = 50;

const base = (xl, yl, rev=false) => ({{
  responsive:true, maintainAspectRatio:false,
  interaction:{{mode:'nearest',axis:'x',intersect:false}},
  plugins:{{legend:{{labels:{{color:'#8b949e',font:{{size:10}}}}}}}},
  scales:{{
    x:{{type:'linear',title:{{display:true,text:xl,color:'#8b949e',font:{{size:10}}}},ticks:{{color:'#8b949e'}},grid:{{color:'#21262d'}}}},
    y:{{reverse:rev,title:{{display:true,text:yl,color:'#8b949e',font:{{size:10}}}},ticks:{{color:'#8b949e'}},grid:{{color:'#21262d'}}}},
  }}
}});

function line(id, ds, xl, yl, rev=false) {{
  const el = document.getElementById(id);
  if (!el || !ds.length) return;
  new Chart(el, {{type:'line', data:{{datasets:ds}}, options:base(xl,yl,rev)}});
}}

// Running mechanics
line('gctChart',  D.gct,     'km', 'GCT (ms)');
line('balChart',  D.bal,     'km', 'GCT balance — left %');
line('cadChart',  D.cadence, 'km', 'Cadence (spm)');
line('voChart',   D.vo,      'km', 'Vert. osc. (mm)');

// Draw 50% reference line on balance chart
const balChart = Chart.getChart('balChart');
if (balChart) {{
  const orig = balChart.draw.bind(balChart);
  balChart.draw = function() {{
    orig();
    const ctx = balChart.ctx, ys = balChart.scales.y;
    const y = ys.getPixelForValue(50);
    ctx.save();
    ctx.strokeStyle = 'rgba(255,255,255,0.2)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4,4]);
    ctx.beginPath();
    ctx.moveTo(balChart.chartArea.left, y);
    ctx.lineTo(balChart.chartArea.right, y);
    ctx.stroke();
    ctx.fillStyle = 'rgba(255,255,255,0.3)';
    ctx.font = '10px sans-serif';
    ctx.fillText('50% (symmetry)', balChart.chartArea.left + 4, y - 4);
    ctx.restore();
  }};
  balChart.update();
}}

// Stream layers
line('hrDistChart',   D.hrByDist,   'km',      'HR (bpm)');
line('paceDistChart', D.paceByDist, 'km',      'Pace (min/km)', true);
line('hrNormChart',   D.hrNorm,     '% of run','HR (bpm)');
line('paceNormChart', D.paceNorm,   '% of run','Pace (min/km)', true);
line('hrAlignChart',  D.hrAligned,  'km from breakpoint','HR (bpm)');
line('paceAlignChart',D.paceAligned,'km from breakpoint','Pace (min/km)', true);

// Breakpoint vertical reference line
['hrAlignChart','paceAlignChart'].forEach(id => {{
  const c = Chart.getChart(id);
  if (!c) return;
  const o = c.draw.bind(c);
  c.draw = function() {{
    o();
    const ctx=c.ctx, x=c.scales.x.getPixelForValue(0);
    ctx.save();
    ctx.strokeStyle='rgba(247,129,102,0.6)';
    ctx.lineWidth=1.5;
    ctx.setLineDash([5,4]);
    ctx.beginPath();
    ctx.moveTo(x,c.chartArea.top);
    ctx.lineTo(x,c.chartArea.bottom);
    ctx.stroke();
    ctx.fillStyle='rgba(247,129,102,0.7)';
    ctx.font='10px sans-serif';
    ctx.fillText('fatigue onset',x+4,c.chartArea.top+14);
    ctx.restore();
  }};
  c.update();
}});

// Scatter
if (D.scatter.length >= 2) {{
  new Chart(document.getElementById('scatterChart'), {{
    type:'scatter',
    data:{{datasets:D.scatter.map(p => ({{
      label:p.label, data:[{{x:p.x,y:p.y}}],
      backgroundColor:p.color, pointRadius:8,
    }}))}},
    options:{{
      responsive:true, maintainAspectRatio:false,
      plugins:{{legend:{{labels:{{color:'#8b949e'}}}}}},
      scales:{{
        x:{{title:{{display:true,text:'EI_A (higher = more efficient)',color:'#8b949e'}},ticks:{{color:'#8b949e'}},grid:{{color:'#21262d'}}}},
        y:{{title:{{display:true,text:'Decoupling % (lower = better)',color:'#8b949e'}},ticks:{{color:'#8b949e'}},grid:{{color:'#21262d'}}}},
      }}
    }}
  }});
}}
}})();
</script>

</div>"""
    return section


# Plain (non-f-string) CSS constant for health_dashboard.py to splice into
# its own <style> block once. Scoped under .rc-section and remapped to the
# dashboard's existing CSS variable names (--text3/--blue/--orange/--green)
# instead of redeclaring a second, slightly different color palette.
EMBEDDED_CSS = """
.rc-section h1{font-size:20px;font-weight:600;margin-bottom:4px;}
.rc-section h2{font-size:13px;font-weight:500;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:14px;}
.rc-section .sub{color:var(--text3);font-size:12px;margin-bottom:24px;}
.rc-section .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:16px;}
.rc-section .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px;}
.rc-section canvas{max-height:280px;}
.rc-section table{width:100%;border-collapse:collapse;font-size:12px;}
.rc-section th{text-align:left;padding:8px 10px;border-bottom:2px solid var(--border);font-size:11px;}
.rc-section td{padding:8px 10px;border-bottom:1px solid var(--border);}
.rc-section .ml{color:var(--text3);}
.rc-section .rc-section-header td{background:var(--surface2);color:var(--text3);font-size:10px;text-transform:uppercase;letter-spacing:.06em;padding:5px 10px;font-weight:600;}
.rc-section .flag-ok{color:var(--green);font-weight:600;}
.rc-section .flag-warn{color:#fbbf24;font-weight:600;}
.rc-section .flag-bad{color:var(--orange);font-weight:600;}
.rc-section .insight{background:var(--surface2);border-left:3px solid var(--blue);border-radius:6px;padding:10px 14px;margin:0 0 10px 0;font-size:12px;line-height:1.6;color:#c0c8e0;}
.rc-section .mech-flag{background:#1f1a14;border-left:3px solid #fbbf24;border-radius:6px;padding:10px 14px;margin:0 0 10px 0;font-size:12px;color:#f5dfa0;}
.rc-section .mech-ok{background:#131f18;border-left:3px solid var(--green);border-radius:6px;padding:10px 14px;margin:0 0 10px 0;font-size:12px;color:#a3e4c3;}
.rc-section .note{font-size:11px;color:var(--text3);margin-top:8px;font-style:italic;}
"""


def build_html(runs, out_path):
    """Standalone CLI entrypoint: wraps build_comparison_section()'s
    underlying content in a full HTML document and writes it to disk.
    Kept for manual/local use; the dashboard embeds the section directly
    instead of generating this second file."""
    section = build_comparison_section(runs)
    # Standalone document needs its own full page chrome + original
    # (unscoped) CSS, since there's no host page styling to inherit here.
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Run Model — Physiology + Mechanics</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
:root {--bg:#0e1116;--surface:#161b22;--surface2:#1f2630;--border:#21262d;--text:#e6edf3;--muted:#8b949e;--accent:#58a6ff;--warn:#f78166;--ok:#34d399;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:24px;font-size:13px;}
h1{font-size:20px;font-weight:600;margin-bottom:4px;}
h2{font-size:13px;font-weight:500;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:14px;}
.sub{color:var(--muted);font-size:12px;margin-bottom:24px;}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:16px;}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px;}
canvas{max-height:280px;}
table{width:100%;border-collapse:collapse;font-size:12px;}
th{text-align:left;padding:8px 10px;border-bottom:2px solid var(--border);font-size:11px;}
td{padding:8px 10px;border-bottom:1px solid var(--border);}
.ml{color:var(--muted);}
.section-header td{background:var(--surface2);color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em;padding:5px 10px;font-weight:600;}
.flag-ok{color:var(--ok);font-weight:600;}
.flag-warn{color:#fbbf24;font-weight:600;}
.flag-bad{color:var(--warn);font-weight:600;}
.insight{background:var(--surface2);border-left:3px solid var(--accent);border-radius:6px;padding:10px 14px;margin:0 0 10px 0;font-size:12px;line-height:1.6;color:#c0c8e0;}
.mech-flag{background:#1f1a14;border-left:3px solid #fbbf24;border-radius:6px;padding:10px 14px;margin:0 0 10px 0;font-size:12px;color:#f5dfa0;}
.mech-ok{background:#131f18;border-left:3px solid var(--ok);border-radius:6px;padding:10px 14px;margin:0 0 10px 0;font-size:12px;color:#a3e4c3;}
.note{font-size:11px;color:var(--muted);margin-top:8px;font-style:italic;}
</style>
</head>
<body>
""" + section + """
</body>
</html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)



# ── MAIN ──────────────────────────────────────────────────────────────────────
def build_comparison_runs(garmin, dates, debug=False):
    """Resolve a list of date keywords (e.g. ['latest','latest-1y','latest-2y'])
    into fully-processed run dicts (points, metrics, dynamics), ready for
    build_comparison_section(). Factored out so both the CLI entrypoint and
    an embedding caller (e.g. health_dashboard.py, which has its own Garmin
    connection and main() already) can produce the same data without going
    through a second standalone HTML file.

    'latest-Ny' entries don't just shift the date back N years -- they
    search a window around that point for a run matching the LATEST run's
    day-of-week (Monday vs Monday vs Monday, not whatever weekday a literal
    date subtraction happens to land on) and, among same-weekday
    candidates, the closest distance to the latest run (so a 16km long run
    isn't compared against a 5km recovery jog just because they're both
    "a year apart"). Plain dates and 'today'/'today-Ny' are unaffected --
    those still resolve to an exact date via find_run_on_date().
    """
    latest_ny_re = re.compile(r"^latest-(\d+)y$")
    needs_latest = any(d.strip().lower() == "latest" or latest_ny_re.match(d.strip().lower())
                        for d in dates)

    latest_date = None
    latest_weekday = None
    latest_distance_km = None
    if needs_latest:
        print("Resolving 'latest' to your actual most recent run date...")
        latest_date = find_latest_run_date(garmin=garmin)
        print(f"  Latest run found: {latest_date.isoformat()}")
        latest_activity, _ = find_run_on_date(garmin, latest_date)
        if latest_activity:
            latest_weekday = latest_date.weekday()
            latest_distance_km = round(latest_activity.get("distance", 0) / 1000, 1)
            print(f"  Latest run: {latest_distance_km}km on a "
                  f"{latest_date.strftime('%A')} -- comparisons will match both.")

    runs = []
    for i, date_str in enumerate(dates):
        key = date_str.strip().lower()
        m = latest_ny_re.match(key)

        if m and latest_date is not None and latest_distance_km is not None:
            years = int(m.group(1))
            try:
                anchor = latest_date.replace(year=latest_date.year - years)
            except ValueError:
                anchor = latest_date.replace(month=2, day=28, year=latest_date.year - years)
            print(f"\nLooking for a {latest_date.strftime('%A')} near {anchor.isoformat()} "
                  f"(~{latest_distance_km}km, {years}y ago)...")
            activity, actual = find_similar_run_near(garmin, anchor, latest_weekday, latest_distance_km)
        else:
            target = parse_date_arg(date_str, latest_date=latest_date)
            print(f"\nLooking for run on {target.isoformat()}...")
            activity, actual = find_run_on_date(garmin, target)

        if not activity:
            print(f"  ❌ No comparable run found, skipping.")
            continue

        dist_km = round(activity.get("distance", 0) / 1000, 1)
        print(f"  ✅ {actual.isoformat()} — {dist_km}km")
        color = COLORS[i % len(COLORS)]
        label = f"{actual.isoformat()} ({dist_km}km)"

        history_inputs = get_score_history_entry(actual.isoformat())
        resting_hr = history_inputs.get("resting_hr")
        if resting_hr:
            print(f"  Resting HR: {resting_hr} bpm (from score history)")
        else:
            print(f"  Resting HR: not found in score history for {actual.isoformat()}")

        print("  Fetching activity stream...")
        try:
            points = fetch_activity_stream(garmin, activity["activityId"], debug=debug)
        except ValueError as e:
            print(f"  ❌ Stream error: {e}")
            continue

        print(f"  Smoothing ({len(points)} data points)...")
        points = smooth_stream(points)

        print("  Fetching lap dynamics...")
        laps = fetch_lap_dynamics(garmin, activity["activityId"], debug=debug)
        dynamics = analyse_dynamics(laps)
        if dynamics.get("has_dynamics"):
            print(f"  GCT early/late: {dynamics.get('gct_early_ms')}ms → {dynamics.get('gct_late_ms')}ms")
            print(f"  Balance early/late: {dynamics.get('bal_early_pct')}%L → {dynamics.get('bal_late_pct')}%L")
            if dynamics.get("mech_fatigue_flag"):
                print(f"  ⚠️  Mechanical fatigue flagged: {dynamics.get('mech_fatigue_detail')}")
            else:
                print(f"  ✓ Balance stable: {dynamics.get('mech_fatigue_detail')}")
        else:
            print("  ⚠️  No GCT/dynamics data available for this run")

        print("  Detecting breakpoint...")
        bp_idx = detect_breakpoint(points)
        if bp_idx >= len(points):
            print(f"  🟢 Fully controlled")
        else:
            print(f"  🔴 Breakpoint at ~{points[bp_idx].get('dist_km', '?')}km")

        print("  Computing metrics...")
        metrics = compute_metrics(points, bp_idx, activity, resting_hr=resting_hr)

        runs.append({
            "label": label, "color": color,
            "points": points, "bp_idx": bp_idx,
            "metrics": metrics, "dynamics": dynamics,
        })
    return runs


def main():
    parser = argparse.ArgumentParser(description="Physiological + mechanical run analysis.")
    parser.add_argument("dates", nargs="+", help="e.g. today today-1y 2025-06-21")
    parser.add_argument("--out", default="run_model.html")
    parser.add_argument("--debug", action="store_true", help="Dump raw metric keys from Garmin")
    args = parser.parse_args()

    print("Connecting to Garmin...")
    garmin = get_garmin()

    runs = build_comparison_runs(garmin, args.dates, debug=args.debug)

    if not runs:
        sys.exit("No valid run data found.")

    print(f"\nGenerating report ({len(runs)} run(s))...")
    build_html(runs, args.out)
    print(f"Done — open {args.out} in your browser.")


if __name__ == "__main__":
    main()
