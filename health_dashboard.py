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


    # Achilles factor rows
    achilles_rows = ""
    for f in achilles_factors:
        col = "#f87171" if f["level"] == "high" else "#facc15" if f["level"] == "medium" else "#4ade80"
        achilles_rows += f'<tr><td>{f["label"]}</td><td style="color:{col}">{f["value"]}</td></tr>\n'

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

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Health Dashboard — {TODAY}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #0a0c10;
    --surface: #111318;
    --surface2: #181c24;
    --border: #1e2330;
    --text: #e2e8f0;
    --muted: #64748b;
    --accent: #38bdf8;
    --accent2: #818cf8;
    --green: #4ade80;
    --yellow: #facc15;
    --red: #f87171;
    --orange: #fb923c;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'DM Mono', monospace;
    font-size: 13px;
    min-height: 100vh;
    padding: 24px;
  }}
  /* noise overlay */
  body::before {{
    content: '';
    position: fixed; inset: 0;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)' opacity='0.04'/%3E%3C/svg%3E");
    pointer-events: none; z-index: 0;
  }}

  .header {{
    display: flex; align-items: baseline; gap: 16px;
    margin-bottom: 28px; padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
  }}
  .header h1 {{
    font-family: 'Syne', sans-serif;
    font-size: 22px; font-weight: 800;
    letter-spacing: -0.5px;
    color: var(--accent);
  }}
  .header .date {{
    color: var(--muted); font-size: 12px;
  }}
  .header .tagline {{
    margin-left: auto; color: var(--muted);
    font-size: 11px; letter-spacing: 1px;
    text-transform: uppercase;
  }}

  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 12px;
    margin-bottom: 20px;
  }}
  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    position: relative;
    overflow: hidden;
  }}
  .card::before {{
    content: '';
    position: absolute; top: 0; left: 0; right: 0;
    height: 2px;
    background: linear-gradient(90deg, var(--accent), transparent);
  }}
  .card.accent2::before {{ background: linear-gradient(90deg, var(--accent2), transparent); }}
  .card.green::before  {{ background: linear-gradient(90deg, var(--green), transparent); }}
  .card.yellow::before {{ background: linear-gradient(90deg, var(--yellow), transparent); }}
  .card.orange::before {{ background: linear-gradient(90deg, var(--orange), transparent); }}
  .card.red::before    {{ background: linear-gradient(90deg, var(--red), transparent); }}

  .card-label {{
    font-size: 10px; letter-spacing: 1.5px;
    text-transform: uppercase; color: var(--muted);
    margin-bottom: 8px;
  }}
  .card-value {{
    font-family: 'Syne', sans-serif;
    font-size: 32px; font-weight: 700;
    line-height: 1; color: var(--text);
  }}
  .card-unit {{
    font-size: 11px; color: var(--muted);
    margin-top: 4px;
  }}
  .card-sub {{
    font-size: 11px; color: var(--muted);
    margin-top: 6px;
  }}

  .section {{
    margin-bottom: 20px;
  }}
  .section-title {{
    font-family: 'Syne', sans-serif;
    font-size: 12px; font-weight: 700;
    letter-spacing: 2px; text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 12px;
    display: flex; align-items: center; gap: 8px;
  }}
  .section-title::after {{
    content: ''; flex: 1;
    height: 1px; background: var(--border);
  }}

  .chart-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    gap: 12px;
  }}
  .chart-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
  }}
  .chart-title {{
    font-size: 11px; letter-spacing: 1px;
    text-transform: uppercase; color: var(--muted);
    margin-bottom: 12px;
  }}
  canvas {{ max-height: 200px; }}

  .run-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }}
  .run-table th {{
    text-align: left; padding: 6px 10px;
    font-size: 10px; letter-spacing: 1px;
    text-transform: uppercase; color: var(--muted);
    border-bottom: 1px solid var(--border);
  }}
  .run-table td {{
    padding: 8px 10px;
    border-bottom: 1px solid var(--border);
  }}
  .run-table tr:last-child td {{ border-bottom: none; }}
  .run-table tr:hover td {{ background: var(--surface2); }}

  .bp-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }}
  .bp-table th {{
    text-align: left; padding: 6px 10px;
    font-size: 10px; letter-spacing: 1px;
    text-transform: uppercase; color: var(--muted);
    border-bottom: 1px solid var(--border);
  }}
  .bp-table td {{
    padding: 7px 10px;
    border-bottom: 1px solid var(--border);
  }}
  .bp-table tr:last-child td {{ border-bottom: none; }}

  .badge {{
    display: inline-block;
    padding: 2px 8px; border-radius: 4px;
    font-size: 10px; letter-spacing: 0.5px;
    text-transform: uppercase; font-weight: 600;
  }}
  .badge-green  {{ background: rgba(74,222,128,0.15); color: var(--green); }}
  .badge-yellow {{ background: rgba(250,204,21,0.15); color: var(--yellow); }}
  .badge-red    {{ background: rgba(248,113,113,0.15); color: var(--red); }}

  .gct-bar-wrap {{
    margin-top: 4px;
    height: 6px; border-radius: 3px;
    background: var(--border); overflow: hidden;
    position: relative;
  }}
  .gct-bar {{
    height: 100%; border-radius: 3px;
    transition: width 0.3s;
  }}
  .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}

</style>
</head>
<body>

<div class="header">
  <h1>HEALTH DASHBOARD</h1>
  <span class="date">{TODAY}</span>
  <span class="tagline">Garmin + Withings</span>
</div>

<!-- READINESS ROW -->
<div class="section">
  <div class="section-title">Today's Readiness</div>
  <div class="grid">
    <div class="card {'green' if isinstance(readiness_score, int) and readiness_score >= 70 else 'yellow' if isinstance(readiness_score, int) and readiness_score >= 50 else 'red'}">
      <div class="card-label">Training Readiness</div>
      <div class="card-value">{readiness_score}</div>
      <div class="card-unit">{readiness_level}</div>
      <div class="card-sub">{readiness.get('feedback', '')}</div>
    </div>
    <div class="card accent2">
      <div class="card-label">Sleep Score</div>
      <div class="card-value">{sleep_score}</div>
      <div class="card-unit">/ 100</div>
    </div>
    <div class="card">
      <div class="card-label">HRV Avg (7d)</div>
      <div class="card-value">{hrv_avg}</div>
      <div class="card-unit">ms</div>
    </div>
    <div class="card">
      <div class="card-label">Resting HR</div>
      <div class="card-value">{resting_hr}</div>
      <div class="card-unit">bpm</div>
    </div>
    <div class="card {'green' if isinstance(body_battery, int) and body_battery >= 60 else 'yellow' if isinstance(body_battery, int) and body_battery >= 30 else 'orange'}">
      <div class="card-label">Body Battery</div>
      <div class="card-value">{body_battery}</div>
      <div class="card-unit">/ 100</div>
    </div>
    <div class="card">
      <div class="card-label">Recovery Time</div>
      <div class="card-value">{readiness.get('recovery_time', '--')}</div>
      <div class="card-unit">minutes</div>
    </div>
  </div>
</div>

<!-- TRAINING PHASE -->
<div class="section">
  <div class="section-title">Training Plan — Week {week_num} of 28</div>
  <div class="grid" style="grid-template-columns: repeat(auto-fill, minmax(140px,1fr))">
    <div class="card accent2">
      <div class="card-label">Current Week</div>
      <div class="card-value">{week_num}</div>
      <div class="card-unit">of 28 total</div>
    </div>
    <div class="card accent2">
      <div class="card-label">Phase</div>
      <div class="card-value" style="font-size:16px; padding-top:4px">{phase["name"]}</div>
      <div class="card-unit">Week {week_in_phase} of {phase_total}</div>
    </div>
    <div class="card">
      <div class="card-label">Phase Target</div>
      <div class="card-value" style="font-size:20px">{phase["km_min"]}–{phase["km_max"]}</div>
      <div class="card-unit">km / week</div>
    </div>
    <div class="card">
      <div class="card-label">This Week</div>
      <div class="card-value" style="font-size:26px">{achilles.get("this_week_km", "--")}</div>
      <div class="card-unit">km so far</div>
    </div>
    <div class="card">
      <div class="card-label">Days to Race</div>
      <div class="card-value">{days_to_race}</div>
      <div class="card-unit">12 Oct 2026</div>
    </div>
    <div class="card">
      <div class="card-label">Phase Focus</div>
      <div class="card-value" style="font-size:12px; padding-top:6px; line-height:1.4">{phase["focus"]}</div>
    </div>
  </div>
  <!-- Phase progress bar -->
  <div class="chart-card" style="margin-top:12px; padding: 14px 16px;">
    <div class="chart-title" style="margin-bottom:10px">28-week plan progress</div>
    <div style="position:relative; height:24px; background:var(--surface2); border-radius:4px; overflow:hidden;">
      <div style="position:absolute; left:0; top:0; bottom:0; width:{round((week_num-1)/28*100)}%; background:linear-gradient(90deg,#818cf8,#38bdf8); border-radius:4px; opacity:0.7;"></div>
      <div style="position:absolute; left:{round((week_num-1)/28*100)}%; top:0; bottom:0; width:2px; background:#fff; opacity:0.8;"></div>
    </div>
    <div style="display:flex; justify-content:space-between; margin-top:6px; font-size:10px; color:var(--muted);">
      <span>May 1</span><span>Rehab</span><span>Return</span><span>Build</span><span>Specific</span><span>Taper</span><span>Oct 12</span>
    </div>
  </div>
</div>

<!-- ACHILLES LOAD -->
<div class="section">
  <div class="section-title">Achilles Load Score</div>
  <div class="grid" style="grid-template-columns: repeat(auto-fill, minmax(140px,1fr)); margin-bottom:12px">
    <div class="card" style="border-top-color:{achilles_color}">
      <div class="card-label">Load Score</div>
      <div class="card-value" style="color:{achilles_color}">{achilles_score}</div>
      <div class="card-unit">/ 100</div>
      <div class="card-sub" style="color:{achilles_color}; text-transform:uppercase; font-size:10px; letter-spacing:1px">{achilles_level} risk</div>
    </div>
    <div class="card">
      <div class="card-label">This Week</div>
      <div class="card-value" style="font-size:24px">{achilles.get("this_week_km","--")}</div>
      <div class="card-unit">km</div>
    </div>
    <div class="card">
      <div class="card-label">Last Week</div>
      <div class="card-value" style="font-size:24px">{achilles.get("last_week_km","--")}</div>
      <div class="card-unit">km</div>
    </div>
  </div>
  <div class="card" style="padding:0; overflow:hidden; max-width:500px">
    <table class="run-table">
      <thead><tr><th>Factor</th><th>Value</th></tr></thead>
      <tbody>{achilles_rows}</tbody>
    </table>
  </div>
</div>

<!-- LAST RUN -->
<div class="section">
  <div class="section-title">Last Run — {last_run.get('date','--')} &nbsp;·&nbsp; {last_run.get('distance','--')} km</div>
  <div class="grid" style="grid-template-columns: repeat(auto-fill, minmax(130px,1fr))">
    <div class="card accent2">
      <div class="card-label">Avg Pace</div>
      <div class="card-value" style="font-size:26px">{pace_str(last_run.get('avg_pace'))}</div>
      <div class="card-unit">min/km</div>
    </div>
    <div class="card">
      <div class="card-label">Avg HR</div>
      <div class="card-value">{last_run.get('avg_hr','--')}</div>
      <div class="card-unit">bpm</div>
    </div>
    <div class="card">
      <div class="card-label">Cadence</div>
      <div class="card-value">{last_run.get('cadence','--')}</div>
      <div class="card-unit">spm</div>
    </div>
    <div class="card" style="border-top-color:{gct_bal_color}">
      <div class="card-label">GCT Balance L</div>
      <div class="card-value" style="font-size:26px;color:{gct_bal_color}">{last_gct_balance if last_gct_balance else '--'}%</div>
      <div class="card-unit">left foot contact</div>
      <div class="gct-bar-wrap"><div class="gct-bar" style="width:{last_gct_balance if last_gct_balance else 50}%;background:{gct_bal_color}"></div></div>
    </div>
    <div class="card" style="border-top-color:{gct_bal_color_r}">
      <div class="card-label">GCT Balance R</div>
      <div class="card-value" style="font-size:26px;color:{gct_bal_color_r}">{last_gct_balance_r if last_gct_balance_r else '--'}%</div>
      <div class="card-unit">right foot contact</div>
      <div class="gct-bar-wrap"><div class="gct-bar" style="width:{last_gct_balance_r if last_gct_balance_r else 50}%;background:{gct_bal_color_r}"></div></div>
    </div>
    <div class="card">
      <div class="card-label">Avg GCT</div>
      <div class="card-value" style="font-size:26px">{last_gct_avg}</div>
      <div class="card-unit">ms</div>
    </div>
    <div class="card">
      <div class="card-label">Calories</div>
      <div class="card-value" style="font-size:26px">{last_run.get('calories','--')}</div>
      <div class="card-unit">kcal</div>
    </div>
  </div>
</div>

<!-- LAST RUN CHARTS -->
<div class="section">
  <div class="section-title">Last Run — Lap Breakdown</div>
  <div class="chart-grid">
    <div class="chart-card">
      <div class="chart-title">GCT Balance Left % per km (50% = perfect)</div>
      <canvas id="lapBalanceChart"></canvas>
    </div>
    <div class="chart-card">
      <div class="chart-title">Ground Contact Time per km (ms)</div>
      <canvas id="lapGctChart"></canvas>
    </div>
    <div class="chart-card">
      <div class="chart-title">Heart Rate per km (bpm)</div>
      <canvas id="lapHrChart"></canvas>
    </div>
    <div class="chart-card">
      <div class="chart-title">Pace per km (min/km)</div>
      <canvas id="lapPaceChart"></canvas>
    </div>
  </div>
</div>

<!-- TRENDS -->
<div class="section">
  <div class="section-title">Training Trends</div>
  <div class="chart-grid">
    <div class="chart-card">
      <div class="chart-title">GCT Balance Left % — recent runs</div>
      <canvas id="gctTrendChart"></canvas>
    </div>
    <div class="chart-card">
      <div class="chart-title">Weekly Mileage (km)</div>
      <canvas id="weeklyChart"></canvas>
    </div>
    <div class="chart-card">
      <div class="chart-title">HRV overnight readings (ms)</div>
      <canvas id="hrvChart"></canvas>
    </div>
  </div>
</div>

<!-- RUN LOG -->
<div class="section">
  <div class="section-title">Run Log</div>
  <div class="card" style="padding:0; overflow:hidden;">
    <table class="run-table">
      <thead>
        <tr>
          <th>Date</th><th>Dist</th><th>Pace</th>
          <th>HR</th><th>Cadence</th><th>GCT L</th><th>GCT R</th><th>GCT avg</th>
        </tr>
      </thead>
      <tbody>
        {run_table_rows}
      </tbody>
    </table>
  </div>
</div>

<!-- BLOOD PRESSURE -->
<div class="section">
  <div class="section-title">Blood Pressure — Withings BPM Connect</div>
  <div class="grid" style="grid-template-columns: repeat(auto-fill, minmax(130px,1fr)); margin-bottom:16px">
    <div class="card {'green' if latest_bp.get('systolic',999) < 120 else 'yellow' if latest_bp.get('systolic',999) < 130 else 'red'}">
      <div class="card-label">Systolic</div>
      <div class="card-value">{latest_bp.get('systolic','--')}</div>
      <div class="card-unit">mmHg</div>
    </div>
    <div class="card {'green' if latest_bp.get('diastolic',999) < 80 else 'yellow' if latest_bp.get('diastolic',999) < 90 else 'red'}">
      <div class="card-label">Diastolic</div>
      <div class="card-value">{latest_bp.get('diastolic','--')}</div>
      <div class="card-unit">mmHg</div>
    </div>
    <div class="card">
      <div class="card-label">Pulse</div>
      <div class="card-value">{latest_bp.get('pulse','--')}</div>
      <div class="card-unit">bpm</div>
    </div>
    <div class="card">
      <div class="card-label">Last Reading</div>
      <div class="card-value" style="font-size:16px">{latest_bp.get('date','--')}</div>
    </div>
  </div>
  <div class="two-col">
    <div class="chart-card">
      <div class="chart-title">Blood Pressure trend (mmHg)</div>
      <canvas id="bpChart"></canvas>
    </div>
    <div class="card" style="padding:0; overflow:hidden;">
      <table class="bp-table">
        <thead>
          <tr><th>Date/Time</th><th>Systolic</th><th>Diastolic</th><th>Pulse</th></tr>
        </thead>
        <tbody>
          {bp_table_rows}
        </tbody>
      </table>
    </div>
  </div>
</div>

<script>
const accent  = '#38bdf8';
const accent2 = '#818cf8';
const green   = '#4ade80';
const yellow  = '#facc15';
const red     = '#f87171';
const orange  = '#fb923c';
const muted   = '#64748b';
const gridColor = 'rgba(30,35,48,0.8)';

const chartDefaults = {{
  responsive: true,
  maintainAspectRatio: true,
  plugins: {{ legend: {{ display: false }} }},
  scales: {{
    x: {{ ticks: {{ color: muted, font: {{ family: 'DM Mono', size: 10 }} }}, grid: {{ color: gridColor }} }},
    y: {{ ticks: {{ color: muted, font: {{ family: 'DM Mono', size: 10 }} }}, grid: {{ color: gridColor }} }},
  }}
}};

// Lap GCT Balance
new Chart(document.getElementById('lapBalanceChart'), {{
  type: 'bar',
  data: {{
    labels: {lap_labels},
    datasets: [
      {{
        label: 'Left',
        data: {lap_balance},
        backgroundColor: {lap_balance}.map(v => v > 51.5 ? 'rgba(248,113,113,0.7)' : v > 50.5 ? 'rgba(250,204,21,0.7)' : 'rgba(74,222,128,0.7)'),
        borderRadius: 3,
      }},
      {{
        label: 'Right',
        data: {lap_balance_r},
        backgroundColor: {lap_balance_r}.map(v => v > 51.5 ? 'rgba(248,113,113,0.4)' : v > 50.5 ? 'rgba(250,204,21,0.4)' : 'rgba(74,222,128,0.4)'),
        borderRadius: 3,
      }}
    ]
  }},
  options: {{ ...chartDefaults,
    plugins: {{ legend: {{ display: true, labels: {{ color: muted, font: {{ family: 'DM Mono', size: 10 }} }} }} }},
    scales: {{ ...chartDefaults.scales,
      y: {{ ...chartDefaults.scales.y, min: 47, max: 53,
        ticks: {{ ...chartDefaults.scales.y.ticks, callback: v => v + '%' }} }}
    }},
  }}
}});

// Lap GCT ms
new Chart(document.getElementById('lapGctChart'), {{
  type: 'line',
  data: {{
    labels: {lap_labels},
    datasets: [{{ data: {lap_gct}, borderColor: accent, backgroundColor: 'rgba(56,189,248,0.1)',
      tension: 0.3, fill: true, pointRadius: 3, pointBackgroundColor: accent }}]
  }},
  options: {{ ...chartDefaults }}
}});

// Lap HR
new Chart(document.getElementById('lapHrChart'), {{
  type: 'line',
  data: {{
    labels: {lap_labels},
    datasets: [{{ data: {lap_hr}, borderColor: red, backgroundColor: 'rgba(248,113,113,0.1)',
      tension: 0.3, fill: true, pointRadius: 3, pointBackgroundColor: red }}]
  }},
  options: {{ ...chartDefaults }}
}});

// Lap Pace
new Chart(document.getElementById('lapPaceChart'), {{
  type: 'line',
  data: {{
    labels: {lap_labels},
    datasets: [{{ data: {lap_pace}, borderColor: green, backgroundColor: 'rgba(74,222,128,0.1)',
      tension: 0.3, fill: true, pointRadius: 3, pointBackgroundColor: green }}]
  }},
  options: {{ ...chartDefaults,
    scales: {{ ...chartDefaults.scales,
      y: {{ ...chartDefaults.scales.y, reverse: true,
        ticks: {{ ...chartDefaults.scales.y.ticks,
          callback: v => {{ const m=Math.floor(v); const s=Math.round((v-m)*60); return m+':'+(s<10?'0':'')+s }} }} }} }} }}
}});

// GCT trend
new Chart(document.getElementById('gctTrendChart'), {{
  type: 'line',
  data: {{
    labels: {gct_trend_labels},
    datasets: [
      {{
        label: 'Left %',
        data: {gct_trend_values},
        borderColor: yellow, backgroundColor: 'rgba(250,204,21,0.1)',
        tension: 0.3, fill: false, pointRadius: 4, pointBackgroundColor: yellow
      }},
      {{
        label: 'Right %',
        data: {gct_trend_values_r},
        borderColor: accent, backgroundColor: 'rgba(56,189,248,0.1)',
        tension: 0.3, fill: false, pointRadius: 4, pointBackgroundColor: accent
      }}
    ]
  }},
  options: {{ ...chartDefaults,
    plugins: {{ legend: {{ display: true, labels: {{ color: muted, font: {{ family: 'DM Mono', size: 10 }} }} }} }},
    scales: {{ ...chartDefaults.scales,
      y: {{ ...chartDefaults.scales.y, min: 47, max: 53,
        ticks: {{ ...chartDefaults.scales.y.ticks, callback: v => v + '%' }} }} }} }}
}});

// Weekly mileage
new Chart(document.getElementById('weeklyChart'), {{
  type: 'bar',
  data: {{
    labels: {weekly_labels},
    datasets: [{{ data: {weekly_values}, backgroundColor: 'rgba(129,140,248,0.7)', borderRadius: 4 }}]
  }},
  options: {{ ...chartDefaults }}
}});

// HRV
new Chart(document.getElementById('hrvChart'), {{
  type: 'line',
  data: {{
    labels: {hrv_labels},
    datasets: [{{ data: {hrv_values}, borderColor: accent2, backgroundColor: 'rgba(129,140,248,0.1)',
      tension: 0.3, fill: true, pointRadius: 2, pointBackgroundColor: accent2 }}]
  }},
  options: {{ ...chartDefaults }}
}});

// BP
new Chart(document.getElementById('bpChart'), {{
  type: 'line',
  data: {{
    labels: {bp_dates},
    datasets: [
      {{ label: 'Systolic',  data: {bp_systolic},  borderColor: red,    backgroundColor: 'rgba(248,113,113,0.1)', tension: 0.3, fill: false, pointRadius: 3 }},
      {{ label: 'Diastolic', data: {bp_diastolic}, borderColor: orange, backgroundColor: 'rgba(251,146,60,0.1)',  tension: 0.3, fill: false, pointRadius: 3 }},
      {{ label: 'Pulse',     data: {bp_pulse},     borderColor: muted,  backgroundColor: 'transparent', tension: 0.3, fill: false, pointRadius: 3, borderDash:[4,4] }},
    ]
  }},
  options: {{ ...chartDefaults,
    plugins: {{ legend: {{ display: true, labels: {{ color: muted, font: {{ family: 'DM Mono', size: 10 }} }} }} }} }}
}});
</script>

<div style="margin-top:32px; padding-top:16px; border-top:1px solid var(--border); color:var(--muted); font-size:10px; letter-spacing:1px;">
  GENERATED {datetime.now().strftime('%Y-%m-%d %H:%M')} &nbsp;·&nbsp; GARMIN CONNECT + WITHINGS API
</div>
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

    ai_commentary = ""

    print("Checking alerts...")
    check_and_send_alerts(garmin_data.get("readiness", {}), achilles, bp_readings)

    print("Generating dashboard...")
    html = generate_html(garmin_data, bp_readings, phase_info, achilles, ai_commentary)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nDone! Open: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
