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
from datetime import datetime, timedelta, date
from dotenv import load_dotenv
from garminconnect import Garmin

load_dotenv()

GARMIN_TOKEN_FILE  = os.path.expanduser("~/.garminconnect/garmin_tokens.json")
WITHINGS_TOKEN_FILE = os.path.expanduser("~/.withings/withings_tokens.json")
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "health_dashboard.html")

TODAY     = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()

# ─── GARMIN ──────────────────────────────────────────────────────────────────

def get_garmin():
    token_data = open(GARMIN_TOKEN_FILE).read()
    garmin = Garmin(email="dummy@dummy.com", password="dummy")
    garmin.login(tokenstore=token_data)
    return garmin

def fetch_garmin_data(garmin):
    data = {}

    # Recent activities
    activities = garmin.get_activities(0, 10)
    runs = [a for a in activities if a.get("activityType", {}).get("typeKey") == "running"]
    data["runs"] = []
    for r in runs[:8]:
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
        data["hrv"] = {
            "values": [r["hrvValue"] for r in readings[-24:]],
            "times": [r["readingTimeLocal"][11:16] for r in readings[-24:]],
            "weekly_avg": data.get("readiness", {}).get("hrv_weekly_avg"),
        }
    except:
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
        from zoneinfo import ZoneInfo
        melb = ZoneInfo("Australia/Melbourne")
        entry = {"date": datetime.fromtimestamp(grp["date"], tz=melb).strftime("%Y-%m-%d %H:%M")}
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

# ─── TRAINING PHASE ──────────────────────────────────────────────────────────

PLAN_START = date(2026, 5, 1)
RACE_DATE  = date(2026, 10, 12)

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

# ─── ACHILLES LOAD SCORE ─────────────────────────────────────────────────────

def compute_achilles_score(runs, phase_info):
    """
    Score 0-100 (higher = more risk).
    Factors:
      - GCT imbalance (left bias > 50.5% = yellow, > 51.5% = red)
      - Weekly mileage vs phase target
      - Week-on-week mileage change > 10% rule
      - Readiness score (low readiness + high load = risk)
    """
    if not runs:
        return {"score": None, "factors": []}

    phase = phase_info["phase"]
    km_target_mid = (phase["km_min"] + phase["km_max"]) / 2

    # Weekly mileage last 2 weeks
    from collections import defaultdict
    weekly = defaultdict(float)
    for r in runs:
        d = datetime.strptime(r["date"], "%Y-%m-%d")
        wk = d.strftime("%Y-W%W")
        weekly[wk] += r["distance"]
    weeks_sorted = sorted(weekly.keys())
    this_week_km = weekly[weeks_sorted[-1]] if weeks_sorted else 0
    last_week_km = weekly[weeks_sorted[-2]] if len(weeks_sorted) >= 2 else this_week_km

    # GCT balance — last run avg
    last_run = runs[0]
    avg_gct_l = round(sum(l["gct_balance"] for l in last_run["laps"]) / len(last_run["laps"]), 1) if last_run["laps"] else 50.0

    # Score components
    factors = []
    score = 0

    # 1. GCT imbalance
    gct_diff = abs(avg_gct_l - 50)
    if gct_diff >= 2:
        score += 35
        factors.append({"label": "GCT imbalance", "value": f"{avg_gct_l}% left", "level": "high"})
    elif gct_diff >= 1:
        score += 15
        factors.append({"label": "GCT imbalance", "value": f"{avg_gct_l}% left", "level": "medium"})
    else:
        factors.append({"label": "GCT imbalance", "value": f"{avg_gct_l}% left", "level": "low"})

    # 2. Weekly mileage vs target
    if km_target_mid > 0:
        load_ratio = this_week_km / km_target_mid
        if load_ratio > 1.2:
            score += 25
            factors.append({"label": "Weekly load", "value": f"{this_week_km:.0f} km (>{phase['km_max']} target)", "level": "high"})
        elif load_ratio > 1.0:
            score += 10
            factors.append({"label": "Weekly load", "value": f"{this_week_km:.0f} km (on target)", "level": "medium"})
        else:
            factors.append({"label": "Weekly load", "value": f"{this_week_km:.0f} km (within target)", "level": "low"})

    # 3. Week-on-week change > 10%
    if last_week_km > 0:
        wow_change = (this_week_km - last_week_km) / last_week_km * 100
        if wow_change > 20:
            score += 25
            factors.append({"label": "Mileage spike", "value": f"+{wow_change:.0f}% vs last week", "level": "high"})
        elif wow_change > 10:
            score += 10
            factors.append({"label": "Mileage spike", "value": f"+{wow_change:.0f}% vs last week", "level": "medium"})
        else:
            factors.append({"label": "Mileage change", "value": f"{wow_change:+.0f}% vs last week", "level": "low"})

    # 4. Consecutive run days
    run_dates = sorted(set(r["date"] for r in runs[:7]))
    max_consec = 1
    consec = 1
    for i in range(1, len(run_dates)):
        d1 = datetime.strptime(run_dates[i-1], "%Y-%m-%d")
        d2 = datetime.strptime(run_dates[i], "%Y-%m-%d")
        if (d2 - d1).days == 1:
            consec += 1
            max_consec = max(max_consec, consec)
        else:
            consec = 1
    if max_consec >= 3:
        score += 15
        factors.append({"label": "Consecutive days", "value": f"{max_consec} days in a row", "level": "high"})
    else:
        factors.append({"label": "Consecutive days", "value": f"Max {max_consec} in a row", "level": "low"})

    score = min(score, 100)
    level = "high" if score >= 60 else "medium" if score >= 30 else "low"
    return {"score": score, "level": level, "factors": factors, "this_week_km": round(this_week_km, 1), "last_week_km": round(last_week_km, 1)}

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

def send_alert_email(subject, body):
    """Send alert via Gmail SMTP. Set GMAIL_APP_PASSWORD in .env"""
    import smtplib
    from email.mime.text import MIMEText
    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not gmail_user or not gmail_pass:
        print("  Email alerts not configured (set GMAIL_USER + GMAIL_APP_PASSWORD in .env)")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = gmail_user
        msg["To"]      = gmail_user
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(gmail_user, gmail_pass)
            s.send_message(msg)
        print(f"  Alert sent: {subject}")
    except Exception as e:
        print(f"  Alert failed: {e}")

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
        send_alert_email(f"Health Alert — {date.today()}", body)

def generate_html(garmin_data, bp_readings, phase_info=None, achilles=None, ai_commentary=""):
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
    }, default=str)

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

    # Pre-compute cycle display values
    last_cycle_duration = f"{last_cycle['duration']:.0f}" if last_cycle else '--'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
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
  <div class="tab" onclick="switchTab('bp')"><span class="tab-icon">↕</span>Blood Pressure</div>
</div>

<div class="content">

<!-- ═══════════════════════════════════════════════════════ OVERVIEW -->
<div class="tab-panel active" id="panel-overview">

  <div class="section">
    <div class="section-header"><div class="section-title">Today's Status</div></div>
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
    <div class="chart-grid">
      <div class="chart-box" style="grid-column: span 2">
        <div class="chart-label">HRV readings last night (ms)</div>
        <canvas id="hrvDetailChart" style="max-height:200px"></canvas>
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
  document.querySelectorAll('.tab').forEach((t,i) => {{
    const panels = ['overview','running','cycling','health','bp'];
    t.classList.toggle('active', panels[i] === name);
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

new Chart(document.getElementById('hrvDetailChart'), {{
  type: 'line',
  data: {{
    labels: {hrv_labels},
    datasets: [{{ data: {hrv_values}, borderColor: C.cyan, backgroundColor: 'rgba(56,217,245,0.08)',
      tension: 0.3, fill: true, pointRadius: 3, pointBackgroundColor: C.cyan }}]
  }},
  options: {{ ...base }}
}});

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

// ── EXPORT ──
const dashboardData = {export_data};

function exportJSON() {{
  const blob = new Blob([JSON.stringify(dashboardData, null, 2)], {{type:'application/json'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `health_${{dashboardData.generated.replace(/[: ]/g,'-')}}.json`;
  a.click();
}}

function openClaude() {{
  const d = dashboardData;
  const r = d.readiness; const lr = d.last_run;
  const t = d.training; const a = d.achilles;
  const bp = d.blood_pressure[0] || {{}};
  const prompt = `Analyse my health data for ${{d.generated}} and give specific, actionable insights.

TRAINING: Week ${{t.week}}/28 — ${{t.phase}} | Target ${{t.phase_target_km}} km/wk | This week: ${{t.this_week_km}} km | Days to race: ${{t.days_to_race}}
ATHLETE: Melbourne Marathon 12 Oct 2026, sub-3:00 goal (PB 3:16). Left insertional Achilles tendinopathy.
READINESS: ${{r.score}}/100 (${{r.level}}) | Sleep: ${{r.sleep_score}} | HRV: ${{r.hrv_weekly_avg}}ms | RHR: ${{r.resting_hr}} | Battery: ${{r.body_battery}}
ACHILLES: ${{a.score}}/100 (${{a.level}}) | ${{a.factors.map(f=>f.label+': '+f.value).join(', ')}}
LAST RUN (${{lr.date}}): ${{lr.distance_km}}km @ ${{lr.avg_pace_min_km ? (Math.floor(lr.avg_pace_min_km)+':'+(Math.round((lr.avg_pace_min_km%1)*60)+'').padStart(2,'0')) : '--'}} | HR ${{lr.avg_hr}} | GCT L${{lr.gct_balance_left_pct}}%/R${{lr.gct_balance_right_pct}}%
BP: ${{bp.systolic}}/${{bp.diastolic}} mmHg pulse ${{bp.pulse}}

Give: 1) Recovery/readiness assessment 2) Achilles risk based on GCT trend 3) Today's training recommendation 4) Any concerns`;
  window.open("https://claude.ai/new?q=" + encodeURIComponent(prompt), '_blank');
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

    print("Computing Achilles load score...")
    achilles = compute_achilles_score(garmin_data["runs"], phase_info)
    print(f"  Load score: {achilles.get('score')}/100 ({achilles.get('level')} risk)")

    print("Checking alerts...")
    check_and_send_alerts(garmin_data.get("readiness", {}), achilles, bp_readings)

    print("Generating dashboard...")
    html = generate_html(garmin_data, bp_readings, phase_info, achilles, "")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nDone! Open: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
