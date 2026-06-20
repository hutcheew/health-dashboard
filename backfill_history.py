"""
backfill_history.py
--------------------
One-off script to backfill score_history.json using Garmin's historical
data, instead of waiting weeks for it to accumulate day-by-day.

This is deliberately kept SEPARATE from health_dashboard.py and is meant to
be run manually/locally, not via the GitHub Action — it makes one Garmin API
call per metric per day, which is slow and carries some rate-limit risk
against Garmin's unofficial API. Run it once, eyeball the output, commit.

Usage:
    python backfill_history.py --days 30

Requires the same Garmin token file as health_dashboard.py
(~/.garminconnect/garmin_tokens.json), and must be run from the same
directory as health_dashboard.py so it can import from it.
"""

import argparse
import json
import os
import time
from datetime import date, timedelta

from health_dashboard import (
    get_garmin,
    compute_achilles_score,
    compute_tissue_capacity,
    compute_training_monotony,
    compute_recovery_score,
    PLAN_START,
    RACE_DATE,
    PHASES,
    HISTORY_FILE,
)


PRE_PLAN_PHASE = {
    "name": "Pre-Plan / Early Injury",
    "weeks": (-999, 0),
    "km_min": 0,
    "km_max": 20,
    "focus": "Injury management, pre-structured-plan",
}


def phase_info_for(target_date):
    """Same logic as get_training_phase() in health_dashboard.py, but
    parametrized by date instead of hardcoded to date.today().

    Dates before PLAN_START get a synthetic 'Pre-Plan / Early Injury' phase
    instead of falling through to PHASES[-1] (Taper) — the original
    next(..., PHASES[-1]) fallback was written assuming it'd only ever be
    called for in-plan dates, since get_training_phase() always uses
    date.today(). Backfilling pre-plan dates exposed that assumption."""
    week_num = int((target_date - PLAN_START).days / 7) + 1
    days_to_race = (RACE_DATE - target_date).days

    if target_date < PLAN_START:
        phase = PRE_PLAN_PHASE
        week_in_phase = 1
        phase_total = 1
    else:
        week_num = min(week_num, 28)
        phase = next((p for p in PHASES if p["weeks"][0] <= week_num <= p["weeks"][1]), PHASES[-1])
        week_in_phase = week_num - phase["weeks"][0] + 1
        phase_total = phase["weeks"][1] - phase["weeks"][0] + 1

    return {
        "week_num": week_num,
        "phase": phase,
        "week_in_phase": week_in_phase,
        "phase_total": phase_total,
        "days_to_race": days_to_race,
        "phase_pct": round(week_in_phase / phase_total * 100),
    }


def fetch_day(garmin, d_str):
    """Mirrors the per-metric calls in fetch_garmin_data(), but parametrized
    by an arbitrary historical date instead of TODAY."""
    out = {"readiness": {}, "hrv": {}, "resting_hr": None, "body_battery": None}

    try:
        readiness = garmin.get_morning_training_readiness(d_str)
        out["readiness"] = {
            "score": readiness.get("score"),
            "sleep_score": readiness.get("sleepScore"),
            "hrv_weekly_avg": readiness.get("hrvWeeklyAverage"),
        }
    except Exception as e:
        print(f"    readiness fetch failed for {d_str}: {e}")

    try:
        hrv = garmin.get_hrv_data(d_str)
        out["hrv"] = {"weekly_avg": hrv.get("hrvSummary", {}).get("weeklyAvg")}
    except Exception as e:
        print(f"    hrv fetch failed for {d_str}: {e}")

    try:
        hr = garmin.get_heart_rates(d_str)
        out["resting_hr"] = hr.get("restingHeartRate")
    except Exception as e:
        print(f"    resting HR fetch failed for {d_str}: {e}")

    try:
        bb = garmin.get_body_battery(d_str)
        if bb and isinstance(bb, list):
            vals = [x[1] for x in bb[0].get("bodyBatteryValuesArray", []) if x[1] is not None]
            out["body_battery"] = vals[-1] if vals else None
    except Exception as e:
        print(f"    body battery fetch failed for {d_str}: {e}")

    # Sleep is filed under the WAKE date (a night from Mon 10pm-Tue 6am is
    # Tuesday's record) -- same convention health_dashboard.py now uses for
    # live fetches. The live script's fallback to YESTERDAY exists only to
    # handle same-morning sync delay; that doesn't apply to historical
    # dates, so backfilling queries d_str directly with no offset.
    try:
        sleep = garmin.get_sleep_data(d_str)
        daily = sleep.get("dailySleepDTO", {})
        out["sleep"] = {"duration_hrs": round(daily.get("sleepTimeSeconds", 0) / 3600, 1)}
    except Exception as e:
        out["sleep"] = {}
        print(f"    sleep fetch failed for {d_str}: {e}")

    return out


def fetch_all_runs(garmin, lookback_days):
    """Pull running activities and lap data once, up front, then slice by
    date per backfill day below — avoids re-fetching the same activities
    repeatedly and keeps each day's computation scoped to only the runs
    that existed as of that date (no future-data leakage).

    Paginates in batches of 100 until either we've gone far enough back to
    cover lookback_days (with a buffer), or Garmin returns an empty page —
    a flat get_activities(0, 60) silently misses older runs once you have
    more than 60 activities of ANY type (rides, strength, etc. all count)
    between today and the oldest date you're backfilling."""
    oldest_needed = (date.today() - timedelta(days=lookback_days + 5)).isoformat()
    activities = []
    offset = 0
    batch_size = 100
    while True:
        batch = garmin.get_activities(offset, batch_size)
        if not batch:
            break
        activities.extend(batch)
        oldest_in_batch = batch[-1].get("startTimeLocal", "")[:10]
        if oldest_in_batch and oldest_in_batch <= oldest_needed:
            break
        if len(batch) < batch_size:
            break  # no more activities to fetch
        offset += batch_size
        if offset >= 2000:  # sanity cap
            print("  Hit 2000-activity safety cap, stopping pagination")
            break

    runs = []
    for r in activities:
        if r.get("activityType", {}).get("typeKey") != "running":
            continue
        aid = r["activityId"]
        try:
            splits = garmin.get_activity_splits(aid)
            laps = splits.get("lapDTOs", [])
        except Exception:
            laps = []
        lap_data = [
            {
                "km": i + 1,
                "gct": round(l.get("groundContactTime", 0), 1),
                "gct_balance": round(l.get("groundContactBalanceLeft", 50), 1),
            }
            for i, l in enumerate(laps)
        ]
        runs.append({
            "date": r["startTimeLocal"][:10],
            "distance": round(r.get("distance", 0) / 1000, 1),
            "duration": round(r.get("duration", 0) / 60, 1),
            "avg_hr": r.get("averageHR"),
            "avg_pace": round(1000 / r.get("averageSpeed", 1) / 60, 2) if r.get("averageSpeed") else None,
            "laps": lap_data,
        })
    runs.sort(key=lambda r: r["date"])
    return runs


def main(n_days, force=False):
    print("Connecting to Garmin...")
    garmin = get_garmin()

    print("Fetching activity history for run slicing...")
    all_runs = fetch_all_runs(garmin, n_days)
    print(f"  {len(all_runs)} runs found")

    history = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, encoding="utf-8") as f:
            history = json.load(f)
    existing_dates = {h["date"] for h in history}

    today = date.today()
    added = 0
    updated = 0
    for n in range(n_days, 0, -1):
        target = today - timedelta(days=n)
        d_str = target.isoformat()

        if d_str in existing_dates and not force:
            print(f"  {d_str}: already have an entry, skipping (use --force to recompute)")
            continue

        print(f"  Backfilling {d_str}...")
        day_data = fetch_day(garmin, d_str)
        # all_runs is sorted ascending (oldest-first) for the date-slicing
        # above to work; but compute_achilles_score/compute_training_monotony
        # assume Garmin's native most-recent-first order (they slice with
        # runs[:7], runs[3:8], etc.) — so reverse before passing in.
        runs_as_of = [r for r in all_runs if r["date"] <= d_str]
        runs_as_of_desc = list(reversed(runs_as_of))
        phase_info = phase_info_for(target)

        achilles = compute_achilles_score(runs_as_of_desc, phase_info, as_of=target)
        tissue_capacity = compute_tissue_capacity(runs_as_of_desc, as_of=target)
        monotony = compute_training_monotony(runs_as_of_desc)
        recovery = compute_recovery_score(
            day_data.get("readiness", {}),
            day_data.get("hrv", {}),
            day_data.get("sleep", {}),
            day_data.get("resting_hr") or 0,
        )
        last_run = runs_as_of[-1] if runs_as_of else {}
        ran_today = bool(last_run) and last_run.get("date") == d_str

        new_entry = {
            "date": d_str,
            "inputs": {
                "resting_hr": day_data.get("resting_hr"),
                "hrv": day_data.get("readiness", {}).get("hrv_weekly_avg"),
                "sleep_score": day_data.get("readiness", {}).get("sleep_score"),
                "body_battery": day_data.get("body_battery"),
                "ran_today": ran_today,
                "last_run": {
                    "date": last_run.get("date"),
                    "distance": last_run.get("distance"),
                    "pace": last_run.get("avg_pace"),
                    "avg_hr": last_run.get("avg_hr"),
                } if last_run else None,
            },
            "scores": {
                "readiness": day_data.get("readiness", {}).get("score"),
                "recovery": recovery.get("score"),
                "achilles": achilles.get("score") if runs_as_of else None,
                "tissue_capacity": tissue_capacity.get("score") if runs_as_of else None,
                "monotony": monotony.get("score") if runs_as_of else None,
            },
        }

        if d_str in existing_dates:
            history = [h for h in history if h["date"] != d_str]
            updated += 1
        else:
            added += 1
        history.append(new_entry)
        time.sleep(1.5)  # be polite to Garmin's unofficial API — avoid rate-limit/block

    history.sort(key=lambda h: h["date"])
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)

    print(f"\nDone. Added {added} new days, recomputed {updated} existing days. "
          f"{len(history)} total days in {HISTORY_FILE}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="How many past days to backfill")
    parser.add_argument("--force", action="store_true",
                         help="Recompute days that already have an entry instead of skipping them "
                              "(use after a scoring/data fix to correct already-backfilled history)")
    args = parser.parse_args()
    main(args.days, force=args.force)
