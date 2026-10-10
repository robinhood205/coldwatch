"""ColdWatch tools: 固定SQL(参数化)のみ。LLMに任意SQLは書かせない。"""
import os
from google.cloud import bigquery
from datetime import timezone, timedelta

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
    WITH latest AS (
      SELECT * EXCEPT(rn) FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY device_name ORDER BY timestamp DESC) rn
        FROM {TABLE}) WHERE rn = 1),
    recent AS (
      SELECT device_name, MIN(temperature) tmin, MAX(temperature) tmax, AVG(temperature) tavg
      FROM {TABLE}
      WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @h HOUR)
      GROUP BY device_name)
    SELECT l.device_name, l.timestamp, l.temperature, l.humidity,
           TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), l.timestamp, MINUTE) AS minutes_ago,
           r.tmin, r.tmax, ROUND(r.tavg,2) tavg
    FROM latest l LEFT JOIN recent r USING(device_name)"""
    rows = _run(sql, [bigquery.ScalarQueryParameter("h", "INT64", hours)])
    for r in rows:
        r["timestamp"] = r["timestamp"].astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")
        r["limit_max"] = _limit(r["device_name"])
        r["over_limit"] = r["temperature"] is not None and r["temperature"] > r["limit_max"]
        r["data_stale"] = r["minutes_ago"] > STALE_MIN
    return {"hours": hours, "devices": rows}


def generate_haccp_weekly_report(week_offset: int = 0) -> dict:
    """週単位のHACCPコンプライアンス集計(判定はコードで実施)。
    Args:
        week_offset: 0=今週(月曜開始), 1=先週, 2=先々週...
    """
    sql = f"""
    SELECT device_name,
           COUNT(*) n, ROUND(MIN(temperature),1) tmin, ROUND(MAX(temperature),1) tmax,
           ROUND(AVG(temperature),1) tavg, ROUND(AVG(humidity),1) havg,
           MIN(voltage) vmin, MIN(signal_strength) smin
    FROM {TABLE}
    WHERE timestamp >= TIMESTAMP(DATE_SUB(DATE_TRUNC(CURRENT_DATE('Asia/Tokyo'), WEEK(MONDAY)), INTERVAL @w WEEK), 'Asia/Tokyo')
      AND timestamp <  TIMESTAMP(DATE_ADD(DATE_SUB(DATE_TRUNC(CURRENT_DATE('Asia/Tokyo'), WEEK(MONDAY)), INTERVAL @w WEEK), INTERVAL 7 DAY), 'Asia/Tokyo')
    GROUP BY device_name"""
    rows = _run(sql, [bigquery.ScalarQueryParameter("w", "INT64", week_offset)])
    # 超過回数(15分×回数=超過分)
    ex_sql = f"""
    SELECT device_name, COUNTIF(temperature > @lim) over_n
    FROM {TABLE}
    WHERE timestamp >= TIMESTAMP(DATE_SUB(DATE_TRUNC(CURRENT_DATE('Asia/Tokyo'), WEEK(MONDAY)), INTERVAL @w WEEK), 'Asia/Tokyo')
      AND timestamp <  TIMESTAMP(DATE_ADD(DATE_SUB(DATE_TRUNC(CURRENT_DATE('Asia/Tokyo'), WEEK(MONDAY)), INTERVAL @w WEEK), INTERVAL 7 DAY), 'Asia/Tokyo')
      AND (CONTAINS_SUBSTR(device_name,'冷凍') = (@lim < 0))
    GROUP BY device_name"""
    over = {}
    for lim in (LIMIT_FREEZER_MAX, LIMIT_FRIDGE_MAX):
        for r in _run(ex_sql, [bigquery.ScalarQueryParameter("lim", "FLOAT64", lim),
                               bigquery.ScalarQueryParameter("w", "INT64", week_offset)]):
            over[r["device_name"]] = r["over_n"]
    expected = 7 * 24 * 4  # 15分間隔
    for r in rows:
        r["limit_max"] = _limit(r["device_name"])
        r["over_limit_count"] = over.get(r["device_name"], 0)
        r["over_limit_minutes"] = r["over_limit_count"] * 15
        r["missing_count"] = max(expected - r["n"], 0)
        r["compliant"] = r["over_limit_count"] == 0 and r["missing_count"] <= expected * 0.05
    return {"week_offset": week_offset, "expected_points": expected, "devices": rows}


def get_trend_for_improvement(weeks: int = 4) -> dict:
    """機器ごとの週次推移(平均・ばらつき・電圧・電波)。老化/故障予兆の分析用。
    Args:
        weeks: 何週間分(既定4)。
    """
    sql = f"""
    SELECT device_name, DATE_TRUNC(DATE(timestamp,'Asia/Tokyo'), WEEK(MONDAY)) week,
           ROUND(AVG(temperature),2) tavg, ROUND(STDDEV(temperature),2) tstd,
           ROUND(MAX(temperature),1) tmax, MIN(voltage) vmin, ROUND(AVG(signal_strength),0) sig
    FROM {TABLE}
    WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @d DAY)
    GROUP BY device_name, week ORDER BY device_name, week"""
    rows = _run(sql, [bigquery.ScalarQueryParameter("d", "INT64", weeks * 7)])
    for r in rows:
        r["week"] = r["week"].isoformat()
    return {"weeks": weeks, "trend": rows}
