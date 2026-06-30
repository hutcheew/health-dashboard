"""
Garmin Run Intelligence Engine (v4)

Classifies:
Recovery, Easy/Base, Long Run, Progression, Tempo, Threshold,
VO2 Max, Anaerobic, Hill Run, Hill Repeats, Marathon Pace, Race, Unknown

Input:
Garmin activity arrays: time, pace, hr, power (optional), elevation (optional)
Optional: RunnerProfile (max_hr / threshold_hr / threshold_power)

v4 changes:
- Added detect_hill_repeats(): finds "run up / recover down / repeat"
  structure from the elevation trace shape, not just total elevation gain.
  A scalar total can't tell one long climb apart from 8x90sec efforts —
  this looks at climb count, climb duration, and whether the gaps between
  climbs look like genuine recovery (gap >= ~half the climb duration, HR
  actually drops) rather than just a flat connector between two hills.
- "Hill Repeats" added as its own type, scored higher than "Hill Run"
  (rolling/steady hills) since physiologically it's closer to an interval
  session than a normal steady run.

v3 changes:
- Fixed hill_score unit bug: distance arriving in metres (e.g. 10000)
  instead of km no longer silently zeroes out elevation load
- Interval detection now distinguishes true intervals (recovery gaps
  between hard segments) from one continuous hard block, so a 30min
  tempo effort doesn't get miscounted as "VO2 Max intervals"
- Added Marathon Pace as a distinct type (sustained z3, controlled drift)
- Added junk-mile / fatigued-aerobic detector: flags runs that look
  "Easy" on label but show fatigue signatures underneath
- Confidence is now best_score / max_achievable_score_for_that_type,
  not best/total — previously a run scoring on multiple categories
  diluted confidence even when the winning category was a clean match
- Added RunnerProfile: classification can use LTHR-relative zones
  instead of flat %MaxHR, so two runners at the same HR aren't
  automatically given the same label
- Output restructured into a dashboard-ready shape (top-level fields a
  frontend renders directly) with a "debug" block for downstream layers
  (training_load.py, AI coach) that need the full audit trail
"""

from dataclasses import dataclass, asdict
import numpy as np

SCHEMA_VERSION = "4.0"
CONFIDENCE_LIMIT = 30  # below this, classification is reported as "Unknown"

# Nat's threshold HR, estimated from a 5K parkrun all-out effort (avg HR 170)
# using the standard single-race formula: LTHR = avg_HR_5K / 1.04 = 163.5,
# rounded to 164 (no meaningful classification difference between the two
# -- verified directly: same zone band, same Easy/Base result either way).
#
# This matters because the classifier's %MaxHR fallback zones are NOT
# personally calibrated and were confirmed to misclassify a real easy
# effort as "Tempo" (140bpm sitting in the generic 70-80%-of-max z3 band).
# Supplying this real LTHR via DEFAULT_PROFILE fixes that -- same category
# of fix as the personal HR baseline and Achilles threshold elsewhere in
# this project. Re-estimate this if fitness changes meaningfully (e.g.
# after returning to full training post-injury) -- LTHR drifts with
# fitness, and this is a single field-test estimate, not a lab value.
NAT_LTHR = 164

# Maximum achievable points per type given the scoring rules in
# classify_run() — used so confidence reflects "how much of the available
# evidence fired" rather than being diluted by how many *other* categories
# also picked up some points on the same run.
MAX_SCORE_BY_TYPE = {
    "Recovery": 40,
    "Easy/Base": 40,
    "Long Run": 50,
    "Progression": 50,
    "Tempo": 50,        # 40 base + 10 steady-effort bonus
    "Threshold": 50,
    "VO2 Max": 70,       # 50 base + 20 true-interval bonus
    "Anaerobic": 50,
    "Hill Run": 40,
    "Hill Repeats": 85,  # 70 base + 15 recovery-pattern validation bonus
    "Race": 80,
    "Marathon Pace": 50,
}


# ==========================
# SAFETY HELPERS
# ==========================

def safe_divide(a, b, default=0.0):
    """Divide a/b, returning `default` instead of raising/NaN on b==0."""
    try:
        if b == 0 or b is None:
            return default
        result = a / b
        if np.isnan(result) or np.isinf(result):
            return default
        return result
    except (TypeError, ZeroDivisionError):
        return default


def safe_mean(data, default=0.0):
    data = np.array(data, dtype=float)
    if data.size == 0:
        return default
    return float(np.mean(data))


def normalize_distance_km(distance):
    """
    Garmin activity exports are inconsistent about units (km vs metres).
    Anything over 100 is almost certainly metres (no run is 100km+ for the
    vast majority of users) -> convert. This prevents hill_score silently
    collapsing to ~0 when distance arrives as metres.
    """
    if distance is None:
        return 0.0
    if distance > 100:
        return distance / 1000
    return distance


# ==========================
# RUNNER PROFILE
# ==========================

@dataclass
class RunnerProfile:
    """
    Per-runner physiology so the same 150bpm doesn't get classified
    identically for two people with very different fitness/thresholds.
    All fields optional — falls back to %MaxHR zones if threshold_hr
    isn't supplied.
    """
    max_hr: float = None
    threshold_hr: float = None       # LTHR, e.g. from a Garmin/lactate test
    threshold_power: float = None    # FTP-equivalent, if power data exists
    easy_hr_limit: float = None      # optional manual override for "easy"


# Default profile for calls that don't explicitly pass one. Without this,
# every caller of analyze_run() defaults to the unconditioned %MaxHR
# fallback zones, which were confirmed (see NAT_LTHR above) to misclassify
# real easy efforts as Tempo.
DEFAULT_PROFILE = RunnerProfile(max_hr=190, threshold_hr=NAT_LTHR)


# ==========================
# DATA MODEL
# ==========================

@dataclass
class RunFeatures:
    duration_min: float
    distance_km: float

    avg_pace: float
    pace_cv: float          # raw pace variability
    gap_pace_cv: float      # grade-adjusted pace variability

    avg_power: float
    power_cv: float

    avg_hr: float
    max_hr: float

    z1: float
    z2: float
    z3: float
    z4: float
    z5: float

    aerobic_te: float
    anaerobic_te: float

    elevation_gain: float
    hill_score: float
    hill_repeat_count: int
    hill_repeat_confidence: int
    hill_recovery_validated: bool

    interval_count: int
    interval_source: str         # "power" or "pace_hr_fallback"
    interval_quality_label: str  # "none" | "interval" | "steady"
    avg_recovery_gap: float

    progression_score: float   # based on GAP, not raw pace
    hr_drift: float             # based on GAP, not raw pace


# ==========================
# BASIC FUNCTIONS
# ==========================

def coefficient_variation(data):
    data = np.array(data, dtype=float)
    if data.size == 0:
        return 0.0
    mean = np.mean(data)
    return safe_divide(np.std(data), mean) * 100


def zone_distribution(hr, max_hr):
    hr = np.array(hr, dtype=float)
    total = len(hr)

    if total == 0 or max_hr == 0:
        return {f"z{i}": 0.0 for i in range(1, 6)}

    zones = {
        "z1": hr < max_hr * 0.60,
        "z2": (hr >= max_hr * 0.60) & (hr < max_hr * 0.70),
        "z3": (hr >= max_hr * 0.70) & (hr < max_hr * 0.80),
        "z4": (hr >= max_hr * 0.80) & (hr < max_hr * 0.90),
        "z5": hr >= max_hr * 0.90,
    }

    return {k: safe_divide(np.sum(v), total) * 100 for k, v in zones.items()}


def zone_distribution_lthr(hr, threshold_hr):
    """
    Friel-style running zones relative to lactate threshold HR rather than
    %MaxHR. Two runners at the same raw HR can be in completely different
    zones once threshold is accounted for — this is what lets the
    classifier tell an "easy" 150bpm apart from a "tempo" 150bpm.
    """
    hr = np.array(hr, dtype=float)
    total = len(hr)

    if total == 0 or threshold_hr == 0:
        return {f"z{i}": 0.0 for i in range(1, 6)}

    pct = hr / threshold_hr

    zones = {
        "z1": pct < 0.85,
        "z2": (pct >= 0.85) & (pct < 0.89),
        "z3": (pct >= 0.89) & (pct < 0.95),
        "z4": (pct >= 0.95) & (pct < 1.00),
        "z5": pct >= 1.00,
    }

    return {k: safe_divide(np.sum(v), total) * 100 for k, v in zones.items()}


def compute_zones(hr, profile: "RunnerProfile" = None, fallback_max_hr=0.0):
    """Dispatcher: use LTHR zones if the profile has one, else %MaxHR."""
    if profile is not None and profile.threshold_hr:
        return zone_distribution_lthr(hr, profile.threshold_hr)

    max_hr = (profile.max_hr if profile and profile.max_hr else fallback_max_hr)
    return zone_distribution(hr, max_hr)


# ==========================
# GRADE-ADJUSTED PACE (GAP)
# ==========================

def compute_grade_pct(elevation, window=10):
    """
    Approximate per-second grade (%) from an elevation trace.
    Smoothed over `window` seconds to reduce GPS/barometer noise,
    since raw second-to-second elevation diffs are very noisy.
    """
    elevation = np.array(elevation, dtype=float)
    n = len(elevation)
    if n < 2:
        return np.zeros(n)

    smoothed = rolling_average(elevation, window=max(window, 2))
    diffs = np.diff(smoothed, prepend=smoothed[0])

    # 1 sample = 1 second; assume ~ (distance_per_sec) horizontal run.
    # Without per-second distance we approximate grade in elevation-change
    # units per second, which is sufficient as a *relative* adjustment signal.
    return diffs


def grade_adjusted_pace(pace, elevation):
    """
    Convert raw pace (sec/unit-distance) into an equivalent flat-ground pace.
    Uses a simplified linear cost model (~3% pace penalty per 1% grade uphill,
    ~half that benefit downhill) as an approximation of Minetti-style running
    cost curves. This is intentionally conservative — good enough to stop
    hills being misread as pacing/fatigue problems, not a physiology paper.
    """
    pace = np.array(pace, dtype=float)
    grade = compute_grade_pct(elevation)

    if len(grade) != len(pace):
        # mismatched lengths -> no adjustment rather than guessing
        return pace

    uphill_penalty = 0.03
    downhill_benefit = 0.015  # downhill "helps" less than uphill "hurts"

    factor = np.where(
        grade >= 0,
        1 + grade * uphill_penalty,
        1 + grade * downhill_benefit,
    )
    factor = np.clip(factor, 0.5, 2.0)  # guard against extreme/noisy spikes

    return pace * factor


# ==========================
# INTERVAL DETECTION
# ==========================

def rolling_average(data, window=60):
    data = np.array(data, dtype=float)
    n = len(data)
    if n == 0:
        return data

    result = np.zeros(n, dtype=float)
    half = window // 2

    for i in range(n):
        start = max(0, i - half)
        end = min(n, i + half)
        result[i] = np.mean(data[start:end])

    return result


def _segments_from_mask(mask, min_len=30):
    segments = []
    start = None
    for i, value in enumerate(mask):
        if value and start is None:
            start = i
        elif not value and start is not None:
            length = i - start
            if length >= min_len:
                segments.append({"start": start, "end": i, "duration": length})
            start = None
    if start is not None and len(mask) - start >= min_len:
        segments.append({"start": start, "end": len(mask), "duration": len(mask) - start})
    return segments


def detect_intervals_power(power):
    power = np.array(power, dtype=float)
    if power.size == 0:
        return []

    rolling = rolling_average(power, window=60)
    intensity = np.array([safe_divide(p, r, default=1.0) for p, r in zip(power, rolling)])
    hard = intensity > 1.15
    return _segments_from_mask(hard, min_len=30)


def detect_intervals_pace_hr(pace, hr):
    """
    Fallback for runners without power data. Detects sustained pace
    surges (relative to a rolling baseline) that are corroborated by an
    HR rise — guards against GPS noise producing fake "intervals" by
    requiring the cardiovascular signal to back up the pace signal.
    """
    pace = np.array(pace, dtype=float)
    hr = np.array(hr, dtype=float)
    if pace.size == 0 or hr.size == 0:
        return []

    # lower pace value = faster, so "surge" = pace below rolling baseline
    rolling_pace = rolling_average(pace, window=90)
    speed_ratio = np.array(
        [safe_divide(rp, p, default=1.0) for p, rp in zip(pace, rolling_pace)]
    )
    pace_surge = speed_ratio > 1.10  # running >=10% faster than local baseline

    rolling_hr = rolling_average(hr, window=90)
    hr_elevated = hr > rolling_hr * 1.05

    hard = pace_surge & hr_elevated
    return _segments_from_mask(hard, min_len=30)


def detect_intervals(power, pace, hr):
    power = np.array(power, dtype=float) if power is not None else np.array([])

    has_real_power = power.size > 0 and np.std(power) > 5  # not flat/missing
    if has_real_power:
        return detect_intervals_power(power), "power"

    return detect_intervals_pace_hr(pace, hr), "pace_hr_fallback"


def interval_quality(intervals):
    """
    Distinguishes true intervals (hard reps with recovery between them)
    from one long sustained hard effort that happens to trip the same
    "hard" mask — e.g. a 20-30min continuous tempo block. Without this,
    both look identical to a naive interval counter.
    """
    if len(intervals) < 2:
        return {"label": "none", "avg_recovery_gap": 0.0}

    gaps = [b["start"] - a["end"] for a, b in zip(intervals, intervals[1:])]
    avg_gap = safe_mean(gaps)

    label = "interval" if avg_gap > 60 else "steady"
    return {"label": label, "avg_recovery_gap": round(avg_gap, 1)}


# ==========================
# PROGRESSION DETECTOR (on GAP)
# ==========================

def detect_progression(gap_pace):
    gap_pace = np.array(gap_pace, dtype=float)
    if gap_pace.size < 3:
        return 0.0

    blocks = np.array_split(gap_pace, 3)
    first = safe_mean(blocks[0])
    last = safe_mean(blocks[-1])

    return safe_divide(first - last, first) * 100


# ==========================
# CARDIAC DRIFT (on GAP)
# ==========================

def detect_drift(hr, gap_pace):
    hr = np.array(hr, dtype=float)
    gap_pace = np.array(gap_pace, dtype=float)

    if hr.size < 2 or gap_pace.size < 2:
        return 0.0

    mid = len(hr) // 2

    h1 = safe_mean(hr[:mid])
    h2 = safe_mean(hr[mid:])

    s1 = safe_divide(1, safe_mean(gap_pace[:mid]))
    s2 = safe_divide(1, safe_mean(gap_pace[mid:]))

    e1 = safe_divide(s1, h1)
    e2 = safe_divide(s2, h2)

    return safe_divide(e1 - e2, e1) * 100


def drift_analysis(drift):
    if drift < 3:
        return {"level": "Excellent", "message": "Strong aerobic control"}
    elif drift < 5:
        return {"level": "Good", "message": "Normal fatigue response"}
    elif drift < 10:
        return {"level": "Moderate", "message": "Aerobic strain detected"}
    else:
        return {"level": "High", "message": "Possible fatigue/dehydration/heat"}


# ==========================
# HILL REPEAT DETECTOR
# ==========================

def detect_hill_repeats(elevation, hr=None, min_climb_sec=20, min_gain_m=3, min_segments=3):
    """
    Detects "run up / recover down / repeat" structure rather than just
    looking at total elevation gain — a scalar total can't tell a single
    long climb apart from 8 repeated 90-second efforts, but the shape of
    the elevation trace can.

    Climbs are found from a smoothed gradient (raw second-to-second
    elevation diffs are too noisy off GPS/barometer). A climb only counts
    once it clears both a minimum duration and a minimum net gain, so GPS
    jitter on flat ground doesn't get counted as a "rep."

    Recovery is validated using two real-world hill-repeat signatures:
    - the gap between climbs is comparable to (or longer than) the climb
      itself — matches "walk down takes longer than running up"
    - HR actually drops during that gap, rather than staying elevated
      (staying elevated would suggest a short flat connector, not a
      genuine recovery descent)
    """
    if elevation is None:
        return {"hill_repeats": 0, "confidence": 0, "segments": [], "recovery_validated": False}

    elevation = np.array(elevation, dtype=float)
    n = len(elevation)
    if n < 20:
        return {"hill_repeats": 0, "confidence": 0, "segments": [], "recovery_validated": False}

    smoothed = rolling_average(elevation, window=10)
    grade = np.diff(smoothed, prepend=smoothed[0])

    uphill = grade > 0.15  # smoothed m/s climb rate; filters GPS noise on flat ground

    candidates = _segments_from_mask(uphill, min_len=min_climb_sec)

    climbs = []
    for seg in candidates:
        gain = float(elevation[seg["end"] - 1] - elevation[seg["start"]]) if seg["end"] > seg["start"] else 0.0
        if gain >= min_gain_m:
            seg = dict(seg)
            seg["gain"] = round(gain, 1)
            climbs.append(seg)

    count = len(climbs)

    if count < min_segments:
        return {
            "hill_repeats": count,
            "confidence": 0,
            "segments": climbs,
            "recovery_validated": False,
        }

    gaps = [b["start"] - a["end"] for a, b in zip(climbs, climbs[1:])]
    avg_gap = safe_mean(gaps)
    avg_climb_duration = safe_mean([c["duration"] for c in climbs])

    avg_hr_drop = 0.0
    if hr is not None:
        hr = np.array(hr, dtype=float)
        if hr.size >= n:
            drops = []
            for a, b in zip(climbs, climbs[1:]):
                end_of_climb_hr = safe_mean(hr[max(0, a["end"] - 10):a["end"]])
                pre_next_climb_hr = safe_mean(hr[max(0, b["start"] - 10):b["start"]])
                drops.append(end_of_climb_hr - pre_next_climb_hr)
            avg_hr_drop = safe_mean(drops)

    # "Walk down takes longer than running up" -> gap should be at least
    # half the climb duration, ideally longer.
    recovery_validated = avg_gap >= avg_climb_duration * 0.5 and avg_hr_drop > 5

    confidence = min(100, count * 25)
    if recovery_validated:
        confidence = min(100, confidence + 15)
    elif avg_hr_drop > 5 or avg_gap >= avg_climb_duration * 0.5:
        confidence = min(100, confidence + 5)  # partial recovery evidence

    return {
        "hill_repeats": count,
        "confidence": confidence,
        "segments": climbs,
        "avg_climb_duration_sec": round(avg_climb_duration, 1),
        "avg_recovery_gap_sec": round(avg_gap, 1),
        "avg_recovery_hr_drop": round(avg_hr_drop, 1),
        "recovery_validated": recovery_validated,
    }


# ==========================
# FEATURE CREATION
# ==========================

def extract_features(activity, profile: "RunnerProfile" = None):
    hr = np.array(activity["hr"], dtype=float)
    pace = np.array(activity["pace"], dtype=float)

    power = activity.get("power")
    power = np.array(power, dtype=float) if power is not None else np.zeros(len(hr))

    elevation = activity.get("elevation")
    elevation = np.array(elevation, dtype=float) if elevation is not None else np.zeros(len(hr))

    fallback_max_hr = activity.get("max_hr", float(np.max(hr)) if hr.size else 0.0)
    max_hr = (profile.max_hr if profile and profile.max_hr else fallback_max_hr)

    distance_km = normalize_distance_km(activity.get("distance", 0.0))

    zones = compute_zones(hr, profile=profile, fallback_max_hr=fallback_max_hr)

    gap_pace = grade_adjusted_pace(pace, elevation)

    intervals, interval_source = detect_intervals(power, pace, hr)
    iq = interval_quality(intervals)

    elevation_gain = float(np.sum(np.maximum(np.diff(elevation), 0))) if elevation.size > 1 else 0.0

    hill_repeats = detect_hill_repeats(elevation, hr=hr)

    return RunFeatures(
        duration_min=safe_divide(len(hr), 60),
        distance_km=distance_km,

        avg_pace=safe_mean(pace),
        pace_cv=coefficient_variation(pace),
        gap_pace_cv=coefficient_variation(gap_pace),

        avg_power=safe_mean(power),
        power_cv=coefficient_variation(power),

        avg_hr=safe_mean(hr),
        max_hr=max_hr,

        z1=zones["z1"], z2=zones["z2"], z3=zones["z3"],
        z4=zones["z4"], z5=zones["z5"],

        aerobic_te=activity.get("aerobic_te", 0),
        anaerobic_te=activity.get("anaerobic_te", 0),

        elevation_gain=elevation_gain,
        hill_score=safe_divide(elevation_gain, distance_km),
        hill_repeat_count=hill_repeats["hill_repeats"],
        hill_repeat_confidence=hill_repeats["confidence"],
        hill_recovery_validated=hill_repeats["recovery_validated"],

        interval_count=len(intervals),
        interval_source=interval_source,
        interval_quality_label=iq["label"],
        avg_recovery_gap=iq["avg_recovery_gap"],

        progression_score=detect_progression(gap_pace),
        hr_drift=detect_drift(hr, gap_pace),
    )


# ==========================
# RACE DETECTION
# ==========================

def detect_race_score(f: RunFeatures):
    score = 0
    if f.z4 + f.z5 > 60:
        score += 40
    if f.interval_count == 0:
        score += 20
    if safe_divide(f.avg_hr, f.max_hr) > 0.88:
        score += 20
    if 15 < f.duration_min < 240:
        score += 20
    return score


# ==========================
# FATIGUE & QUALITY SCORING
# ==========================

def fatigue_score(f: RunFeatures):
    score = 0
    if f.hr_drift > 10:
        score += 40
    if f.gap_pace_cv > 10:
        score += 20
    if f.anaerobic_te > 3:
        score += 20
    if f.z4 + f.z5 > 50:
        score += 20
    return score


def fatigue_label(score):
    if score >= 60:
        return "High"
    elif score >= 30:
        return "Moderate"
    return "Low"


def training_quality(scores):
    weights = {
        "VO2 Max": 1.0,
        "Threshold": 0.9,
        "Tempo": 0.7,
        "Anaerobic": 0.8,
        "Long Run": 0.8,
        "Easy/Base": 0.5,
        "Recovery": 0.3,
        "Hill Run": 0.6,
        "Hill Repeats": 0.85,
        "Progression": 0.6,
        "Race": 1.0,
        "Marathon Pace": 0.85,
    }

    total = sum(value * weights.get(key, 0.5) for key, value in scores.items())
    return round(min(safe_divide(total, 10), 10), 1)


# ==========================
# JUNK MILE / FATIGUED-AEROBIC DETECTOR
# ==========================

def junk_mile_score(f: "RunFeatures"):
    """
    Flags a run that *looks* easy on the surface (low pace, moderate HR)
    but shows signs the aerobic system was struggling — high drift, high
    variability, elevated time in z3/z4 for what should be steady running.
    This is the case Garmin's own "Base"/"Easy" label misses: the run
    happened in the right zone on average, but the body was working much
    harder than the label suggests.
    """
    score = 0
    if f.hr_drift > 10:
        score += 40
    if f.z3 + f.z4 > 60:
        score += 30
    if f.gap_pace_cv > 10:
        score += 20
    return score


JUNK_MILE_THRESHOLD = 60


# ==========================
# CLASSIFIER
# ==========================

def classify_run(f: RunFeatures):
    scores = {
        "Recovery": 0, "Easy/Base": 0, "Long Run": 0, "Progression": 0,
        "Tempo": 0, "Threshold": 0, "VO2 Max": 0, "Anaerobic": 0,
        "Hill Run": 0, "Hill Repeats": 0, "Race": 0, "Marathon Pace": 0,
    }
    reasons = []

    if f.duration_min < 45 and f.z1 + f.z2 > 80:
        scores["Recovery"] += 40
        reasons.append("Low intensity short run")

    if f.z1 + f.z2 > 70:
        scores["Easy/Base"] += 40
        reasons.append("Mostly aerobic HR")

    if f.duration_min > 75:
        scores["Long Run"] += 50
        reasons.append("Long duration")

    if f.progression_score > 8:
        scores["Progression"] += 50
        reasons.append("Grade-adjusted pace increased through run")

    if f.z3 > 35:
        scores["Tempo"] += 40
        reasons.append("Sustained moderate-high HR")

    if f.z4 > 30:
        scores["Threshold"] += 50
        reasons.append("High threshold intensity")

    if f.interval_count >= 4:
        scores["VO2 Max"] += 50
        reasons.append(f"Repeated hard segments ({f.interval_source})")

    # Interval-quality bonus: tells true reps (with recovery) apart from
    # one long sustained hard block that would otherwise look the same.
    if f.interval_count >= 2:
        if f.interval_quality_label == "interval":
            scores["VO2 Max"] += 20
            reasons.append(
                f"Recovery gaps (avg {f.avg_recovery_gap:.0f}s) between hard "
                "segments indicate true intervals"
            )
        elif f.interval_quality_label == "steady":
            scores["Tempo"] += 10
            reasons.append("Hard effort sustained without recovery gaps (steady-state)")

    if f.anaerobic_te > 2.5:
        scores["Anaerobic"] += 50
        reasons.append("High anaerobic training effect")

    if f.hill_score > 15:
        scores["Hill Run"] += 40
        reasons.append("High elevation load — rolling/steady hill effort")

    # Hill Repeats: physiologically closer to intervals than a normal run
    # (run hard up, recover down, repeat), so it's scored independently of
    # — and typically outscores — the steady "Hill Run" case above when a
    # genuine repeat structure is present.
    if f.hill_repeat_count >= 3:
        scores["Hill Repeats"] += 70
        reasons.append(f"{f.hill_repeat_count} hill climb efforts detected")

        if f.hill_recovery_validated:
            scores["Hill Repeats"] += 15
            reasons.append("Recovery descents confirmed (HR drop + walk-back pacing)")

    # Marathon pace: sustained moderate-hard effort with controlled drift —
    # distinct from Tempo (shorter, more clearly "hard") and from Long Run
    # (which says nothing about intensity).
    if f.duration_min > 60 and f.z3 > 50 and f.hr_drift < 8:
        scores["Marathon Pace"] += 50
        reasons.append("Long sustained z3 effort with controlled cardiac drift")

    race_score = detect_race_score(f)
    if race_score > 70:
        scores["Race"] += 80
        reasons.append("Sustained high effort, race-like signature")

    total = sum(scores.values())

    if total == 0:
        return {
            "run_type": "Unknown",
            "confidence": 0.0,
            "scores": scores,
            "reasons": ["No pattern matched"],
            "features": asdict(f),
        }

    best = max(scores, key=scores.get)

    # Confidence = how much of the *achievable* evidence for the winning
    # category actually fired, not best/total — total dilutes confidence
    # any time multiple categories pick up partial points on the same run
    # (e.g. a hilly long run legitimately scoring on both Long Run and
    # Hill Run shouldn't make either one look "low confidence").
    max_possible = MAX_SCORE_BY_TYPE.get(best, scores[best] or 1)
    confidence = round(min(safe_divide(scores[best], max_possible) * 100, 100), 1)

    if confidence < CONFIDENCE_LIMIT:
        return {
            "run_type": "Unknown",
            "confidence": confidence,
            "scores": scores,
            "reasons": ["Insufficient pattern detected"],
            "features": asdict(f),
        }

    return {
        "run_type": best,
        "confidence": confidence,
        "scores": scores,
        "reasons": reasons,
        "features": asdict(f),
    }


# ==========================
# INTENT COMPARISON (planned vs actual)
# ==========================

def compare_intent(planned_type, actual_type):
    if not planned_type:
        return None

    if planned_type == actual_type:
        return {"execution": "On plan", "note": f"Run matched planned '{planned_type}' workout."}

    return {
        "execution": "Deviated from plan",
        "note": f"Planned '{planned_type}' but executed more like '{actual_type}'.",
    }


# ==========================
# COACH SUMMARY (templated, not AI-generated)
# ==========================

def generate_coach_summary(run_type, drift_info, fatigue_lvl, hill_score, junk_flag,
                            marathon_pace_flag, hill_repeat_count, hill_recovery_validated):
    """
    Simple templated sentence — deliberately not calling an LLM here, since
    this module should produce a deterministic, debuggable summary that the
    eventual AI-coach layer can use as a grounded input rather than
    hallucinate from raw numbers.
    """
    parts = []

    if junk_flag:
        parts.append(
            f"Logged as {run_type}, but signs of aerobic fatigue suggest this "
            "wasn't a clean easy effort."
        )
    elif run_type == "Hill Repeats":
        rec = "with clear recovery descents" if hill_recovery_validated else "though recovery pattern was less clear-cut"
        parts.append(f"Hill Repeats session — {hill_repeat_count} climbing efforts detected, {rec}.")
    else:
        parts.append(f"{run_type} session.")

    if hill_score > 15 and run_type != "Hill Repeats":
        parts.append("Significant elevation load — fatigue here is partly terrain, not just effort.")

    if drift_info["level"] in ("Moderate", "High"):
        parts.append(f"{drift_info['level']} cardiac drift: {drift_info['message'].lower()}.")

    if fatigue_lvl == "High":
        parts.append("Consider an easier day next to let this absorb.")

    if marathon_pace_flag:
        parts.append("Sustained effort at marathon-effort intensity with controlled drift — solid race-pace work.")

    return " ".join(parts)


# ==========================
# MAIN ANALYSIS ENTRY POINT
# ==========================

def analyze_run(activity, planned_type=None, profile: "RunnerProfile" = None):
    # Default to the calibrated profile (real LTHR) rather than silently
    # falling back to the uncalibrated %MaxHR zones -- that fallback was
    # confirmed to misclassify real easy efforts as Tempo (see NAT_LTHR's
    # comment above). Callers can still pass profile=False explicitly if
    # they genuinely want the raw fallback for some reason.
    if profile is None:
        profile = DEFAULT_PROFILE
    elif profile is False:
        profile = None

    features = extract_features(activity, profile=profile)
    classification = classify_run(features)

    fscore = fatigue_score(features)
    flevel = fatigue_label(fscore)
    drift_info = drift_analysis(features.hr_drift)
    quality = training_quality(classification["scores"])
    intent = compare_intent(planned_type, classification["run_type"])

    jscore = junk_mile_score(features)
    junk_flag = jscore >= JUNK_MILE_THRESHOLD and classification["run_type"] in (
        "Easy/Base", "Recovery", "Long Run",
    )

    marathon_pace_flag = classification["run_type"] == "Marathon Pace"

    coach_summary = generate_coach_summary(
        classification["run_type"], drift_info, flevel,
        features.hill_score, junk_flag, marathon_pace_flag,
        features.hill_repeat_count, features.hill_recovery_validated,
    )

    # Dashboard-ready top level: the fields a frontend renders directly.
    result = {
        "schema_version": SCHEMA_VERSION,
        "run_type": classification["run_type"],
        "confidence": classification["confidence"],
        "quality_score": quality,
        "training_effect": {
            "aerobic": features.aerobic_te,
            "anaerobic": features.anaerobic_te,
        },
        "fatigue": {
            "score": fscore,
            "status": flevel.lower(),
        },
        "junk_mile": {
            "score": jscore,
            "flag": junk_flag,
        },
        "hill_repeats": {
            "count": features.hill_repeat_count,
            "confidence": features.hill_repeat_confidence,
            "recovery_validated": features.hill_recovery_validated,
        },
        "coach_summary": coach_summary,
        "metrics": {
            "pace": round(features.avg_pace, 1),
            "hr": round(features.avg_hr, 1),
            "drift": round(features.hr_drift, 1),
        },
        # Everything below is the full debug/audit trail — kept for the
        # AI-coach and training_load.py layers, not meant for direct display.
        "debug": {
            "cardiac_drift": {"value": round(features.hr_drift, 1), **drift_info},
            "reasons": classification["reasons"],
            "scores": classification["scores"],
            "features": classification["features"],
        },
    }

    if intent is not None:
        result["intent"] = intent

    return result


# ==========================
# TEST
# ==========================

if __name__ == "__main__":

    # Example 1: steady easy run, no power data -> exercises the fallback
    # interval detector and GAP on flat terrain (grade ~0, so GAP == pace).
    test_run_easy = {
        "distance": 12,
        "hr": [140] * 3600,
        "pace": [300] * 3600,
        "power": None,
        "elevation": [0] * 3600,
        "aerobic_te": 2.8,
        "anaerobic_te": 0.5,
        "max_hr": 190,
    }

    print("=== Easy run (no power) ===")
    print(analyze_run(test_run_easy, planned_type="Easy/Base"))

    # Example 2: hilly run with no power, to sanity check that GAP keeps
    # hill climbing from being misread as pace fade / fatigue.
    n = 1800
    elevation_profile = list(np.concatenate([
        np.linspace(0, 80, n // 2),   # climb
        np.linspace(80, 0, n // 2),   # descend
    ]))
    pace_profile = [310] * (n // 2) + [290] * (n // 2)  # slower uphill, faster down
    hr_profile = [150] * (n // 2) + [148] * (n // 2)

    test_run_hilly = {
        "distance": 8,
        "hr": hr_profile,
        "pace": pace_profile,
        "power": None,
        "elevation": elevation_profile,
        "aerobic_te": 3.0,
        "anaerobic_te": 0.3,
        "max_hr": 190,
    }

    print("\n=== Hilly run (no power) ===")
    print(analyze_run(test_run_hilly, planned_type="Easy/Base"))

    # Example 3: distance given in metres (the hill_score bug case) —
    # same hilly profile as above but distance=8000 instead of 8.
    test_run_metres = dict(test_run_hilly)
    test_run_metres["distance"] = 8000

    print("\n=== Hilly run, distance given in metres (bug check) ===")
    r = analyze_run(test_run_metres, planned_type="Easy/Base")
    print("hill_score:", r["debug"]["features"]["hill_score"], "(should match the km version above)")

    # Example 4: "junk mile" — looks easy on label but HR drift + variability
    # are high, simulating fatigue/heat/poor recovery under an easy run.
    n2 = 3000
    junk_hr = list(np.linspace(135, 168, n2))      # drifts up a lot
    junk_pace = list(310 + 25 * np.sin(np.linspace(0, 20, n2)))  # choppy pace
    test_run_junk = {
        "distance": 10,
        "hr": junk_hr,
        "pace": junk_pace,
        "power": None,
        "elevation": [0] * n2,
        "aerobic_te": 3.2,
        "anaerobic_te": 0.4,
        "max_hr": 190,
    }

    print("\n=== 'Junk mile' easy run (fatigue under the hood) ===")
    print(analyze_run(test_run_junk, planned_type="Easy/Base"))

    # Example 5: using a RunnerProfile with LTHR instead of %MaxHR
    profile = RunnerProfile(max_hr=190, threshold_hr=172)
    print("\n=== Same easy run, classified against LTHR profile ===")
    print(analyze_run(test_run_easy, planned_type="Easy/Base", profile=profile))

    # Example 6: hill repeats matching the user's actual workout —
    # run up ~90sec, walk down ~140sec (longer than the climb), HR rises
    # to ~170 on the climb and drops back to ~130 on the walk-down. 8 reps.
    reps = 8
    climb_sec = 90
    descent_sec = 140
    climb_gain = 35  # metres

    elevation_hr = []
    elevation_profile2 = []
    pace_profile2 = []

    for _ in range(reps):
        elevation_hr += list(np.linspace(150, 170, climb_sec))      # HR rising on climb
        elevation_hr += list(np.linspace(170, 128, descent_sec))    # HR dropping on walk-down

        elevation_profile2 += list(np.linspace(0, climb_gain, climb_sec))
        elevation_profile2 += list(np.linspace(climb_gain, 0, descent_sec))

        pace_profile2 += [330] * climb_sec   # harder uphill running pace
        pace_profile2 += [650] * descent_sec  # slow walk back down

    test_run_hill_repeats = {
        "distance": 6,
        "hr": elevation_hr,
        "pace": pace_profile2,
        "power": None,
        "elevation": elevation_profile2,
        "aerobic_te": 3.5,
        "anaerobic_te": 1.8,
        "max_hr": 190,
    }

    print("\n=== Hill repeats (8x90sec up, 140sec walk-down) ===")
    result = analyze_run(test_run_hill_repeats, planned_type="Hill Repeats")
    print({k: v for k, v in result.items() if k != "debug"})
    print("hill_repeats debug:", result["hill_repeats"])
