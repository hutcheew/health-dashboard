"""
compare_runs.py (Multi-Run with Auto-Shift Version)
--------------------------------------------------
Pulls multiple runs from Garmin and renders an overlaid multi-line chart
along with biomechanical, efficiency, and asymmetry trends. If a date has 
no run, it automatically scans forward for the next available one.

Usage:
    python compare_runs.py today yesterday today-1y today-2y
"""

import argparse
import json
import re
import sys
from datetime import date, timedelta

from health_dashboard import get_garmin


def parse_date_arg(s):
    s = s.strip().lower()
    if s == "today":
        return date.today()
    if s == "yesterday":
        return date.today() - timedelta(days=1)
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
        raise SystemExit(f"Couldn't parse date argument '{s}'. Use YYYY-MM-DD, 'today', 'yesterday', or 'today-Ny'.")


def find_next_available_run(garmin, target_date, max_lookahead_days=7):
    """Scans forward starting from target_date until a running activity is found."""
    for offset in range(max_lookahead_days + 1):
        current_date = target_date + timedelta(days=offset)
        d_str = current_date.isoformat()
        
        activities = garmin.get_activities_by_date(d_str, d_str, "running")
        if activities:
            longest_run = max(activities, key=lambda a: a.get("distance", 0))
            return longest_run, current_date
            
    return None, target_date


def get_run_metrics(garmin, activity):
    activity_id = activity["activityId"]
    splits = garmin.get_activity_splits(activity_id)
    laps = splits.get("lapDTOs", [])

    points = []
    cumulative_km = 0.0
    
    gct_list, gct_b_list, cadence_list, vert_osc_list, lap_efficiencies = [], [], [], [], []

    for lap in laps:
        dist_m = lap.get("distance", 0)
        dist_km = dist_m / 1000
        if dist_km <= 0:
            continue
            
        cumulative_km += dist_km
        avg_hr = lap.get("averageHR")
        avg_speed = lap.get("averageSpeed")
        pace = round(1000 / avg_speed / 60, 2) if avg_speed else None
        
        gct = lap.get("groundContactTime")
        gct_b = lap.get("groundContactBalanceLeft")
        cadence = lap.get("averageRunCadence")
        vo = lap.get("verticalOscillation")

        if gct: gct_list.append(gct)
        if gct_b: gct_b_list.append(gct_b)
        if cadence: cadence_list.append(cadence)
        if vo: vert_osc_list.append(vo)

        if avg_speed and avg_hr and avg_hr > 0:
            lap_efficiencies.append(round((avg_speed * 60) / avg_hr, 2))

        points.append({"km": round(cumulative_km, 2), "hr": avg_hr, "pace": pace})

    summary_pace = round(1000 / (activity.get("averageSpeed", 1)) / 60, 2) if activity.get("averageSpeed") else 0
    summary_hr = activity.get("averageHR", 0)
    avg_ei = round((1000 / summary_pace) / summary_hr, 2) if summary_pace > 0 and summary_hr > 0 else 0

    final_cadence = int(sum(cadence_list)/len(cadence_list)) if cadence_list else (int(activity.get("averageRunningCadenceInStepsPerMinute")) if activity.get("averageRunningCadenceInStepsPerMinute") else "--")
    final_gct = f"{int(sum(gct_list)/len(gct_list))}ms" if gct_list else (f"{int(activity.get('avgGroundContactTime'))}ms" if activity.get("avgGroundContactTime") else "--")
    final_gct_b = f"{round(sum(gct_b_list)/len(gct_b_list), 1)}%" if gct_b_list else (f"{round(activity.get('avgGroundContactBalance'), 1)}%" if activity.get("avgGroundContactBalance") else "--")
    final_vo = f"{round(sum(vert_osc_list)/len(vert_osc_list)/10, 1)}cm" if vert_osc_list else (f"{round(activity.get('avgVerticalOscillation')/10, 1)}cm" if activity.get("avgVerticalOscillation") else "--")

    decoupling = "--"
    if len(lap_efficiencies) >= 2:
        mid = len(lap_efficiencies) // 2
        first_half = sum(lap_efficiencies[:mid]) / mid
        second_half = sum(lap_efficiencies[mid:]) / (len(lap_efficiencies) - mid)
        if first_half > 0:
            decoupling = f"{round(((first_half - second_half) / first_half) * 100, 1)}%"

    asymmetry_risk = "🟢 Symmetric"
    if gct_b_list and len(gct_b_list) >= 4:
        if abs((sum(gct_b_list[:2]) / 2) - (sum(gct_b_list[-2:]) / 2)) >= 1.5:
            asymmetry_risk = "🔴 Fatigue Drift"
        elif abs(sum(gct_b_list) / len(gct_b_list) - 50.0) >= 1.0:
            asymmetry_risk = "🟡 Asymmetry"

    return {
        "points": points,
        "summary": {
            "pace": summary_pace, "hr": f"{int(summary_hr)} bpm" if summary_hr else "--",
            "ei": avg_ei or "--", "decoupling": decoupling, "cadence": f"{final_cadence} spm" if final_cadence != "--" else "--",
            "gct": final_gct, "gct_b": final_gct_b, "vo": final_vo, "risk": asymmetry_risk
        }
    }


def build_html(runs_data, out_path):
    try:
        with open("chart.js", "r", encoding="utf-8") as f:
            chart_js_code = f.read()
    except FileNotFoundError:
        chart_js_code = ""

    colors = ["#58a6ff", "#f78166", "#34d399", "#fbbf24", "#bc8cff", "#ff7b72"]
    headers_html = "".join(f"<th>{r['label']}</th>" for r in runs_data)
    
    def get_row_html(metric_key):
        return "".join(f"<td class='val-highlight'>{r['metrics']['summary'][metric_key]}</td>" for r in runs_data)

    def fmt_pace(p):
        if not p or p == 0: return "--"
        return f"{int(p)}:{int(round((p - int(p)) * 60)):02d}/km"

    pace_row_html = "".join(f"<td class='val-highlight'>{fmt_pace(r['metrics']['summary']['pace'])}</td>" for r in runs_data)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Multi-Run Diagnostics</title>
<script>{chart_js_code}</script>
{"<script src='https://cdn.jsdelivr.net/npm/chart.js'></script>" if not chart_js_code else ""}
<style>
  :root {{ --bg:#0e1116; --surface:#161b22; --text:#e6edf3; --muted:#8b949e; }}
  body {{ background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,sans-serif; padding:24px; }}
  .card {{ background:var(--surface); border:1px solid #21262d; border-radius:12px; padding:20px; margin-bottom:20px; overflow-x:auto; }}
  h2 {{ font-size:13px; color:var(--muted); text-transform:uppercase; border-bottom:1px solid #21262d; padding-bottom:8px; margin:0 0 12px 0; }}
  table {{ width:100%; border-collapse:collapse; font-size:14px; text-align:left; }}
  th {{ color:var(--muted); padding:10px; border-bottom:2px solid #21262d; }}
  td {{ padding:12px 10px; border-bottom:1px solid #212630; }}
  .val-highlight {{ font-family:monospace; }}
  .chart-container {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; }}
  canvas {{ max-height:320px; }}
</style>
</head>
<body>
  <h1>Multi-Run Fitness Diagnostics</h1>
  <div class="card">
    <h2>Performance Matrix Matrix</h2>
    <table>
      <thead><tr><th>Metric</th>{headers_html}</tr></thead>
      <tbody>
        <tr><td>Pace</td>{pace_row_html}</tr>
        <tr><td>Avg HR</td>{get_row_html('hr')}</tr>
        <tr><td>Efficiency (EI)</td>{get_row_html('ei')}</tr>
        <tr><td>Decoupling</td>{get_row_html('decoupling')}</tr>
        <tr><td>Cadence</td>{get_row_html('cadence')}</tr>
        <tr><td>GCT</td>{get_row_html('gct')}</tr>
        <tr><td>GCT Balance (L)</td>{get_row_html('gct_b')}</tr>
        <tr><td>Vert Oscillation</td>{get_row_html('vo')}</tr>
        <tr><td>Achilles Assessment</td>{get_row_html('risk')}</tr>
      </tbody>
    </table>
  </div>

  <div class="chart-container">
    <div class="card"><h2>Heart Rate Profile (by km)</h2><canvas id="hrChart"></canvas></div>
    <div class="card"><h2>Pace Profile (by km)</h2><canvas id="paceChart"></canvas></div>
  </div>

  <script>
    const runDatasets = {json.dumps([{ 'label': r['label'], 'color': colors[i % len(colors)], 'points': r['metrics']['points'] } for i, r in enumerate(runs_data)])};
    
    function buildChartConfig(field, reverseY=false) {{
      return {{
        type: 'line',
        data: {{
          datasets: runDatasets.map(r => ({{
            label: r.label,
            data: r.points.map(p => ({{x: p.km, y: p[field]}})).filter(p => p.y !== null && p.y !== undefined),
            borderColor: r.color,
            backgroundColor: r.color,
            tension: 0.15,
            pointRadius: 2
          }}))
        }},
        options: {{
          responsive: true, maintainAspectRatio: false,
          scales: {{
            x: {{ type: 'linear', title: {{ display:true, text:'Distance (km)', color:'#8b949e' }}, grid:{{color:'#21262d'}} }},
            y: {{ reverse: reverseY, grid:{{color:'#21262d'}}, ticks:{{color:'#8b949e'}} }}
          }}
        }}
      }};
    }}
    
    new Chart(document.getElementById('hrChart'), buildChartConfig('hr'));
    new Chart(document.getElementById('paceChart'), buildChartConfig('pace', true));
  </script>
</body>
</html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    parser = argparse.ArgumentParser(description="Compare multiple runs side by side.")
    parser.add_argument("dates", nargs="+", help="List of dates to evaluate (e.g., today yesterday today-1y)")
    parser.add_argument("--out", default="run_comparison.html", help="Output path")
    args = parser.parse_args()

    print("Connecting to Garmin...")
    garmin = get_garmin()
    runs_data = []

    for idx, date_str in enumerate(args.dates):
        target_date = parse_date_arg(date_str)
        
        # Call the rolling search block instead of a strict target date lookup
        activity, actual_date = find_next_available_run(garmin, target_date, max_lookahead_days=7)
        
        if not activity:
            print(f"❌ Error: Looked forward 7 days from {target_date.isoformat()} and found no runs. Skipping.")
            continue
            
        if actual_date != target_date:
            print(f"⏭️ No run on {target_date.isoformat()} -> Shifted forward to next available on {actual_date.isoformat()}")
        else:
            print(f"🎯 Found exact run on {actual_date.isoformat()}")

        dist = round(activity.get("distance", 0) / 1000, 1)
        metrics = get_run_metrics(garmin, activity)
        runs_data.append({
            "label": f"{actual_date.isoformat()} ({dist}k)",
            "metrics": metrics
        })

    if not runs_data:
        sys.exit("Error: No valid run data targets found across the specified dates.")

    build_html(runs_data, args.out)
    print(f"\nDone! Dynamic diagnostic report generated at {args.out}")


if __name__ == "__main__":
    main()