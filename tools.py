"""ColdWatch tools: 固定SQL(参数化)のみ。LLMに任意SQLは書かせない。"""
import os
from google.cloud import bigquery
from datetime import timezone, timedelta, datetime

PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "coldwatch-hackathon")
TABLE = f"`{PROJECT}.haccp_monitoring.records`"
bq = bigquery.Client(project=PROJECT)

# HACCP基準(例)。社内基準に合わせて変更。名前に「冷凍」→冷凍、それ以外→冷蔵
LIMIT_FREEZER_MAX = -15.0
LIMIT_FRIDGE_MAX = 10.0
STALE_MIN = 10**9  # 15分間隔×2回欠測


def _limit(device_name: str) -> float:
    return LIMIT_FREEZER_MAX if "冷凍" in (device_name or "") else LIMIT_FRIDGE_MAX


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
    for r in rows:
        r["timestamp"] = r["timestamp"].astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")
        r["limit_max"] = _limit(r["device_name"])
        r["over_limit"] = r["temperature"] is not None and r["temperature"] > r["limit_max"]
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
    # 超過回数(15分×回数=超過分)
    ex_sql = f"""
    WITH max_ts AS (
      SELECT MAX(timestamp) AS max_timestamp FROM {TABLE}
    )
    SELECT device_name, COUNTIF(temperature > @lim) over_n
    FROM {TABLE}, max_ts
    WHERE timestamp >= TIMESTAMP_SUB(max_ts.max_timestamp, INTERVAL @d DAY)
      AND timestamp <  max_ts.max_timestamp
      AND (CONTAINS_SUBSTR(device_name,'冷凍') = (@lim < 0))
    GROUP BY device_name"""
    over = {}
    for lim in (LIMIT_FREEZER_MAX, LIMIT_FRIDGE_MAX):
        for r in _run(ex_sql, [bigquery.ScalarQueryParameter("lim", "FLOAT64", lim),
                               bigquery.ScalarQueryParameter("d", "INT64", days)]):
            over[r["device_name"]] = r["over_n"]

    # New: Calculate average over_limit_count for the past 90 days and multiply by `days`
    over_normal_sql = f"""
    WITH max_ts AS (
      SELECT MAX(timestamp) AS max_timestamp FROM {TABLE}
    ), daily_over AS (
        SELECT
            device_name,
            DATE(timestamp, 'Asia/Tokyo') AS record_date,
            COUNTIF(temperature > @fridge_lim AND CONTAINS_SUBSTR(device_name, '冷凍') = FALSE) AS over_fridge,
            COUNTIF(temperature > @freezer_lim AND CONTAINS_SUBSTR(device_name, '冷凍') = TRUE) AS over_freezer
        FROM {TABLE}, max_ts
        WHERE timestamp >= TIMESTAMP_SUB(max_ts.max_timestamp, INTERVAL 30 DAY)
          AND timestamp < max_ts.max_timestamp
        GROUP BY device_name, record_date
    )
    SELECT
        device_name,
        ROUND(AVG(CASE WHEN CONTAINS_SUBSTR(device_name, '冷凍') THEN over_freezer ELSE over_fridge END) * @d, 1) AS avg_over_normal
    FROM daily_over
    GROUP BY device_name
    """
    over_normal_data = _run(over_normal_sql, [
        bigquery.ScalarQueryParameter("fridge_lim", "FLOAT64", LIMIT_FRIDGE_MAX),
        bigquery.ScalarQueryParameter("freezer_lim", "FLOAT64", LIMIT_FREEZER_MAX),
        bigquery.ScalarQueryParameter("d", "INT64", days)
    ])
    over_normal_map = {r["device_name"]: r["avg_over_normal"] for r in over_normal_data}
    expected = days * 96  # 15分間隔
    for r in rows:
        r["limit_max"] = _limit(r["device_name"])
        r["over_limit_count"] = over.get(r["device_name"], 0)
        r["over_limit_minutes"] = r["over_limit_count"] * 15
        r["missing_count"] = max(expected - r["n"], 0)
        r["compliant"] = r["over_limit_count"] == 0 and r["missing_count"] <= expected * 0.05
        r["over_normal"] = over_normal_map.get(r["device_name"], 0.0)
    return {"days": days, "expected_points": expected, "devices": rows}


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
