"""ColdWatch tools: 固定SQL(参数化)のみ。LLMに任意SQLは書かせない。"""
import os
from google.cloud import bigquery
from datetime import timezone, timedelta, datetime

PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "coldwatch-hackathon")
TABLE = f"`{PROJECT}.haccp_monitoring.records`"
bq = bigquery.Client(project=PROJECT)

# HACCP基準(例)。社内基準に合わせて変更。名前に「冷凍」→冷凍、それ以外→冷蔵

DEFAULTS = {"freezer": {"limit_max": -15.0, "min_duration_min": 30}, "fridge": {"limit_max": 10.0, "min_duration_min": 30}}
DEVICE_CONFIG = {}  # 機器ごとの上書き。例: "冷凍庫1": {"limit_max": -18.0, "min_duration_min": 30}
STALE_MIN = 10**9  # 15分間隔×2回欠測

def _cfg(device_name: str) -> dict:
    base_type = "freezer" if "冷凍" in (device_name or "") else "fridge"
    config = DEFAULTS[base_type].copy()
    config.update(DEVICE_CONFIG.get(device_name, {}))
    return config

def _excursions(points, limit, min_minutes):
    """
    points: list of (timestamp, temperature) tuples, sorted by timestamp.
    limit: temperature limit for excursion.
    min_minutes: minimum duration for an excursion to be considered an event.
    """
    excursion_events = []
    current_excursion = []

    # Calculate median interval for more accurate duration calculation
    intervals = []
    for i in range(1, len(points)):
        diff = (points[i][0] - points[i-1][0]).total_seconds() / 60
        if diff > 0: # Avoid zero or negative intervals
            intervals.append(diff)
    median_interval = sorted(intervals)[len(intervals) // 2] if intervals else 15

    for ts, temp in points:
        if temp is not None and temp > limit:
            current_excursion.append({"timestamp": ts, "temperature": temp})
        else:
            if current_excursion:
                start_ts = current_excursion[0]["timestamp"]
                end_ts = current_excursion[-1]["timestamp"]
                duration_minutes = (end_ts - start_ts).total_seconds() / 60 + median_interval

                if duration_minutes >= min_minutes:
                    max_temp = max(p["temperature"] for p in current_excursion)

                    excursion_events.append({
                        "start": start_ts.astimezone(timezone(timedelta(hours=9))).strftime("%m-%d %H:%M"),
                        "end": end_ts.astimezone(timezone(timedelta(hours=9))).strftime("%m-%d %H:%M"),
                        "minutes": round(duration_minutes),
                        "max_temp": max_temp
                    })
                current_excursion = []

    # Check for an excursion that extends to the end of the data
    if current_excursion:
        start_ts = current_excursion[0]["timestamp"]
        end_ts = current_excursion[-1]["timestamp"]
        duration_minutes = (end_ts - start_ts).total_seconds() / 60 + median_interval

        if duration_minutes >= min_minutes:
            max_temp = max(p["temperature"] for p in current_excursion)

            excursion_events.append({
                "start": start_ts.astimezone(timezone(timedelta(hours=9))).strftime("%m-%d %H:%M"),
                "end": end_ts.astimezone(timezone(timedelta(hours=9))).strftime("%m-%d %H:%M"),
                "minutes": round(duration_minutes),
                "max_temp": max_temp
            })

    return excursion_events

def _run(sql, params=None):
    cfg = bigquery.QueryJobConfig(query_parameters=params or [])
    return [dict(r) for r in bq.query(sql, job_config=cfg).result()]


def get_current_status(hours: int = 3) -> dict:
    """全機器の現在の温度・湿度と直近の状況を返す。
    Args:
        hours: 直近何時間を見るか(既定3)。
    """
    sql = f"""
    WITH max_ts AS (
      SELECT MAX(timestamp) AS max_timestamp FROM {TABLE}
    ), latest AS (
      SELECT * EXCEPT(rn) FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY device_name ORDER BY timestamp DESC) rn
        FROM {TABLE}) WHERE rn = 1
    ), recent AS (
      SELECT device_name, MIN(temperature) tmin, MAX(temperature) tmax, AVG(temperature) tavg
      FROM {TABLE}, max_ts
      WHERE timestamp >= TIMESTAMP_SUB(max_ts.max_timestamp, INTERVAL @h HOUR)
      GROUP BY device_name
    ), baseline AS (
      SELECT device_name, AVG(temperature) bavg, STDDEV(temperature) bstd
      FROM {TABLE}, max_ts
      WHERE timestamp >= TIMESTAMP_SUB(max_ts.max_timestamp, INTERVAL 30 DAY)
      GROUP BY device_name
    )
    SELECT l.device_name, l.timestamp, l.temperature, l.humidity,
           TIMESTAMP_DIFF(max_ts.max_timestamp, l.timestamp, MINUTE) AS minutes_ago,
           r.tmin, r.tmax, ROUND(r.tavg,2) tavg,
           b.bavg, b.bstd
    FROM latest l LEFT JOIN recent r USING(device_name)
                  LEFT JOIN baseline b USING(device_name), max_ts"""
    rows = _run(sql, [bigquery.ScalarQueryParameter("h", "INT64", hours)])

    # Fetch recent temperature points for excursion check
    recent_temp_sql = f"""
    WITH max_ts AS (
        SELECT MAX(timestamp) AS max_timestamp FROM {TABLE}
    )
    SELECT device_name, timestamp, temperature
    FROM {TABLE}, max_ts
    WHERE timestamp >= TIMESTAMP_SUB(max_ts.max_timestamp, INTERVAL @check_hours HOUR)
      AND timestamp <= max_ts.max_timestamp
    ORDER BY device_name, timestamp ASC
    """
    # Use hours + 3 as a buffer for continuous check. Max 3 hours + 3 = 6 hours.
    recent_temp_data = _run(recent_temp_sql, [bigquery.ScalarQueryParameter("check_hours", "INT64", hours + 3)])

    device_recent_points = {}
    for pt in recent_temp_data:
        device_recent_points.setdefault(pt["device_name"], []).append((pt["timestamp"], pt["temperature"]))
    for r in rows:
        r["timestamp"] = r["timestamp"].astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")
        cfg = _cfg(r["device_name"])
        r["limit_max"] = cfg["limit_max"]
        r["min_duration_min"] = cfg["min_duration_min"]

        # Check for over_now and continuous over_limit
        r["over_now"] = r["temperature"] is not None and r["temperature"] > r["limit_max"]

        device_points = device_recent_points.get(r["device_name"], [])
        if device_points:
            # Calculate median interval for more accurate duration calculation
            intervals = []
            for i in range(1, len(device_points)):
                diff = (device_points[i][0] - device_points[i-1][0]).total_seconds() / 60
                if diff > 0:  # Avoid zero or negative intervals
                    intervals.append(diff)
            median_interval = sorted(intervals)[len(intervals) // 2] if intervals else 15

            # Only consider points leading up to the very latest
            latest_ts = device_points[-1][0]
            consecutive_excursion_points = []

            # Iterate backwards from the latest point
            for i in range(len(device_points) - 1, -1, -1):
                ts, temp = device_points[i]
                if temp is not None and temp > r["limit_max"]:
                    if not consecutive_excursion_points or \
                       (consecutive_excursion_points[0][0] - ts).total_seconds() / 60 < (len(consecutive_excursion_points) + 1) * median_interval: # Check if it's truly consecutive considering median_interval
                        consecutive_excursion_points.insert(0, (ts, temp))
                    else:
                        break
                else:
                    break

            if consecutive_excursion_points:
                start_consecutive_ts = consecutive_excursion_points[0][0]
                end_consecutive_ts = consecutive_excursion_points[-1][0]
                r["over_minutes_now"] = round((end_consecutive_ts - start_consecutive_ts).total_seconds() / 60 + median_interval)
            else:
                r["over_minutes_now"] = 0

            r["over_limit"] = r["over_minutes_now"] >= r["min_duration_min"]
        else:
            r["over_minutes_now"] = 0
            r["over_limit"] = False

        r["data_stale"] = r["minutes_ago"] > STALE_MIN

        # New: Anomaly detection based on historical baseline
        r["unusual"] = False
        if r["temperature"] is not None and r["bavg"] is not None and r["bstd"] is not None and r["bstd"] > 0:
            z = round((r["temperature"] - r["bavg"]) / r["bstd"], 1)
            if abs(z) > 3:
                r["unusual"] = True
            r["z_score"] = z
        else:
            r["z_score"] = None
    return {"hours": hours, "devices": rows}


def generate_haccp_weekly_report(days: int = 7) -> dict:
    """指定日数間の温度管理の集計(HACCP記録用)。
    Args:
        days: 直近何日間を見るか(既定7)。
    """
    sql = f"""
    WITH max_ts AS (
      SELECT MAX(timestamp) AS max_timestamp FROM {TABLE}
    )
    SELECT device_name,
           COUNT(*) n, ROUND(MIN(temperature),1) tmin, ROUND(MAX(temperature),1) tmax,
           ROUND(AVG(temperature),1) tavg, ROUND(AVG(humidity),1) havg,
           MIN(voltage) vmin, MIN(signal_strength) smin
    FROM {TABLE}, max_ts
    WHERE timestamp >= TIMESTAMP_SUB(max_ts.max_timestamp, INTERVAL @d DAY)
      AND timestamp <  max_ts.max_timestamp
    GROUP BY device_name"""
    rows = _run(sql, [bigquery.ScalarQueryParameter("d", "INT64", days)])

    max_timestamp_query = f"SELECT MAX(timestamp) AS max_timestamp FROM {TABLE}"
    max_timestamp_result = _run(max_timestamp_query)
    max_timestamp_val = max_timestamp_result[0]["max_timestamp"] if max_timestamp_result else datetime.now(timezone.utc)

    # Raw temperature points for excursion analysis
    full_data_sql = f"""
    WITH max_ts AS (
        SELECT MAX(timestamp) AS max_timestamp FROM {TABLE}
    )
    SELECT device_name, timestamp, temperature
    FROM {TABLE}, max_ts
    WHERE timestamp >= TIMESTAMP_SUB(max_ts.max_timestamp, INTERVAL @days_plus_30 DAY)
      AND timestamp <= max_ts.max_timestamp
    ORDER BY device_name, timestamp ASC
    """
    # Fetch data for `days` + 30 days for baseline calculation
    full_temp_data = _run(full_data_sql, [bigquery.ScalarQueryParameter("days_plus_30", "INT64", days + 30)])

    device_all_points = {}
    for pt in full_temp_data:
        device_all_points.setdefault(pt["device_name"], []).append((pt["timestamp"], pt["temperature"]))

    expected = days * 96  # 15分間隔

    for r in rows:
        device_name = r["device_name"]
        cfg = _cfg(device_name)
        limit_max = cfg["limit_max"]
        min_duration_min = cfg["min_duration_min"]

        r["limit_max"] = limit_max
        r["min_duration_min"] = min_duration_min

        all_points_for_device = device_all_points.get(device_name, [])

        # Filter points for the current `days` period
        current_period_points = [
            (ts, temp) for ts, temp in all_points_for_device
            if (max_timestamp_val - ts).days < days
        ]

        # Calculate raw over points (just for reference, not for compliance)
        r["raw_over_points"] = sum(1 for ts, temp in current_period_points if temp is not None and temp > limit_max)

        # Calculate excursions for the current period
        excursions = _excursions(current_period_points, limit_max, min_duration_min)
        excursions.sort(key=lambda x: x["minutes"], reverse=True)

        r["over_limit_count"] = len(excursions)
        r["over_limit_minutes"] = sum(e["minutes"] for e in excursions)
        r["excursions"] = excursions[:3] # Top 3 longest excursions

        # Calculate over_normal (average daily excursions over the past 30 days)
        baseline_period_points = [
            (ts, temp) for ts, temp in all_points_for_device
            if (max_timestamp_val - ts).days >= days and (max_timestamp_val - ts).days < (days + 30)
        ]
        baseline_excursions = _excursions(baseline_period_points, limit_max, min_duration_min)
        avg_daily_excursions_in_baseline = len(baseline_excursions) / 30 if baseline_excursions else 0
        r["over_normal"] = round(avg_daily_excursions_in_baseline * days, 1)

        r["missing_count"] = max(expected - r["n"], 0)
        r["compliant"] = r["over_limit_count"] == 0 and (r["missing_count"] / expected) <= 0.10 # 10% missing allowed
    return {"days": days, "expected_points": expected, "devices": rows}

def get_data_range() -> dict:
    """テーブル全体のデータ期間(最小・最大timestamp)を返す。"""
    sql = f"""
    SELECT
      FORMAT_TIMESTAMP('%Y/%m/%d %H:%M', GREATEST(TIMESTAMP_SUB(MAX(timestamp), INTERVAL 30 DAY), MIN(timestamp)), 'Asia/Tokyo') AS from_timestamp,
      FORMAT_TIMESTAMP('%Y/%m/%d %H:%M', MAX(timestamp), 'Asia/Tokyo') AS to_timestamp
    FROM {TABLE}
    """
    result = _run(sql)
    if result and result[0]["from_timestamp"] and result[0]["to_timestamp"]:
        return {"from": result[0]["from_timestamp"], "to": result[0]["to_timestamp"]}
    return {"from": "N/A", "to": "N/A"}




def get_trend_for_improvement(weeks: int = 5) -> dict:
    """機器ごとの週次推移(平均・ばらつき・電圧・電波)。老化/故障予兆の分析用。
    Args:
        weeks: 何週間分(既定5)。
    """
    sql = f"""
    WITH max_ts AS (
      SELECT MAX(timestamp) AS max_timestamp FROM {TABLE}
    )
    SELECT device_name, DATE_TRUNC(DATE(timestamp,'Asia/Tokyo'), WEEK(MONDAY)) week,
           ROUND(AVG(temperature),2) tavg, ROUND(STDDEV(temperature),2) tstd,
           ROUND(MAX(temperature),1) tmax, MIN(voltage) vmin, ROUND(AVG(signal_strength),0) sig
    FROM {TABLE}, max_ts
    WHERE timestamp >= TIMESTAMP_SUB(max_ts.max_timestamp, INTERVAL @d DAY)
    GROUP BY device_name, week ORDER BY device_name, week"""
    rows = _run(sql, [bigquery.ScalarQueryParameter("d", "INT64", weeks * 7)])
    for r in rows:
        r["week"] = r["week"].isoformat()

    # Calculate change from first to last week
    device_trends = {}
    for r in rows:
        device_trends.setdefault(r["device_name"], []).append(r)

    change_first_to_last = []
    for device_name, trends in device_trends.items():
        if len(trends) >= 2:
            trends.sort(key=lambda x: x["week"])
            first_week = trends[0]
            last_week = trends[-1]

            from_date = datetime.fromisoformat(first_week["week"])
            to_date = datetime.fromisoformat(last_week["week"])
            weeks_span = int((to_date - from_date).days / 7)

            tavg_change = round((last_week.get("tavg", 0) or 0) - (first_week.get("tavg", 0) or 0), 2)
            tstd_change = round((last_week.get("tstd", 0) or 0) - (first_week.get("tstd", 0) or 0), 2)
            voltage_change = round((last_week.get("vmin", 0) or 0) - (first_week.get("vmin", 0) or 0), 2)
            voltage_per_week = round(voltage_change / weeks_span, 1) if weeks_span > 0 else 0.0

            change_first_to_last.append({
                "device_name": device_name,
                "from_week": first_week["week"],
                "to_week": last_week["week"],
                "weeks_span": weeks_span,
                "tavg_change": tavg_change,
                "tstd_change": tstd_change,
                "voltage_change": voltage_change,
                "voltage_per_week": voltage_per_week
            })

    # New: Recent 7 days vs previous baseline (past 90 days excluding recent 7 days)
    max_timestamp_query = f"SELECT MAX(timestamp) AS max_timestamp FROM {TABLE}"
    max_timestamp_result = _run(max_timestamp_query)
    max_timestamp_val = max_timestamp_result[0]["max_timestamp"] if max_timestamp_result else datetime.now(timezone(timedelta(hours=9)))

    recent_vs_baseline_sql = f"""
    WITH max_ts AS (
      SELECT MAX(timestamp) AS max_timestamp FROM {TABLE}
    ), recent_data AS (
      SELECT
        device_name,
        AVG(temperature) AS recent_tavg,
        STDDEV(temperature) AS recent_tstd,
        MIN(voltage) AS recent_vmin
      FROM {TABLE}, max_ts
      WHERE timestamp >= TIMESTAMP_SUB(max_ts.max_timestamp, INTERVAL 7 DAY)
        AND timestamp < max_ts.max_timestamp
      GROUP BY device_name
    ),
    baseline_data AS (
      SELECT
        device_name,
        AVG(temperature) AS baseline_tavg,
        STDDEV(temperature) AS baseline_tstd,
        MIN(voltage) AS baseline_vmin
      FROM {TABLE}, max_ts
      WHERE timestamp >= TIMESTAMP_SUB(max_ts.max_timestamp, INTERVAL 30 DAY)
        AND timestamp < TIMESTAMP_SUB(max_ts.max_timestamp, INTERVAL 7 DAY)
      GROUP BY device_name
    )
    SELECT
      COALESCE(r.device_name, b.device_name) AS device_name,
      r.recent_tavg,
      r.recent_tstd,
      r.recent_vmin,
      b.baseline_tavg,
      b.baseline_tstd,
      b.baseline_vmin
    FROM recent_data r
    FULL OUTER JOIN baseline_data b ON r.device_name = b.device_name
    """
    recent_vs_baseline_raw = _run(recent_vs_baseline_sql)

    recent_vs_baseline = []
    for r in recent_vs_baseline_raw:
        recent_tavg = r["recent_tavg"] or 0
        recent_tstd = r["recent_tstd"] or 0
        recent_vmin = r["recent_vmin"] or 0
        baseline_tavg = r["baseline_tavg"] or 0
        baseline_tstd = r["baseline_tstd"] or 0
        baseline_vmin = r["baseline_vmin"] or 0

        tavg_change = round(recent_tavg - baseline_tavg, 2)
        tstd_change = round(recent_tstd - baseline_tstd, 2)
        voltage_change = round(recent_vmin - baseline_vmin, 2)

        recent_vs_baseline.append({
            "device_name": r["device_name"],
            "recent_from": (max_timestamp_val - timedelta(days=7)).astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d"),
            "recent_to": max_timestamp_val.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d"),
            "tavg_change": tavg_change,
            "tstd_change": tstd_change,
            "voltage_change": voltage_change
        })

    return {"weeks": weeks, "trend": rows, "change_first_to_last": change_first_to_last, "recent_vs_baseline": recent_vs_baseline}
