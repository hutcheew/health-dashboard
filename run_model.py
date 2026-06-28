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
    "easy_pace_threshold":    6.0,   # min/km — slower than this = recovery run candidate
    "tempo_hr_min":         150,     # bpm — HR below this is unlikely to be a true tempo effort
    # Fatigue onset thresholds -- fixed starting values (not yet personal-
    # baseline-calibrated, same caveat as the original HR thresholds before
    # compute_personal_hr_baseline existed). Worth revisiting once enough
    # runs have been analysed to see whether these trip too often/rarely.
    "onset_gct_pct":    5.0,   # GCT drift from early-run baseline (%)
    "onset_stride_pct": 3.0,   # stride shrinkage from early-run baseline (%)
    "onset_balance_pct": 1.5,  # GCT balance drift from 50/50 or from early baseline (percentage points)
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
      1. Distance similarity to the latest run -- comparing a 5km recovery
         jog against a 16km long run a year apart isn't a meaningful
         comparison, and this matters more than which day it fell on.
      2. Day-of-week as a TIEBREAKER -- only decides between candidates
         whose distances are already close (within ~1km equivalent); it
         does not override a clearly better distance match on a
         different day. (Earlier version had this the other way around --
         flipped per explicit instruction: weekday was overriding distance
         even when the weekday-matched candidate's distance was much worse.)
      3. Date proximity to anchor_date as the final, smallest tiebreaker.
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

    def score(c):
        _, d, dist_km = c
        dist_penalty = abs(dist_km - target_distance_km) if target_distance_km else 0
        # Weekday mismatch costs roughly "1km of distance difference" --
        # enough to decide between near-identical distances, not enough to
        # override a real distance gap (e.g. 5km vs 3km, the case that
        # prompted this flip).
        weekday_penalty = 0 if d.weekday() == target_weekday else 1.0
        date_penalty = abs((d - anchor_date).days) * 0.01
        return dist_penalty + weekday_penalty + date_penalty

    activity, actual_date, dist_km = min(candidates, key=score)
    return activity, actual_date


def compute_ytd_mileage(garmin, through_date):
    """Total running distance from Jan 1 of through_date's year through
    through_date itself (inclusive) -- lets each comparison run show "how
    much had I run by this point" in its own calendar year, so volume
    context carries over alongside the single day's distance.
    """
    start = date(through_date.year, 1, 1).isoformat()
    end = through_date.isoformat()
    activities = garmin.get_activities_by_date(start, end, "running")
    return round(sum(a.get("distance", 0) for a in activities) / 1000, 1)


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
    time_key  = next((k for k in ["sumDuration", "sumElapsedDuration"] if k in idx), None)
    hr_key    = next((k for k in ["directHeartRate", "heartRate"] if k in idx), None)
    spd_key   = next((k for k in ["directGradeAdjustedSpeed", "directSpeed"] if k in idx), None)
    dist_key  = next((k for k in ["sumDistance", "directDistance"] if k in idx), None)
    power_key = next((k for k in ["directPower", "power"] if k in idx), None)

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
        power    = get(power_key)

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
            "power": round(float(power), 1) if power else None,
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
        # Deliberately still broad, not narrowed to specific exception
        # types: this call is inside a per-run loop, and a re-raise here
        # would turn one bad API call into a crash of the whole comparison
        # feature, instead of just skipping this run's dynamics -- same
        # graceful-degradation pattern used elsewhere in this codebase.
        # Including the exception TYPE (not just the message) means a
        # genuine code bug (TypeError/KeyError) is still distinguishable
        # from an expected network hiccup, without re-raising.
        print(f"    ⚠️  Could not fetch lap splits ({type(e).__name__}): {e}")
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

        # Vertical oscillation in mm. Garmin's field naming isn't consistent
        # across metrics (groundContactTime has no avg/average prefix
        # despite being a lap average; cadence below already needs two
        # fallback names for the same reason) -- try both known variants
        # rather than assuming one.
        vert_osc = lap.get("avgVerticalOscillation") or lap.get("verticalOscillation")

        # Stride length in m. Same naming inconsistency as vert_osc above --
        # this was only trying avgStrideLength, but the actual field (per
        # direct --debug output) is strideLength with no prefix, so this
        # has been silently returning None the whole time, same root cause
        # as the vertical oscillation bug.
        stride = lap.get("avgStrideLength") or lap.get("strideLength")

        # Vertical ratio (%) = vertical oscillation / stride length. Better
        # than raw vertical oscillation for cross-comparison since it
        # normalizes for height/stride differences -- a taller runner or a
        # longer stride naturally produces more oscillation without that
        # meaning anything is mechanically wrong.
        vert_ratio = lap.get("verticalRatio")

        # Power (watts) — averagePower and normalizedPower
        avg_power  = lap.get("averagePower")
        norm_power = lap.get("normalizedPower")

        # Power Variability Index = NP / avg_power (1.00 = perfectly steady)
        pvi = round(norm_power / avg_power, 3) if avg_power and norm_power and avg_power > 0 else None

        # Mechanical efficiency: speed (m/min) / power (W)
        # Higher = covering more ground per watt
        avg_speed_mpm = (1000 / lap.get("averageSpeed", 1) / 60) if lap.get("averageSpeed") else None
        mech_eff = round(avg_speed_mpm / avg_power, 4) if avg_speed_mpm and avg_power and avg_power > 0 else None

        # Elevation — needed for hill detection
        elev_gain = lap.get("elevationGain", 0) or 0
        elev_loss = lap.get("elevationLoss", 0) or 0
        avg_grade_pct = round((elev_gain - elev_loss) / dist_m * 100, 2) if dist_m > 0 else None

        result.append({
            "lap": i + 1,
            "dist_km": round(dist_m / 1000, 2),
            "cum_km": round(cum_km, 2),
            "gct_ms": round(gct, 1) if gct else None,
            "gct_balance_left": round(gct_balance_left, 1) if gct_balance_left is not None else None,
            "gct_balance_right": gct_balance_right,
            "cadence": round(cadence, 1) if cadence else None,
            "vert_osc_mm": round(vert_osc, 1) if vert_osc else None,
            "vert_ratio_pct": round(vert_ratio, 2) if vert_ratio else None,
            "stride_m": round(stride, 2) if stride else None,
            "avg_power": round(avg_power, 1) if avg_power else None,
            "norm_power": round(norm_power, 1) if norm_power else None,
            "pvi": pvi,
            "mech_eff": mech_eff,
            "avg_hr": lap.get("averageHR"),
            "max_hr": lap.get("maxHR"),
            "avg_pace": round(1000 / lap.get("averageSpeed", 1) / 60, 2) if lap.get("averageSpeed") else None,
            "duration_sec": round(lap.get("duration", 0) or 0, 1),
            "elev_gain_m": round(elev_gain, 1),
            "elev_loss_m": round(elev_loss, 1),
            "avg_grade_pct": avg_grade_pct,
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
def lap_avg(laps_subset, key):
    """Average a numeric field across a list of laps, ignoring None values.
    Was previously duplicated as a nested function inside both
    detect_fatigue_onset() and analyse_dynamics() -- lifted here since
    both need the exact same logic."""
    vals = [l[key] for l in laps_subset if l.get(key) is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def detect_fatigue_onset(laps):
    """Walk the run km-by-km (not just early-vs-late) to find the FIRST
    point any mechanical signal crosses its fatigue threshold, relative to
    this run's own early-baseline (first third of laps, same window
    analyse_dynamics uses elsewhere). This is the actual point of this
    function: "GCT drifted 11%" only tells you the overall change; this
    tells you it started at km 7.4, which is the more useful coaching
    framing -- mechanics degrading before performance visibly collapses.

    Returns None if nothing crosses any threshold (i.e. mechanically
    controlled the whole way, same spirit as compute_metrics' existing
    "fully_controlled" pace/HR breakpoint result).
    """
    if not laps or len(laps) < 4:
        return None  # not enough laps for a meaningful early baseline + later detection

    n = len(laps)
    third = max(1, n // 3)
    baseline_laps = laps[:third]

    gct_base = lap_avg(baseline_laps, "gct_ms")
    stride_base = lap_avg(baseline_laps, "stride_m")
    bal_base = lap_avg(baseline_laps, "gct_balance_left")

    onsets = []  # (km, signal, detail) for every signal that crosses, at its first crossing km

    if gct_base:
        for lap in laps[third:]:
            gct = lap.get("gct_ms")
            if gct is None:
                continue
            drift_pct = (gct - gct_base) / gct_base * 100
            if drift_pct >= CONFIG["onset_gct_pct"]:
                onsets.append((lap["cum_km"], "GCT", f"+{drift_pct:.1f}% vs early baseline"))
                break

    if stride_base:
        for lap in laps[third:]:
            stride = lap.get("stride_m")
            if stride is None:
                continue
            collapse_pct = (stride_base - stride) / stride_base * 100
            if collapse_pct >= CONFIG["onset_stride_pct"]:
                onsets.append((lap["cum_km"], "Stride", f"-{collapse_pct:.1f}% vs early baseline"))
                break

    if bal_base:
        for lap in laps[third:]:
            bal = lap.get("gct_balance_left")
            if bal is None:
                continue
            drift = abs(bal - bal_base)
            if drift >= CONFIG["onset_balance_pct"]:
                onsets.append((lap["cum_km"], "Balance", f"{drift:.1f}pt drift vs early baseline"))
                break

    if not onsets:
        return None

    onsets.sort(key=lambda o: o[0])  # earliest km first
    first_km = onsets[0][0]
    # Other signals that crossed at or very near the same point are worth
    # reporting too -- a single km-wide window groups "happened together"
    # without over-claiming false precision about which signal is truly first.
    concurrent = [o for o in onsets if abs(o[0] - first_km) < 1.0]
    return {
        "onset_km": first_km,
        "primary_signal": onsets[0][1],
        "primary_detail": onsets[0][2],
        "signals": [{"signal": o[1], "km": o[0], "detail": o[2]} for o in concurrent],
    }


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
    # bal_drift is always defined as drift in LEFT-side loading (matching
    # the comparison table's "(L%)" label). For the injury-relevance
    # narrative specifically, mirror it when INJURY_SIDE is "right" so the
    # message describes the actual INJURED side's loading change -- not
    # unconditionally "left" regardless of which side is configured as
    # injured. (The flag's trigger condition itself was already direction-
    # agnostic via abs(), so only the narrative needed this fix.)
    mech_fatigue_flag = None
    mech_fatigue_detail = None
    if bal_drift is not None:
        injured_side_drift = bal_drift if INJURY_SIDE != "right" else -bal_drift
        drift_abs = abs(injured_side_drift)
        if drift_abs >= CONFIG["gct_balance_drift_flag"]:
            direction = "↑" if injured_side_drift > 0 else "↓"
            side = "more" if injured_side_drift > 0 else "less"
            mech_fatigue_flag = True
            mech_fatigue_detail = (
                f"{INJURY_SIDE.capitalize()} (injured side) loading drifted {direction} {drift_abs:.1f}% "
                f"(L%: {bal_early:.1f}% → {bal_late:.1f}%) — "
                f"body loading {INJURY_SIDE} side {side} when fatigued. "
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

    # Vertical ratio change -- normalizes oscillation for stride length, so
    # it's comparable across different efforts/paces for the same runner
    # in a way raw vertical oscillation isn't.
    vr_early = lap_avg(early, "vert_ratio_pct")
    vr_late  = lap_avg(late,  "vert_ratio_pct")
    vr_change = round(vr_late - vr_early, 2) if vr_early is not None and vr_late is not None else None

    # Stride length drift ("stride collapse %") -- cadence staying steady
    # while stride length shrinks under fatigue is a documented pattern in
    # distance-running fatigue, and specifically relevant here: it's a way
    # the body can maintain pace while reducing per-stride load, which is
    # exactly the kind of compensation worth watching for an Achilles.
    stride_early = lap_avg(early, "stride_m")
    stride_late  = lap_avg(late,  "stride_m")
    stride_collapse_pct = (
        round((stride_early - stride_late) / stride_early * 100, 1)
        if stride_early and stride_late else None
    )

    # Power drift across laps
    power_early = lap_avg(early, "avg_power")
    power_late  = lap_avg(late,  "avg_power")
    power_drift = round(power_late - power_early, 1) if power_early and power_late else None

    # Mechanical efficiency drift (speed/power — lower late = wasting more watts)
    meff_early = lap_avg(early, "mech_eff")
    meff_late  = lap_avg(late,  "mech_eff")
    meff_drift_pct = round((meff_early - meff_late) / meff_early * 100, 1) \
                     if meff_early and meff_late and meff_early > 0 else None

    # PVI summary (average across laps — closer to 1.0 = steadier effort)
    pvi_vals = [l["pvi"] for l in laps if l.get("pvi") is not None]
    avg_pvi = round(sum(pvi_vals) / len(pvi_vals), 3) if pvi_vals else None

    # ── FATIGUE RESISTANCE SCORE (0-100) ─────────────────────────────────────
    # Composite score answering: "how well did mechanics hold up under fatigue?"
    # Higher = more fatigue-resistant.
    #
    # Weights (from the blueprint):
    #   40% aerobic decoupling (passed in from metrics, or estimated from HR drift)
    #   30% GCT drift
    #   20% stride collapse
    #   10% HR drift (within seg A vs overall — proxy without needing full metrics)
    #
    # Since analyse_dynamics doesn't have access to the stream-based decoupling
    # computed in compute_metrics, we estimate cardiovascular drift from lap HR
    # and use it as a proxy for the decoupling component.
    frs = None
    frs_components = {}

    hr_early_lap = lap_avg(early, "avg_hr")
    hr_late_lap  = lap_avg(late,  "avg_hr")
    hr_drift_pct = abs(round((hr_late_lap - hr_early_lap) / hr_early_lap * 100, 1)) \
                   if hr_early_lap and hr_late_lap and hr_early_lap > 0 else None

    if gct_drift is not None or stride_collapse_pct is not None or hr_drift_pct is not None:
        # Convert each metric to a 0-100 penalty (0 = perfect, 100 = terrible)
        # then invert so FRS 100 = no fatigue, 0 = complete collapse

        # GCT drift penalty: 0ms = 0, 30ms+ = 100
        gct_penalty = min(100, max(0, (gct_drift or 0) / 30 * 100)) if gct_drift is not None else 50

        # Stride collapse penalty: 0% = 0, 5%+ = 100
        stride_penalty = min(100, max(0, (stride_collapse_pct or 0) / 5 * 100)) if stride_collapse_pct is not None else 50

        # HR drift penalty: 0% = 0, 8%+ = 100
        hr_penalty = min(100, max(0, (hr_drift_pct or 0) / 8 * 100)) if hr_drift_pct is not None else 50

        # Balance drift penalty: 0% = 0, 4%+ = 100
        bal_penalty = min(100, max(0, abs(bal_drift or 0) / 4 * 100)) if bal_drift is not None else 50

        # Weighted penalty (lower weights where we're using proxies)
        n_components = sum(1 for x in [gct_drift, stride_collapse_pct, hr_drift_pct, bal_drift] if x is not None)
        if n_components >= 2:
            weighted = (
                gct_penalty     * 0.30 +
                stride_penalty  * 0.20 +
                hr_penalty      * 0.40 +
                bal_penalty     * 0.10
            )
            frs = round(100 - weighted)
            frs_components = {
                "gct_component":     round(100 - gct_penalty),
                "stride_component":  round(100 - stride_penalty),
                "hr_component":      round(100 - hr_penalty),
                "balance_component": round(100 - bal_penalty),
            }

    # Mechanical Fatigue Curve — the km-by-km table the blueprint describes
    # Formatted as a list of dicts for charting and the summary table
    fatigue_curve = []
    for lap in laps:
        fatigue_curve.append({
            "km":      lap["cum_km"],
            "gct":     lap.get("gct_ms"),
            "balance": lap.get("gct_balance_left"),
            "stride":  lap.get("stride_m"),
            "cadence": lap.get("cadence"),
            "hr":      lap.get("avg_hr"),
            "pace":    lap.get("avg_pace"),
            "power":   lap.get("avg_power"),
        })

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
        "vert_ratio_early_pct": vr_early,
        "vert_ratio_late_pct":  vr_late,
        "vert_ratio_change_pct": vr_change,
        "stride_early_m": stride_early,
        "stride_late_m":  stride_late,
        "stride_collapse_pct": stride_collapse_pct,
        "power_early": power_early,
        "power_late":  power_late,
        "power_drift": power_drift,
        "meff_early": meff_early,
        "meff_late":  meff_late,
        "meff_drift_pct": meff_drift_pct,
        "avg_pvi": avg_pvi,
        "hr_drift_pct": hr_drift_pct,
        "frs": frs,
        "frs_components": frs_components,
        "fatigue_curve": fatigue_curve,
        "fatigue_onset": detect_fatigue_onset(laps),
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

    # Aerobic EI: speed (m/min) / HR — "is cardiovascular system improving?"
    def aero_ei(lst):
        vals = [1000 / p["pace_smooth"] / p["hr_smooth"]
                for p in lst if p.get("pace_smooth") and p.get("hr_smooth")
                and p["pace_smooth"] > 0 and p["hr_smooth"] > 0]
        return round(sum(vals) / len(vals), 3) if vals else None

    # Mechanical EI: speed (m/min) / power (W) — "am I producing speed economically?"
    def mech_ei(lst):
        vals = [1000 / p["pace_smooth"] / p["power"]
                for p in lst if p.get("pace_smooth") and p.get("power")
                and p["pace_smooth"] > 0 and p["power"] > 0]
        return round(sum(vals) / len(vals), 4) if vals else None

    hr_a   = avg(seg_a, "hr_smooth")
    hr_b   = avg(seg_b, "hr_smooth")
    pace_a = avg(seg_a, "pace_smooth")
    pace_b = avg(seg_b, "pace_smooth")

    # Aerobic EI (replaces old single EI)
    aei_a = aero_ei(seg_a)
    aei_b = aero_ei(seg_b)

    # Mechanical EI (new — needs power in stream)
    mei_a = mech_ei(seg_a)
    mei_b = mech_ei(seg_b)

    # Aerobic decoupling (first half vs second half, same as before)
    mid = len(total) // 2
    aei_first  = aero_ei(total[:mid])
    aei_second = aero_ei(total[mid:])
    aero_decoupling = round((aei_first - aei_second) / aei_first * 100, 1) \
                      if aei_first and aei_second and aei_first > 0 else None

    # Mechanical decoupling (mechanical EI drift — "am I wasting more watts as I tire?")
    mei_first  = mech_ei(total[:mid])
    mei_second = mech_ei(total[mid:])
    mech_decoupling = round((mei_first - mei_second) / mei_first * 100, 1) \
                      if mei_first and mei_second and mei_first > 0 else None

    # Run-level Power Variability Index from stream
    # NP ≈ 4th-root mean of 4th-powers of 30s rolling average power
    # This is a simplified version — proper NP needs 30s rolling avg
    power_vals = [p["power"] for p in total if p.get("power") and p["power"] > 0]
    avg_power_run = round(sum(power_vals) / len(power_vals), 1) if power_vals else None
    if len(power_vals) >= 30:
        # 30s rolling 4th-power mean
        fourth_powers = [v**4 for v in power_vals]
        rolling_4th = []
        for i in range(len(fourth_powers)):
            window = fourth_powers[max(0, i-29):i+1]
            rolling_4th.append(sum(window) / len(window))
        norm_power_run = round((sum(rolling_4th) / len(rolling_4th)) ** 0.25, 1)
        pvi_run = round(norm_power_run / avg_power_run, 3) if avg_power_run else None
    else:
        norm_power_run = None
        pvi_run = None

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
        # Aerobic EI (was just ei_a/ei_b)
        "ei_a": aei_a, "ei_b": aei_b,
        "decoupling": aero_decoupling,
        # Mechanical EI (new)
        "mei_a": mei_a, "mei_b": mei_b,
        "mech_decoupling": mech_decoupling,
        # Power (new)
        "avg_power": avg_power_run,
        "norm_power": norm_power_run,
        "pvi": pvi_run,
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
    """Add dist_pct (0-100% of total distance) to each point IN-PLACE.
    Mutates the list and returns nothing -- the only call site never used
    the return value, so returning `points` was misleading (made this
    look like a pure function when it's actually a side-effecting one)."""
    total = points[-1].get("dist_km") if points else None
    if not total:
        return
    for p in points:
        p["dist_pct"] = round((p.get("dist_km") or 0) / total * 100, 1)


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
def _fatigue_curve_header(runs):
    """Two-row header: km spans both rows; each run gets its own labeled
    group of 6 metric columns via colspan, instead of one flat header row
    that only matched the first run's columns (and was off by one even
    for that run, due to the per-row run-label cell with no header)."""
    metric_labels = ["GCT (ms)", "Balance (L%)", "Stride (m)", "Cadence", "HR", "Power (W)"]
    top = "<th rowspan=\"2\">km</th>" + "".join(
        f"<th colspan=\"6\">{r['label'][:16]}</th>" for r in runs
    )
    bottom = "".join(f"<th>{lbl}</th>" for _ in runs for lbl in metric_labels)
    return f"<tr>{top}</tr><tr>{bottom}</tr>"


def _fatigue_curve_rows(runs):
    """Build HTML rows for the mechanical fatigue curve table, one row per
    km. No per-row run-label cell -- that's now conveyed by the header's
    colspan grouping instead, which is also what fixes the column count
    actually matching the header."""

    def km_bucket(raw_km):
        # Round to the nearest whole km for cross-run grouping. cum_km is
        # already round(..., 2) at the source, so within a single run this
        # isn't float accumulation noise -- the real issue is auto-lap
        # boundaries landing at different cumulative distances ACROSS
        # different runs/days (e.g. 1.01km vs 0.99km, both "lap 1"
        # conceptually). Deduping/matching on the raw value would
        # splinter that into near-duplicate rows, one per run, instead of
        # one shared row with every run's data side by side.
        return round(raw_km)

    all_kms = sorted(set(
        km_bucket(c["km"])
        for r in runs
        for c in r["dynamics"].get("fatigue_curve", [])
    ))
    rows = []
    for lap_km in all_kms:
        cells = f"<td style='color:var(--muted,#8b949e)'>{lap_km}</td>"
        for r in runs:
            curve = r["dynamics"].get("fatigue_curve", [])
            row_data = next((c for c in curve if km_bucket(c["km"]) == lap_km), {})
            for k in ["gct", "balance", "stride", "cadence", "hr", "power"]:
                v = row_data.get(k)
                cells += f"<td>{v if v is not None else '--'}</td>"
        rows.append(f"<tr>{cells}</tr>")
    return "\n".join(rows)


def classify_workout(points, laps, metrics, activity):
    """
    Multi-signal workout classification engine.

    Takes the same data already computed for every run and returns:
      type:       one of EASY | RECOVERY | TEMPO | LONG_RUN | INTERVAL |
                         HILL_REPEAT | PROGRESSION | FARTLEK | UNKNOWN
      confidence: 0.0 – 1.0
      reasons:    list of plain-English strings explaining the decision
      emoji:      single emoji for quick display

    Design principles:
    - Uses HR *and* power *and* elevation and pace variability so e.g.
      a marathon pace run (high steady power, low variability) isn't
      confused with an interval session.
    - Rules are ordered from most-specific to least-specific so the
      classifier commits early on strong signals and only falls through
      to generic types when nothing distinctive is found.
    - Every rule produces an explicit confidence value and reason strings
      so the dashboard can explain *why* it classified the run.
    """
    if not points or not laps:
        return {"type": "UNKNOWN", "label": "Unclassified", "confidence": 0.0, "reasons": ["Insufficient data"], "emoji": "❓", "signals": {}}

    reasons = []
    evidence_for = {}  # type -> list of (weight, reason)

    def vote(wtype, weight, reason):
        evidence_for.setdefault(wtype, []).append((weight, reason))

    # ── EXTRACT FEATURES ─────────────────────────────────────────────────────

    total_duration_min = points[-1]["t"] / 60 if points else 0
    total_dist_km = metrics.get("total_dist_km") or 0

    # HR features
    hr_vals = [p["hr_smooth"] for p in points if p.get("hr_smooth")]
    avg_hr    = sum(hr_vals) / len(hr_vals) if hr_vals else 0
    hr_std    = (sum((h - avg_hr)**2 for h in hr_vals) / len(hr_vals))**0.5 if hr_vals else 0
    hr_cv     = hr_std / avg_hr if avg_hr > 0 else 0  # coefficient of variation

    # HR zone distribution
    # Using absolute HR anchors based on your empirical data (185 logged runs):
    #   Easy runs avg HR ~142, marathon HR ~166-167, threshold ~150-155
    # These anchor-based zones are more reliable than %max_hr since your
    # actual max during races is 166-167, not 190 (the 190 CONFIG value is
    # a conservative ceiling for HR reserve calculation, not a zone boundary)
    max_hr = CONFIG["max_hr"]
    Z1_MAX = 130  # very easy / recovery
    Z2_MAX = 148  # easy aerobic (your typical easy run sits in this range)
    Z3_MAX = 158  # moderate / marathon pace
    Z4_MAX = 168  # threshold / hard
    # Z5 = above 168

    z1_pct = sum(1 for h in hr_vals if h < Z1_MAX)         / len(hr_vals) * 100 if hr_vals else 0
    z2_pct = sum(1 for h in hr_vals if Z1_MAX <= h < Z2_MAX) / len(hr_vals) * 100 if hr_vals else 0
    z3_pct = sum(1 for h in hr_vals if Z2_MAX <= h < Z3_MAX) / len(hr_vals) * 100 if hr_vals else 0
    z4_pct = sum(1 for h in hr_vals if Z3_MAX <= h < Z4_MAX) / len(hr_vals) * 100 if hr_vals else 0
    z5_pct = sum(1 for h in hr_vals if h >= Z4_MAX)          / len(hr_vals) * 100 if hr_vals else 0
    low_hr_pct = z1_pct + z2_pct  # Z1+Z2 = below aerobic threshold

    # Pace features
    pace_vals = [p["pace_smooth"] for p in points if p.get("pace_smooth") and p["pace_smooth"] < 12]
    avg_pace  = sum(pace_vals) / len(pace_vals) if pace_vals else 0
    pace_std  = (sum((p - avg_pace)**2 for p in pace_vals) / len(pace_vals))**0.5 if pace_vals else 0
    pace_cv   = pace_std / avg_pace if avg_pace > 0 else 0

    # First half vs second half (for progression detection)
    mid = len(points) // 2
    first_half_pace  = sum(p["pace_smooth"] for p in points[:mid] if p.get("pace_smooth")) / max(mid, 1)
    second_half_pace = sum(p["pace_smooth"] for p in points[mid:] if p.get("pace_smooth")) / max(mid, 1)

    # Power features (from activity summary if lap data has it)
    power_vals = [p["power"] for p in points if p.get("power") and p["power"] > 0]
    avg_power_run = sum(power_vals) / len(power_vals) if power_vals else None
    pvi_run = metrics.get("pvi")
    np_ap_ratio = pvi_run  # same thing

    # Elevation features (from laps)
    total_elev_gain = sum(l.get("elev_gain_m", 0) for l in laps)
    elev_per_km = total_elev_gain / total_dist_km if total_dist_km > 0 else 0

    # Cadence variability
    cad_vals = [l["cadence"] for l in laps if l.get("cadence")]
    cad_std = (sum((c - sum(cad_vals)/len(cad_vals))**2 for c in cad_vals) / len(cad_vals))**0.5 if len(cad_vals) > 1 else 0

    # HR decoupling (drift) — from existing metrics
    hr_drift = metrics.get("decoupling") or 0

    # ── INTERVAL DETECTION ───────────────────────────────────────────────────
    # Find "work blocks": sustained periods where HR or power spikes
    # significantly above the run average, separated by recoveries
    HIGH_HR_THRESH  = avg_hr + hr_std * 0.7
    LOW_HR_THRESH   = avg_hr - hr_std * 0.5
    MIN_BLOCK_SEC   = 90
    MIN_RECOV_SEC   = 30

    high_blocks  = []
    recov_blocks = []
    in_high = False
    block_start = None

    for i, p in enumerate(points):
        hr = p.get("hr_smooth")
        if hr is None:
            continue
        if hr >= HIGH_HR_THRESH and not in_high:
            in_high = True
            block_start = p["t"]
        elif hr < LOW_HR_THRESH and in_high:
            duration = p["t"] - block_start
            if duration >= MIN_BLOCK_SEC:
                high_blocks.append({"start": block_start, "end": p["t"], "duration": duration})
            in_high = False
            block_start = p["t"]
    if in_high and block_start:
        duration = points[-1]["t"] - block_start
        if duration >= MIN_BLOCK_SEC:
            high_blocks.append({"start": block_start, "end": points[-1]["t"], "duration": duration})

    n_intervals = len(high_blocks)
    avg_interval_sec = sum(b["duration"] for b in high_blocks) / n_intervals if n_intervals > 0 else 0

    # Check for recovery drops between high blocks
    has_recoveries = False
    if len(high_blocks) >= 2:
        for j in range(len(high_blocks) - 1):
            gap = high_blocks[j+1]["start"] - high_blocks[j]["end"]
            if gap >= MIN_RECOV_SEC:
                has_recoveries = True
                break

    # ── HILL REPEAT DETECTION ────────────────────────────────────────────────
    # High-grade laps + effort spike + recovery pattern
    steep_laps = [l for l in laps if abs(l.get("avg_grade_pct") or 0) >= 3.0
                  and (l.get("duration_sec") or 0) < 180]
    hill_repeats_detected = len(steep_laps) >= 3 and has_recoveries

    # ── APPLY DECISION LOGIC (ordered most-specific → least-specific) ─────────

    # 1. Hill Repeats — most distinctive pattern
    if hill_repeats_detected:
        vote("HILL_REPEAT", 1.0, f"{len(steep_laps)} steep laps (≥3% grade, <3min) with recovery periods")
        if elev_per_km > 15:
            vote("HILL_REPEAT", 0.5, f"High elevation: {round(elev_per_km)}m gain/km")
        if n_intervals >= 3:
            vote("HILL_REPEAT", 0.4, f"{n_intervals} HR spikes matching repeat structure")

    # 2. Intervals — repeated hard blocks with clear recoveries
    if n_intervals >= 3 and has_recoveries and not hill_repeats_detected:
        vote("INTERVAL", 1.0, f"{n_intervals} high-intensity blocks detected")
        if avg_interval_sec > 0:
            vote("INTERVAL", 0.5, f"Avg work duration: {round(avg_interval_sec)}s")
        if np_ap_ratio and np_ap_ratio > 1.08:
            vote("INTERVAL", 0.4, f"NP/AP ratio {np_ap_ratio:.3f} — variable effort confirmed")
        if z4_pct + z5_pct > 30:
            vote("INTERVAL", 0.4, f"{round(z4_pct+z5_pct)}% of run in Z4-Z5")

    # 3. Tempo — high intensity, very steady, continuous
    is_steady = (np_ap_ratio or 0) < 1.06 and pace_cv < 0.04
    is_high_intensity = (z3_pct + z4_pct) > 40 and avg_hr >= CONFIG["tempo_hr_min"]
    if is_high_intensity and is_steady and n_intervals < 3 and total_duration_min > 15:
        vote("TEMPO", 1.0, f"{round(z3_pct+z4_pct)}% of run in Z3-Z4 with steady output")
        if np_ap_ratio:
            vote("TEMPO", 0.5, f"NP/AP {np_ap_ratio:.3f} — very controlled pacing")
        if total_duration_min > 25:
            vote("TEMPO", 0.3, f"Continuous {round(total_duration_min)}min at threshold")

    # 4. Progression — second half meaningfully faster
    if first_half_pace > 0 and second_half_pace > 0:
        speed_ratio = first_half_pace / second_half_pace  # >1 means 2nd half faster
        if speed_ratio >= 1.05:
            vote("PROGRESSION", 1.0, f"Second half {round((speed_ratio-1)*100)}% faster than first")
            if (z3_pct + z4_pct) < 50:
                vote("PROGRESSION", 0.4, "HR remained controlled throughout")

    # 5. Long Run — duration + steady aerobic effort
    if total_duration_min >= 75:
        vote("LONG_RUN", 1.0, f"Duration {round(total_duration_min)}min")
        if np_ap_ratio and np_ap_ratio < 1.08:
            vote("LONG_RUN", 0.5, f"NP/AP {np_ap_ratio:.3f} — aerobically steady")
        if abs(hr_drift) < 8:
            vote("LONG_RUN", 0.4, f"HR drift {hr_drift}% — good aerobic durability")

    # 6. Recovery Run — HR below aerobic threshold, slow pace
    is_very_low_intensity = low_hr_pct > 70 and avg_hr < 135
    is_slow = avg_pace > CONFIG["easy_pace_threshold"]
    if is_very_low_intensity and is_slow and total_dist_km < 12:
        vote("RECOVERY", 1.0, f"{round(low_hr_pct)}% of run below Z2, avg HR {round(avg_hr)} bpm")
        vote("RECOVERY", 0.5, f"Avg pace {fmt_pace(avg_pace)} — slower than normal easy")

    # 7. Easy Run — below threshold, steady, not recovery
    is_low_intensity = low_hr_pct > 55 and avg_hr < max_hr * 0.75
    if is_low_intensity and is_steady and not is_very_low_intensity:
        vote("EASY", 1.0, f"{round(low_hr_pct)}% of run in Z1-Z2")
        if np_ap_ratio and np_ap_ratio < 1.06:
            vote("EASY", 0.5, f"NP/AP {np_ap_ratio:.3f} — aerobic base work")

    # 8. Fartlek — high variability but not structured enough for intervals
    if n_intervals >= 2 and not has_recoveries and hr_cv > 0.06:
        vote("FARTLEK", 0.8, f"Variable HR (CV={hr_cv:.2f}) without clear structured recovery")
        if pace_cv > 0.06:
            vote("FARTLEK", 0.4, f"Variable pace (CV={pace_cv:.2f}) — unstructured surges")

    # ── PICK WINNER ───────────────────────────────────────────────────────────
    if not evidence_for:
        return {"type": "UNKNOWN", "label": "Unclassified", "confidence": 0.0, "reasons": ["No clear pattern detected"], "emoji": "❓", "signals": {}}

    # Score each type: sum of weights
    scores = {t: sum(w for w, _ in ev) for t, ev in evidence_for.items()}
    best_type = max(scores, key=scores.get)
    total_weight = scores[best_type]

    # Confidence: normalise against a "perfect" score of ~2.0
    # (a type that hits all its signals gets ~2.0 combined weight)
    raw_confidence = min(0.98, total_weight / 2.2)

    # Penalise if another type is close (ambiguous run)
    sorted_scores = sorted(scores.values(), reverse=True)
    if len(sorted_scores) >= 2 and sorted_scores[1] / sorted_scores[0] > 0.65:
        raw_confidence *= 0.85  # ambiguous signal
        reasons.append(f"Note: also shows characteristics of {sorted(scores, key=scores.get, reverse=True)[1]}")

    type_reasons = [r for _, r in evidence_for[best_type]]
    type_reasons.extend(reasons)

    EMOJIS = {
        "EASY":        "🟢",
        "RECOVERY":    "🔵",
        "TEMPO":       "🟡",
        "LONG_RUN":    "🟣",
        "INTERVAL":    "🔴",
        "HILL_REPEAT": "⛰️",
        "PROGRESSION": "📈",
        "FARTLEK":     "🌀",
        "UNKNOWN":     "❓",
    }
    LABELS = {
        "EASY":        "Easy Run",
        "RECOVERY":    "Recovery Run",
        "TEMPO":       "Tempo / Threshold",
        "LONG_RUN":    "Long Run",
        "INTERVAL":    "Interval Session",
        "HILL_REPEAT": "Hill Repeats",
        "PROGRESSION": "Progression Run",
        "FARTLEK":     "Fartlek",
        "UNKNOWN":     "Unclassified",
    }

    return {
        "type":       best_type,
        "label":      LABELS[best_type],
        "confidence": round(raw_confidence, 2),
        "reasons":    type_reasons[:4],  # cap at 4 for display
        "emoji":      EMOJIS[best_type],
        "signals": {
            "avg_hr":       round(avg_hr, 1),
            "low_hr_pct":   round(low_hr_pct, 1),
            "z4_z5_pct":    round(z4_pct + z5_pct, 1),
            "pace_cv":      round(pace_cv, 3),
            "hr_cv":        round(hr_cv, 3),
            "np_ap_ratio":  np_ap_ratio,
            "n_intervals":  n_intervals,
            "total_elev_m": round(total_elev_gain, 1),
            "duration_min": round(total_duration_min, 1),
        },
    }


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
<div id="rc-chart-modal">
  <div class="rc-modal-inner">
    <button class="rc-modal-close" aria-label="Close">&times;</button>
    <canvas id="rc-chart-modal-canvas"></canvas>
  </div>
</div>
<h1>Run Model — Physiology + Mechanics</h1>
<div class="sub">HR threshold: {threshold} bpm · Max HR: {mhr} bpm · GCT balance drift flag: ±{CONFIG["gct_balance_drift_flag"]}% · Injury side: {INJURY_SIDE}</div>

<!-- WORKOUT CLASSIFICATION -->
<div class="card" style="margin-bottom:14px">
  <h2>Workout Classification</h2>
  <div style="display:grid;grid-template-columns:{"1fr ".join([""] * (len(runs) + 1)).strip()};gap:12px">
    {"".join(f'''
    <div style="background:var(--surface2,#1f2630);border-radius:8px;padding:14px">
      <div style="font-size:10px;color:var(--muted,#8b949e);margin-bottom:6px;text-transform:uppercase;letter-spacing:.05em">{r["label"]}</div>
      <div style="font-size:22px;margin-bottom:4px">{r.get("classification", {}).get("emoji","❓")} <span style="font-size:16px;font-weight:600;color:{r["color"]}">{r.get("classification", {}).get("label","Unknown")}</span></div>
      <div style="font-size:11px;color:var(--muted,#8b949e);margin-bottom:8px">Confidence: <strong style="color:{"#34d399" if (r.get("classification",{}).get("confidence",0) or 0) >= 0.80 else "#fbbf24" if (r.get("classification",{}).get("confidence",0) or 0) >= 0.60 else "#f78166"}">{round((r.get("classification",{}).get("confidence",0) or 0)*100)}%</strong></div>
      <ul style="font-size:11px;color:var(--muted,#8b949e);padding-left:14px;line-height:1.7;margin:0">
        {"".join(f"<li>{reason}</li>" for reason in (r.get("classification",{}).get("reasons",[]) or [])[:4])}
      </ul>
    </div>''' for r in runs)}
  </div>
</div>

<!-- MECHANICAL FATIGUE FLAGS -->
{"".join(f'''
<div class="{"mech-flag" if r["dynamics"].get("mech_fatigue_flag") else "mech-ok" if r["dynamics"].get("mech_fatigue_flag") is False else ""}">
  <strong style="color:{"#fbbf24" if r["dynamics"].get("mech_fatigue_flag") else "#34d399"}">{r["label"]}</strong>
  {"⚠️ Mechanical fatigue flagged: " if r["dynamics"].get("mech_fatigue_flag") else "✓ "}
  {r["dynamics"].get("mech_fatigue_detail") or "No GCT balance data available for this run."}
</div>''' for r in runs if r["dynamics"].get("mech_fatigue_detail") is not None)}

<!-- FATIGUE ONSET -->
{"".join(f'''
<div class="mech-flag">
  <strong style="color:#fbbf24">{r["label"]}</strong>
  ⚠️ Fatigue started at km {r["dynamics"]["fatigue_onset"]["onset_km"]} —
  {(" + ".join(s["signal"] for s in r["dynamics"]["fatigue_onset"]["signals"]))}
  ({r["dynamics"]["fatigue_onset"]["primary_detail"]})
</div>''' for r in runs if r["dynamics"].get("fatigue_onset"))}
{"".join(f'''
<div class="mech-ok">
  <strong style="color:#34d399">{r["label"]}</strong>
  ✓ Mechanically controlled the whole way — no signal crossed its fatigue threshold.
</div>''' for r in runs if r["dynamics"].get("has_dynamics") and not r["dynamics"].get("fatigue_onset"))}

<!-- FATIGUE RESISTANCE SCORES -->
{"".join(f'''
<div class="card" style="margin-bottom:12px">
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
    <div>
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--text2,#8b949e);margin-bottom:4px">Fatigue Resistance — {r["label"]}</div>
      <div style="font-size:28px;font-weight:700;color:{"#34d399" if (r["dynamics"].get("frs") or 0) >= 70 else "#fbbf24" if (r["dynamics"].get("frs") or 0) >= 45 else "#f78166"}">{r["dynamics"].get("frs") or "--"}<span style="font-size:14px;color:var(--text2,#8b949e)">/100</span></div>
      <div style="font-size:11px;color:var(--text2,#8b949e);margin-top:2px">{"🟢 Good — mechanics held up" if (r["dynamics"].get("frs") or 0) >= 70 else "🟡 Moderate — some drift detected" if (r["dynamics"].get("frs") or 0) >= 45 else "🔴 High fatigue — significant mechanical breakdown"}</div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 16px;font-size:11px;color:var(--text2,#8b949e)">
      <div>GCT <span style="color:var(--text,#e6edf3)">{r["dynamics"]["frs_components"].get("gct_component","--")}/100</span></div>
      <div>Stride <span style="color:var(--text,#e6edf3)">{r["dynamics"]["frs_components"].get("stride_component","--")}/100</span></div>
      <div>HR <span style="color:var(--text,#e6edf3)">{r["dynamics"]["frs_components"].get("hr_component","--")}/100</span></div>
      <div>Balance <span style="color:var(--text,#e6edf3)">{r["dynamics"]["frs_components"].get("balance_component","--")}/100</span></div>
    </div>
  </div>
</div>''' for r in runs if r["dynamics"].get("frs") is not None)}

<!-- METRICS TABLE -->
<div class="card">
  <h2>Performance + Physiology Matrix</h2>
  <table>
    <thead><tr><th>Metric</th>{headers}</tr></thead>
    <tbody>
      <tr class="rc-section-header"><td colspan="{1 + len(runs)}">Context</td></tr>
      {row("Resting HR (morning)", lambda r: f"{r['metrics']['resting_hr']} bpm" if r['metrics'].get('resting_hr') else "--")}
      {row("HR reserve at breakpoint", lambda r: f"{r['metrics']['hr_reserve_pct']}% of max" if r['metrics'].get('hr_reserve_pct') else "--")}
      {row("YTD mileage (same point in year)", lambda r: f"{r['ytd_km']}km" if r.get('ytd_km') is not None else "--")}
      <tr class="rc-section-header"><td colspan="{1 + len(runs)}">Physiology</td></tr>
      {row("Total distance", lambda r: f"{r['metrics']['total_dist_km']}km" if r['metrics'].get('total_dist_km') else "--")}
      {row("Breakpoint", lambda r: f"🔴 {r['metrics']['bp_dist_km']}km ({r['metrics']['fatigue_onset_pct']}%)" if not r['metrics']['fully_controlled'] else "🟢 Fully controlled")}
      {row("Avg pace (A — controlled)", lambda r: fmt_pace(r['metrics'].get('pace_a')))}
      {row("Avg pace (B — fatigue)",    lambda r: fmt_pace(r['metrics'].get('pace_b')))}
      {row("Avg HR (A — controlled)",   lambda r: f"{r['metrics']['hr_a']} bpm" if r['metrics'].get('hr_a') else "--")}
      {row("Avg HR (B — fatigue)",      lambda r: f"{r['metrics']['hr_b']} bpm" if r['metrics'].get('hr_b') else "--")}
      {row("HR rise (B−A)",             lambda r: f"+{r['metrics']['hr_rise']} bpm" if r['metrics'].get('hr_rise') else "--")}
      <tr class="rc-section-header"><td colspan="{1 + len(runs)}">Aerobic Efficiency (speed / HR)</td></tr>
      {row("Aerobic EI — Seg A",        lambda r: str(r['metrics']['ei_a']) if r['metrics'].get('ei_a') else "--")}
      {row("Aerobic EI — Seg B",        lambda r: str(r['metrics']['ei_b']) if r['metrics'].get('ei_b') else "--")}
      {row("Aero decoupling",           lambda r: f"{r['metrics']['decoupling']}%" if r['metrics'].get('decoupling') is not None else "--")}
      <tr class="rc-section-header"><td colspan="{1 + len(runs)}">Mechanical Efficiency (speed / power)</td></tr>
      {row("Mechanical EI — Seg A",     lambda r: str(r['metrics']['mei_a']) if r['metrics'].get('mei_a') else "--")}
      {row("Mechanical EI — Seg B",     lambda r: str(r['metrics']['mei_b']) if r['metrics'].get('mei_b') else "--")}
      {row("Mech decoupling",           lambda r: f"{r['metrics']['mech_decoupling']}%" if r['metrics'].get('mech_decoupling') is not None else "--")}
      {row("Avg power",                 lambda r: f"{r['metrics']['avg_power']}W" if r['metrics'].get('avg_power') else "--")}
      {row("Norm power",                lambda r: f"{r['metrics']['norm_power']}W" if r['metrics'].get('norm_power') else "--")}
      {row("Power Variability Index",   lambda r: f"{r['metrics']['pvi']} {'✓ steady' if r['metrics'].get('pvi') and r['metrics']['pvi'] <= 1.05 else '↑ variable'}" if r['metrics'].get('pvi') else "--")}
      <tr class="rc-section-header"><td colspan="{1 + len(runs)}">Running Mechanics</td></tr>
      {row("GCT — early (ms)",          lambda r: str(r['dynamics'].get('gct_early_ms') or '--'))}
      {row("GCT — late (ms)",           lambda r: str(r['dynamics'].get('gct_late_ms') or '--'))}
      {row("GCT drift",                 lambda r: f"{r['dynamics']['gct_drift_ms']:+.1f}ms" if r['dynamics'].get('gct_drift_ms') is not None else "--")}
      {row("Balance — early (L%)",      lambda r: f"{r['dynamics'].get('bal_early_pct')}%L" if r['dynamics'].get('bal_early_pct') is not None else "--")}
      {row("Balance — late (L%)",       lambda r: f"{r['dynamics'].get('bal_late_pct')}%L" if r['dynamics'].get('bal_late_pct') is not None else "--")}
      {row("Balance drift",             lambda r: f"{r['dynamics']['bal_drift_pct']:+.1f}%" if r['dynamics'].get('bal_drift_pct') is not None else "--")}
      {row("Stride — early (m)",        lambda r: str(r['dynamics'].get('stride_early_m') or '--'))}
      {row("Stride — late (m)",         lambda r: str(r['dynamics'].get('stride_late_m') or '--'))}
      {row("Stride collapse",           lambda r: f"{r['dynamics']['stride_collapse_pct']:+.1f}%" if r['dynamics'].get('stride_collapse_pct') is not None else "--")}
      {row("Cadence drop",              lambda r: f"{r['dynamics']['cadence_drop']:+.1f} spm" if r['dynamics'].get('cadence_drop') is not None else "--")}
      {row("Vert. ratio change",        lambda r: f"{r['dynamics']['vert_ratio_change_pct']:+.2f}%" if r['dynamics'].get('vert_ratio_change_pct') is not None else "--")}
      {row("HR crossings",              lambda r: str(r['metrics']['crossings']))}
    </tbody>
  </table>
  <p class="note">Aerobic EI = speed/HR (cardiovascular). Mechanical EI = speed/power (economy). PVI 1.00–1.05 = steady pacing. FRS = Fatigue Resistance Score (GCT 30%, stride 20%, HR 40%, balance 10%). GCT balance: 50% = symmetry. Left = Achilles side.</p>
</div>

<!-- MECHANICAL FATIGUE CURVE -->
{"" if not any(r["dynamics"].get("fatigue_curve") for r in runs) else f'''
<div class="card" style="margin-bottom:12px;overflow-x:auto">
  <h2>Mechanical Fatigue Curve — km by km</h2>
  <table style="font-size:11px;min-width:500px">
    <thead>
      {_fatigue_curve_header(runs)}
    </thead>
    <tbody>
      {_fatigue_curve_rows(runs)}
    </tbody>
  </table>
  <p class="note" style="margin-top:8px">Rising GCT + falling stride = mechanical fatigue. Balance drifting from 50% = side compensation.</p>
</div>
'''}

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

// Tap-to-expand: every chart in this section opens larger in a modal when
// tapped, rather than staying cramped at its small inline size -- mainly
// for narrow phone screens, where the inline 2-column charts especially
// can be hard to read precisely.
const modal = document.getElementById('rc-chart-modal');
const modalCanvas = document.getElementById('rc-chart-modal-canvas');
function rcOpenChart(canvasId) {{
  const original = Chart.getChart(canvasId);
  if (!original || !modal || !modalCanvas) return;
  const existingModalChart = Chart.getChart(modalCanvas);
  if (existingModalChart) existingModalChart.destroy();
  new Chart(modalCanvas, {{
    type: original.config.type,
    data: original.config.data,
    options: Object.assign({{}}, original.config.options, {{ responsive: true, maintainAspectRatio: false }}),
  }});
  modal.classList.add('rc-modal-open');
}}
function rcCloseChart() {{
  if (!modal) return;
  modal.classList.remove('rc-modal-open');
  const existingModalChart = Chart.getChart(modalCanvas);
  if (existingModalChart) existingModalChart.destroy();
}}
document.querySelectorAll('.rc-section canvas:not(#rc-chart-modal-canvas)').forEach(c => {{
  c.addEventListener('click', () => rcOpenChart(c.id));
}});
if (modal) {{
  modal.addEventListener('click', (e) => {{ if (e.target === modal) rcCloseChart(); }});
}}
const closeBtn = document.querySelector('#rc-chart-modal .rc-modal-close');
if (closeBtn) closeBtn.addEventListener('click', rcCloseChart);
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
.rc-section canvas{cursor:zoom-in;}
@media (max-width:600px){
  .rc-section .grid2{grid-template-columns:1fr;}
}
#rc-chart-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:9999;align-items:center;justify-content:center;padding:16px;}
#rc-chart-modal.rc-modal-open{display:flex;}
#rc-chart-modal .rc-modal-inner{background:var(--surface);border-radius:10px;padding:16px;width:100%;max-width:900px;max-height:90vh;display:flex;flex-direction:column;}
#rc-chart-modal .rc-modal-close{align-self:flex-end;background:none;border:none;color:var(--text3);font-size:24px;line-height:1;cursor:pointer;padding:0 0 8px 0;}
#rc-chart-modal canvas{cursor:default;flex:1;min-height:0;}
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
<script>
// Offline fallback — if the CDN didn't load (no network, or run_model.html
// opened from the filesystem without internet), replace Chart with a stub
// that renders a clear error message instead of silently blank canvases.
if (typeof Chart === "undefined") {
  window.Chart = function(canvas, cfg) {
    var ctx = canvas.getContext ? canvas.getContext("2d") : null;
    if (ctx) {
      ctx.fillStyle = "#8b949e";
      ctx.font = "12px sans-serif";
      ctx.fillText("Chart.js unavailable (offline?)", 10, canvas.height / 2);
    }
  };
  Chart.getChart = function() { return null; };
  Chart.register = function() {};
  console.warn("Chart.js CDN unavailable — charts will not render. Open this file while online, or serve it from a local server.");
}
</script>
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
canvas{cursor:zoom-in;}
@media (max-width:600px){
  .grid2{grid-template-columns:1fr;}
}
#rc-chart-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:9999;align-items:center;justify-content:center;padding:16px;}
#rc-chart-modal.rc-modal-open{display:flex;}
#rc-chart-modal .rc-modal-inner{background:var(--surface);border-radius:10px;padding:16px;width:100%;max-width:900px;max-height:90vh;display:flex;flex-direction:column;}
#rc-chart-modal .rc-modal-close{align-self:flex-end;background:none;border:none;color:var(--muted);font-size:24px;line-height:1;cursor:pointer;padding:0 0 8px 0;}
#rc-chart-modal canvas{cursor:default;flex:1;min-height:0;}
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

        print("  Classifying workout type...")
        classification = classify_workout(points, laps, metrics, activity)
        print(f"  {classification['emoji']} {classification['label']} (confidence: {round(classification['confidence']*100)}%)")

        print(f"  Computing {actual.year} YTD mileage through {actual.isoformat()}...")
        ytd_km = compute_ytd_mileage(garmin, actual)
        print(f"  YTD: {ytd_km}km")

        runs.append({
            "label": label, "color": color,
            "points": points, "bp_idx": bp_idx,
            "metrics": metrics, "dynamics": dynamics,
            "ytd_km": ytd_km,
            "classification": classification,
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
