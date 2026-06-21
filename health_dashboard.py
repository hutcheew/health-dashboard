"""
health_dashboard.py
-------------------
Fetches data from Garmin + Withings and generates a self-contained HTML dashboard.

Usage:
    python -X utf8 health_dashboard.py

Output:
    health_dashboard.html  (open in browser)

Requires:
    pip install garminconnect withings-api python-dotenv requests
"""

import os, json, requests
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo
MELB_TZ = ZoneInfo("Australia/Melbourne")
from dotenv import load_dotenv
from garminconnect import Garmin

load_dotenv()

GARMIN_TOKEN_FILE  = os.path.expanduser("~/.garminconnect/garmin_tokens.json")
WITHINGS_TOKEN_FILE = os.path.expanduser("~/.withings/withings_tokens.json")
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "health_dashboard.html")

_melb_now = datetime.now(MELB_TZ)
TODAY     = _melb_now.date().isoformat()
YESTERDAY = (_melb_now.date() - timedelta(days=1)).isoformat()

# ─── GARMIN ──────────────────────────────────────────────────────────────────

def get_garmin():
    token_data = open(GARMIN_TOKEN_FILE).read()
    garmin = Garmin(email="dummy@dummy.com", password="dummy")
    garmin.login(tokenstore=token_data)
    return garmin

def fetch_garmin_data(garmin):
    data = {}

    # Recent activities — fetch 20 to get enough history for load calculations
    activities = garmin.get_activities(0, 20)
    runs = [a for a in activities if a.get("activityType", {}).get("typeKey") == "running"]
    data["runs"] = []
    for r in runs[:10]:
        aid = r["activityId"]
        splits = garmin.get_activity_splits(aid)
        laps = splits.get("lapDTOs", [])
        lap_data = []
        for i, lap in enumerate(laps):
            lap_data.append({
                "km": i + 1,
                "pace": round(1000 / lap.get("averageSpeed", 1) / 60, 2) if lap.get("averageSpeed") else None,
                "hr": lap.get("averageHR"),
                "gct": round(lap.get("groundContactTime", 0), 1),
                "gct_balance": round(lap.get("groundContactBalanceLeft", 50), 1),
                "cadence": round(lap.get("averageRunningCadenceInStepsPerMinute", 0)),
            })
        data["runs"].append({
            "date": r["startTimeLocal"][:10],
            "distance": round(r.get("distance", 0) / 1000, 1),
            "duration": round(r.get("duration", 0) / 60, 1),
            "avg_hr": r.get("averageHR"),
            "avg_pace": round(1000 / r.get("averageSpeed", 1) / 60, 2) if r.get("averageSpeed") else None,
            "cadence": round(r.get("averageRunningCadenceInStepsPerMinute", 0)),
            "calories": r.get("calories"),
            "elevation": round(r.get("elevationGain", 0) or 0),
            "laps": lap_data,
        })

    # Cycling activities
    cycles = [a for a in activities if a.get("activityType", {}).get("typeKey") == "road_biking"]
    data["cycles"] = []
    for c in cycles[:8]:
        data["cycles"].append({
            "date": c["startTimeLocal"][:10],
            "distance": round(c.get("distance", 0) / 1000, 1),
            "duration": round(c.get("duration", 0) / 60, 1),
            "avg_hr": c.get("averageHR"),
            "avg_speed": round(c.get("averageSpeed", 0) * 3.6, 1),  # m/s to km/h
            "max_speed": round(c.get("maxSpeed", 0) * 3.6, 1),
            "calories": c.get("calories"),
            "elevation": round(c.get("elevationGain", 0)),
        })

    # Training readiness
    try:
        readiness = garmin.get_morning_training_readiness(TODAY)
        data["readiness"] = {
            "score": readiness.get("score"),
            "level": readiness.get("level"),
            "sleep_score": readiness.get("sleepScore"),
            "hrv_weekly_avg": readiness.get("hrvWeeklyAverage"),
            "recovery_time": readiness.get("recoveryTime"),
            "acwr": round(readiness.get("acwrFactorPercent", 0) / 100, 2),
            "feedback": readiness.get("feedbackShort", "").replace("_", " ").title(),
        }
    except:
        data["readiness"] = {}

    # HRV
    try:
        hrv = garmin.get_hrv_data(TODAY)
        readings = hrv.get("hrvReadings", [])
        # Sample every 3rd reading to keep chart readable (Garmin records ~every 5 min)
        sampled = readings[::3] if len(readings) > 40 else readings
        data["hrv"] = {
            "values": [r["hrvValue"] for r in sampled],
            "times": [r["readingTimeLocal"][11:16] for r in sampled],
            "weekly_avg": data.get("readiness", {}).get("hrv_weekly_avg"),
        }
        print(f"  HRV readings: {len(readings)} total, {len(sampled)} sampled, range {sampled[0]['readingTimeLocal'][11:16] if sampled else '?'} - {sampled[-1]['readingTimeLocal'][11:16] if sampled else '?'}")
    except Exception as e:
        print(f"  HRV fetch failed: {e}")
        data["hrv"] = {}

    # Resting HR
    try:
        hr = garmin.get_heart_rates(TODAY)
        data["resting_hr"] = hr.get("restingHeartRate")
        data["min_hr"] = hr.get("minHeartRate")
        data["max_hr"] = hr.get("maxHeartRate")
    except:
        data["resting_hr"] = None

    # Body battery
    try:
        bb = garmin.get_body_battery(TODAY)
        if bb and isinstance(bb, list):
            vals = [x[1] for x in bb[0].get("bodyBatteryValuesArray", []) if x[1] is not None]
            data["body_battery"] = vals[-1] if vals else None
        else:
            data["body_battery"] = None
    except:
        data["body_battery"] = None

    # VO2 Max + Race predictions
    try:
        status = garmin.get_training_status(TODAY)
        vo2 = status.get("mostRecentVO2Max", {}).get("generic", {})
        data["vo2max"] = round(vo2.get("vo2MaxPreciseValue", 0), 1) if vo2.get("vo2MaxPreciseValue") else None
        load = status.get("mostRecentTrainingLoadBalance", {}).get("metricsTrainingLoadBalanceDTOMap", {})
        if load:
            first = list(load.values())[0]
            data["training_load"] = {
                "aerobic_low":  round(first.get("monthlyLoadAerobicLow", 0)),
                "aerobic_high": round(first.get("monthlyLoadAerobicHigh", 0)),
                "anaerobic":    round(first.get("monthlyLoadAnaerobic", 0)),
                "target_low_min": first.get("monthlyLoadAerobicLowTargetMin"),
                "target_low_max": first.get("monthlyLoadAerobicLowTargetMax"),
                "target_high_min": first.get("monthlyLoadAerobicHighTargetMin"),
                "target_high_max": first.get("monthlyLoadAerobicHighTargetMax"),
            }
        else:
            data["training_load"] = {}
    except Exception as e:
        print(f"  Training status failed: {e}")
        data["vo2max"] = None
        data["training_load"] = {}

    # Race predictions
    try:
        preds = garmin.get_race_predictions()
        def secs_to_time(s):
            if not s: return "--"
            h, m, sec = s//3600, (s%3600)//60, s%60
            return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
        data["race_predictions"] = {
            "5k":      secs_to_time(preds.get("time5K")),
            "10k":     secs_to_time(preds.get("time10K")),
            "half":    secs_to_time(preds.get("timeHalfMarathon")),
            "marathon": secs_to_time(preds.get("timeMarathon")),
            "marathon_secs": preds.get("timeMarathon"),
        }
    except:
        data["race_predictions"] = {}

    # Endurance score
    try:
        end = garmin.get_endurance_score(TODAY, TODAY)
        score_dto = end.get("enduranceScoreDTO", {})
        data["endurance_score"] = {
            "score": score_dto.get("overallScore"),
            "classification": score_dto.get("classification"),
            "lower_well_trained": score_dto.get("classificationLowerLimitWellTrained"),
            "lower_expert": score_dto.get("classificationLowerLimitExpert"),
        }
    except:
        data["endurance_score"] = {}

    # Running tolerance (injury prevention — load vs capacity)
    try:
        tol = garmin.get_running_tolerance(TODAY, TODAY)
        if tol:
            t = tol[0]
            data["running_tolerance"] = {
                "load": t.get("totalImpactLoad"),
                "tolerance": t.get("tolerance"),
                "pct": round(t.get("totalImpactLoad", 0) / t.get("tolerance", 1) * 100) if t.get("tolerance") else None,
            }
        else:
            data["running_tolerance"] = {}
    except:
        data["running_tolerance"] = {}

    # Intensity minutes
    try:
        im = garmin.get_intensity_minutes_data(TODAY)
        data["intensity_minutes"] = {
            "weekly_total": im.get("weeklyTotal"),
            "weekly_moderate": im.get("weeklyModerate"),
            "weekly_vigorous": im.get("weeklyVigorous"),
            "goal": im.get("weekGoal", 150),
            "goal_met_day": im.get("dayOfGoalMet"),
        }
    except:
        data["intensity_minutes"] = {}

    # Sleep data
    # Garmin's get_sleep_data(date) returns the session that ENDED on `date`
    # (i.e. the date you woke up) -- not the date you fell asleep. Use TODAY
    # to get last night's sleep, matching get_morning_training_readiness(TODAY)
    # above, which already correctly reflects last night. Using YESTERDAY here
    # returned the wrong, one-night-too-old session (e.g. on a Sunday it
    # showed Friday->Saturday's sleep instead of Saturday->Sunday's).
    try:
        sleep = garmin.get_sleep_data(TODAY)
        daily = sleep.get("dailySleepDTO", {})

        # Sleep stage timeline — convert GMT to Melbourne local HH:MM
        stage_map = {0.0: "deep", 1.0: "light", 2.0: "awake", 3.0: "rem",
                     0: "deep", 1: "light", 2: "awake", 3: "rem"}
        sleep_levels = []
        raw_levels = sleep.get("sleepLevels", [])
        print(f"  Raw sleep levels from Garmin: {len(raw_levels)}")
        for level in raw_levels:
            try:
                start_gmt = level.get("startGMT", "")
                end_gmt   = level.get("endGMT", "")
                start_dt = datetime.fromisoformat(start_gmt.rstrip("0").rstrip(".")).replace(tzinfo=timezone.utc).astimezone(MELB_TZ)
                end_dt   = datetime.fromisoformat(end_gmt.rstrip("0").rstrip(".")).replace(tzinfo=timezone.utc).astimezone(MELB_TZ)
                sleep_levels.append({
                    "start": start_dt.strftime("%H:%M"),
                    "end":   end_dt.strftime("%H:%M"),
                    "stage": stage_map.get(level.get("activityLevel"), "light"),
                })
            except Exception as e:
                print(f"  Sleep level parse error: {e} — {level}")
        print(f"  Parsed sleep levels: {len(sleep_levels)}, sample: {sleep_levels[:2] if sleep_levels else 'none'}")

        # Sleep start in minutes from midnight for chart alignment
        sleep_start_ts = daily.get("sleepStartTimestampLocal", 0)

        data["sleep"] = {
            "duration_hrs": round(daily.get("sleepTimeSeconds", 0) / 3600, 1),
            "deep_hrs": round(daily.get("deepSleepSeconds", 0) / 3600, 1),
            "light_hrs": round(daily.get("lightSleepSeconds", 0) / 3600, 1),
            "rem_hrs": round(daily.get("remSleepSeconds", 0) / 3600, 1),
            "awake_hrs": round(daily.get("awakeSleepSeconds", 0) / 3600, 1),
            "score": daily.get("sleepScores", {}).get("overall", {}).get("value"),
            "start": sleep_start_ts,
            "end": daily.get("sleepEndTimestampLocal"),
            "avg_spo2": daily.get("averageSpO2Value"),
            "avg_respiration": daily.get("averageRespirationValue"),
            "avg_stress": daily.get("avgSleepStress"),
            "levels": sleep_levels,
        }
    except:
        data["sleep"] = {}

    return data

# ─── WITHINGS ────────────────────────────────────────────────────────────────

def fetch_withings_bp():
    token_data = json.load(open(WITHINGS_TOKEN_FILE))
    access_token = token_data["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.post(
        "https://wbsapi.withings.net/measure",
        headers=headers,
        data={"action": "getmeas", "meastypes": "9,10,11"}
    )
    result = resp.json()
    readings = []
    for grp in result.get("body", {}).get("measuregrps", []):
        entry = {"date": datetime.fromtimestamp(grp["date"], tz=MELB_TZ).strftime("%Y-%m-%d %H:%M")}
        for m in grp.get("measures", []):
            val = m["value"] * (10 ** m["unit"])
            if m["type"] == 9:   entry["diastolic"] = round(val)
            elif m["type"] == 10: entry["systolic"] = round(val)
            elif m["type"] == 11: entry["pulse"] = round(val)
        if "systolic" in entry:
            readings.append(entry)
    return readings[:20]  # last 20 readings

# ─── HTML GENERATION ─────────────────────────────────────────────────────────

def pace_str(pace_min):
    if not pace_min: return "--"
    m = int(pace_min)
    s = int((pace_min - m) * 60)
    return f"{m}:{s:02d}"

# ─── INTERVALS.ICU ───────────────────────────────────────────────────────────

INTERVALS_ATHLETE_ID = os.environ.get("INTERVALS_ATHLETE_ID", "i416094")
INTERVALS_API_KEY    = os.environ.get("INTERVALS_API_KEY", "")

def fetch_intervals():
    try:
        auth = ("API_KEY", INTERVALS_API_KEY)
        base = "https://intervals.icu/api/v1"
        today = date.today().isoformat()
        start = (date.today() - timedelta(days=90)).isoformat()

        # Wellness — CTL, ATL, TSB
        r = requests.get(
            f"{base}/athlete/{INTERVALS_ATHLETE_ID}/wellness",
            auth=auth, params={"oldest": start, "newest": today}
        )
        if r.status_code != 200:
            print(f"  intervals.icu wellness error: {r.status_code}")
            return None

        wellness = r.json()
        ctl_atl = []
        for d in wellness:
            if d.get("ctl") is not None:
                ctl_atl.append({
                    "date": d["id"],
                    "ctl":  round(d["ctl"], 1),
                    "atl":  round(d["atl"], 1),
                    "tsb":  round(d["ctl"] - d["atl"], 1),
                    "ramp": round(d.get("rampRate", 0), 2),
                })

        # Recent activities for training load
        r2 = requests.get(
            f"{base}/athlete/{INTERVALS_ATHLETE_ID}/activities",
            auth=auth, params={"oldest": (date.today() - timedelta(days=30)).isoformat(), "newest": today}
        )
        activities = r2.json() if r2.status_code == 200 else []
        load_data = []
        for a in activities:
            if a.get("icu_training_load"):
                load_data.append({
                    "date": a["start_date_local"][:10],
                    "type": a.get("type", ""),
                    "load": a["icu_training_load"],
                    "ctl":  round(a.get("icu_ctl", 0), 1),
                    "atl":  round(a.get("icu_atl", 0), 1),
                    "hr_zones": a.get("icu_hr_zone_times", []),
                    "decoupling": a.get("decoupling"),
                    "efficiency": a.get("icu_efficiency_factor"),
                    "intensity": a.get("icu_intensity"),
                })

        latest = ctl_atl[-1] if ctl_atl else {}
        print(f"  CTL: {latest.get('ctl')}  ATL: {latest.get('atl')}  TSB: {latest.get('tsb')}")

        return {
            "ctl_atl": ctl_atl,
            "load_data": load_data,
            "latest": latest,
        }
    except Exception as e:
        print(f"  intervals.icu fetch failed: {e}")
        return None

# ─── WEATHER ─────────────────────────────────────────────────────────────────
# Wantirna South, VIC coordinates
WEATHER_LAT = -37.8557
WEATHER_LNG = 145.2311

def fetch_weather():
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude":  WEATHER_LAT,
                "longitude": WEATHER_LNG,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,windspeed_10m_max,weathercode",
                "hourly": "temperature_2m,precipitation_probability,windspeed_10m",
                "timezone": "Australia/Melbourne",
                "forecast_days": 7,
            },
            timeout=10
        )
        data = resp.json()
        daily = data["daily"]
        hourly = data["hourly"]

        def window_score(hours):
            if not hours: return None
            avg_temp = sum(hourly["temperature_2m"][h] for h in hours) / len(hours)
            avg_rain = sum(hourly["precipitation_probability"][h] for h in hours) / len(hours)
            avg_wind = sum(hourly["windspeed_10m"][h] for h in hours) / len(hours)
            score = avg_temp * 0.5 + avg_rain * 0.3 + avg_wind * 0.2
            return {"score": score, "temp": round(avg_temp,1), "rain": round(avg_rain), "wind": round(avg_wind,1)}

        # Build daily forecasts with best run windows
        days = []
        for i in range(7):
            day_date = daily["time"][i]
            morning_hours   = [j for j, t in enumerate(hourly["time"]) if t.startswith(day_date) and t[11:13] in ["06","07","08","09"]]
            afternoon_hours = [j for j, t in enumerate(hourly["time"]) if t.startswith(day_date) and t[11:13] in ["16","17","18","19"]]
            morning   = window_score(morning_hours)
            afternoon = window_score(afternoon_hours)
            if morning and afternoon:
                best = "Morning" if morning["score"] <= afternoon["score"] else "Afternoon"
                best_detail = morning if best == "Morning" else afternoon
            elif morning:
                best = "Morning"
                best_detail = morning
            elif afternoon:
                best = "Afternoon"
                best_detail = afternoon
            else:
                best = "--"
                best_detail = None

            max_t = daily["temperature_2m_max"][i]
            rain  = daily["precipitation_probability_max"][i]
            wind  = daily["windspeed_10m_max"][i]
            warnings = []
            if max_t >= 28: warnings.append(f"Heat {max_t}°C")
            if rain >= 60:  warnings.append(f"Rain {rain}%")
            if wind >= 30:  warnings.append(f"Wind {wind}km/h")

            days.append({
                "date":        day_date,
                "max_temp":    max_t,
                "min_temp":    daily["temperature_2m_min"][i],
                "rain_pct":    rain,
                "wind_max":    wind,
                "weathercode": daily["weathercode"][i],
                "best_window": best,
                "best_detail": best_detail,
                "morning":     morning,
                "afternoon":   afternoon,
                "warnings":    warnings,
            })

        # Tomorrow summary (index 1) for top card
        tmr = days[1]
        warnings = tmr["warnings"]
        return {
            "days": days,
            "best_window": tmr["best_window"],
            "morning": tmr["morning"],
            "afternoon": tmr["afternoon"],
            "warnings": warnings,
        }
    except Exception as e:
        print(f"  Weather fetch failed: {e}")
        return None

WEATHER_CODES = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Icy fog", 51: "Light drizzle", 53: "Drizzle",
    55: "Heavy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 80: "Rain showers",
    81: "Showers", 82: "Heavy showers", 95: "Thunderstorm", 99: "Hailstorm",
}

def weather_icon(code):
    if code == 0: return "☀️"
    if code in (1, 2): return "⛅"
    if code == 3: return "☁️"
    if code in (45, 48): return "🌫️"
    if code in (51, 53, 55, 61, 63, 65, 80, 81, 82): return "🌧️"
    if code in (71, 73, 75): return "❄️"
    if code in (95, 99): return "⛈️"
    return "🌤️"



PLAN_START = date(2026, 5, 1)
RACE_DATE  = date(2026, 10, 12)
INJURY_DATE = date(2026, 1, 17)  # limping after the run on this date — left Achilles insertional tendinopathy onset

PHASES = [
    {"name": "Rehab / Base",       "weeks": (1, 4),   "km_min": 20, "km_max": 30,  "focus": "Easy aerobic, seated calf raises"},
    {"name": "Return to Run",      "weeks": (5, 8),   "km_min": 30, "km_max": 40,  "focus": "Gradual mileage build, form drills"},
    {"name": "Build",              "weeks": (9, 16),  "km_min": 40, "km_max": 60,  "focus": "Tempo runs, long run progression"},
    {"name": "Marathon Specific",  "weeks": (17, 24), "km_min": 60, "km_max": 75,  "focus": "Race-pace long runs, lactate work"},
    {"name": "Taper",              "weeks": (25, 28), "km_min": 30, "km_max": 40,  "focus": "Reduce volume, sharpen, rest"},
]

def get_training_phase():
    today = date.today()
    week_num = min(int((today - PLAN_START).days / 7) + 1, 28)
    days_to_race = (RACE_DATE - today).days
    phase = next((p for p in PHASES if p["weeks"][0] <= week_num <= p["weeks"][1]), PHASES[-1])
    week_in_phase = week_num - phase["weeks"][0] + 1
    phase_total   = phase["weeks"][1] - phase["weeks"][0] + 1
    return {
        "week_num": week_num,
        "phase": phase,
        "week_in_phase": week_in_phase,
        "phase_total": phase_total,
        "days_to_race": days_to_race,
        "phase_pct": round(week_in_phase / phase_total * 100),
    }

# ─── SCORE HISTORY ───────────────────────────────────────────────────────────

HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "score_history.json")

def save_score_history(garmin_data, achilles, recovery, tissue_capacity, monotony, load_data, checkin=None):
    """Append (or overwrite) today's scores + key inputs to score_history.json.
    Overwrites today's entry rather than appending, so running this twice in
    one day (morning + evening cron) doesn't create duplicates — the later
    run wins since it has the day's completed training data.

    checkin: today's subjective check-in dict (from get_latest_checkin), if
    any. Stored alongside the objective Garmin/run data so injury_model.py's
    replay_injury_penalty() has real check-in history to work from, instead
    of needing a separate file lookup at scoring time. Only stored if it's
    actually FOR today (not yesterday's, which get_latest_checkin can return
    within its 24h grace window) — otherwise a rest-day's entry could
    silently inherit yesterday's symptom report under today's date."""
    history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                history = json.load(f)
        except Exception as e:
            print(f"  Score history load failed: {e}")
            history = []

    readiness = garmin_data.get("readiness", {})
    runs = garmin_data.get("runs", [])
    last_run = runs[0] if runs else {}
    ran_today = bool(last_run) and last_run.get("date") == TODAY

    checkin_is_for_today = bool(checkin) and checkin.get("date") == TODAY

    # If this call doesn't have a valid same-day check-in, don't blindly
    # null out today's checkin fields -- a different run earlier today
    # (e.g. the morning cron) may have already saved real check-in data.
    # Overwriting wholesale would silently discard it. Fall back to
    # whatever's already recorded for today, if anything.
    existing_today = next((h for h in history if h.get("date") == TODAY), None)
    existing_inputs = (existing_today or {}).get("inputs", {})

    if checkin_is_for_today:
        checkin_stiffness = checkin.get("stiffness")
        checkin_first_steps_pain = checkin.get("first_steps_pain")
        checkin_post_run_pain = checkin.get("post_run_pain")
        checkin_calf_raises = checkin.get("calf_raises")
    else:
        checkin_stiffness = existing_inputs.get("checkin_stiffness")
        checkin_first_steps_pain = existing_inputs.get("checkin_first_steps_pain")
        checkin_post_run_pain = existing_inputs.get("checkin_post_run_pain")
        checkin_calf_raises = existing_inputs.get("checkin_calf_raises")

    entry = {
        "date": TODAY,
        "inputs": {
            "resting_hr": garmin_data.get("resting_hr"),
            "hrv": readiness.get("hrv_weekly_avg"),
            "sleep_score": readiness.get("sleep_score"),
            "body_battery": garmin_data.get("body_battery"),
            "ran_today": ran_today,
            "last_run": {
                "date": last_run.get("date"),
                "distance": last_run.get("distance"),
                "pace": last_run.get("avg_pace"),
                "avg_hr": last_run.get("avg_hr"),
            } if last_run else None,
            "checkin_stiffness": checkin_stiffness,
            "checkin_first_steps_pain": checkin_first_steps_pain,
            "checkin_post_run_pain": checkin_post_run_pain,
            "checkin_calf_raises": checkin_calf_raises,
        },
        "scores": {
            "readiness": readiness.get("score"),
            "recovery": recovery.get("score"),
            "achilles": achilles.get("score"),
            "tissue_capacity": tissue_capacity.get("score"),
            "monotony": monotony.get("score"),
            "acr": load_data.get("acr"),
        },
    }

    history = [h for h in history if h.get("date") != TODAY]
    history.append(entry)
    history.sort(key=lambda h: h["date"])

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    print(f"  Score history: {len(history)} days total")

# ─── DAILY CHECK-IN ──────────────────────────────────────────────────────────

CHECKINS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkins.json")

def load_checkins():
    """Load committed check-in history (synced from browser export)."""
    try:
        if os.path.exists(CHECKINS_FILE):
            with open(CHECKINS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return sorted(data, key=lambda x: x.get("date", ""), reverse=True)
    except Exception as e:
        print(f"  Check-ins load failed: {e}")
    return []

def get_latest_checkin(checkins):
    """Return today's check-in, or yesterday's if within 24h relevance window."""
    if not checkins:
        return None
    today = date.today()
    for c in checkins:
        if c.get("date") == today.isoformat():
            return c
    for c in checkins:
        d = c.get("date", "")
        if not d:
            continue
        try:
            if (today - datetime.strptime(d, "%Y-%m-%d").date()).days <= 1:
                return c
        except ValueError:
            continue
    return None

def apply_checkin_factors(achilles, checkin):
    """Blend subjective morning check-in into Achilles watch-list score."""
    if not checkin or achilles.get("score") is None:
        return achilles

    result = {
        **achilles,
        "factors": list(achilles.get("factors", [])),
        "score": achilles.get("score", 0),
    }
    subjective = []

    stiffness = int(checkin.get("stiffness", 0) or 0)
    first_steps = int(checkin.get("first_steps_pain", 0) or 0)
    post_run = int(checkin.get("post_run_pain", 0) or 0)
    calf_raises = bool(checkin.get("calf_raises"))

    if stiffness >= 7:
        result["score"] = min(100, result["score"] + 15)
        subjective.append({"label": "Morning stiffness", "value": f"{stiffness}/10 — elevated", "level": "high", "points": 15})
    elif stiffness >= 4:
        result["score"] = min(100, result["score"] + 8)
        subjective.append({"label": "Morning stiffness", "value": f"{stiffness}/10 — moderate", "level": "medium", "points": 8})
    else:
        subjective.append({"label": "Morning stiffness", "value": f"{stiffness}/10 ✓", "level": "low", "points": 0})

    if first_steps >= 5:
        result["score"] = min(100, result["score"] + 12)
        subjective.append({"label": "First-steps pain", "value": f"{first_steps}/10", "level": "high", "points": 12})
    elif first_steps >= 3:
        result["score"] = min(100, result["score"] + 6)
        subjective.append({"label": "First-steps pain", "value": f"{first_steps}/10", "level": "medium", "points": 6})
    else:
        subjective.append({"label": "First-steps pain", "value": f"{first_steps}/10 ✓", "level": "low", "points": 0})

    if post_run >= 5:
        result["score"] = min(100, result["score"] + 10)
        subjective.append({"label": "Post-run pain", "value": f"{post_run}/10", "level": "high", "points": 10})
    elif post_run >= 3:
        subjective.append({"label": "Post-run pain", "value": f"{post_run}/10", "level": "medium", "points": 0})
    else:
        subjective.append({"label": "Post-run pain", "value": f"{post_run}/10 ✓", "level": "low", "points": 0})

    subjective.append({
        "label": "Seated calf raises",
        "value": "Done ✓" if calf_raises else "Not logged today",
        "level": "low" if calf_raises else "medium",
        "points": 0,
    })

    result["factors"] = subjective + result["factors"]
    score = result["score"]
    if score <= 20:
        result["level"] = "low"
        result["tier"] = "Nothing notable"
    elif score <= 45:
        result["level"] = "medium"
        result["tier"] = "Watch — monitor load and form"
    elif score <= 70:
        result["level"] = "high"
        result["tier"] = "Several flags — ease back, consider physio"
    else:
        result["level"] = "high"
        result["tier"] = "Many flags stacked — rest and professional assessment"
    return result

# ─── COACHING ENGINE ─────────────────────────────────────────────────────────

def compute_training_load(run):
    """
    Training Load = Distance x Intensity Factor x HR Factor
    Returns 0-100+ stress score for a single run.
    """
    dist = run.get("distance", 0)
    hr   = run.get("avg_hr", 0) or 0
    pace = run.get("avg_pace", 6.0) or 6.0
    elev = run.get("elevation", 0) or 0

    # HR intensity factor
    if hr < 130:      hr_factor = 0.7
    elif hr < 140:    hr_factor = 0.85
    elif hr < 150:    hr_factor = 1.0   # easy/moderate
    elif hr < 160:    hr_factor = 1.3   # moderate/hard
    else:             hr_factor = 1.6   # hard

    # Pace intensity factor (faster = more stress)
    if pace < 4.5:    pace_factor = 1.5
    elif pace < 5.0:  pace_factor = 1.3
    elif pace < 5.5:  pace_factor = 1.1
    elif pace < 6.0:  pace_factor = 1.0
    else:             pace_factor = 0.9

    # Elevation factor
    elev_factor = 1.0 + (elev / 1000) * 0.2  # +20% per 1000m gain

    load = dist * hr_factor * pace_factor * elev_factor
    return round(load, 1)

def classify_session(run):
    """Classify run as easy/moderate/hard based on HR."""
    hr = run.get("avg_hr", 0) or 0
    if hr < 140:   return "easy"
    elif hr < 155: return "moderate"
    else:          return "hard"

def compute_weekly_loads(runs):
    """Compute per-run loads and weekly acute/chronic totals."""
    from collections import defaultdict
    today   = datetime.now(MELB_TZ).date()
    loads   = {}
    weekly  = defaultdict(float)
    session_types = []

    for r in runs:
        load = compute_training_load(r)
        loads[r["date"]] = load
        d  = datetime.strptime(r["date"], "%Y-%m-%d").date()
        wk = datetime.strptime(r["date"], "%Y-%m-%d").strftime("%Y-W%W")
        weekly[wk] += load
        days_ago = (today - d).days
        session_types.append({
            "date": r["date"],
            "days_ago": days_ago,
            "load": load,
            "type": classify_session(r),
            "distance": r["distance"],
            "hr": r.get("avg_hr", 0),
        })

    # Acute load = last 7 days
    acute  = sum(s["load"] for s in session_types if s["days_ago"] <= 7)
    # Chronic load = last 28 days / 4 (weekly avg)
    chronic_total = sum(s["load"] for s in session_types if s["days_ago"] <= 28)
    chronic = chronic_total / 4 if chronic_total > 0 else 1

    acr = round(acute / chronic, 2) if chronic > 0 else 1.0

    # Intensity distribution last 7 days
    recent = [s for s in session_types if s["days_ago"] <= 7]
    total_recent = len(recent) or 1
    easy_pct = round(sum(1 for s in recent if s["type"] == "easy") / total_recent * 100)
    mod_pct  = round(sum(1 for s in recent if s["type"] == "moderate") / total_recent * 100)
    hard_pct = round(sum(1 for s in recent if s["type"] == "hard") / total_recent * 100)

    # Back-to-back stress — hard/long runs within 4 days
    hard_long = [s for s in session_types if s["days_ago"] <= 7 and (s["type"] == "hard" or s["distance"] >= 14)]
    btb_risk = False
    if len(hard_long) >= 2:
        dates = sorted([datetime.strptime(s["date"], "%Y-%m-%d") for s in hard_long])
        for i in range(1, len(dates)):
            if (dates[i] - dates[i-1]).days <= 4:
                btb_risk = True
                break

    return {
        "acute": round(acute, 1),
        "chronic": round(chronic, 1),
        "acr": acr,
        "acr_status": "optimal" if 0.8 <= acr <= 1.3 else "high" if acr > 1.3 else "low",
        "easy_pct": easy_pct,
        "mod_pct": mod_pct,
        "hard_pct": hard_pct,
        "btb_risk": btb_risk,
        "session_types": session_types,
        "weekly": dict(weekly),
    }

    return {
        "acute": round(acute, 1),
        "chronic": round(chronic, 1),
        "acr": acr,
        "acr_status": "optimal" if 0.8 <= acr <= 1.3 else "high" if acr > 1.3 else "low",
        "easy_pct": easy_pct,
        "mod_pct": mod_pct,
        "hard_pct": hard_pct,
        "btb_risk": btb_risk,
        "session_types": session_types,
        "weekly": dict(weekly),
    }


def compute_tissue_capacity(runs, as_of=None):
    """
    Tissue Capacity Score 0-100.
    Answers: "What is your tendon currently prepared to handle?"

    This is the missing piece. Same 20km run = very different risk depending on
    what the tendon has been consistently exposed to recently.

    High capacity = tendon has been progressively loaded and is adapted.
    Low capacity  = sudden exposure to stress without preparation.

    Inputs:
      - 28-day running volume (primary — Achilles adapts to consistent load)
      - Longest recent run (longest single exposure)
      - Number of runs >60 min (repeated long exposures build capacity)
      - Run frequency (consistent stimulus = adaptation)
      - Previous Achilles history (permanent -15 penalty — never fully resets)

    as_of: reference date for the 28-day window. Defaults to the real
    current date (production use). Pass an explicit date when computing
    scores for a historical date (e.g. backfilling) — otherwise the window
    is measured from the wrong day.
    """
    if not runs:
        return {"score": 0, "level": "low", "summary": "No run data — capacity unknown",
                "limiting_factor": "Insufficient data"}

    today = as_of or datetime.now(MELB_TZ).date()
    score = 0

    # ── Factor 1: 28-day running volume (50% of score) ────────────────────────
    # A runner doing 40km/week for 4 weeks has well-adapted tendons
    # A runner doing 10km/week does not
    runs_28d = [r for r in runs if (today - datetime.strptime(r["date"], "%Y-%m-%d").date()).days <= 28]
    vol_28d  = sum(r["distance"] for r in runs_28d)

    if vol_28d >= 160:     vol_score = 50   # 40+ km/week for 4 weeks — well adapted
    elif vol_28d >= 120:   vol_score = 42   # 30+ km/week — moderate adaptation
    elif vol_28d >= 80:    vol_score = 32   # 20+ km/week — some adaptation
    elif vol_28d >= 40:    vol_score = 20   # 10+ km/week — limited adaptation
    else:                  vol_score = 8    # very low — minimal adaptation
    score += vol_score

    # ── Factor 2: Longest run in last 28 days (20% of score) ──────────────────
    # Tendon needs to have been exposed to long runs to handle long runs
    longest_28d = max((r["distance"] for r in runs_28d), default=0)
    if longest_28d >= 18:   long_score = 20
    elif longest_28d >= 14: long_score = 15
    elif longest_28d >= 10: long_score = 10
    elif longest_28d >= 6:  long_score = 5
    else:                   long_score = 0
    score += long_score

    # ── Factor 3: Number of runs >60 min in last 28 days (15% of score) ───────
    # Repeated long exposure builds tendon resilience
    long_runs = sum(1 for r in runs_28d if r.get("duration", 0) >= 60)
    if long_runs >= 6:     expo_score = 15
    elif long_runs >= 4:   expo_score = 11
    elif long_runs >= 2:   expo_score = 7
    elif long_runs >= 1:   expo_score = 3
    else:                  expo_score = 0
    score += expo_score

    # ── Factor 4: Run frequency (15% of score) ────────────────────────────────
    # Consistent stimulus = adaptation. Gaps disrupt adaptation.
    run_count_28d = len(runs_28d)
    if run_count_28d >= 16:   freq_score = 15  # ~4x/week
    elif run_count_28d >= 12: freq_score = 12  # ~3x/week
    elif run_count_28d >= 8:  freq_score = 8   # ~2x/week
    elif run_count_28d >= 4:  freq_score = 4   # ~1x/week
    else:                     freq_score = 0
    score += freq_score

    # ── Penalty: Previous Achilles injury — only applies from actual onset ─────
    # Scar tissue = lower capacity ceiling, but only once the injury exists.
    # Backfilling pre-injury dates with this penalty applied would corrupt
    # exactly the clean pre-injury baseline that's most valuable to have.
    if today >= INJURY_DATE:
        score -= 15

    score = max(0, min(score, 100))

    # Determine limiting factor
    if vol_score < 20:
        limiting = "28-day volume too low to have well-adapted tendons"
    elif long_score < 10:
        limiting = "No recent long run exposure — tendon not prepared for distance"
    elif expo_score < 7:
        limiting = "Few long run exposures — limited repeated-bout adaptation"
    elif freq_score < 8:
        limiting = "Inconsistent training frequency — gaps disrupt adaptation"
    else:
        limiting = "Previous injury history limits maximum capacity"

    if score >= 70:
        level = "high"
        summary = "Tendon well-adapted — consistent training base"
    elif score >= 45:
        level = "medium"
        summary = "Moderate capacity — proceed carefully with load increases"
    else:
        level = "low"
        summary = "Low capacity — tendon not yet prepared for high loads"

    return {
        "score":           score,
        "level":           level,
        "summary":         summary,
        "limiting_factor": limiting,
        "vol_28d":         round(vol_28d, 1),
        "longest_28d":     round(longest_28d, 1),
        "long_run_count":  long_runs,
        "run_count_28d":   run_count_28d,
    }


def compute_training_monotony(runs):
    """
    Training Monotony Score — how repetitive is the training stimulus?
    High monotony = same pace, same distance, same effort = repetitive loading.
    For Achilles: repetitive loading without variation = overuse pattern.

    Low monotony = good (varied stimulus, adaptation)
    High monotony = warning (repetitive tissue loading)
    """
    if len(runs) < 4:
        return {"score": 0, "level": "unknown", "detail": "Need 4+ runs for monotony analysis"}

    recent = runs[:7]  # last 7 runs
    distances = [r["distance"] for r in recent if r.get("distance")]
    paces     = [r["avg_pace"] for r in recent if r.get("avg_pace")]
    hrs       = [r["avg_hr"] for r in recent if r.get("avg_hr")]

    def coefficient_of_variation(vals):
        if len(vals) < 2: return 0
        mean = sum(vals) / len(vals)
        if mean == 0: return 0
        std  = (sum((v - mean)**2 for v in vals) / len(vals)) ** 0.5
        return round(std / mean * 100, 1)

    dist_cv = coefficient_of_variation(distances)
    pace_cv = coefficient_of_variation(paces)
    hr_cv   = coefficient_of_variation(hrs)

    # Low CV = high monotony (little variation)
    # Monotony score: 0 = varied, 100 = identical runs
    dist_mono = max(0, 30 - dist_cv)   # >30% distance variation = no monotony
    pace_mono = max(0, 15 - pace_cv)   # >15% pace variation = no monotony
    hr_mono   = max(0, 10 - hr_cv)     # >10% HR variation = no monotony

    # Normalise to 0-100
    monotony = min(100, round((dist_mono/30 * 40) + (pace_mono/15 * 35) + (hr_mono/10 * 25)))

    flags = []
    if dist_cv < 15: flags.append(f"Distance varies only {dist_cv:.0f}% — similar run lengths")
    if pace_cv < 8:  flags.append(f"Pace varies only {pace_cv:.0f}% — similar effort level")
    if hr_cv < 5:    flags.append(f"HR varies only {hr_cv:.0f}% — no intensity variation")

    return {
        "score":    monotony,
        "level":    "high" if monotony >= 65 else "medium" if monotony >= 35 else "low",
        "dist_cv":  dist_cv,
        "pace_cv":  pace_cv,
        "hr_cv":    hr_cv,
        "flags":    flags,
        "detail":   f"Distance CV: {dist_cv}% | Pace CV: {pace_cv}% | HR CV: {hr_cv}%",
    }


def generate_why_today(readiness, achilles, tissue_capacity, load_data, recovery, monotony, weather, checkin=None):
    """
    "Why today?" explanation engine.
    Generates a plain-English explanation of today's status.
    Returns positive factors, negative factors, and a decision sentence.
    """
    positives = []
    negatives = []
    decision  = ""

    r_score = readiness.get("score", 0) or 0
    a_score = achilles.get("score", 0) or 0
    tc_score = tissue_capacity.get("score", 0) or 0
    rec_score = recovery.get("score", 0) or 0
    acr = load_data.get("acr", 1.0)
    mono = monotony.get("score", 0)

    # Positives
    if r_score >= 70:  positives.append(f"Readiness {r_score}/100 — body ready to train")
    if rec_score >= 70: positives.append(f"Recovery {rec_score}/100 — well recovered")
    if acr <= 1.1:     positives.append(f"Load ratio {acr} — within safe zone")
    if a_score <= 20:  positives.append("Achilles watch-list clear")
    if tc_score >= 60: positives.append(f"Tissue capacity {tc_score}/100 — tendon well adapted")

    # Negatives
    if r_score < 50 and r_score > 0:  negatives.append(f"Readiness {r_score}/100 — body under-recovered")
    if rec_score < 50 and rec_score > 0: negatives.append(f"Recovery score low ({rec_score}/100)")
    if acr > 1.3:      negatives.append(f"Load ratio {acr} — acute load too high vs chronic")
    if a_score >= 46:  negatives.append(f"Achilles watch-list elevated ({a_score}/100)")
    if tc_score < 45:  negatives.append(f"Tissue capacity low ({tc_score}/100) — tendon not fully adapted")
    if mono >= 65:     negatives.append(f"Training monotony high — repetitive loading pattern")
    if load_data.get("btb_risk"): negatives.append("Back-to-back hard sessions detected")

    if checkin:
        stiffness = int(checkin.get("stiffness", 0) or 0)
        first_steps = int(checkin.get("first_steps_pain", 0) or 0)
        post_run = int(checkin.get("post_run_pain", 0) or 0)
        if stiffness >= 7:
            negatives.append(f"Morning stiffness {stiffness}/10 — tendon likely irritated")
        elif stiffness <= 2 and first_steps <= 2 and post_run <= 2:
            positives.append(f"Subjective check-in clear (stiffness {stiffness}/10)")
        if first_steps >= 5:
            negatives.append(f"First-steps pain {first_steps}/10")
        if post_run >= 5:
            negatives.append(f"Post-run pain {post_run}/10 — reduce load")
        if not checkin.get("calf_raises"):
            negatives.append("Seated calf raises not logged today")

    # Check today's weather
    if weather and weather.get("days"):
        today_w = weather["days"][0]
        if today_w.get("max_temp", 0) >= 28:
            negatives.append(f"Heat {today_w['max_temp']}°C — add 15-20s/km to pace targets")
        if today_w.get("rain_pct", 0) >= 60:
            negatives.append(f"Rain {today_w['rain_pct']}% — surface may be slippery")

    # Fitness ceiling vs injury ceiling
    cardio_ceiling = r_score
    tendon_ceiling = tc_score
    limiter = None
    if tendon_ceiling < cardio_ceiling - 20:
        limiter = f"⚠ Workout limited by tendon ({tendon_ceiling}/100), not cardio ({cardio_ceiling}/100)"

    # Decision sentence
    red_count = len([n for n in negatives if any(x in n for x in ["Achilles", "ratio", "capacity", "back-to-back"])])
    if red_count >= 2 or a_score >= 60:
        decision = "Swap today's planned session for easy running or rest. Several load flags are stacked."
    elif red_count == 1 or r_score < 50:
        decision = "Reduce today's session by 20-30%. One key flag — don't push through."
    elif not negatives:
        decision = "All clear — proceed with planned session."
    else:
        decision = "Mostly clear — proceed but stay within HR target."

    return {
        "positives": positives,
        "negatives": negatives,
        "decision":  decision,
        "limiter":   limiter,
    }


def compute_recovery_score(readiness, hrv, sleep, resting_hr):
    """
    Recovery Score v3 — weighted composite 0-100.
    Weights: HRV 35%, Sleep 30%, Resting HR 20%, Training load 15%
    Each input normalised to 0-100 against reasonable baselines.
    Bands: 80-100 = Green · 50-79 = Yellow · <50 = Red
    """
    components = []

    # HRV — 35% weight
    # Normalise vs typical recreational runner range 25-65ms
    hrv_avg = hrv.get("weekly_avg", 0) or 0
    if hrv_avg > 0:
        hrv_norm = min(100, max(0, (hrv_avg - 25) / (65 - 25) * 100))
        hrv_score = round(hrv_norm * 0.35)
        status = "good" if hrv_avg >= 45 else "moderate" if hrv_avg >= 30 else "low"
        components.append({"label": f"HRV ({hrv_avg}ms)", "score": hrv_score, "weight": "35%", "status": status})
    else:
        hrv_score = 17  # neutral when no data
        components.append({"label": "HRV (no data)", "score": hrv_score, "weight": "35%", "status": "unknown"})

    # Sleep — 30% weight
    sleep_score_val = readiness.get("sleep_score", 0) or 0
    sleep_dur = sleep.get("duration_hrs", 0) or 0
    if sleep_score_val > 0:
        sleep_norm = min(100, sleep_score_val)
        sleep_contrib = round(sleep_norm * 0.30)
        status = "good" if sleep_score_val >= 80 else "moderate" if sleep_score_val >= 60 else "low"
        components.append({"label": f"Sleep ({sleep_score_val}/100, {sleep_dur}h)", "score": sleep_contrib, "weight": "30%", "status": status})
    else:
        sleep_contrib = 15
        components.append({"label": "Sleep (no data)", "score": sleep_contrib, "weight": "30%", "status": "unknown"})

    # Resting HR — 20% weight
    # Normalise: 40bpm = excellent (100), 80bpm = poor (0)
    rhr = resting_hr if isinstance(resting_hr, (int, float)) and resting_hr > 0 else 0
    if rhr > 0:
        rhr_norm = min(100, max(0, (80 - rhr) / (80 - 40) * 100))
        rhr_contrib = round(rhr_norm * 0.20)
        status = "good" if rhr <= 50 else "moderate" if rhr <= 60 else "low"
        components.append({"label": f"Resting HR ({rhr} bpm)", "score": rhr_contrib, "weight": "20%", "status": status})
    else:
        rhr_contrib = 10
        components.append({"label": "Resting HR (no data)", "score": rhr_contrib, "weight": "20%", "status": "unknown"})

    # Training load (ACR) — 15% weight
    # Optimal ACR 0.8-1.3 = 100, >1.5 = 0
    r_score = readiness.get("score", 0) or 0
    if r_score > 0:
        load_norm = min(100, r_score)
        load_contrib = round(load_norm * 0.15)
        status = "good" if r_score >= 70 else "moderate" if r_score >= 50 else "low"
        components.append({"label": f"Readiness ({r_score}/100)", "score": load_contrib, "weight": "15%", "status": status})
    else:
        load_contrib = 7
        components.append({"label": "Readiness (no data)", "score": load_contrib, "weight": "15%", "status": "unknown"})

    total = hrv_score + sleep_contrib + rhr_contrib + load_contrib
    total = min(total, 100)

    return {
        "score":  total,
        "level":  "good" if total >= 80 else "moderate" if total >= 50 else "poor",
        "factors": components,
    }

def injury_detective(runs, achilles_score):
    """
    Look back 21 days and rank contributors to current injury risk.
    """
    today = datetime.now(MELB_TZ).date()
    recent = [r for r in runs if (today - datetime.strptime(r["date"], "%Y-%m-%d").date()).days <= 21]
    if not recent or achilles_score < 30:
        return []

    contributors = []
    for r in recent:
        load = compute_training_load(r)
        risk_pct = 0
        reasons = []
        dist = r.get("distance", 0)
        hr   = r.get("avg_hr", 0) or 0

        if dist >= 19:
            risk_pct += 42
            reasons.append(f"{dist}km long run")
        elif dist >= 16:
            risk_pct += 20
            reasons.append(f"{dist}km long run")

        if hr > 148 and dist >= 14:
            risk_pct += 15
            reasons.append(f"HR {hr} on long run")

        if load > 20:
            risk_pct += 10
            reasons.append(f"High load session ({load})")

        if risk_pct > 0:
            contributors.append({
                "date": r["date"],
                "distance": dist,
                "hr": hr,
                "load": load,
                "risk_pct": risk_pct,
                "reasons": reasons,
            })

    contributors.sort(key=lambda x: x["risk_pct"], reverse=True)
    return contributors[:4]

def generate_workout(decision, phase_info, achilles):
    """
    Generate specific workout recommendation based on current state.
    Goal: Sub-3:00 marathon.
    """
    phase = phase_info["phase"]
    level = decision.get("level", "easy")
    a_level = achilles.get("level", "low")
    week = phase_info["week_num"]

    if level == "rest":
        return {
            "type": "Rest",
            "description": "Complete rest or gentle walk only.",
            "distance": "0 km",
            "pace": "--",
            "hr": "--",
            "avoid": ["Running", "High impact"],
        }

    if a_level == "high":
        return {
            "type": "Easy Recovery Run",
            "description": "Short easy run only — Achilles risk is elevated.",
            "distance": "4-6 km",
            "pace": "6:00-6:30/km",
            "hr": "<135 bpm",
            "avoid": ["Tempo", "Intervals", "Hills", "Long run"],
        }

    if level == "easy":
        if week <= 8:
            return {
                "type": "Easy Base Run",
                "description": "Easy aerobic run. Focus on relaxed form and cadence.",
                "distance": "6-8 km",
                "pace": "5:45-6:15/km",
                "hr": "<145 bpm",
                "avoid": ["Tempo", "Intervals"],
            }
        else:
            return {
                "type": "Easy Run + Strides",
                "description": "Easy run with 4x20s strides at the end.",
                "distance": "8-10 km",
                "pace": "5:30-6:00/km",
                "hr": "<148 bpm",
                "avoid": ["Tempo", "Hills"],
            }

    if level == "moderate":
        if week <= 8:
            return {
                "type": "Moderate Easy Run",
                "description": "Comfortable effort — should be able to hold a conversation.",
                "distance": "8-10 km",
                "pace": "5:30-5:50/km",
                "hr": "<150 bpm",
                "avoid": ["Hard intervals", "Long run"],
            }
        else:
            return {
                "type": "Tempo Run",
                "description": "Warm up 2km, 4km at tempo effort, cool down 2km.",
                "distance": "8 km",
                "pace": "4:50-5:10/km (tempo portion)",
                "hr": "155-165 bpm",
                "avoid": ["Back-to-back hard sessions"],
            }

    # Hard day
    if week >= 17:
        return {
            "type": "Marathon Pace Run",
            "description": "Warm up 2km, 8-10km at marathon goal pace (4:15-4:20/km), cool down 2km.",
            "distance": "12-14 km",
            "pace": "4:15-4:20/km (race portion)",
            "hr": "158-168 bpm",
            "avoid": ["Skipping warm-up"],
        }
    return {
        "type": "Quality Session",
        "description": "Interval or tempo work based on phase focus.",
        "distance": "10 km",
        "pace": "5:00-5:20/km",
        "hr": "155-165 bpm",
        "avoid": ["Too much volume same week"],
    }

def compute_achilles_score(runs, phase_info, as_of=None):
    """
    Achilles Watch-List v3 — flag system, not a risk prediction.
    Based on patterns associated with Achilles overload in literature.
    High score = "worth paying attention to", not a probability of injury.
    Weights are unvalidated starting guesses — adjust based on personal outcome log.

    Bands:
      0-20:  Nothing notable
      21-45: Watch — keep an eye on load and form
      46-70: Several flags — consider easing back, check with physio
      71+:   Many flags stacked — consider rest and professional assessment

    as_of: reference date for the "last 7/5 days" windows. Defaults to the
    real current date (production use). Pass an explicit date when
    computing scores for a historical date (e.g. backfilling) — otherwise
    these windows are measured from the wrong day. `runs` should be ordered
    most-recent-first (matching Garmin's native order) since the cadence/GCT
    baseline below assumes that ordering.
    """
    if not runs:
        return {"score": None, "level": "low", "factors": [], "this_week_km": 0, "last_week_km": 0,
                "disclaimer": "No recent run data available."}

    from collections import defaultdict
    phase = phase_info["phase"]

    # ── Weekly mileage ────────────────────────────────────────────────────────
    weekly = defaultdict(float)
    for r in runs:
        d = datetime.strptime(r["date"], "%Y-%m-%d")
        wk = d.strftime("%Y-W%W")
        weekly[wk] += r["distance"]
    weeks_sorted = sorted(weekly.keys())
    this_week_km = weekly[weeks_sorted[-1]] if weeks_sorted else 0
    last_week_km = weekly[weeks_sorted[-2]] if len(weeks_sorted) >= 2 else this_week_km

    # ── Last 7 days ───────────────────────────────────────────────────────────
    today = as_of or datetime.now(MELB_TZ).date()
    recent_runs = [r for r in runs if (today - datetime.strptime(r["date"], "%Y-%m-%d").date()).days <= 7]
    recent_km   = sum(r["distance"] for r in recent_runs)

    # ── Last 5 days ───────────────────────────────────────────────────────────
    last5_runs  = [r for r in runs if (today - datetime.strptime(r["date"], "%Y-%m-%d").date()).days <= 5]

    # ── Baseline cadence (avg of runs 4-8 for comparison) ────────────────────
    baseline_cadence = None
    older_cadence_runs = [r for r in runs[3:8] if r.get("cadence") and r["cadence"] > 0]
    if older_cadence_runs:
        baseline_cadence = sum(r["cadence"] for r in older_cadence_runs) / len(older_cadence_runs)

    # ── Baseline GCT (avg of runs 4-8) ───────────────────────────────────────
    baseline_gct = None
    older_gct_runs = [r for r in runs[3:8] if r.get("laps")]
    if older_gct_runs:
        all_gcts = [l["gct"] for r in older_gct_runs for l in r["laps"] if l.get("gct")]
        if all_gcts:
            baseline_gct = sum(all_gcts) / len(all_gcts)

    factors = []
    score   = 0

    # ── Flag 1: Long run > 16km (+10) or > 20km (+20) ─────────────────────────
    if recent_runs:
        longest_run = max(r["distance"] for r in recent_runs)
        longest_hr  = next((r.get("avg_hr", 0) for r in recent_runs if r["distance"] == longest_run), 0) or 0
        if longest_run >= 20:
            score += 20
            factors.append({"label": "Long run >20km", "value": f"{longest_run:.1f}km", "level": "high", "points": 20})
        elif longest_run >= 16:
            score += 10
            factors.append({"label": "Long run >16km", "value": f"{longest_run:.1f}km", "level": "medium", "points": 10})
        else:
            factors.append({"label": "Long run distance", "value": f"{longest_run:.1f}km ✓", "level": "low", "points": 0})

        # ── Flag 2: Long run with HR > 150 (+15) ──────────────────────────────
        # Was >145 — but across 19 logged runs >=14km, average HR was 151.8
        # and >145 flagged 84% of them (basically every long run, controlled
        # effort or not). >150 splits closer to the real midpoint (47%
        # flagged), separating genuinely elevated-effort long runs from
        # normal ones instead of flagging almost everything.
        if longest_run >= 14 and longest_hr > 150:
            score += 15
            factors.append({"label": "Long run HR >150", "value": f"HR {longest_hr} on {longest_run:.1f}km", "level": "high", "points": 15})
        elif longest_run >= 14:
            factors.append({"label": "Long run HR", "value": f"HR {longest_hr} ✓", "level": "low", "points": 0})

    # ── Flag 3: 3+ big sessions in 5 days (+20) ───────────────────────────────
    big_sessions_5d = sum(1 for r in last5_runs
                          if r["distance"] >= 12 or (r.get("avg_hr", 0) or 0) > 145)
    if big_sessions_5d >= 3:
        score += 20
        factors.append({"label": "3+ big sessions in 5 days", "value": f"{big_sessions_5d} sessions", "level": "high", "points": 20})
    elif big_sessions_5d == 2:
        factors.append({"label": "Sessions in 5 days", "value": f"{big_sessions_5d} big sessions", "level": "medium", "points": 0})
    else:
        factors.append({"label": "Session density", "value": f"{big_sessions_5d} big sessions in 5 days ✓", "level": "low", "points": 0})

    # ── Flag 4: Cadence drop >5% vs baseline (+10) ────────────────────────────
    if baseline_cadence and recent_runs:
        recent_cadence_vals = [r["cadence"] for r in recent_runs[:3] if r.get("cadence") and r["cadence"] > 0]
        if recent_cadence_vals:
            recent_cadence_avg = sum(recent_cadence_vals) / len(recent_cadence_vals)
            cadence_drop_pct = (baseline_cadence - recent_cadence_avg) / baseline_cadence * 100
            if cadence_drop_pct > 5:
                score += 10
                factors.append({"label": "Cadence drop >5%", "value": f"↓{cadence_drop_pct:.1f}% vs baseline ({baseline_cadence:.0f}→{recent_cadence_avg:.0f} spm)", "level": "high", "points": 10})
            elif cadence_drop_pct > 2:
                factors.append({"label": "Cadence trend", "value": f"↓{cadence_drop_pct:.1f}% slight drop", "level": "medium", "points": 0})
            else:
                factors.append({"label": "Cadence", "value": f"Stable {recent_cadence_avg:.0f} spm ✓", "level": "low", "points": 0})

    # ── Flag 5: GCT increase >5% vs baseline (+10) ────────────────────────────
    if baseline_gct and runs[0].get("laps"):
        recent_gct_vals = [l["gct"] for l in runs[0]["laps"] if l.get("gct")]
        if recent_gct_vals:
            recent_gct_avg = sum(recent_gct_vals) / len(recent_gct_vals)
            gct_increase_pct = (recent_gct_avg - baseline_gct) / baseline_gct * 100
            if gct_increase_pct > 5:
                score += 10
                factors.append({"label": "GCT increase >5%", "value": f"↑{gct_increase_pct:.1f}% vs baseline ({baseline_gct:.0f}→{recent_gct_avg:.0f}ms)", "level": "high", "points": 10})
            else:
                factors.append({"label": "GCT", "value": f"{recent_gct_avg:.0f}ms (baseline {baseline_gct:.0f}ms) ✓", "level": "low", "points": 0})

    # ── Flag 6: Previous Achilles injury (gated on actual onset date) ──────────
    # Only applies from INJURY_DATE forward — unconditionally applying this
    # broke backfilling, since pre-injury days got penalized for an injury
    # that hadn't happened yet. No decay yet (still flat +15 once active) —
    # that's a deliberate follow-up, not done here.
    if today >= INJURY_DATE:
        score += 15
        days_since = (today - INJURY_DATE).days
        factors.append({"label": "Previous Achilles injury", "value": f"Left insertional (onset {INJURY_DATE.isoformat()}, {days_since}d ago)", "level": "high", "points": 15})

    # ── Week-on-week mileage ──────────────────────────────────────────────────
    if last_week_km > 0:
        wow_change = (this_week_km - last_week_km) / last_week_km * 100
        if wow_change > 20:
            score += 10
            factors.append({"label": "Mileage spike", "value": f"+{wow_change:.0f}% vs last week", "level": "high", "points": 10})
        elif wow_change > 10:
            factors.append({"label": "Mileage change", "value": f"+{wow_change:.0f}% vs last week", "level": "medium", "points": 0})
        else:
            factors.append({"label": "Mileage change", "value": f"{wow_change:+.0f}% vs last week ✓", "level": "low", "points": 0})

    score = min(score, 100)
    if score <= 20:
        level = "low"
        tier  = "Nothing notable"
    elif score <= 45:
        level = "medium"
        tier  = "Watch — monitor load and form"
    elif score <= 70:
        level = "high"
        tier  = "Several flags — ease back, consider physio"
    else:
        level = "high"
        tier  = "Many flags stacked — rest and professional assessment"

    return {
        "score":         score,
        "level":         level,
        "tier":          tier,
        "factors":       factors,
        "this_week_km":  round(this_week_km, 1),
        "last_week_km":  round(last_week_km, 1),
        "recent_km":     round(recent_km, 1),
        "disclaimer":    "This is a flag checklist, not a validated injury prediction. High score = worth paying attention to.",
    }


# ─── AI COMMENTARY ───────────────────────────────────────────────────────────

def get_ai_commentary(garmin_data, bp_readings, phase_info, achilles):
    try:
        runs     = garmin_data.get("runs", [])
        readiness= garmin_data.get("readiness", {})
        last_run = runs[0] if runs else {}
        laps     = last_run.get("laps", [])
        avg_gct_l = round(sum(l["gct_balance"] for l in laps) / len(laps), 1) if laps else 50.0
        latest_bp = bp_readings[0] if bp_readings else {}

        prompt = f"""You are a running coach and sports physiotherapist. Analyse this athlete's daily health data and give 3-4 concise, actionable insights. Be direct and specific. No fluff.

ATHLETE CONTEXT:
- Training for Melbourne Marathon on 12 Oct 2026 (sub-3:00 goal, current PB 3:16)
- Left insertional Achilles tendinopathy (active management)
- Seated calf raises 3x/week approved. Heel drops off step are PROHIBITED.
- Currently in Week {phase_info['week_num']} of 28 — {phase_info['phase']['name']} phase

TODAY'S DATA:
- Training readiness: {readiness.get('score', 'N/A')}/100 ({readiness.get('level', '')})
- Sleep score: {readiness.get('sleep_score', 'N/A')}/100
- HRV weekly avg: {garmin_data.get('hrv', {}).get('weekly_avg', 'N/A')} ms
- Resting HR: {garmin_data.get('resting_hr', 'N/A')} bpm
- Body battery: {garmin_data.get('body_battery', 'N/A')}/100
- Recovery time: {readiness.get('recovery_time', 'N/A')} min

LAST RUN ({last_run.get('date', 'N/A')}):
- Distance: {last_run.get('distance', 'N/A')} km
- Avg pace: {pace_str(last_run.get('avg_pace'))} min/km
- Avg HR: {last_run.get('avg_hr', 'N/A')} bpm
- GCT balance LEFT: {avg_gct_l}% (right: {round(100-avg_gct_l,1)}%)

ACHILLES LOAD SCORE: {achilles.get('score', 'N/A')}/100 ({achilles.get('level', '')})
Factors: {', '.join(f['label'] + ': ' + f['value'] for f in achilles.get('factors', []))}

BLOOD PRESSURE (latest): {latest_bp.get('systolic', 'N/A')}/{latest_bp.get('diastolic', 'N/A')} mmHg, pulse {latest_bp.get('pulse', 'N/A')} bpm

This week: {achilles.get('this_week_km', 'N/A')} km | Last week: {achilles.get('last_week_km', 'N/A')} km
Phase target: {phase_info['phase']['km_min']}–{phase_info['phase']['km_max']} km/wk

Give 3-4 bullet insights. Each bullet: one sentence, specific and actionable. Flag any Achilles risk clearly. End with one sentence on today's recommended training."""

        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""),
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 500,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        result = resp.json()
        print(f"  API status: {resp.status_code}")
        print(f"  API response keys: {list(result.keys())}")
        if "error" in result:
            print(f"  API error: {result['error']}")
            return f"AI commentary unavailable: {result['error'].get('message', 'unknown error')}"
        return result["content"][0]["text"]
    except Exception as e:
        return f"AI commentary unavailable: {e}"

# ─── EMAIL ALERT ─────────────────────────────────────────────────────────────

def send_html_email(subject, html_body):
    """Send HTML email via Gmail SMTP."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    gmail_user = os.environ.get("GMAIL_USER", "hutcheew@gmail.com")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not gmail_pass:
        print("  Email not configured (set GMAIL_APP_PASSWORD)")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = gmail_user
        msg["To"]      = gmail_user
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(gmail_user, gmail_pass)
            s.send_message(msg)
        print(f"  Email sent: {subject}")
    except Exception as e:
        print(f"  Email failed: {e}")

def generate_daily_report(garmin_data, bp_readings, phase_info, achilles, weather, intervals):
    """Call Gemini to generate a full daily report and email it."""
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        print("  No Gemini key — skipping daily report")
        return

    runs      = garmin_data.get("runs", [])
    readiness = garmin_data.get("readiness", {})
    hrv       = garmin_data.get("hrv", {})
    sleep     = garmin_data.get("sleep", {})
    last_run  = runs[0] if runs else {}
    laps      = last_run.get("laps", [])
    avg_gct_l = round(sum(l["gct_balance"] for l in laps) / len(laps), 1) if laps else 50.0
    latest_bp = bp_readings[0] if bp_readings else {}
    phase     = phase_info["phase"]
    latest_ctl = intervals.get("latest", {}) if intervals else {}

    # Best run window today and tomorrow
    weather_summary = "Weather data unavailable"
    best_today = "--"
    best_tomorrow = "--"
    if weather and weather.get("days"):
        days = weather["days"]
        today_d = days[0]
        tmr_d   = days[1] if len(days) > 1 else {}
        best_today    = f"{today_d.get('best_window','--')} ({today_d.get('max_temp')}°C, {today_d.get('rain_pct')}% rain)"
        best_tomorrow = f"{tmr_d.get('best_window','--')} ({tmr_d.get('max_temp')}°C, {tmr_d.get('rain_pct')}% rain)" if tmr_d else "--"
        weather_summary = f"Today: {today_d.get('max_temp')}°C {today_d.get('rain_pct')}% rain | Tomorrow: {tmr_d.get('max_temp','?')}°C {tmr_d.get('rain_pct','?')}% rain"

    prompt = f"""You are an expert running coach and sports physiotherapist. Generate a comprehensive daily health and training report for Nat.

ATHLETE PROFILE:
- Melbourne Marathon 12 Oct 2026, sub-3:00 goal (current PB 3:16)
- Left insertional Achilles tendinopathy (active management)
- Seated calf raises 3x/week APPROVED. Heel drops off step PROHIBITED.
- Week {phase_info['week_num']}/28 — {phase['name']} phase (Week {phase_info['week_in_phase']} of {phase_info['phase_total']})
- Phase target: {phase['km_min']}–{phase['km_max']} km/week
- Days to race: {phase_info['days_to_race']}

TODAY'S METRICS:
- Readiness: {readiness.get('score','N/A')}/100 ({readiness.get('level','')})
- Sleep: {readiness.get('sleep_score','N/A')}/100 | Duration: {sleep.get('duration_hrs','N/A')}h | Deep: {sleep.get('deep_hrs','N/A')}h | REM: {sleep.get('rem_hrs','N/A')}h
- HRV 7d avg: {hrv.get('weekly_avg','N/A')} ms | Resting HR: {garmin_data.get('resting_hr','N/A')} bpm
- Body battery: {garmin_data.get('body_battery','N/A')}/100 | Recovery time: {readiness.get('recovery_time','N/A')} min
- CTL (fitness): {latest_ctl.get('ctl','N/A')} | ATL (fatigue): {latest_ctl.get('atl','N/A')} | TSB (form): {latest_ctl.get('tsb','N/A')}

LAST RUN ({last_run.get('date','N/A')}):
- {last_run.get('distance','N/A')} km @ {pace_str(last_run.get('avg_pace'))} min/km | HR: {last_run.get('avg_hr','N/A')} bpm
- GCT balance: Left {avg_gct_l}% / Right {round(100-avg_gct_l,1)}% | Cadence: {last_run.get('cadence','N/A')} spm

ACHILLES LOAD: {achilles.get('score','N/A')}/100 ({achilles.get('level','low')} risk)
Factors: {', '.join(f['label']+': '+f['value'] for f in achilles.get('factors',[]))}
This week: {achilles.get('this_week_km','N/A')} km | Last week: {achilles.get('last_week_km','N/A')} km

BLOOD PRESSURE: {latest_bp.get('systolic','N/A')}/{latest_bp.get('diastolic','N/A')} mmHg | Pulse: {latest_bp.get('pulse','N/A')} bpm

WEATHER (Wantirna South):
{weather_summary}
Best run window today: {best_today}
Best run window tomorrow: {best_tomorrow}

Generate a report with these exact sections:

## Daily Status
One sentence summary of how Nat is feeling today based on readiness, sleep, HRV and body battery.

## Achilles Update
2-3 sentences on Achilles risk based on GCT balance trend and load score. Include specific advice.

## Today's Training Recommendation
Specific session recommendation for today (type, distance, pace, effort level). Consider readiness score, phase target, and best run window. If readiness < 50, recommend rest or easy cross-training. Include best time to run based on weather.

## This Week's Plan
Day by day plan for the rest of the week aligned to the {phase['name']} phase target of {phase['km_min']}–{phase['km_max']} km. Be specific with session types and distances.

## Blood Pressure Note
One sentence on BP trend — is it within healthy range?

## Key Focus
One sentence on the single most important thing to focus on this week for the sub-3:00 goal.

Keep each section concise and actionable. Use specific numbers."""

    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_key}",
            headers={"Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30
        )
        result = resp.json()
        if "error" in result:
            print(f"  Gemini error: {result['error']}")
            return
        report_text = result["candidates"][0]["content"]["parts"][0]["text"]
        print("  Daily report generated.")

        # Convert markdown to HTML email
        html_report = format_email_html(report_text, garmin_data, bp_readings, phase_info, achilles, weather, latest_ctl)
        today_str = date.today().strftime("%A, %d %b %Y")
        subject = f"Daily Training Report — {today_str} | Week {phase_info['week_num']}/28"
        send_html_email(subject, html_report)

    except Exception as e:
        print(f"  Daily report failed: {e}")

def format_email_html(report_text, garmin_data, bp_readings, phase_info, achilles, weather, latest_ctl):
    """Format the report as a clean HTML email."""
    readiness = garmin_data.get("readiness", {})
    runs = garmin_data.get("runs", [])
    last_run = runs[0] if runs else {}
    latest_bp = bp_readings[0] if bp_readings else {}
    phase = phase_info["phase"]

    r_score = readiness.get("score", "--")
    r_color = "#3dd68c" if isinstance(r_score, int) and r_score >= 70 else "#f5c842" if isinstance(r_score, int) and r_score >= 50 else "#f26565"
    a_score = achilles.get("score", "--")
    a_color = "#f26565" if achilles.get("level") == "high" else "#f5c842" if achilles.get("level") == "medium" else "#3dd68c"
    tsb = latest_ctl.get("tsb", "--")
    tsb_color = "#3dd68c" if isinstance(tsb, (int, float)) and tsb >= 0 else "#f26565"

    best_window = "--"
    if weather and weather.get("days"):
        best_window = f"{weather['days'][0].get('best_window','--')} — {weather['days'][0].get('max_temp')}°C, {weather['days'][0].get('rain_pct')}% rain"

    # Convert markdown sections to HTML
    import re
    sections_html = ""
    current_section = ""
    current_content = []
    for line in report_text.split("\n"):
        if line.startswith("## "):
            if current_section and current_content:
                content = "<br>".join(c for c in current_content if c.strip())
                sections_html += f"""
                <div style="margin-bottom:20px;">
                  <div style="font-size:11px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:#5c6480;margin-bottom:8px;border-bottom:1px solid #252836;padding-bottom:6px;">{current_section}</div>
                  <div style="font-size:14px;line-height:1.7;color:#c0c8e0;">{content}</div>
                </div>"""
            current_section = line[3:].strip()
            current_content = []
        else:
            line = re.sub(r'\*\*(.*?)\*\*', r'<strong style="color:#f0f2f8">\1</strong>', line)
            line = re.sub(r'^\- ', '• ', line)
            current_content.append(line)

    if current_section and current_content:
        content = "<br>".join(c for c in current_content if c.strip())
        sections_html += f"""
        <div style="margin-bottom:20px;">
          <div style="font-size:11px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:#5c6480;margin-bottom:8px;border-bottom:1px solid #252836;padding-bottom:6px;">{current_section}</div>
          <div style="font-size:14px;line-height:1.7;color:#c0c8e0;">{content}</div>
        </div>"""

    today_str = date.today().strftime("%A, %d %b %Y")

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0d0f14;font-family:Inter,Arial,sans-serif;">
<div style="max-width:600px;margin:0 auto;padding:24px;">

  <!-- Header -->
  <div style="border-bottom:1px solid #252836;padding-bottom:16px;margin-bottom:24px;">
    <div style="font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:#5c6480;margin-bottom:4px;">NAT / HEALTH DASHBOARD</div>
    <div style="font-size:20px;font-weight:600;color:#f0f2f8;">{today_str}</div>
    <div style="font-size:12px;color:#5c6480;margin-top:4px;">Week {phase_info['week_num']}/28 — {phase['name']} · {phase_info['days_to_race']} days to Melbourne Marathon</div>
  </div>

  <!-- Stats row -->
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:24px;">
    <div style="background:#13151c;border:1px solid #252836;border-radius:8px;padding:12px;text-align:center;">
      <div style="font-size:10px;color:#5c6480;letter-spacing:.8px;text-transform:uppercase;margin-bottom:4px;">Readiness</div>
      <div style="font-size:22px;font-weight:600;color:{r_color};">{r_score}</div>
    </div>
    <div style="background:#13151c;border:1px solid #252836;border-radius:8px;padding:12px;text-align:center;">
      <div style="font-size:10px;color:#5c6480;letter-spacing:.8px;text-transform:uppercase;margin-bottom:4px;">Achilles</div>
      <div style="font-size:22px;font-weight:600;color:{a_color};">{a_score}</div>
    </div>
    <div style="background:#13151c;border:1px solid #252836;border-radius:8px;padding:12px;text-align:center;">
      <div style="font-size:10px;color:#5c6480;letter-spacing:.8px;text-transform:uppercase;margin-bottom:4px;">Form (TSB)</div>
      <div style="font-size:22px;font-weight:600;color:{tsb_color};">{tsb}</div>
    </div>
    <div style="background:#13151c;border:1px solid #252836;border-radius:8px;padding:12px;text-align:center;">
      <div style="font-size:10px;color:#5c6480;letter-spacing:.8px;text-transform:uppercase;margin-bottom:4px;">BP</div>
      <div style="font-size:16px;font-weight:600;color:#f0f2f8;padding-top:3px;">{latest_bp.get('systolic','--')}/{latest_bp.get('diastolic','--')}</div>
    </div>
  </div>

  <!-- Best run window -->
  <div style="background:#13151c;border:1px solid #3dd68c;border-left:3px solid #3dd68c;border-radius:8px;padding:12px 16px;margin-bottom:24px;">
    <div style="font-size:10px;color:#5c6480;letter-spacing:.8px;text-transform:uppercase;margin-bottom:4px;">Best Run Window Today</div>
    <div style="font-size:15px;font-weight:600;color:#3dd68c;">{best_window}</div>
  </div>

  <!-- AI Report -->
  <div style="background:#13151c;border:1px solid #252836;border-radius:10px;padding:20px;margin-bottom:24px;">
    {sections_html}
  </div>

  <!-- Footer -->
  <div style="text-align:center;padding-top:16px;border-top:1px solid #252836;">
    <a href="https://hutcheew.github.io/health-dashboard" style="display:inline-block;background:#1a1d27;color:#4f8ef7;text-decoration:none;padding:10px 24px;border-radius:6px;font-size:12px;border:1px solid #4f8ef7;">View Full Dashboard</a>
    <div style="font-size:10px;color:#5c6480;margin-top:12px;">Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} AEST</div>
  </div>

</div>
</body></html>"""

def check_and_send_alerts(readiness, achilles, bp_readings):
    alerts = []
    r_score = readiness.get("score")
    if r_score and r_score < 40:
        alerts.append(f"Low readiness: {r_score}/100 — consider rest or easy session today.")
    if achilles.get("level") == "high":
        alerts.append(f"Achilles load score HIGH ({achilles['score']}/100) — review training load.")
    latest_bp = bp_readings[0] if bp_readings else {}
    sys_val = latest_bp.get("systolic", 0)
    if sys_val and sys_val >= 140:
        alerts.append(f"BP elevated: {latest_bp.get('systolic')}/{latest_bp.get('diastolic')} mmHg — monitor closely.")
    if alerts:
        body = "\n\n".join(alerts) + f"\n\nDashboard: https://hutcheew.github.io/health-dashboard"
        send_html_email(f"Health Alert — {date.today()}", f"<pre style='font-family:Arial;font-size:14px;color:#f0f2f8;background:#0d0f14;padding:20px'>{chr(10).join(alerts)}<br><br><a href='https://hutcheew.github.io/health-dashboard' style='color:#4f8ef7'>View Dashboard</a></pre>")

def compute_training_decision(readiness, achilles, hrv, sleep, weather, phase_info, intervals):
    """
    Compute today's training decision:
    - Status: TRAIN HARD / TRAIN EASY / MODERATE / REST
    - Recommended session with pace/HR targets
    - List of green/yellow/red factors
    - What to avoid today
    """
    r_score  = readiness.get("score") or 0
    a_level  = achilles.get("level", "low")
    a_score  = achilles.get("score", 0) or 0
    sleep_s  = sleep.get("score") or readiness.get("sleep_score") or 0
    hrv_avg  = hrv.get("weekly_avg") or 0
    tsb      = (intervals or {}).get("latest", {}).get("tsb")
    phase    = phase_info["phase"]
    week_num = phase_info["week_num"]

    # Best run window today
    best_window = "--"
    today_temp  = "--"
    if weather and weather.get("days"):
        d = weather["days"][0]
        best_window = d.get("best_window", "--")
        today_temp  = f"{d.get('max_temp','?')}°C"

    # Decision logic
    factors_green  = []
    factors_yellow = []
    factors_red    = []

    # Readiness
    if r_score >= 75:
        factors_green.append("Readiness high")
    elif r_score >= 50:
        factors_yellow.append(f"Readiness moderate ({r_score}/100)")
    else:
        factors_red.append(f"Readiness low ({r_score}/100)")

    # Sleep
    if sleep_s >= 75:
        factors_green.append("Sleep good")
    elif sleep_s >= 55:
        factors_yellow.append(f"Sleep fair ({sleep_s}/100)")
    else:
        factors_red.append(f"Sleep poor ({sleep_s}/100)")

    # HRV
    if hrv_avg >= 35:
        factors_green.append("HRV stable")
    elif hrv_avg >= 25:
        factors_yellow.append(f"HRV moderate ({hrv_avg}ms)")
    elif hrv_avg > 0:
        factors_red.append(f"HRV low ({hrv_avg}ms)")

    # Achilles
    if a_level == "low":
        factors_green.append("Achilles risk low")
    elif a_level == "medium":
        factors_yellow.append("Achilles risk moderate")
    else:
        factors_red.append("Achilles risk HIGH")

    # Training load (TSB)
    if tsb is not None:
        if tsb >= 5:
            factors_green.append(f"Form positive (TSB +{tsb})")
        elif tsb >= -10:
            factors_yellow.append(f"Training load balanced (TSB {tsb})")
        else:
            factors_red.append(f"Accumulated fatigue (TSB {tsb})")

    # Determine status
    red_count    = len(factors_red)
    yellow_count = len(factors_yellow)

    if red_count == 0 and yellow_count <= 1 and r_score >= 75:
        status     = "TRAIN HARD"
        status_color = "#3dd68c"
        status_emoji = "🟢"
        avoid      = []
        if phase["name"] in ["Build", "Marathon Specific"]:
            rec_type = "Tempo or interval session"
            rec_dist = f"{round(phase['km_min']*0.2)}–{round(phase['km_max']*0.2)} km"
            rec_pace = "4:30–5:00/km"
            rec_hr   = "<160 bpm"
        else:
            rec_type = "Progressive run"
            rec_dist = f"{round(phase['km_min']*0.2)}–{round(phase['km_max']*0.2)} km"
            rec_pace = "5:00–5:30/km"
            rec_hr   = "<155 bpm"
    elif red_count == 0 and r_score >= 50:
        status     = "TRAIN EASY"
        status_color = "#4f8ef7"
        status_emoji = "🔵"
        avoid      = ["Tempo runs", "Intervals", "Long run"]
        rec_type   = "Easy aerobic run"
        rec_dist   = f"{round(phase['km_min']*0.15)}–{round(phase['km_min']*0.25)} km"
        rec_pace   = "5:40–6:10/km"
        rec_hr     = "<145 bpm"
    elif red_count <= 1 and r_score >= 40:
        status     = "MODERATE"
        status_color = "#f5c842"
        status_emoji = "🟡"
        avoid      = ["Tempo runs", "Intervals", "Racing effort"]
        rec_type   = "Easy recovery run or cross-train"
        rec_dist   = f"4–6 km"
        rec_pace   = "6:00–6:30/km"
        rec_hr     = "<140 bpm"
    else:
        status     = "REST DAY"
        status_color = "#f26565"
        status_emoji = "🔴"
        avoid      = ["Running", "High intensity", "Long sessions"]
        rec_type   = "Rest, gentle walk, or mobility work"
        rec_dist   = "--"
        rec_pace   = "--"
        rec_hr     = "--"

    # Achilles override
    if a_level == "high":
        avoid.extend(["Hill repeats", "Speed work", "Back-to-back run days"])
        rec_type = "Easy flat run only — monitor Achilles closely"

    return {
        "status":       status,
        "status_color": status_color,
        "status_emoji": status_emoji,
        "factors_green":  factors_green,
        "factors_yellow": factors_yellow,
        "factors_red":    factors_red,
        "rec_type":     rec_type,
        "rec_dist":     rec_dist,
        "rec_pace":     rec_pace,
        "rec_hr":       rec_hr,
        "avoid":        avoid,
        "best_window":  best_window,
        "today_temp":   today_temp,
    }


def generate_html(garmin_data, bp_readings, phase_info=None, achilles=None, ai_commentary="", weather=None, intervals=None, load_data=None, recovery=None, injury_contributors=None, tissue_capacity=None, monotony=None, why_today=None, checkins=None):
    runs        = garmin_data.get("runs", [])
    readiness   = garmin_data.get("readiness", {})
    hrv         = garmin_data.get("hrv", {})
    resting_hr  = garmin_data.get("resting_hr", "--")
    body_battery= garmin_data.get("body_battery", "--")
    last_run    = runs[0] if runs else {}
    laps        = last_run.get("laps", [])

    # GCT trend across recent runs (avg balance per run)
    gct_trend_labels = json.dumps([r["date"] for r in reversed(runs[:8])])
    gct_trend_values = json.dumps([
        round(sum(l["gct_balance"] for l in r["laps"]) / len(r["laps"]), 2)
        if r["laps"] else 50.0
        for r in reversed(runs[:8])
    ])
    gct_trend_values_r = json.dumps([
        round(100 - sum(l["gct_balance"] for l in r["laps"]) / len(r["laps"]), 2)
        if r["laps"] else 50.0
        for r in reversed(runs[:8])
    ])
    gct_trend_gct = json.dumps([
        round(sum(l["gct"] for l in r["laps"]) / len(r["laps"]), 1)
        if r["laps"] else 0
        for r in reversed(runs[:8])
    ])

    # Weekly mileage
    weekly = {}
    for r in runs:
        d = datetime.strptime(r["date"], "%Y-%m-%d")
        week = d.strftime("%Y-W%W")
        weekly[week] = weekly.get(week, 0) + r["distance"]
    weekly_labels = json.dumps(list(weekly.keys())[-6:])
    weekly_values = json.dumps([round(weekly[k], 1) for k in list(weekly.keys())[-6:]])

    # Last run lap charts
    lap_labels   = json.dumps([f"km {l['km']}" for l in laps])
    lap_gct      = json.dumps([l["gct"] for l in laps])
    lap_balance  = json.dumps([l["gct_balance"] for l in laps])
    lap_balance_r = json.dumps([round(100 - l["gct_balance"], 1) for l in laps])
    lap_hr       = json.dumps([l["hr"] for l in laps])
    lap_pace     = json.dumps([l["pace"] for l in laps])

    # BP chart
    bp_dates     = json.dumps([b["date"][:10] for b in reversed(bp_readings[:12])])
    bp_systolic  = json.dumps([b.get("systolic") for b in reversed(bp_readings[:12])])
    bp_diastolic = json.dumps([b.get("diastolic") for b in reversed(bp_readings[:12])])
    bp_pulse     = json.dumps([b.get("pulse") for b in reversed(bp_readings[:12])])

    # HRV chart
    hrv_labels   = json.dumps(hrv.get("times", []))
    hrv_values   = json.dumps(hrv.get("values", []))

    latest_bp = bp_readings[0] if bp_readings else {}
    readiness_score = readiness.get("score", "--")
    readiness_level = readiness.get("level", "").replace("_", " ").title()
    sleep_score     = readiness.get("sleep_score", "--")
    hrv_avg         = hrv.get("weekly_avg", "--")

    # Sleep and intervals — needed throughout generate_html
    sleep      = garmin_data.get("sleep", {})
    latest_ctl = intervals.get("latest", {}) if intervals else {}

    # GCT balance colour indicator
    def gct_color(val):
        if not val: return "#888"
        diff = abs(val - 50)
        if diff < 1: return "#4ade80"
        if diff < 2: return "#facc15"
        return "#f87171"

    last_gct_balance = round(sum(l["gct_balance"] for l in laps) / len(laps), 1) if laps else None
    last_gct_balance_r = round(100 - last_gct_balance, 1) if last_gct_balance else None
    last_gct_avg     = round(sum(l["gct"] for l in laps) / len(laps), 1) if laps else "--"
    gct_bal_color    = gct_color(last_gct_balance)
    gct_bal_color_r  = gct_color(last_gct_balance_r)

    # New coaching engine data
    load_data           = load_data or {}
    recovery            = recovery or {}
    injury_contributors = injury_contributors or []
    checkins            = checkins or []
    latest_checkin      = get_latest_checkin(checkins)
    checkin_history_json = json.dumps(list(reversed(checkins[-14:])))
    today_iso           = date.today().isoformat()

    acr         = load_data.get("acr", 1.0)
    acr_status  = load_data.get("acr_status", "optimal")
    acute_load  = load_data.get("acute", "--")
    chronic_load = load_data.get("chronic", "--")
    easy_pct    = load_data.get("easy_pct", 0)
    mod_pct     = load_data.get("mod_pct", 0)
    hard_pct    = load_data.get("hard_pct", 0)
    btb_risk    = load_data.get("btb_risk", False)
    acr_color   = "var(--green)" if acr_status == "optimal" else "var(--red)" if acr_status == "high" else "var(--yellow)"

    recovery_score = recovery.get("score", "--")
    recovery_level = recovery.get("level", "--")
    recovery_color = "var(--green)" if recovery_level == "good" else "var(--yellow)" if recovery_level == "moderate" else "var(--red)"
    recovery_factors = recovery.get("factors", [])

    # Generate specific workout
    decision_data = compute_training_decision(
        garmin_data.get("readiness", {}), achilles,
        garmin_data.get("hrv", {}), garmin_data.get("sleep", {}),
        weather, phase_info, intervals
    ) if phase_info else {}
    workout = generate_workout(decision_data, phase_info or get_training_phase(), achilles or {})

    # Injury detective HTML
    injury_html = ""
    if injury_contributors:
        for c in injury_contributors[:3]:
            injury_html += f"""
            <div style="padding:10px 14px;background:var(--surface2);border-left:3px solid var(--red);border-radius:6px;margin-bottom:8px;font-size:12px">
              <div style="color:var(--text2);font-weight:500">{c['date']} — {c['distance']}km @ HR{c['hr']}</div>
              <div style="color:var(--text3);margin-top:2px">{' · '.join(c['reasons'])} — <span style="color:var(--red)">{c['risk_pct']}% contribution</span></div>
            </div>"""

    # Recovery factors HTML
    recovery_html = ""
    for f in recovery_factors:
        col = "var(--green)" if f.get("status") == "good" else "var(--yellow)" if f.get("status") == "moderate" else "var(--red)"
        label = f.get("label", "")
        score_val = f.get("score", "")
        weight = f.get("weight", "")
        recovery_html += f'<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border);font-size:12px"><span style="color:var(--text2)">{label} <span style="color:var(--text3);font-size:10px">{weight}</span></span><span style="color:{col};font-family:JetBrains Mono,monospace">{score_val} pts</span></div>'

    # Workout HTML
    avoid_items = "".join(f'<span style="background:rgba(242,101,101,0.1);color:var(--red);padding:2px 8px;border-radius:4px;font-size:11px;margin:2px">{a}</span>' for a in workout.get("avoid", []))

    # Phase info
    if phase_info is None:
        phase_info = get_training_phase()
    if achilles is None:
        achilles = compute_achilles_score(runs, phase_info)

    phase         = phase_info["phase"]
    week_num      = phase_info["week_num"]
    week_in_phase = phase_info["week_in_phase"]
    phase_total   = phase_info["phase_total"]
    days_to_race  = phase_info["days_to_race"]
    phase_pct     = phase_info["phase_pct"]

    achilles_score = achilles.get("score", "--")
    achilles_level = achilles.get("level", "low")
    achilles_color = "#f87171" if achilles_level == "high" else "#facc15" if achilles_level == "medium" else "#4ade80"
    achilles_factors = achilles.get("factors", [])

    # AI commentary — pre-build rows
    ai_rows = ""
    if ai_commentary:
        for line in ai_commentary.strip().split("\n"):
            line = line.strip().lstrip("•-* ")
            if line:
                ai_rows += f'<div class="ai-row">{line}</div>\n'

    # Achilles factor rows
    achilles_rows = ""
    for f in achilles_factors:
        col = "#f87171" if f["level"] == "high" else "#facc15" if f["level"] == "medium" else "#4ade80"
        achilles_rows += f'<tr><td>{f["label"]}</td><td style="color:{col}">{f["value"]}</td></tr>\n'

    # Pre-build JSON data for export button
    recent_runs_export = []
    for r in runs[:8]:
        recent_runs_export.append({
            "date": r["date"],
            "distance_km": r["distance"],
            "avg_pace": r["avg_pace"],
            "avg_hr": r["avg_hr"],
            "gct_balance_left": round(sum(l["gct_balance"] for l in r["laps"])/len(r["laps"]),1) if r["laps"] else None,
            "gct_balance_right": round(100-sum(l["gct_balance"] for l in r["laps"])/len(r["laps"]),1) if r["laps"] else None,
        })

    export_data = json.dumps({
        "generated": datetime.now().strftime('%Y-%m-%d %H:%M'),
        "training": {
            "week": week_num,
            "phase": phase["name"],
            "week_in_phase": week_in_phase,
            "phase_total": phase_total,
            "days_to_race": days_to_race,
            "phase_target_km": f"{phase['km_min']}-{phase['km_max']}",
            "this_week_km": achilles.get("this_week_km"),
            "last_week_km": achilles.get("last_week_km"),
        },
        "readiness": {
            "score": readiness.get("score"),
            "level": readiness.get("level"),
            "sleep_score": readiness.get("sleep_score"),
            "hrv_weekly_avg": hrv.get("weekly_avg"),
            "resting_hr": resting_hr,
            "body_battery": body_battery,
            "recovery_time": readiness.get("recovery_time"),
            "acwr": readiness.get("acwr"),
        },
        "achilles": {
            "score": achilles.get("score"),
            "level": achilles.get("level"),
            "factors": achilles.get("factors", []),
        },
        "last_run": {
            "date": last_run.get("date"),
            "distance_km": last_run.get("distance"),
            "avg_pace_min_km": last_run.get("avg_pace"),
            "avg_hr": last_run.get("avg_hr"),
            "cadence": last_run.get("cadence"),
            "gct_balance_left_pct": last_gct_balance,
            "gct_balance_right_pct": last_gct_balance_r,
            "avg_gct_ms": last_gct_avg,
            "laps": last_run.get("laps", []),
        },
        "recent_runs": recent_runs_export,
        "blood_pressure": bp_readings[:10],
        "checkins": checkins[:14],
        "latest_checkin": latest_checkin,
    }, default=str)

    # ── COACH PANEL DATA ─────────────────────────────────────────────────────

    # Aerobic decoupling — compare HR vs pace first half vs second half of last run
    decoupling_pct  = None
    decoupling_status = "N/A"
    decoupling_color = "var(--text3)"
    if laps and len(laps) >= 4:
        half = len(laps) // 2
        first_half = laps[:half]
        second_half = laps[half:]
        def avg_efficiency(lap_list):
            valid = [(l["pace"], l["hr"]) for l in lap_list if l.get("pace") and l.get("hr") and l["hr"] > 0]
            if not valid: return None
            return sum(p / h for p, h in valid) / len(valid)
        eff1 = avg_efficiency(first_half)
        eff2 = avg_efficiency(second_half)
        if eff1 and eff2 and eff1 > 0:
            decoupling_pct = round(abs(eff2 - eff1) / eff1 * 100, 1)
            if decoupling_pct < 3:
                decoupling_status = "Excellent"
                decoupling_color = "var(--green)"
            elif decoupling_pct < 5:
                decoupling_status = "Good"
                decoupling_color = "var(--green)"
            elif decoupling_pct < 8:
                decoupling_status = "Moderate — more Zone 2"
                decoupling_color = "var(--yellow)"
            else:
                decoupling_status = "High — aerobic base needed"
                decoupling_color = "var(--red)"

    # HR Efficiency trend — pace/HR ratio per recent run (higher = better)
    hr_eff_labels = json.dumps([r["date"] for r in reversed(runs[:8])])
    hr_eff_values = json.dumps([
        round(r["avg_pace"] / r["avg_hr"] * 1000, 2)
        if r.get("avg_pace") and r.get("avg_hr") and r["avg_hr"] > 0 else None
        for r in reversed(runs[:8])
    ])

    # Running Form Score (0-100)
    form_score = None
    form_factors = []
    if laps:
        avg_cadence = last_run.get("cadence", 0)
        avg_gct_val = last_gct_avg if isinstance(last_gct_avg, (int, float)) else 0
        gct_diff = abs(last_gct_balance - 50) if last_gct_balance else 0

        cadence_score = min(100, max(0, int((avg_cadence - 155) / (185 - 155) * 100))) if avg_cadence else 0
        gct_score     = max(0, 100 - int(avg_gct_val - 220) * 2) if avg_gct_val > 220 else 100
        balance_score = max(0, 100 - int(gct_diff * 20))
        form_score    = round((cadence_score * 0.35 + gct_score * 0.35 + balance_score * 0.3))

        form_factors = [
            {"label": "Cadence", "value": f"{avg_cadence} spm", "score": cadence_score,
             "status": "✓" if avg_cadence >= 170 else "↑"},
            {"label": "GCT", "value": f"{last_gct_avg} ms", "score": gct_score,
             "status": "✓" if avg_gct_val <= 260 else "↑"},
            {"label": "Balance", "value": f"L{last_gct_balance}%/R{last_gct_balance_r}%", "score": balance_score,
             "status": "✓" if gct_diff < 1 else "⚠" if gct_diff < 2 else "✗"},
        ]

    form_color = "var(--green)" if form_score and form_score >= 80 else "var(--yellow)" if form_score and form_score >= 60 else "var(--red)"

    # Coach note
    coach_insights = []
    if avg_cadence := last_run.get("cadence", 0):
        if avg_cadence >= 175:
            coach_insights.append("Cadence is excellent — maintain this.")
        elif avg_cadence >= 165:
            coach_insights.append("Cadence is good — aim for 175+ on easy runs.")
        else:
            coach_insights.append("Cadence is low — focus on quick light steps.")

    if decoupling_pct is not None:
        if decoupling_pct < 5:
            coach_insights.append("Aerobic efficiency is strong — ready to build mileage.")
        else:
            coach_insights.append(f"Aerobic drift {decoupling_pct}% — prioritise Zone 2 this week.")

    latest_ctl_val = latest_ctl.get("ctl", 0) or 0
    latest_tsb_val = latest_ctl.get("tsb", 0) or 0
    if latest_tsb_val < -10:
        coach_insights.append("Form is negative — fatigue accumulating, consider easy days.")
    elif latest_tsb_val > 10:
        coach_insights.append("Form is positive — well rested, good time for a quality session.")
    else:
        coach_insights.append("Form is neutral — consistent training is working.")

    if not coach_insights:
        coach_insights.append("Keep building your aerobic base consistently.")

    coach_note_html = "".join(f'<div style="margin-bottom:8px;padding-left:12px;border-left:2px solid var(--purple);font-size:13px;color:var(--text2);line-height:1.5">{c}</div>' for c in coach_insights)

    # Form factors HTML
    form_factors_html = ""
    for f in form_factors:
        bar_w = f["score"]
        bar_col = "var(--green)" if f["score"] >= 80 else "var(--yellow)" if f["score"] >= 60 else "var(--red)"
        form_factors_html += f"""
        <div style="margin-bottom:12px">
          <div style="display:flex;justify-content:space-between;margin-bottom:4px">
            <span style="font-size:12px;color:var(--text2)">{f['status']} {f['label']}</span>
            <span style="font-size:12px;color:{bar_col};font-family:'JetBrains Mono',monospace">{f['value']}</span>
          </div>
          <div style="height:4px;background:var(--border);border-radius:2px;overflow:hidden">
            <div style="height:100%;width:{bar_w}%;background:{bar_col};border-radius:2px"></div>
          </div>
        </div>"""

    # CTL/ATL/TSB bar display
    ctl_val  = latest_ctl.get("ctl", 0) or 0
    atl_val  = latest_ctl.get("atl", 0) or 0
    tsb_val  = latest_ctl.get("tsb", 0) or 0
    ctl_pct  = min(100, round(ctl_val / 100 * 100))  # assume 100 CTL = elite
    atl_pct  = min(100, round(atl_val / 100 * 100))
    tsb_col  = "var(--green)" if tsb_val >= 0 else "var(--red)"
    tsb_abs  = min(100, round(abs(tsb_val) / 30 * 100))

    # Pre-build table rows (avoids nested f-string issues in Python < 3.12)
    run_table_rows = ""
    for r in runs:
        if r['laps']:
            avg_l = sum(l["gct_balance"] for l in r["laps"]) / len(r["laps"])
            avg_r = 100 - avg_l
            l_cls = "green" if abs(avg_l - 50) < 1 else "yellow" if abs(avg_l - 50) < 2 else "red"
            r_cls = "green" if abs(avg_r - 50) < 1 else "yellow" if abs(avg_r - 50) < 2 else "red"
            gct_l_cell = f'<span class="badge badge-{l_cls}">{round(avg_l,1)}%</span>'
            gct_r_cell = f'<span class="badge badge-{r_cls}">{round(avg_r,1)}%</span>'
            gct_avg = round(sum(l["gct"] for l in r["laps"]) / len(r["laps"]), 1)
        else:
            gct_l_cell = "--"
            gct_r_cell = "--"
            gct_avg = "--"
        run_table_rows += f"""
        <tr>
          <td>{r['date']}</td>
          <td>{r['distance']} km</td>
          <td>{pace_str(r['avg_pace'])}</td>
          <td>{r['avg_hr'] or '--'}</td>
          <td>{r['cadence']} spm</td>
          <td>{gct_l_cell}</td>
          <td>{gct_r_cell}</td>
          <td>{gct_avg} ms</td>
        </tr>"""

    bp_table_rows = ""
    for b in bp_readings[:10]:
        sys_val = b.get('systolic', 999)
        dia_val = b.get('diastolic', 999)
        sys_col = 'var(--green)' if sys_val < 120 else 'var(--yellow)' if sys_val < 130 else 'var(--red)'
        dia_col = 'var(--green)' if dia_val < 80  else 'var(--yellow)' if dia_val < 90  else 'var(--red)'
        bp_table_rows += f"""
          <tr>
            <td>{b['date']}</td>
            <td style="color:{sys_col}">{b.get('systolic','--')}</td>
            <td style="color:{dia_col}">{b.get('diastolic','--')}</td>
            <td>{b.get('pulse','--')}</td>
          </tr>"""

    # Cycling data prep
    cycles = garmin_data.get("cycles", [])
    last_cycle = cycles[0] if cycles else {}

    weekly_cycle = {}
    for c in cycles:
        d = datetime.strptime(c["date"], "%Y-%m-%d")
        wk = d.strftime("%Y-W%W")
        weekly_cycle[wk] = weekly_cycle.get(wk, 0) + c["distance"]

    # Merge weekly run + cycle for combined chart
    all_weeks = sorted(set(list(weekly.keys()) + list(weekly_cycle.keys())))[-6:]
    weekly_combined_labels = json.dumps(all_weeks)
    weekly_run_vals   = json.dumps([round(weekly.get(w, 0), 1) for w in all_weeks])

    # Mileage week-on-week change %
    run_vals_list = [round(weekly.get(w, 0), 1) for w in all_weeks]
    wow_changes = []
    for i, v in enumerate(run_vals_list):
        if i == 0 or run_vals_list[i-1] == 0:
            wow_changes.append(0)
        else:
            wow_changes.append(round((v - run_vals_list[i-1]) / run_vals_list[i-1] * 100, 1))
    mileage_change_vals = json.dumps(wow_changes)
    weekly_cycle_vals = json.dumps([round(weekly_cycle.get(w, 0), 1) for w in all_weeks])

    # Cycle table rows
    cycle_table_rows = ""
    for c in cycles[:8]:
        cycle_table_rows += f"""
        <tr>
          <td>{c['date']}</td>
          <td>{c['distance']} km</td>
          <td>{c['duration']:.0f} min</td>
          <td>{c['avg_speed']} km/h</td>
          <td>{c['avg_hr'] or '--'}</td>
          <td>{c['elevation']} m</td>
          <td>{c['calories'] or '--'}</td>
        </tr>"""

    # Training Decision Engine
    decision = compute_training_decision(
        readiness, achilles, hrv, sleep, weather, phase_info, intervals
    )
    dec_status = decision["status"]
    dec_color  = decision["status_color"]
    dec_emoji  = decision["status_emoji"]

    def factor_html(factors, color):
        return "".join(f'<div class="factor"><div class="factor-dot" style="background:{color}"></div>{f}</div>' for f in factors)

    factors_html = (
        factor_html(decision["factors_green"],  "#3dd68c") +
        factor_html(decision["factors_yellow"], "#f5c842") +
        factor_html(decision["factors_red"],    "#f26565")
    )

    avoid_html = ""
    if decision["avoid"]:
        avoid_html = f'<div class="avoid-list">✗ Avoid today: {" · ".join(decision["avoid"])}</div>'

    # New metrics
    vo2max           = garmin_data.get("vo2max", "--")
    training_load    = garmin_data.get("training_load", {})
    race_preds       = garmin_data.get("race_predictions", {})
    endurance        = garmin_data.get("endurance_score", {})
    run_tolerance    = garmin_data.get("running_tolerance", {})
    intensity_mins   = garmin_data.get("intensity_minutes", {})

    marathon_secs = race_preds.get("marathon_secs")
    sub3_secs = 3 * 3600
    marathon_gap = None
    if marathon_secs:
        gap = marathon_secs - sub3_secs
        sign = "+" if gap > 0 else "-"
        gap = abs(gap)
        marathon_gap = f"{sign}{gap//60}:{gap%60:02d} vs sub-3:00"

    tol_pct = run_tolerance.get("pct")
    tol_color = "var(--green)" if tol_pct and tol_pct < 60 else "var(--yellow)" if tol_pct and tol_pct < 80 else "var(--red)"
    im_total = intensity_mins.get("weekly_total", 0) or 0
    im_goal  = intensity_mins.get("goal", 150) or 150
    im_pct   = min(round(im_total / im_goal * 100), 100) if im_goal else 0
    im_color = "var(--green)" if im_pct >= 100 else "var(--yellow)" if im_pct >= 60 else "var(--red)"
    tl_low   = training_load.get("aerobic_low", 0) or 0
    tl_high  = training_load.get("aerobic_high", 0) or 0
    tl_ana   = training_load.get("anaerobic", 0) or 0
    tl_total = tl_low + tl_high + tl_ana or 1

    # Sleep data
    sleep          = garmin_data.get("sleep", {})
    sleep_duration = sleep.get("duration_hrs", "--")
    sleep_deep     = sleep.get("deep_hrs", "--")
    sleep_light    = sleep.get("light_hrs", "--")
    sleep_rem      = sleep.get("rem_hrs", "--")
    sleep_awake    = sleep.get("awake_hrs", "--")
    sleep_spo2     = sleep.get("avg_spo2", "--")
    sleep_resp     = sleep.get("avg_respiration", "--")
    sleep_stress   = sleep.get("avg_stress", "--")

    # Sleep stage chart data
    sleep_stages_data = json.dumps([
        sleep.get("deep_hrs", 0) or 0,
        sleep.get("light_hrs", 0) or 0,
        sleep.get("rem_hrs", 0) or 0,
        sleep.get("awake_hrs", 0) or 0,
    ])

    # Intervals.icu CTL/ATL/TSB
    ctl_atl_labels = json.dumps([])
    ctl_values     = json.dumps([])
    atl_values     = json.dumps([])
    tsb_values     = json.dumps([])
    latest_ctl = "--"
    latest_atl = "--"
    latest_tsb = "--"
    latest_ramp = "--"

    if intervals:
        ctl_atl = intervals.get("ctl_atl", [])[-60:]  # last 60 days
        ctl_atl_labels = json.dumps([d["date"] for d in ctl_atl])
        ctl_values     = json.dumps([d["ctl"] for d in ctl_atl])
        atl_values     = json.dumps([d["atl"] for d in ctl_atl])
        tsb_values     = json.dumps([d["tsb"] for d in ctl_atl])
        latest = intervals.get("latest", {})
        latest_ctl  = latest.get("ctl", "--")
        latest_atl  = latest.get("atl", "--")
        latest_tsb  = latest.get("tsb", "--")
        latest_ramp = latest.get("ramp", "--")

    tsb_color = "var(--green)" if isinstance(latest_tsb, (int, float)) and latest_tsb >= 0 else "var(--red)"

    # HRV + Sleep stage overlay data
    hrv_times_raw = hrv.get("times", [])
    hrv_vals_raw  = hrv.get("values", [])
    sleep_levels  = sleep.get("levels", [])

    def time_to_minutes(t):
        """Convert HH:MM to minutes since midnight. Returns int."""
        try:
            h, m = t.split(":")
            return int(h) * 60 + int(m)
        except:
            return -1

    def get_stage_at(t_str):
        t = time_to_minutes(t_str)
        if t < 0: return None
        for l in sleep_levels:
            s = time_to_minutes(l["start"])
            e = time_to_minutes(l["end"])
            if s <= e:
                if s <= t <= e: return l["stage"]
            else:  # overnight wrap e.g. 22:00 -> 06:00
                if t >= s or t <= e: return l["stage"]
        return None

    stage_series = [get_stage_at(t) for t in hrv_times_raw]
    matched = sum(1 for s in stage_series if s is not None)
    print(f"  Sleep stage overlay: {len(sleep_levels)} segments, {matched}/{len(hrv_times_raw)} HRV points matched")

    # Build per-stage datasets — null where stage doesn't match, HRV value where it does
    def stage_data(stage_name):
        return json.dumps([
            hrv_vals_raw[i] if stage_series[i] == stage_name else None
            for i in range(len(stage_series))
        ])

    deep_data  = stage_data("deep")
    light_data = stage_data("light")
    rem_data   = stage_data("rem")
    awake_data = stage_data("awake")

    sleep_stages_json = json.dumps([
        {"start": l["start"], "end": l["end"], "stage": l["stage"]}
        for l in sleep_levels
    ] if sleep_levels else [])

    # Weather prep
    weather_html = ""
    if weather:
        days = weather["days"]
        best = weather["best_window"]
        morning = weather.get("morning", {})
        afternoon = weather.get("afternoon", {})
        warnings = weather.get("warnings", [])

        # 3-day forecast cards
        day_cards = ""
        labels = ["Today", "Tomorrow", "Day +2"]
        for i, d in enumerate(days[:3]):
            icon = weather_icon(d["weathercode"])
            desc = WEATHER_CODES.get(d["weathercode"], "")
            day_cards += f"""
            <div class="stat">
              <div class="stat-accent" style="background:{'var(--yellow)' if d['max_temp']>=28 else 'var(--blue)'}"></div>
              <div class="stat-label">{labels[i]} · {d['date']}</div>
              <div class="stat-value sm">{icon} {d['max_temp']}°</div>
              <div class="stat-unit">{d['min_temp']}° low · {desc}</div>
              <div class="stat-sub">💧{d['rain_pct']}% · 💨{d['wind_max']} km/h</div>
            </div>"""

        warning_html = ""
        for w in warnings:
            warning_html += f'<div style="padding:8px 12px;background:rgba(242,101,101,0.08);border-left:3px solid var(--red);border-radius:6px;font-size:12px;color:var(--text2);margin-bottom:6px;">{w}</div>'

        # 7-day run windows table
        run_window_rows = ""
        day_names = ["Today", "Tomorrow", "Wed", "Thu", "Fri", "Sat", "Sun"]
        from datetime import datetime as dt
        for i, d in enumerate(days):
            try:
                day_name = dt.strptime(d["date"], "%Y-%m-%d").strftime("%a")
            except:
                day_name = day_names[i] if i < len(day_names) else d["date"]
            bw = d.get("best_window", "--")
            bd_detail = d.get("best_detail")
            warn_str = " · ".join(d.get("warnings", [])) or "Good"
            warn_color = "var(--red)" if d.get("warnings") else "var(--green)"
            bw_color = "var(--green)" if not d.get("warnings") else "var(--yellow)"
            detail_str = f"{bd_detail['temp']}°C {bd_detail['rain']}% rain" if bd_detail else "--"
            run_window_rows += f"""
            <tr>
              <td style="font-weight:500">{day_name} {d['date'][5:]}</td>
              <td>{weather_icon(d['weathercode'])} {d['max_temp']}° / {d['min_temp']}°</td>
              <td>💧{d['rain_pct']}%</td>
              <td style="color:{bw_color};font-weight:500">{bw}</td>
              <td style="color:var(--text3)">{detail_str}</td>
              <td style="color:{warn_color}">{warn_str}</td>
            </tr>"""

        best_color = "var(--green)" if not warnings else "var(--yellow)"
        tomorrow_date = days[1]["date"]
        window_detail = ""
        if morning and afternoon:
            window_detail = f"Morning: {morning['temp']}°C {morning['rain']}% rain · Afternoon: {afternoon['temp']}°C {afternoon['rain']}% rain"

        weather_html = f"""
  <div class="section">
    <div class="section-header"><div class="section-title">Weather — Wantirna South</div></div>
    <div class="stat-grid">{day_cards}</div>
    {warning_html}
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px;margin-top:10px;margin-bottom:10px;">
      <div style="font-size:10px;color:var(--text3);letter-spacing:0.8px;text-transform:uppercase;margin-bottom:8px;">Best Run Window Tomorrow — {tomorrow_date}</div>
      <div style="font-size:20px;font-weight:600;color:{best_color};margin-bottom:4px;">{best}</div>
      <div style="font-size:11px;color:var(--text3);">{window_detail}</div>
    </div>
    <div class="tbl-wrap">
      <table class="tbl">
        <thead><tr><th>Day</th><th>Temp</th><th>Rain</th><th>Best Window</th><th>Conditions</th><th>Status</th></tr></thead>
        <tbody>{run_window_rows}</tbody>
      </table>
    </div>
  </div>"""

    # Pre-compute cycle display values
    last_cycle_duration = f"{last_cycle['duration']:.0f}" if last_cycle else '--'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<meta name="theme-color" content="#0d0f14">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Health">
<link rel="manifest" href="manifest.json">
<title>Nat's Health Dashboard — {TODAY}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #0d0f14;
    --surface: #13151c;
    --surface2: #1a1d27;
    --surface3: #21253a;
    --border: #252836;
    --border2: #2e3347;
    --text: #f0f2f8;
    --text2: #9ba3bf;
    --text3: #5c6480;
    --blue: #4f8ef7;
    --purple: #7c6cf7;
    --green: #3dd68c;
    --yellow: #f5c842;
    --red: #f26565;
    --orange: #f59e42;
    --cyan: #38d9f5;
    --pink: #f06292;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', sans-serif;
    font-size: 13px;
    min-height: 100vh;
  }}

  /* ── TOP BAR ── */
  .topbar {{
    display: flex; align-items: center;
    padding: 0 20px;
    height: 52px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    position: sticky; top: 0; z-index: 100;
    gap: 16px;
  }}
  .topbar-logo {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px; font-weight: 500;
    color: var(--blue);
    letter-spacing: 0.5px;
    white-space: nowrap;
  }}
  .topbar-date {{
    font-size: 11px; color: var(--text3);
    font-family: 'JetBrains Mono', monospace;
  }}
  .topbar-right {{
    margin-left: auto;
    display: flex; align-items: center; gap: 8px;
  }}
  .status-dot {{
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--green);
    animation: pulse 2s infinite;
  }}
  @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:0.4}} }}
  .status-text {{ font-size: 10px; color: var(--text3); }}

  /* ── TABS ── */
  .tabs {{
    display: flex;
    padding: 0 20px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    gap: 0;
    overflow-x: auto;
  }}
  .tab {{
    padding: 10px 16px;
    font-size: 12px; font-weight: 500;
    color: var(--text3);
    cursor: pointer;
    border-bottom: 2px solid transparent;
    white-space: nowrap;
    transition: all 0.15s;
    user-select: none;
  }}
  .tab:hover {{ color: var(--text2); }}
  .tab.active {{ color: var(--text); border-bottom-color: var(--blue); }}
  .tab-icon {{ margin-right: 6px; }}

  /* ── CONTENT ── */
  .content {{ padding: 20px; max-width: 1400px; margin: 0 auto; }}
  .tab-panel {{ display: none; }}
  .tab-panel.active {{ display: block; }}

  /* ── STAT GRID ── */
  .stat-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
    gap: 10px;
    margin-bottom: 16px;
  }}
  .stat {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px;
    position: relative;
    overflow: hidden;
    transition: border-color 0.2s;
  }}
  .stat:hover {{ border-color: var(--border2); }}
  .stat-accent {{
    position: absolute; top: 0; left: 0; right: 0;
    height: 2px; border-radius: 10px 10px 0 0;
  }}
  .stat-label {{
    font-size: 10px; font-weight: 500;
    letter-spacing: 0.8px; text-transform: uppercase;
    color: var(--text3); margin-bottom: 8px;
  }}
  .stat-value {{
    font-size: 28px; font-weight: 600;
    line-height: 1; color: var(--text);
    font-family: 'JetBrains Mono', monospace;
  }}
  .stat-value.sm {{ font-size: 18px; padding-top: 4px; }}
  .stat-unit {{ font-size: 10px; color: var(--text3); margin-top: 4px; }}
  .stat-sub {{ font-size: 11px; color: var(--text2); margin-top: 6px; }}

  /* ── SECTION ── */
  .section {{ margin-bottom: 20px; }}
  .section-header {{
    display: flex; align-items: center; gap: 10px;
    margin-bottom: 12px;
  }}
  .section-title {{
    font-size: 11px; font-weight: 600;
    letter-spacing: 1.2px; text-transform: uppercase;
    color: var(--text3);
  }}
  .section-header::after {{
    content: ''; flex: 1;
    height: 1px; background: var(--border);
  }}

  /* ── CHARTS ── */
  .chart-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 10px;
  }}
  .chart-box {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px;
  }}
  .chart-label {{
    font-size: 10px; font-weight: 500;
    letter-spacing: 0.8px; text-transform: uppercase;
    color: var(--text3); margin-bottom: 12px;
  }}
  canvas {{ max-height: 180px; }}

  /* ── TABLES ── */
  .tbl-wrap {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
  }}
  .tbl {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  .tbl th {{
    text-align: left; padding: 8px 12px;
    font-size: 10px; font-weight: 500; letter-spacing: 0.8px;
    text-transform: uppercase; color: var(--text3);
    background: var(--surface2);
    border-bottom: 1px solid var(--border);
  }}
  .tbl td {{
    padding: 9px 12px;
    border-bottom: 1px solid var(--border);
    color: var(--text2);
  }}
  .tbl tr:last-child td {{ border-bottom: none; }}
  .tbl tr:hover td {{ background: var(--surface2); color: var(--text); }}
  .tbl td:first-child {{ color: var(--text); font-weight: 500; }}

  /* ── BADGES ── */
  .badge {{
    display: inline-block; padding: 2px 7px;
    border-radius: 4px; font-size: 10px; font-weight: 600;
    letter-spacing: 0.3px;
  }}
  .badge-green  {{ background: rgba(61,214,140,0.12); color: var(--green); }}
  .badge-yellow {{ background: rgba(245,200,66,0.12); color: var(--yellow); }}
  .badge-red    {{ background: rgba(242,101,101,0.12); color: var(--red); }}
  .badge-blue   {{ background: rgba(79,142,247,0.12); color: var(--blue); }}

  /* ── GCT BAR ── */
  .gct-bar-wrap {{
    margin-top: 6px; height: 4px; border-radius: 2px;
    background: var(--border); overflow: hidden;
  }}
  .gct-bar {{ height: 100%; border-radius: 2px; }}

  /* ── PHASE BAR ── */
  .phase-bar {{
    height: 6px; background: var(--surface2);
    border-radius: 3px; overflow: hidden; position: relative;
    margin: 8px 0 4px;
  }}
  .phase-fill {{
    height: 100%; border-radius: 3px;
    background: linear-gradient(90deg, var(--blue), var(--purple));
  }}

  /* ── EXPORT BUTTONS ── */
  .btn {{
    display: inline-flex; align-items: center; gap: 6px;
    padding: 8px 16px; border-radius: 7px;
    font-family: 'Inter', sans-serif; font-size: 12px; font-weight: 500;
    cursor: pointer; transition: all 0.15s; border: 1px solid;
  }}
  .btn-outline {{
    background: transparent;
    border-color: var(--border2);
    color: var(--text2);
  }}
  .btn-outline:hover {{ border-color: var(--blue); color: var(--blue); }}
  .btn-purple {{
    background: rgba(124,108,247,0.1);
    border-color: rgba(124,108,247,0.3);
    color: var(--purple);
  }}
  .btn-purple:hover {{ background: rgba(124,108,247,0.2); }}

  /* ── CHECK-IN ── */
  .checkin-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
  }}
  .checkin-field {{ margin-bottom: 14px; }}
  .checkin-label {{
    display: flex;
    justify-content: space-between;
    font-size: 12px;
    color: var(--text2);
    margin-bottom: 6px;
  }}
  .checkin-val {{
    font-family: 'JetBrains Mono', monospace;
    color: var(--blue);
    font-size: 11px;
  }}
  .checkin-slider {{ width: 100%; accent-color: var(--blue); }}
  .checkin-toggle {{
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 12px;
    color: var(--text2);
    cursor: pointer;
    user-select: none;
  }}
  .checkin-toggle input {{ width: 16px; height: 16px; accent-color: var(--green); }}
  .checkin-trend {{ font-size: 11px; color: var(--text3); }}
  .checkin-trend table {{ width: 100%; border-collapse: collapse; }}
  .checkin-trend td {{
    padding: 5px 6px;
    border-bottom: 1px solid var(--border);
    text-align: center;
  }}
  .checkin-trend td:first-child {{ text-align: left; color: var(--text2); }}
  .pain-low {{ color: var(--green); }}
  .pain-med {{ color: var(--yellow); }}
  .pain-high {{ color: var(--red); }}

  /* ── DECISION ENGINE ── */
  .decision-card {{
    border-radius: 12px;
    border: 1px solid var(--border2);
    padding: 20px;
    margin-bottom: 16px;
    position: relative;
    overflow: hidden;
  }}
  .decision-card::before {{
    content: '';
    position: absolute; inset: 0;
    background: linear-gradient(135deg, var(--decision-color, var(--green)) 0%, transparent 60%);
    opacity: 0.06;
    pointer-events: none;
  }}
  .decision-status {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 28px; font-weight: 700;
    letter-spacing: -0.5px;
    margin-bottom: 4px;
  }}
  .decision-sub {{
    font-size: 11px; color: var(--text3);
    letter-spacing: 0.5px; margin-bottom: 16px;
  }}
  .decision-body {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
  }}
  .decision-factors {{ display: flex; flex-direction: column; gap: 6px; }}
  .factor {{
    display: flex; align-items: center; gap: 8px;
    font-size: 12px; color: var(--text2);
  }}
  .factor-dot {{
    width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0;
  }}
  .decision-rec {{
    background: var(--surface2);
    border-radius: 8px; padding: 14px;
  }}
  .rec-label {{
    font-size: 10px; letter-spacing: 1px; text-transform: uppercase;
    color: var(--text3); margin-bottom: 10px;
  }}
  .rec-row {{
    display: flex; justify-content: space-between;
    font-size: 12px; padding: 4px 0;
    border-bottom: 1px solid var(--border);
  }}
  .rec-row:last-child {{ border-bottom: none; }}
  .rec-key {{ color: var(--text3); }}
  .rec-val {{ color: var(--text); font-weight: 500; }}
  .avoid-list {{
    margin-top: 10px; font-size: 11px; color: var(--red);
  }}
  @media(max-width:600px) {{
    .decision-body {{ grid-template-columns: 1fr; }}
    .decision-status {{ font-size: 22px; }}
  }}

  /* ── RESPONSIVE ── */
  @media (max-width: 600px) {{
    .content {{ padding: 12px; }}
    .stat-grid {{ grid-template-columns: repeat(2, 1fr); }}
    .chart-grid {{ grid-template-columns: 1fr; }}
    .topbar-logo {{ font-size: 11px; }}
  }}
</style>
</head>
<body>

<!-- TOP BAR -->
<div class="topbar">
  <div class="topbar-logo">NAT / HEALTH</div>
  <div class="topbar-date">{TODAY}</div>
  <div style="font-size:10px;color:var(--text3);font-family:'JetBrains Mono',monospace;">Updated {datetime.now().strftime('%H:%M')}</div>
  <div class="topbar-right">
    <div class="status-dot"></div>
    <div class="status-text">LIVE</div>
  </div>
</div>

<!-- TABS -->
<div class="tabs">
  <div class="tab active" onclick="switchTab('overview')"><span class="tab-icon">◎</span>Overview</div>
  <div class="tab" onclick="switchTab('running')"><span class="tab-icon">⟋</span>Running</div>
  <div class="tab" onclick="switchTab('cycling')"><span class="tab-icon">◯</span>Cycling</div>
  <div class="tab" onclick="switchTab('health')"><span class="tab-icon">♡</span>Health</div>
  <div class="tab" onclick="switchTab('performance')"><span class="tab-icon">◈</span>Performance</div>
  <div class="tab" onclick="switchTab('bp')"><span class="tab-icon">↕</span>Blood Pressure</div>
  <div class="tab" onclick="switchTab('hrcompare')"><span class="tab-icon">⇌</span>HR Compare</div>
</div>

<div class="content">

<!-- ═══════════════════════════════════════════════════════ OVERVIEW -->
<div class="tab-panel active" id="panel-overview">

  <!-- TRAINING DECISION ENGINE -->
  <div class="decision-card" style="--decision-color:{dec_color};border-color:{dec_color}33">
    <div class="decision-status" style="color:{dec_color}">{dec_emoji} {dec_status}</div>
    <div class="decision-sub">TRAINING DECISION · {TODAY} · Week {week_num}/28 — {phase["name"]}</div>
    <div class="decision-body">
      <div class="decision-factors">
        <div style="font-size:10px;letter-spacing:1px;text-transform:uppercase;color:var(--text3);margin-bottom:6px">Why</div>
        {factors_html}
        {avoid_html}
      </div>
      <div class="decision-rec">
        <div class="rec-label">Recommended Session</div>
        <div class="rec-row"><span class="rec-key">Type</span><span class="rec-val">{decision["rec_type"]}</span></div>
        <div class="rec-row"><span class="rec-key">Distance</span><span class="rec-val">{decision["rec_dist"]}</span></div>
        <div class="rec-row"><span class="rec-key">Pace</span><span class="rec-val">{decision["rec_pace"]}</span></div>
        <div class="rec-row"><span class="rec-key">HR target</span><span class="rec-val">{decision["rec_hr"]}</span></div>
        <div class="rec-row"><span class="rec-key">Best time</span><span class="rec-val">{decision["best_window"]} · {decision["today_temp"]}</span></div>
      </div>
    </div>
  </div>

  <!-- DAILY ACHILLES CHECK-IN -->
  <div class="section" id="checkin-section">
    <div class="section-header">
      <div class="section-title">Morning Check-in</div>
      <div style="font-size:10px;color:var(--text3)">30 sec · subjective tendon feedback</div>
    </div>
    <div class="chart-box">
      <div class="checkin-grid">
        <div>
          <div class="checkin-field">
            <div class="checkin-label"><span>Morning stiffness</span><span class="checkin-val" id="stiffness-val">0</span></div>
            <input type="range" class="checkin-slider" id="stiffness" min="0" max="10" value="0" oninput="updateCheckinPreview()">
          </div>
          <div class="checkin-field">
            <div class="checkin-label"><span>Pain — first steps</span><span class="checkin-val" id="first-steps-val">0</span></div>
            <input type="range" class="checkin-slider" id="first-steps" min="0" max="10" value="0" oninput="updateCheckinPreview()">
          </div>
          <div class="checkin-field">
            <div class="checkin-label"><span>Pain — after yesterday's run</span><span class="checkin-val" id="post-run-val">0</span></div>
            <input type="range" class="checkin-slider" id="post-run" min="0" max="10" value="0" oninput="updateCheckinPreview()">
          </div>
          <label class="checkin-toggle">
            <input type="checkbox" id="calf-raises">
            Seated calf raises done today
          </label>
          <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">
            <button class="btn btn-outline" onclick="saveCheckin()">Save today</button>
            <button class="btn btn-outline" onclick="exportCheckins()">Export history</button>
          </div>
          <div id="checkin-status" style="font-size:11px;color:var(--text3);margin-top:8px"></div>
        </div>
        <div>
          <div class="chart-label">Last 7 days</div>
          <div class="checkin-trend" id="checkin-trend">Loading…</div>
          <div style="font-size:10px;color:var(--text3);margin-top:10px;line-height:1.5">
            Auto-syncs to the server on save (one-time setup per device — see cloudflare-worker/SETUP.md). <code style="background:var(--surface2);padding:1px 5px;border-radius:3px">Export history</code> is a manual backup if sync is unavailable.
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- VITALS ROW -->
  <div class="section">
    <div class="section-header"><div class="section-title">Today's Vitals</div></div>
    <div class="stat-grid">
      <div class="stat">
        <div class="stat-accent" style="background:{'var(--green)' if isinstance(readiness_score, int) and readiness_score >= 70 else 'var(--yellow)' if isinstance(readiness_score, int) and readiness_score >= 50 else 'var(--red)'}"></div>
        <div class="stat-label">Readiness</div>
        <div class="stat-value">{readiness_score}</div>
        <div class="stat-unit">{readiness_level}</div>
        <div class="stat-sub">{readiness.get('feedback','')}</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--purple)"></div>
        <div class="stat-label">Sleep Score</div>
        <div class="stat-value">{sleep_score}</div>
        <div class="stat-unit">/ 100</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--cyan)"></div>
        <div class="stat-label">HRV Avg 7d</div>
        <div class="stat-value">{hrv_avg}</div>
        <div class="stat-unit">ms</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--red)"></div>
        <div class="stat-label">Resting HR</div>
        <div class="stat-value">{resting_hr}</div>
        <div class="stat-unit">bpm</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:{'var(--green)' if isinstance(body_battery, int) and body_battery >= 60 else 'var(--yellow)' if isinstance(body_battery, int) and body_battery >= 30 else 'var(--orange)'}"></div>
        <div class="stat-label">Body Battery</div>
        <div class="stat-value">{body_battery}</div>
        <div class="stat-unit">/ 100</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--blue)"></div>
        <div class="stat-label">Recovery</div>
        <div class="stat-value sm">{readiness.get('recovery_time','--')}</div>
        <div class="stat-unit">min</div>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-header"><div class="section-title">Training Plan — Week {week_num} of 28</div></div>
    <div class="stat-grid">
      <div class="stat">
        <div class="stat-accent" style="background:var(--blue)"></div>
        <div class="stat-label">Phase</div>
        <div class="stat-value sm">{phase["name"]}</div>
        <div class="stat-unit">Week {week_in_phase} of {phase_total}</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--purple)"></div>
        <div class="stat-label">This Week</div>
        <div class="stat-value">{achilles.get("this_week_km","--")}</div>
        <div class="stat-unit">km (target {phase["km_min"]}–{phase["km_max"]})</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--orange)"></div>
        <div class="stat-label">Days to Race</div>
        <div class="stat-value">{days_to_race}</div>
        <div class="stat-unit">12 Oct 2026</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:{'var(--red)' if achilles_level=='high' else 'var(--yellow)' if achilles_level=='medium' else 'var(--green)'}"></div>
        <div class="stat-label">Achilles Load</div>
        <div class="stat-value" style="color:{achilles_color}">{achilles_score}</div>
        <div class="stat-unit" style="color:{achilles_color}">{achilles_level} risk</div>
      </div>
    </div>
    <div class="chart-box" style="padding:12px 16px;">
      <div class="chart-label">28-week progress</div>
      <div class="phase-bar">
        <div class="phase-fill" style="width:{round((week_num-1)/28*100)}%"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text3);">
        <span>May 1</span><span>Rehab</span><span>Return</span><span>Build</span><span>Specific</span><span>Taper</span><span>Oct 12</span>
      </div>
    </div>
  </div>

{weather_html}

  <div class="section">
    <div class="section-header"><div class="section-title">Training Load vs Capacity</div></div>
    <div class="stat-grid">
      <div class="stat">
        <div class="stat-accent" style="background:{acr_color}"></div>
        <div class="stat-label">Load Ratio (ACR)</div>
        <div class="stat-value" style="color:{acr_color}">{acr}</div>
        <div class="stat-unit">{'🟢 Optimal 0.8-1.3' if acr_status=='optimal' else '🔴 Too high >1.3' if acr_status=='high' else '🟡 Low <0.8'}</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--blue)"></div>
        <div class="stat-label">Acute Load (7d)</div>
        <div class="stat-value sm">{acute_load}</div>
        <div class="stat-unit">stress units</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--text3)"></div>
        <div class="stat-label">Chronic Load (28d avg)</div>
        <div class="stat-value sm">{chronic_load}</div>
        <div class="stat-unit">stress units</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:{'var(--green)' if easy_pct>=70 else 'var(--yellow)' if easy_pct>=50 else 'var(--red)'}"></div>
        <div class="stat-label">Intensity Mix (7d)</div>
        <div class="stat-value sm" style="font-size:14px;padding-top:4px">{easy_pct}% easy</div>
        <div class="stat-unit">{mod_pct}% mod · {hard_pct}% hard {'⚠ grey zone' if mod_pct>40 else '✓ 80/20'}</div>
      </div>
      {'<div class="stat"><div class="stat-accent" style="background:var(--red)"></div><div class="stat-label">Back-to-Back Risk</div><div class="stat-value sm" style="color:var(--red);font-size:14px;padding-top:4px">⚠ Detected</div><div class="stat-unit">2+ hard/long runs in 4 days</div></div>' if btb_risk else ''}
    </div>
    <!-- ACR gauge -->
    <div class="chart-box" style="padding:12px 16px;margin-top:10px">
      <div class="chart-label">Acute:Chronic Load Ratio — safe zone 0.8 to 1.3</div>
      <div style="position:relative;height:12px;background:var(--surface2);border-radius:6px;margin:8px 0">
        <div style="position:absolute;left:0;top:0;bottom:0;width:40%;background:rgba(245,200,66,0.3);border-radius:6px 0 0 6px"></div>
        <div style="position:absolute;left:40%;top:0;bottom:0;width:25%;background:rgba(61,214,140,0.3)"></div>
        <div style="position:absolute;left:65%;top:0;bottom:0;right:0;background:rgba(242,101,101,0.3);border-radius:0 6px 6px 0"></div>
        <div style="position:absolute;top:-2px;bottom:-2px;width:4px;border-radius:2px;background:{acr_color};left:{min(95,round(acr/2.0*100))}%;transform:translateX(-50%)"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--text3)">
        <span>0 — Low</span><span>0.8</span><span>1.0 Optimal</span><span>1.3</span><span>2.0+ Danger</span>
      </div>
    </div>
  </div>

  <!-- Recovery Score -->
  <div class="section">
    <div class="section-header"><div class="section-title">Recovery Score</div></div>
    <div style="display:grid;grid-template-columns:160px 1fr;gap:12px;align-items:start">
      <div class="stat" style="text-align:center">
        <div class="stat-accent" style="background:{recovery_color}"></div>
        <div class="stat-label">Recovery</div>
        <div class="stat-value" style="font-size:42px;color:{recovery_color}">{recovery_score}</div>
        <div class="stat-unit" style="color:{recovery_color}">{recovery_level}</div>
      </div>
      <div class="chart-box">
        <div class="chart-label">Factors</div>
        {recovery_html}
      </div>
    </div>
  </div>

  <!-- Workout Generator -->
  <div class="section">
    <div class="section-header"><div class="section-title">Today's Recommended Session</div></div>
    <div class="chart-box">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
        <div>
          <div style="font-size:16px;font-weight:600;color:var(--text);margin-bottom:4px">{workout.get('type','--')}</div>
          <div style="font-size:13px;color:var(--text2);line-height:1.6;margin-bottom:12px">{workout.get('description','')}</div>
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px">
            <div style="text-align:center;background:var(--surface2);border-radius:8px;padding:10px">
              <div style="font-size:9px;color:var(--text3);letter-spacing:0.8px;text-transform:uppercase;margin-bottom:4px">Distance</div>
              <div style="font-size:14px;font-weight:600;color:var(--blue)">{workout.get('distance','--')}</div>
            </div>
            <div style="text-align:center;background:var(--surface2);border-radius:8px;padding:10px">
              <div style="font-size:9px;color:var(--text3);letter-spacing:0.8px;text-transform:uppercase;margin-bottom:4px">Pace</div>
              <div style="font-size:12px;font-weight:600;color:var(--green)">{workout.get('pace','--')}</div>
            </div>
            <div style="text-align:center;background:var(--surface2);border-radius:8px;padding:10px">
              <div style="font-size:9px;color:var(--text3);letter-spacing:0.8px;text-transform:uppercase;margin-bottom:4px">HR Target</div>
              <div style="font-size:12px;font-weight:600;color:var(--red)">{workout.get('hr','--')}</div>
            </div>
          </div>
        </div>
        <div>
          <div style="font-size:10px;color:var(--text3);letter-spacing:0.8px;text-transform:uppercase;margin-bottom:8px">Avoid Today</div>
          <div style="display:flex;flex-wrap:wrap;gap:4px">{avoid_items}</div>
        </div>
      </div>
    </div>
  </div>

  {'<div class="section"><div class="section-header"><div class="section-title">Injury Detective — Last 21 Days</div></div>' + injury_html + '</div>' if injury_contributors else ''}

  <div class="section">
    <div class="section-header"><div class="section-title">Fitness / Fatigue / Form — intervals.icu</div></div>
    <div class="stat-grid" style="margin-bottom:12px">
      <div class="stat">
        <div class="stat-accent" style="background:var(--blue)"></div>
        <div class="stat-label">Fitness (CTL)</div>
        <div class="stat-value">{latest_ctl}</div>
        <div class="stat-unit">chronic training load</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--red)"></div>
        <div class="stat-label">Fatigue (ATL)</div>
        <div class="stat-value">{latest_atl}</div>
        <div class="stat-unit">acute training load</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:{tsb_color}"></div>
        <div class="stat-label">Form (TSB)</div>
        <div class="stat-value" style="color:{tsb_color}">{latest_tsb}</div>
        <div class="stat-unit">{'fresh' if isinstance(latest_tsb,(int,float)) and latest_tsb>=0 else 'fatigued'}</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--purple)"></div>
        <div class="stat-label">Ramp Rate</div>
        <div class="stat-value sm">{latest_ramp}</div>
        <div class="stat-unit">CTL/week</div>
      </div>
    </div>
    <div class="chart-grid">
      <div class="chart-box" style="grid-column:span 2">
        <div class="chart-label">CTL / ATL / TSB — 60 days</div>
        <canvas id="ctlChart" style="max-height:200px"></canvas>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-header"><div class="section-title">Weekly Activity</div></div>
    <div class="chart-grid">
      <div class="chart-box">
        <div class="chart-label">Weekly km — Running + Cycling</div>
        <canvas id="weeklyComboChart"></canvas>
      </div>
      <div class="chart-box">
        <div class="chart-label">Overnight HRV (ms)</div>
        <canvas id="hrvChart"></canvas>
      </div>
    </div>
  </div>

</div>

<!-- ═══════════════════════════════════════════════════════ RUNNING -->
<div class="tab-panel" id="panel-running">

  <!-- COACH PANEL -->
  <div class="section">
    <div class="section-header"><div class="section-title">Running Coach Panel</div></div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:12px">

      <!-- CTL/ATL/TSB -->
      <div class="chart-box">
        <div class="chart-label">Fitness / Fatigue / Form</div>
        <div style="margin-bottom:12px">
          <div style="display:flex;justify-content:space-between;margin-bottom:4px">
            <span style="font-size:12px;color:var(--text2)">Fitness (CTL)</span>
            <span style="font-size:12px;color:var(--blue);font-family:'JetBrains Mono',monospace">{round(ctl_val,1)}</span>
          </div>
          <div style="height:6px;background:var(--border);border-radius:3px;overflow:hidden">
            <div style="height:100%;width:{ctl_pct}%;background:var(--blue);border-radius:3px"></div>
          </div>
        </div>
        <div style="margin-bottom:12px">
          <div style="display:flex;justify-content:space-between;margin-bottom:4px">
            <span style="font-size:12px;color:var(--text2)">Fatigue (ATL)</span>
            <span style="font-size:12px;color:var(--red);font-family:'JetBrains Mono',monospace">{round(atl_val,1)}</span>
          </div>
          <div style="height:6px;background:var(--border);border-radius:3px;overflow:hidden">
            <div style="height:100%;width:{atl_pct}%;background:var(--red);border-radius:3px"></div>
          </div>
        </div>
        <div>
          <div style="display:flex;justify-content:space-between;margin-bottom:4px">
            <span style="font-size:12px;color:var(--text2)">Form (TSB)</span>
            <span style="font-size:12px;color:{tsb_col};font-family:'JetBrains Mono',monospace">{round(tsb_val,1):+.1f}</span>
          </div>
          <div style="height:6px;background:var(--border);border-radius:3px;overflow:hidden">
            <div style="height:100%;width:{tsb_abs}%;background:{tsb_col};border-radius:3px"></div>
          </div>
        </div>
      </div>

      <!-- Running Form Score -->
      <div class="chart-box">
        <div class="chart-label">Running Form Score</div>
        <div style="text-align:center;margin-bottom:12px">
          <div style="font-size:42px;font-weight:700;color:{form_color};font-family:'JetBrains Mono',monospace">{form_score if form_score else '--'}</div>
          <div style="font-size:10px;color:var(--text3);margin-top:2px">/ 100</div>
        </div>
        {form_factors_html}
      </div>

      <!-- Aerobic Decoupling -->
      <div class="chart-box">
        <div class="chart-label">Aerobic Decoupling</div>
        <div style="text-align:center;margin-bottom:16px">
          <div style="font-size:36px;font-weight:700;color:{decoupling_color};font-family:'JetBrains Mono',monospace">{f'{decoupling_pct}%' if decoupling_pct is not None else '--'}</div>
          <div style="font-size:12px;color:{decoupling_color};margin-top:4px">{decoupling_status}</div>
          <div style="font-size:10px;color:var(--text3);margin-top:4px">Target: &lt;5%</div>
        </div>
        <div style="height:6px;background:var(--border);border-radius:3px;overflow:hidden;margin-bottom:12px">
          <div style="height:100%;width:{min(100, round(decoupling_pct/10*100)) if decoupling_pct else 0}%;background:{decoupling_color};border-radius:3px"></div>
        </div>
        <div style="font-size:10px;color:var(--text3)">HR vs pace efficiency comparing first vs second half of run. &lt;5% = aerobically efficient.</div>
      </div>

    </div>

    <!-- Coach Note -->
    <div style="background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:16px">
      <div style="font-size:10px;color:var(--text3);letter-spacing:0.8px;text-transform:uppercase;margin-bottom:12px">Coach Note</div>
      {coach_note_html}
    </div>
  </div>

  <!-- HR Efficiency Trend -->
  <div class="section">
    <div class="section-header"><div class="section-title">HR Efficiency Trend</div></div>
    <div class="chart-grid">
      <div class="chart-box">
        <div class="chart-label">Pace/HR ratio — higher = more efficient (fitness improving)</div>
        <canvas id="hrEffChart"></canvas>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-header"><div class="section-title">Last Run — {last_run.get('date','--')} · {last_run.get('distance','--')} km</div></div>
    <div class="stat-grid">
      <div class="stat">
        <div class="stat-accent" style="background:var(--blue)"></div>
        <div class="stat-label">Avg Pace</div>
        <div class="stat-value sm">{pace_str(last_run.get('avg_pace'))}</div>
        <div class="stat-unit">min/km</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--red)"></div>
        <div class="stat-label">Avg HR</div>
        <div class="stat-value">{last_run.get('avg_hr','--')}</div>
        <div class="stat-unit">bpm</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--green)"></div>
        <div class="stat-label">Cadence</div>
        <div class="stat-value">{last_run.get('cadence','--')}</div>
        <div class="stat-unit">spm</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:{gct_bal_color}"></div>
        <div class="stat-label">GCT Balance L</div>
        <div class="stat-value sm" style="color:{gct_bal_color}">{last_gct_balance if last_gct_balance else '--'}%</div>
        <div class="gct-bar-wrap"><div class="gct-bar" style="width:{last_gct_balance or 50}%;background:{gct_bal_color}"></div></div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:{gct_bal_color_r}"></div>
        <div class="stat-label">GCT Balance R</div>
        <div class="stat-value sm" style="color:{gct_bal_color_r}">{last_gct_balance_r if last_gct_balance_r else '--'}%</div>
        <div class="gct-bar-wrap"><div class="gct-bar" style="width:{last_gct_balance_r or 50}%;background:{gct_bal_color_r}"></div></div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--cyan)"></div>
        <div class="stat-label">Avg GCT</div>
        <div class="stat-value sm">{last_gct_avg}</div>
        <div class="stat-unit">ms</div>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-header"><div class="section-title">Lap Breakdown</div></div>
    <div class="chart-grid">
      <div class="chart-box">
        <div class="chart-label">GCT Balance L/R % per km</div>
        <canvas id="lapBalanceChart"></canvas>
      </div>
      <div class="chart-box">
        <div class="chart-label">HR per km (bpm)</div>
        <canvas id="lapHrChart"></canvas>
      </div>
      <div class="chart-box">
        <div class="chart-label">Pace per km (min/km)</div>
        <canvas id="lapPaceChart"></canvas>
      </div>
      <div class="chart-box">
        <div class="chart-label">GCT per km (ms)</div>
        <canvas id="lapGctChart"></canvas>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-header"><div class="section-title">Weekly Mileage Trend</div></div>
    <div class="chart-grid">
      <div class="chart-box">
        <div class="chart-label">Weekly km with 10% rule</div>
        <canvas id="mileageTrendChart"></canvas>
      </div>
      <div class="chart-box">
        <div class="chart-label">Week-on-week change % (safe limit: 10%)</div>
        <canvas id="mileageChangeChart"></canvas>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-header"><div class="section-title">GCT Balance Trend</div></div>
    <div class="chart-grid">
      <div class="chart-box">
        <div class="chart-label">GCT Balance L/R % — recent runs</div>
        <canvas id="gctTrendChart"></canvas>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-header"><div class="section-title">Achilles Load</div></div>
    <div class="stat-grid" style="margin-bottom:12px">
      <div class="stat">
        <div class="stat-accent" style="background:{achilles_color}"></div>
        <div class="stat-label">Load Score</div>
        <div class="stat-value" style="color:{achilles_color}">{achilles_score}</div>
        <div class="stat-unit" style="color:{achilles_color}">{achilles_level} risk / 100</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--blue)"></div>
        <div class="stat-label">This Week</div>
        <div class="stat-value">{achilles.get("this_week_km","--")}</div>
        <div class="stat-unit">km</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--text3)"></div>
        <div class="stat-label">Last Week</div>
        <div class="stat-value">{achilles.get("last_week_km","--")}</div>
        <div class="stat-unit">km</div>
      </div>
    </div>
    <div class="tbl-wrap">
      <table class="tbl">
        <thead><tr><th>Factor</th><th>Value</th></tr></thead>
        <tbody>{achilles_rows}</tbody>
      </table>
    </div>
  </div>

  <div class="section">
    <div class="section-header"><div class="section-title">Run Log</div></div>
    <div class="tbl-wrap">
      <table class="tbl">
        <thead><tr><th>Date</th><th>Dist</th><th>Pace</th><th>HR</th><th>Cadence</th><th>GCT L</th><th>GCT R</th><th>GCT avg</th></tr></thead>
        <tbody>{run_table_rows}</tbody>
      </table>
    </div>
  </div>

</div>

<!-- ═══════════════════════════════════════════════════════ CYCLING -->
<div class="tab-panel" id="panel-cycling">

  <div class="section">
    <div class="section-header"><div class="section-title">Last Ride — {last_cycle.get('date','--')}</div></div>
    <div class="stat-grid">
      <div class="stat">
        <div class="stat-accent" style="background:var(--blue)"></div>
        <div class="stat-label">Distance</div>
        <div class="stat-value">{last_cycle.get('distance','--')}</div>
        <div class="stat-unit">km</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--green)"></div>
        <div class="stat-label">Avg Speed</div>
        <div class="stat-value">{last_cycle.get('avg_speed','--')}</div>
        <div class="stat-unit">km/h</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--red)"></div>
        <div class="stat-label">Avg HR</div>
        <div class="stat-value">{last_cycle.get('avg_hr','--')}</div>
        <div class="stat-unit">bpm</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--orange)"></div>
        <div class="stat-label">Duration</div>
        <div class="stat-value sm">{last_cycle_duration}</div>
        <div class="stat-unit">min</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--purple)"></div>
        <div class="stat-label">Elevation</div>
        <div class="stat-value">{last_cycle.get('elevation','--')}</div>
        <div class="stat-unit">m gain</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--yellow)"></div>
        <div class="stat-label">Calories</div>
        <div class="stat-value sm">{last_cycle.get('calories','--')}</div>
        <div class="stat-unit">kcal</div>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-header"><div class="section-title">Ride Log</div></div>
    <div class="tbl-wrap">
      <table class="tbl">
        <thead><tr><th>Date</th><th>Distance</th><th>Duration</th><th>Avg Speed</th><th>HR</th><th>Elevation</th><th>Calories</th></tr></thead>
        <tbody>{cycle_table_rows if cycle_table_rows else '<tr><td colspan="7" style="text-align:center;color:var(--text3)">No cycling activities found</td></tr>'}</tbody>
      </table>
    </div>
  </div>

</div>

<!-- ═══════════════════════════════════════════════════════ HEALTH -->
<div class="tab-panel" id="panel-health">

  <div class="section">
    <div class="section-header"><div class="section-title">Last Night's Sleep</div></div>
    <div class="stat-grid" style="margin-bottom:12px">
      <div class="stat">
        <div class="stat-accent" style="background:var(--purple)"></div>
        <div class="stat-label">Total Sleep</div>
        <div class="stat-value">{sleep_duration}</div>
        <div class="stat-unit">hours</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--blue)"></div>
        <div class="stat-label">Deep Sleep</div>
        <div class="stat-value">{sleep_deep}</div>
        <div class="stat-unit">hours</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--cyan)"></div>
        <div class="stat-label">REM Sleep</div>
        <div class="stat-value">{sleep_rem}</div>
        <div class="stat-unit">hours</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--text3)"></div>
        <div class="stat-label">Light Sleep</div>
        <div class="stat-value">{sleep_light}</div>
        <div class="stat-unit">hours</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--yellow)"></div>
        <div class="stat-label">Awake</div>
        <div class="stat-value">{sleep_awake}</div>
        <div class="stat-unit">hours</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--green)"></div>
        <div class="stat-label">Avg SpO2</div>
        <div class="stat-value">{sleep_spo2}</div>
        <div class="stat-unit">%</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--blue)"></div>
        <div class="stat-label">Respiration</div>
        <div class="stat-value">{sleep_resp}</div>
        <div class="stat-unit">breaths/min</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--orange)"></div>
        <div class="stat-label">Sleep Stress</div>
        <div class="stat-value">{sleep_stress}</div>
        <div class="stat-unit">avg stress</div>
      </div>
    </div>
    <div class="chart-grid">
      <div class="chart-box">
        <div class="chart-label">Sleep stages breakdown (hours)</div>
        <canvas id="sleepStagesChart" style="max-height:200px"></canvas>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-header"><div class="section-title">Recovery Metrics</div></div>
    <div class="stat-grid">
      <div class="stat">
        <div class="stat-accent" style="background:{'var(--green)' if isinstance(readiness_score, int) and readiness_score >= 70 else 'var(--yellow)' if isinstance(readiness_score, int) and readiness_score >= 50 else 'var(--red)'}"></div>
        <div class="stat-label">Training Readiness</div>
        <div class="stat-value">{readiness_score}</div>
        <div class="stat-unit">{readiness_level}</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--purple)"></div>
        <div class="stat-label">Sleep Score</div>
        <div class="stat-value">{sleep_score}</div>
        <div class="stat-unit">/ 100</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--cyan)"></div>
        <div class="stat-label">HRV 7d Avg</div>
        <div class="stat-value">{hrv_avg}</div>
        <div class="stat-unit">ms</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--red)"></div>
        <div class="stat-label">Resting HR</div>
        <div class="stat-value">{resting_hr}</div>
        <div class="stat-unit">bpm</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:{'var(--green)' if isinstance(body_battery, int) and body_battery >= 60 else 'var(--yellow)' if isinstance(body_battery, int) and body_battery >= 30 else 'var(--orange)'}"></div>
        <div class="stat-label">Body Battery</div>
        <div class="stat-value">{body_battery}</div>
        <div class="stat-unit">/ 100</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--blue)"></div>
        <div class="stat-label">Recovery Time</div>
        <div class="stat-value sm">{readiness.get('recovery_time','--')}</div>
        <div class="stat-unit">min</div>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-header"><div class="section-title">HRV Overnight</div></div>
    <div class="stat-grid" style="margin-bottom:12px">
      <div class="stat">
        <div class="stat-accent" style="background:var(--cyan)"></div>
        <div class="stat-label">HRV 7d Average</div>
        <div class="stat-value">{hrv_avg}</div>
        <div class="stat-unit">ms</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--cyan)"></div>
        <div class="stat-label">Last Reading</div>
        <div class="stat-value">{hrv.get('values', [None])[-1] if hrv.get('values') else '--'}</div>
        <div class="stat-unit">ms · {hrv.get('times', ['--'])[-1] if hrv.get('times') else '--'}</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--cyan)"></div>
        <div class="stat-label">Peak Last Night</div>
        <div class="stat-value">{max(hrv.get('values', [0])) if hrv.get('values') else '--'}</div>
        <div class="stat-unit">ms</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--cyan)"></div>
        <div class="stat-label">Low Last Night</div>
        <div class="stat-value">{min(hrv.get('values', [0])) if hrv.get('values') else '--'}</div>
        <div class="stat-unit">ms</div>
      </div>
    </div>
    <div class="chart-grid">
      <div class="chart-box" style="grid-column: span 2">
        <div class="chart-label">HRV overnight with sleep stages (ms)</div>
        <canvas id="hrvDetailChart" style="max-height:220px"></canvas>
      </div>
    </div>
  </div>

</div>

<!-- ═══════════════════════════════════════════════════════ BLOOD PRESSURE -->
<div class="tab-panel" id="panel-bp">

  <div class="section">
    <div class="section-header"><div class="section-title">Latest Reading — {latest_bp.get('date','--')}</div></div>
    <div class="stat-grid">
      <div class="stat">
        <div class="stat-accent" style="background:{'var(--green)' if latest_bp.get('systolic',999)<120 else 'var(--yellow)' if latest_bp.get('systolic',999)<130 else 'var(--red)'}"></div>
        <div class="stat-label">Systolic</div>
        <div class="stat-value">{latest_bp.get('systolic','--')}</div>
        <div class="stat-unit">mmHg</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:{'var(--green)' if latest_bp.get('diastolic',999)<80 else 'var(--yellow)' if latest_bp.get('diastolic',999)<90 else 'var(--red)'}"></div>
        <div class="stat-label">Diastolic</div>
        <div class="stat-value">{latest_bp.get('diastolic','--')}</div>
        <div class="stat-unit">mmHg</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--blue)"></div>
        <div class="stat-label">Pulse</div>
        <div class="stat-value">{latest_bp.get('pulse','--')}</div>
        <div class="stat-unit">bpm</div>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-header"><div class="section-title">BP Trend</div></div>
    <div class="chart-grid">
      <div class="chart-box" style="grid-column:span 2">
        <div class="chart-label">Blood Pressure history (mmHg)</div>
        <canvas id="bpChart" style="max-height:220px"></canvas>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-header"><div class="section-title">BP Log</div></div>
    <div class="tbl-wrap">
      <table class="tbl">
        <thead><tr><th>Date/Time</th><th>Systolic</th><th>Diastolic</th><th>Pulse</th><th>Status</th></tr></thead>
        <tbody>{bp_table_rows}</tbody>
      </table>
    </div>
  </div>

</div>

<!-- ═══════════════════════════════════════════════════════ PERFORMANCE -->
<div class="tab-panel" id="panel-performance">

  <div class="section">
    <div class="section-header"><div class="section-title">Fitness Metrics</div></div>
    <div class="stat-grid">
      <div class="stat">
        <div class="stat-accent" style="background:var(--blue)"></div>
        <div class="stat-label">VO2 Max</div>
        <div class="stat-value">{vo2max}</div>
        <div class="stat-unit">ml/kg/min</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--purple)"></div>
        <div class="stat-label">Endurance Score</div>
        <div class="stat-value">{endurance.get('score','--')}</div>
        <div class="stat-unit">Well Trained 6500+</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:{tol_color}"></div>
        <div class="stat-label">Run Tolerance</div>
        <div class="stat-value" style="color:{tol_color}">{tol_pct if tol_pct else '--'}%</div>
        <div class="stat-unit">of weekly capacity</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:{im_color}"></div>
        <div class="stat-label">Intensity Mins</div>
        <div class="stat-value" style="color:{im_color}">{im_total}</div>
        <div class="stat-unit">/ {im_goal} min goal</div>
        <div class="gct-bar-wrap"><div class="gct-bar" style="width:{im_pct}%;background:{im_color}"></div></div>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-header"><div class="section-title">Race Predictions</div></div>
    <div class="stat-grid">
      <div class="stat">
        <div class="stat-accent" style="background:var(--green)"></div>
        <div class="stat-label">5K</div>
        <div class="stat-value sm">{race_preds.get('5k','--')}</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--green)"></div>
        <div class="stat-label">10K</div>
        <div class="stat-value sm">{race_preds.get('10k','--')}</div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--yellow)"></div>
        <div class="stat-label">Half Marathon</div>
        <div class="stat-value sm">{race_preds.get('half','--')}</div>
      </div>
      <div class="stat" style="grid-column:span 2">
        <div class="stat-accent" style="background:{'var(--green)' if marathon_secs and marathon_secs <= sub3_secs else 'var(--orange)'}"></div>
        <div class="stat-label">Marathon Prediction</div>
        <div class="stat-value sm" style="color:{'var(--green)' if marathon_secs and marathon_secs <= sub3_secs else 'var(--orange)'}">{race_preds.get('marathon','--')}</div>
        <div class="stat-unit">{marathon_gap if marathon_gap else ''}</div>
      </div>
    </div>
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px;margin-top:10px;">
      <div style="font-size:10px;color:var(--text3);letter-spacing:0.8px;text-transform:uppercase;margin-bottom:10px;">Progress to Sub-3:00</div>
      <div style="display:flex;align-items:center;gap:12px;">
        <div style="flex:1;height:8px;background:var(--surface2);border-radius:4px;overflow:hidden;">
          <div style="height:100%;border-radius:4px;background:{'var(--green)' if marathon_secs and marathon_secs<=sub3_secs else 'var(--orange)'};width:{min(100, round(sub3_secs/(marathon_secs or sub3_secs)*100))}%"></div>
        </div>
        <div style="font-size:12px;color:var(--text2);white-space:nowrap">PB 3:16 → Goal 3:00</div>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-header"><div class="section-title">Monthly Training Load</div></div>
    <div class="stat-grid">
      <div class="stat">
        <div class="stat-accent" style="background:var(--blue)"></div>
        <div class="stat-label">Aerobic Low</div>
        <div class="stat-value sm">{tl_low}</div>
        <div class="stat-unit">target {training_load.get('target_low_min','?')}–{training_load.get('target_low_max','?')}</div>
        <div class="gct-bar-wrap"><div class="gct-bar" style="width:{min(100,round(tl_low/(training_load.get('target_low_max',1) or 1)*100))}%;background:var(--blue)"></div></div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--orange)"></div>
        <div class="stat-label">Aerobic High</div>
        <div class="stat-value sm">{tl_high}</div>
        <div class="stat-unit">target {training_load.get('target_high_min','?')}–{training_load.get('target_high_max','?')}</div>
        <div class="gct-bar-wrap"><div class="gct-bar" style="width:{min(100,round(tl_high/(training_load.get('target_high_max',1) or 1)*100))}%;background:var(--orange)"></div></div>
      </div>
      <div class="stat">
        <div class="stat-accent" style="background:var(--red)"></div>
        <div class="stat-label">Anaerobic</div>
        <div class="stat-value sm">{tl_ana}</div>
        <div class="stat-unit">this month</div>
        <div class="gct-bar-wrap"><div class="gct-bar" style="width:{min(100,round(tl_ana/tl_total*100))}%;background:var(--red)"></div></div>
      </div>
    </div>
  </div>

</div>

<!-- ═══════════════════════════════════════════════════════ HR COMPARE -->
<div class="tab-panel" id="panel-hrcompare">
  <div class="section">
    <div class="section-header"><div class="section-title">Polar Verity Sense vs Garmin HR</div></div>
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;width:100%;min-height:600px;">
      <iframe src="hr_comparison.html" style="width:100%;height:700px;border:none;background:var(--bg);" title="HR Comparison"></iframe>
    </div>
    <div style="margin-top:10px;font-size:11px;color:var(--text3);line-height:1.6;">
      To update: export Polar FIT file → drop into <code style="background:var(--surface2);padding:2px 6px;border-radius:4px;">Desktop/polar_fits/</code> folder → watcher auto-generates and pushes.
    </div>
  </div>
</div>

<!-- EXPORT -->
<div style="margin-top:24px;padding-top:16px;border-top:1px solid var(--border);display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
  <button class="btn btn-outline" onclick="exportJSON()">⬇ Export JSON</button>
  <button class="btn btn-purple" onclick="openClaude()">✦ Analyse with Claude</button>
  <span style="margin-left:auto;font-size:10px;color:var(--text3);font-family:'JetBrains Mono',monospace;">
    GENERATED {datetime.now().strftime('%Y-%m-%d %H:%M')} · GARMIN + WITHINGS
  </span>
</div>

</div><!-- end .content -->

<script>
// ── TAB SWITCHING ──
function switchTab(name) {{
  document.querySelectorAll('.tab').forEach(t => {{
    const onclick = t.getAttribute('onclick') || '';
    const match = onclick.match(/switchTab\(['"](\w+)['"]\)/);
    t.classList.toggle('active', match && match[1] === name);
  }});
  document.querySelectorAll('.tab-panel').forEach(p => {{
    p.classList.toggle('active', p.id === 'panel-' + name);
  }});
}}

// ── CHART DEFAULTS ──
const C = {{
  blue: '#4f8ef7', purple: '#7c6cf7', green: '#3dd68c',
  yellow: '#f5c842', red: '#f26565', orange: '#f59e42',
  cyan: '#38d9f5', muted: '#5c6480', grid: 'rgba(37,40,54,0.8)'
}};
const base = {{
  responsive: true, maintainAspectRatio: true,
  plugins: {{ legend: {{ display: false }} }},
  scales: {{
    x: {{ ticks: {{ color: C.muted, font: {{ size:10 }} }}, grid: {{ color: C.grid }} }},
    y: {{ ticks: {{ color: C.muted, font: {{ size:10 }} }}, grid: {{ color: C.grid }} }},
  }}
}};

// ── CHARTS ──
new Chart(document.getElementById('weeklyComboChart'), {{
  type: 'bar',
  data: {{
    labels: {weekly_combined_labels},
    datasets: [
      {{ label: 'Run', data: {weekly_run_vals}, backgroundColor: 'rgba(79,142,247,0.7)', borderRadius: 3 }},
      {{ label: 'Cycle', data: {weekly_cycle_vals}, backgroundColor: 'rgba(124,108,247,0.7)', borderRadius: 3 }},
    ]
  }},
  options: {{ ...base, plugins: {{ legend: {{ display: true, labels: {{ color: C.muted, font: {{ size:10 }} }} }} }} }}
}});

new Chart(document.getElementById('hrvChart'), {{
  type: 'line',
  data: {{
    labels: {hrv_labels},
    datasets: [{{ data: {hrv_values}, borderColor: C.cyan, backgroundColor: 'rgba(56,217,245,0.08)',
      tension: 0.3, fill: true, pointRadius: 2 }}]
  }},
  options: {{ ...base }}
}});

// HRV + Sleep Stage Overlay
(function() {{
  const hrvTimes = {hrv_labels};
  const hrvVals  = {hrv_values};
  const deepData  = {deep_data};
  const lightData = {light_data};
  const remData   = {rem_data};
  const awakeData = {awake_data};

  // Max HRV for band height
  const maxHrv = Math.max(...hrvVals.filter(v => v)) * 1.1;

  // Convert stage data to filled bands at maxHrv height
  const toBand = (data) => data.map(v => v !== null ? maxHrv : null);

  new Chart(document.getElementById('hrvDetailChart'), {{
    type: 'line',
    data: {{
      labels: hrvTimes,
      datasets: [
        {{ label: 'Deep',  data: toBand(deepData),  backgroundColor: 'rgba(79,142,247,0.45)',  borderColor:'transparent', fill:true, pointRadius:0, tension:0, order:2, spanGaps:false }},
        {{ label: 'REM',   data: toBand(remData),   backgroundColor: 'rgba(124,108,247,0.45)', borderColor:'transparent', fill:true, pointRadius:0, tension:0, order:2, spanGaps:false }},
        {{ label: 'Light', data: toBand(lightData), backgroundColor: 'rgba(150,160,190,0.35)', borderColor:'transparent', fill:true, pointRadius:0, tension:0, order:2, spanGaps:false }},
        {{ label: 'Awake', data: toBand(awakeData), backgroundColor: 'rgba(245,200,66,0.4)',   borderColor:'transparent', fill:true, pointRadius:0, tension:0, order:2, spanGaps:false }},
        {{
          label: 'HRV (ms)',
          data: hrvVals,
          borderColor: '#38d9f5',
          backgroundColor: 'transparent',
          tension: 0.3,
          fill: false,
          pointRadius: 2,
          pointBackgroundColor: '#38d9f5',
          borderWidth: 2,
          order: 1,
        }}
      ]
    }},
    options: {{
      ...base,
      plugins: {{
        legend: {{ display: true, labels: {{ color: C.muted, font: {{size:10}} }} }}
      }},
      scales: {{
        x: {{ ...base.scales.x, ticks: {{ ...base.scales.x.ticks, maxTicksLimit: 8 }} }},
        y: {{ ...base.scales.y, title: {{ display: true, text: 'HRV (ms)', color: C.muted, font: {{size:10}} }} }},
      }}
    }}
  }});
}})();


new Chart(document.getElementById('lapBalanceChart'), {{
  type: 'bar',
  data: {{
    labels: {lap_labels},
    datasets: [
      {{ label: 'Left', data: {lap_balance}, backgroundColor: {lap_balance}.map(v => v>51.5?'rgba(242,101,101,0.7)':v>50.5?'rgba(245,200,66,0.7)':'rgba(61,214,140,0.7)'), borderRadius:3 }},
      {{ label: 'Right', data: {lap_balance_r}, backgroundColor: {lap_balance_r}.map(v => v>51.5?'rgba(242,101,101,0.4)':v>50.5?'rgba(245,200,66,0.4)':'rgba(61,214,140,0.4)'), borderRadius:3 }},
    ]
  }},
  options: {{ ...base,
    plugins: {{ legend: {{ display:true, labels: {{ color:C.muted, font:{{size:10}} }} }} }},
    scales: {{ ...base.scales, y: {{ ...base.scales.y, min:47, max:53, ticks: {{ ...base.scales.y.ticks, callback: v=>v+'%' }} }} }}
  }}
}});

new Chart(document.getElementById('lapHrChart'), {{
  type: 'line',
  data: {{ labels: {lap_labels}, datasets: [{{ data: {lap_hr}, borderColor: C.red, backgroundColor: 'rgba(242,101,101,0.08)', tension:0.3, fill:true, pointRadius:3, pointBackgroundColor:C.red }}] }},
  options: {{ ...base }}
}});

new Chart(document.getElementById('lapPaceChart'), {{
  type: 'line',
  data: {{ labels: {lap_labels}, datasets: [{{ data: {lap_pace}, borderColor: C.green, backgroundColor: 'rgba(61,214,140,0.08)', tension:0.3, fill:true, pointRadius:3, pointBackgroundColor:C.green }}] }},
  options: {{ ...base, scales: {{ ...base.scales, y: {{ ...base.scales.y, reverse:true,
    ticks: {{ ...base.scales.y.ticks, callback: v => {{ const m=Math.floor(v); const s=Math.round((v-m)*60); return m+':'+(s<10?'0':'')+s }} }} }} }} }}
}});

new Chart(document.getElementById('lapGctChart'), {{
  type: 'line',
  data: {{ labels: {lap_labels}, datasets: [{{ data: {lap_gct}, borderColor: C.cyan, backgroundColor: 'rgba(56,217,245,0.08)', tension:0.3, fill:true, pointRadius:3, pointBackgroundColor:C.cyan }}] }},
  options: {{ ...base }}
}});

// Mileage trend with 10% rule
new Chart(document.getElementById('mileageTrendChart'), {{
  type: 'bar',
  data: {{
    labels: {weekly_combined_labels},
    datasets: [
      {{ label: 'Run km', data: {weekly_run_vals}, backgroundColor: {weekly_run_vals}.map((v,i) => {{
        const prev = {weekly_run_vals}[i-1] || v;
        const chg = prev > 0 ? (v - prev) / prev * 100 : 0;
        return chg > 20 ? 'rgba(242,101,101,0.8)' : chg > 10 ? 'rgba(245,200,66,0.8)' : 'rgba(79,142,247,0.7)';
      }}), borderRadius: 4 }},
    ]
  }},
  options: {{ ...base, plugins: {{ legend: {{ display:false }} }} }}
}});

// Mileage week-on-week change
new Chart(document.getElementById('mileageChangeChart'), {{
  type: 'bar',
  data: {{
    labels: {weekly_combined_labels},
    datasets: [
      {{ label: 'WoW Change %', data: {mileage_change_vals},
        backgroundColor: {mileage_change_vals}.map(v => v > 20 ? 'rgba(242,101,101,0.8)' : v > 10 ? 'rgba(245,200,66,0.8)' : v < 0 ? 'rgba(92,100,128,0.5)' : 'rgba(61,214,140,0.7)'),
        borderRadius: 4
      }},
    ]
  }},
  options: {{ ...base,
    plugins: {{ legend: {{ display:false }},
      annotation: {{ annotations: {{
        safe: {{ type:'line', yMin:10, yMax:10, borderColor:'rgba(245,200,66,0.5)', borderDash:[4,4], label:{{ content:'10% limit', display:true, color:'rgba(245,200,66,0.7)', font:{{size:9}} }} }},
        danger: {{ type:'line', yMin:20, yMax:20, borderColor:'rgba(242,101,101,0.5)', borderDash:[4,4] }},
      }} }}
    }},
    scales: {{ ...base.scales, y: {{ ...base.scales.y, ticks: {{ ...base.scales.y.ticks, callback: v => v + '%' }} }} }}
  }}
}});

// HR Efficiency Trend
new Chart(document.getElementById('hrEffChart'), {{
  type: 'line',
  data: {{
    labels: {hr_eff_labels},
    datasets: [{{
      label: 'HR Efficiency (pace/HR)',
      data: {hr_eff_values},
      borderColor: C.purple,
      backgroundColor: 'rgba(124,108,247,0.1)',
      tension: 0.3, fill: true, pointRadius: 4,
      pointBackgroundColor: {hr_eff_values}.map(v => v ? C.purple : 'transparent'),
      spanGaps: true,
    }}]
  }},
  options: {{ ...base,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{ ...base.scales,
      y: {{ ...base.scales.y,
        ticks: {{ ...base.scales.y.ticks, callback: v => v.toFixed(2) }},
        title: {{ display: true, text: 'Higher = better', color: C.muted, font: {{size:9}} }}
      }}
    }}
  }}
}});

new Chart(document.getElementById('gctTrendChart'), {{
  type: 'line',
  data: {{
    labels: {gct_trend_labels},
    datasets: [
      {{ label: 'Left %', data: {gct_trend_values}, borderColor: C.yellow, tension:0.3, fill:false, pointRadius:4, pointBackgroundColor:C.yellow }},
      {{ label: 'Right %', data: {gct_trend_values_r}, borderColor: C.blue, tension:0.3, fill:false, pointRadius:4, pointBackgroundColor:C.blue }},
    ]
  }},
  options: {{ ...base,
    plugins: {{ legend: {{ display:true, labels: {{ color:C.muted, font:{{size:10}} }} }} }},
    scales: {{ ...base.scales, y: {{ ...base.scales.y, min:47, max:53, ticks: {{ ...base.scales.y.ticks, callback:v=>v+'%' }} }} }}
  }}
}});

new Chart(document.getElementById('bpChart'), {{
  type: 'line',
  data: {{
    labels: {bp_dates},
    datasets: [
      {{ label: 'Systolic',  data: {bp_systolic},  borderColor: C.red,    tension:0.3, fill:false, pointRadius:3 }},
      {{ label: 'Diastolic', data: {bp_diastolic}, borderColor: C.orange, tension:0.3, fill:false, pointRadius:3 }},
      {{ label: 'Pulse',     data: {bp_pulse},     borderColor: C.muted,  tension:0.3, fill:false, pointRadius:3, borderDash:[4,4] }},
    ]
  }},
  options: {{ ...base, plugins: {{ legend: {{ display:true, labels: {{ color:C.muted, font:{{size:10}} }} }} }} }}
}});

// CTL/ATL/TSB chart
new Chart(document.getElementById('ctlChart'), {{
  type: 'line',
  data: {{
    labels: {ctl_atl_labels},
    datasets: [
      {{ label: 'Fitness (CTL)', data: {ctl_values}, borderColor: C.blue,   backgroundColor:'rgba(79,142,247,0.08)', tension:0.3, fill:true,  pointRadius:0, borderWidth:2 }},
      {{ label: 'Fatigue (ATL)', data: {atl_values}, borderColor: C.red,    backgroundColor:'rgba(242,101,101,0.08)', tension:0.3, fill:true,  pointRadius:0, borderWidth:2 }},
      {{ label: 'Form (TSB)',    data: {tsb_values}, borderColor: C.green,  backgroundColor:'rgba(61,214,140,0.08)',  tension:0.3, fill:true,  pointRadius:0, borderWidth:1, borderDash:[4,4] }},
    ]
  }},
  options: {{ ...base, plugins: {{ legend: {{ display:true, labels: {{ color:C.muted, font:{{size:10}} }} }} }} }}
}});

// Sleep stages doughnut
new Chart(document.getElementById('sleepStagesChart'), {{
  type: 'doughnut',
  data: {{
    labels: ['Deep', 'Light', 'REM', 'Awake'],
    datasets: [{{ 
      data: {sleep_stages_data},
      backgroundColor: [C.blue, C.muted, C.cyan, C.yellow],
      borderWidth: 0,
      hoverOffset: 4,
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: true,
    plugins: {{ legend: {{ display:true, position:'right', labels: {{ color:C.muted, font:{{size:10}}, boxWidth:10 }} }} }},
  }}
}});

// PWA — Body Battery Push Notification
(function() {{
  const bodyBattery = {body_battery if isinstance(body_battery, int) else 'null'};
  const readinessScore = {readiness_score if isinstance(readiness_score, int) else 'null'};

  // Register service worker for PWA
  if ('serviceWorker' in navigator) {{
    navigator.serviceWorker.register('sw.js').catch(() => {{}});
  }}

  // Request notification permission and alert on low body battery
  if ('Notification' in window && bodyBattery !== null) {{
    if (Notification.permission === 'default') {{
      // Show install prompt
      const banner = document.createElement('div');
      banner.style.cssText = 'position:fixed;bottom:16px;left:16px;right:16px;background:#13151c;border:1px solid #4f8ef7;border-radius:10px;padding:12px 16px;display:flex;align-items:center;gap:12px;z-index:1000;font-size:12px;';
      banner.innerHTML = '<span style="color:#f0f2f8">Enable notifications for body battery alerts?</span><button onclick="requestNotificationPermission()" style="background:#4f8ef7;color:#fff;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:11px;white-space:nowrap">Enable</button><button onclick="this.parentElement.remove()" style="background:transparent;color:#5c6480;border:none;padding:6px;cursor:pointer">✕</button>';
      document.body.appendChild(banner);
      setTimeout(() => banner.remove(), 10000);
    }} else if (Notification.permission === 'granted') {{
      checkAlerts(bodyBattery, readinessScore);
    }}
  }}
}})();

function requestNotificationPermission() {{
  Notification.requestPermission().then(p => {{
    if (p === 'granted') {{
      new Notification('Health Dashboard', {{ body: 'Notifications enabled! You will be alerted for low body battery.' }});
      document.querySelector('[onclick="requestNotificationPermission()"]')?.closest('div')?.remove();
    }}
  }});
}}

function checkAlerts(battery, readiness) {{
  const alerts = [];
  if (battery !== null && battery < 25) alerts.push(`Body Battery critical: ${{battery}}%`);
  else if (battery !== null && battery < 40) alerts.push(`Body Battery low: ${{battery}}%`);
  if (readiness !== null && readiness < 40) alerts.push(`Low readiness: ${{readiness}}/100 — consider rest today`);

  if (alerts.length > 0) {{
    const lastAlert = localStorage.getItem('lastHealthAlert');
    const today = new Date().toDateString();
    if (lastAlert !== today) {{
      new Notification('Health Alert', {{ body: alerts.join(' | '), icon: '' }});
      localStorage.setItem('lastHealthAlert', today);
    }}
  }}
}}

// ── DAILY CHECK-IN ──
const CHECKIN_KEY = 'health_dashboard_checkins';
const SYNC_SECRET_KEY = 'health_dashboard_sync_secret';
const SYNC_WORKER_URL = 'https://checkin-sync.yoursubdomain.workers.dev'; // <-- paste your Cloudflare Worker URL here, see cloudflare-worker/SETUP.md

function getSyncSecret() {{
  let secret = localStorage.getItem(SYNC_SECRET_KEY);
  if (!secret) {{
    secret = prompt('One-time setup: paste your sync secret (see cloudflare-worker/SETUP.md). Leave blank to skip auto-sync and use manual export instead.');
    if (secret) localStorage.setItem(SYNC_SECRET_KEY, secret);
  }}
  return secret;
}}

async function syncCheckinToServer(entry) {{
  const status = document.getElementById('checkin-status');
  if (!SYNC_WORKER_URL) {{
    if (status) status.textContent = 'Saved locally — auto-sync not configured yet, use Export to sync.';
    return;
  }}
  const secret = getSyncSecret();
  if (!secret) {{
    if (status) status.textContent = 'Saved locally — auto-sync skipped, use Export to sync.';
    return;
  }}
  if (status) status.textContent = 'Saving for ' + entry.date + '\\u2026 syncing\\u2026';
  try {{
    const resp = await fetch(SYNC_WORKER_URL, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ ...entry, secret }}),
    }});
    if (resp.ok) {{
      if (status) status.textContent = 'Synced \\u2713 ' + entry.date + ' saved to server.';
    }} else {{
      const err = await resp.json().catch(() => ({{}}));
      if (status) status.textContent = 'Saved locally, sync failed (' + (err.error || resp.status) + ') — try again or use Export.';
    }}
  }} catch (e) {{
    if (status) status.textContent = 'Saved locally, sync failed (network) — try again or use Export.';
  }}
}}
const checkInServerHistory = {checkin_history_json};
const checkInToday = '{today_iso}';

function painClass(v) {{
  if (v >= 5) return 'pain-high';
  if (v >= 3) return 'pain-med';
  return 'pain-low';
}}

function getLocalCheckins() {{
  try {{
    return JSON.parse(localStorage.getItem(CHECKIN_KEY) || '[]');
  }} catch (e) {{
    return [];
  }}
}}

function getMergedCheckins() {{
  const byDate = {{}};
  checkInServerHistory.forEach(c => {{ if (c.date) byDate[c.date] = c; }});
  getLocalCheckins().forEach(c => {{ if (c.date) byDate[c.date] = c; }});
  return Object.values(byDate).sort((a, b) => a.date.localeCompare(b.date));
}}

function updateCheckinPreview() {{
  ['stiffness', 'first-steps', 'post-run'].forEach(id => {{
    const el = document.getElementById(id);
    const val = document.getElementById(id + '-val');
    if (el && val) val.textContent = el.value;
  }});
}}

function loadTodayCheckin() {{
  const merged = getMergedCheckins();
  const today = merged.find(c => c.date === checkInToday);
  if (!today) return;
  document.getElementById('stiffness').value = today.stiffness ?? 0;
  document.getElementById('first-steps').value = today.first_steps_pain ?? 0;
  document.getElementById('post-run').value = today.post_run_pain ?? 0;
  document.getElementById('calf-raises').checked = !!today.calf_raises;
  updateCheckinPreview();
  const status = document.getElementById('checkin-status');
  if (status) status.textContent = 'Loaded today\\'s entry (' + (today.source || 'saved') + ')';
}}

function renderCheckinTrend() {{
  const el = document.getElementById('checkin-trend');
  if (!el) return;
  const rows = getMergedCheckins().slice(-7);
  if (!rows.length) {{
    el.textContent = 'No check-ins yet — log your first one above.';
    return;
  }}
  let html = '<table><tr><td>Date</td><td>Stiff</td><td>Steps</td><td>Post</td><td>Raises</td></tr>';
  rows.forEach(c => {{
    const s = c.stiffness ?? 0;
    const f = c.first_steps_pain ?? 0;
    const p = c.post_run_pain ?? 0;
    html += `<tr>
      <td>${{c.date === checkInToday ? 'Today' : c.date.slice(5)}}</td>
      <td class="${{painClass(s)}}">${{s}}</td>
      <td class="${{painClass(f)}}">${{f}}</td>
      <td class="${{painClass(p)}}">${{p}}</td>
      <td>${{c.calf_raises ? '✓' : '—'}}</td>
    </tr>`;
  }});
  html += '</table>';
  el.innerHTML = html;
}}

function saveCheckin() {{
  const entry = {{
    date: checkInToday,
    stiffness: parseInt(document.getElementById('stiffness').value, 10),
    first_steps_pain: parseInt(document.getElementById('first-steps').value, 10),
    post_run_pain: parseInt(document.getElementById('post-run').value, 10),
    calf_raises: document.getElementById('calf-raises').checked,
    source: 'browser',
    saved_at: new Date().toISOString(),
  }};
  const local = getLocalCheckins().filter(c => c.date !== checkInToday);
  local.push(entry);
  localStorage.setItem(CHECKIN_KEY, JSON.stringify(local));
  renderCheckinTrend();
  syncCheckinToServer(entry);
}}

function exportCheckins() {{
  const json = JSON.stringify(getMergedCheckins(), null, 2);
  const dataUri = 'data:application/json;charset=utf-8,' + encodeURIComponent(json);
  const link = document.createElement('a');
  link.href = dataUri;
  link.download = 'checkins.json';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}}

loadTodayCheckin();
renderCheckinTrend();

// ── EXPORT ──
const dashboardData = {export_data};

function exportJSON() {{
  const json = JSON.stringify(dashboardData, null, 2);
  const filename = `health_${{dashboardData.generated.replace(/[: ]/g,'-')}}.json`;

  // Use data URI — works on iOS Safari and all mobile browsers
  const dataUri = 'data:application/json;charset=utf-8,' + encodeURIComponent(json);
  const a = document.createElement('a');
  a.href = dataUri;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);

  // iOS Safari fallback — open in new tab if download didn't trigger
  setTimeout(() => {{
    if (navigator.userAgent.match(/iP(hone|ad)/i)) {{
      window.open(dataUri, '_blank');
    }}
  }}, 500);
}}

function openClaude() {{
  const d = dashboardData;
  const r = d.readiness; const lr = d.last_run;
  const t = d.training; const achilles = d.achilles;
  const bp = d.blood_pressure[0] || {{}};
  const ci = getMergedCheckins().slice(-1)[0] || d.latest_checkin || null;
  const checkinLine = ci
    ? `MORNING CHECK-IN (${{ci.date}}): Stiffness ${{ci.stiffness}}/10 | First-steps pain ${{ci.first_steps_pain}}/10 | Post-run pain ${{ci.post_run_pain}}/10 | Calf raises: ${{ci.calf_raises ? 'yes' : 'no'}}`
    : 'MORNING CHECK-IN: not logged today';
  const prompt = `Analyse my health data for ${{d.generated}} and give specific, actionable insights.

TRAINING: Week ${{t.week}}/28 — ${{t.phase}} | Target ${{t.phase_target_km}} km/wk | This week: ${{t.this_week_km}} km | Days to race: ${{t.days_to_race}}
ATHLETE: Melbourne Marathon 12 Oct 2026, sub-3:00 goal (PB 3:16). Left insertional Achilles tendinopathy.
READINESS: ${{r.score}}/100 (${{r.level}}) | Sleep: ${{r.sleep_score}} | HRV: ${{r.hrv_weekly_avg}}ms | RHR: ${{r.resting_hr}} | Battery: ${{r.body_battery}}
ACHILLES: ${{achilles.score}}/100 (${{achilles.level}}) | ${{achilles.factors.map(f=>f.label+': '+f.value).join(', ')}}
${{checkinLine}}
LAST RUN (${{lr.date}}): ${{lr.distance_km}}km @ ${{lr.avg_pace_min_km ? (Math.floor(lr.avg_pace_min_km)+':'+(Math.round((lr.avg_pace_min_km%1)*60)+'').padStart(2,'0')) : '--'}} | HR ${{lr.avg_hr}} | GCT L${{lr.gct_balance_left_pct}}%/R${{lr.gct_balance_right_pct}}%
BP: ${{bp.systolic}}/${{bp.diastolic}} mmHg pulse ${{bp.pulse}}

Give: 1) Recovery/readiness assessment 2) Achilles risk based on GCT trend 3) Today's training recommendation 4) Any concerns`;
  // Use location.href for mobile compatibility (window.open blocked on some mobile browsers)
  const url = "https://claude.ai/new?q=" + encodeURIComponent(prompt);
  const link = document.createElement('a');
  link.href = url;
  link.target = '_blank';
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}}
</script>

</body>
</html>"""
    return html

# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    print("Connecting to Garmin...")
    garmin = get_garmin()
    print("Fetching Garmin data (this may take ~30s for lap splits)...")
    garmin_data = fetch_garmin_data(garmin)
    print(f"  Runs fetched: {len(garmin_data['runs'])}")

    print("Fetching Withings BP data...")
    bp_readings = fetch_withings_bp()
    print(f"  BP readings fetched: {len(bp_readings)}")

    print("Computing training phase...")
    phase_info = get_training_phase()
    print(f"  Week {phase_info['week_num']} — {phase_info['phase']['name']}")

    print("Computing training load engine...")
    load_data = compute_weekly_loads(garmin_data["runs"])
    print(f"  Acute: {load_data['acute']} | Chronic: {load_data['chronic']} | ACR: {load_data['acr']} ({load_data['acr_status']})")

    print("Computing tissue capacity score...")
    tissue_capacity = compute_tissue_capacity(garmin_data["runs"])
    print(f"  Tissue capacity: {tissue_capacity['score']}/100 ({tissue_capacity['level']}) — {tissue_capacity['limiting_factor']}")

    print("Computing training monotony...")
    monotony = compute_training_monotony(garmin_data["runs"])
    print(f"  Monotony: {monotony['score']}/100 ({monotony['level']})")
    print(f"  Intensity: {load_data['easy_pct']}% easy / {load_data['mod_pct']}% moderate / {load_data['hard_pct']}% hard")
    if load_data['btb_risk']:
        print("  ⚠ Back-to-back stress detected")

    print("Computing Achilles load score...")
    checkins = load_checkins()
    latest_checkin = get_latest_checkin(checkins)
    if latest_checkin:
        print(f"  Check-in: {latest_checkin['date']} — stiffness {latest_checkin.get('stiffness', '?')}/10")
    achilles = compute_achilles_score(garmin_data["runs"], phase_info)
    achilles = apply_checkin_factors(achilles, latest_checkin)
    print(f"  Load score: {achilles.get('score')}/100 ({achilles.get('level')} risk)")

    print("Computing recovery score...")
    recovery = compute_recovery_score(
        garmin_data.get("readiness", {}),
        garmin_data.get("hrv", {}),
        garmin_data.get("sleep", {}),
        garmin_data.get("resting_hr", 0),
    )
    print(f"  Recovery: {recovery['score']}/100 ({recovery['level']})")

    print("Running injury detective...")
    injury_contributors = injury_detective(garmin_data["runs"], achilles.get("score", 0))
    if injury_contributors:
        print(f"  Top contributor: {injury_contributors[0]['date']} — {injury_contributors[0]['risk_pct']}% risk")

    print("Fetching intervals.icu data...")
    intervals = fetch_intervals()

    print("Fetching weather...")
    weather = fetch_weather()
    if weather:
        print(f"  Tomorrow: {weather['days'][1]['max_temp']}°C, {weather['days'][1]['rain_pct']}% rain — best window: {weather['best_window']}")

    print("Generating why-today explanation...")
    why_today = generate_why_today(
        garmin_data.get("readiness", {}), achilles, tissue_capacity,
        load_data, recovery, monotony, weather, latest_checkin
    )
    print(f"  Decision: {why_today['decision'][:60]}...")

    print("Checking alerts...")
    check_and_send_alerts(garmin_data.get("readiness", {}), achilles, bp_readings)

    print("Generating daily email report...")
    generate_daily_report(garmin_data, bp_readings, phase_info, achilles, weather, intervals)

    print("Saving score history...")
    save_score_history(garmin_data, achilles, recovery, tissue_capacity, monotony, load_data, checkin=latest_checkin)

    print("Generating dashboard...")
    html = generate_html(garmin_data, bp_readings, phase_info, achilles, "", weather, intervals,
                         load_data=load_data, recovery=recovery, injury_contributors=injury_contributors,
                         tissue_capacity=tissue_capacity, monotony=monotony, why_today=why_today,
                         checkins=checkins)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nDone! Open: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
