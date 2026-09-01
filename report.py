"""GCP 크레딧 잔액을 조회해서 디스코드로 보고한다.

빌링 데이터는 Cloud Billing -> BigQuery 내보내기(표준 사용량 비용) 테이블에서 읽는다.
콘솔의 "크레딧 사용됨" 숫자와 맞추기 위해, 비용(cost)이 아니라 실제로 차감된
프로모션 크레딧 금액을 기준 지표로 쓴다.
"""

import os
import re
import sys
import datetime as dt
from dataclasses import dataclass
from typing import Optional

import requests
from google.api_core.exceptions import NotFound
from google.cloud import bigquery

KST = dt.timezone(dt.timedelta(hours=9))

TABLE_RE = re.compile(r"^[A-Za-z0-9._\-]+$")
TZ_RE = re.compile(r"^[A-Za-z0-9_+\-/]+$")
CREDIT_TYPE_RE = re.compile(r"^[A-Z_]+$")


def env(name, default=None, required=False):
    # GitHub Actions 는 미설정 variable 을 빈 문자열로 넘기므로 빈 값도 기본값으로 취급한다.
    value = os.environ.get(name, "")
    if value == "":
        value = default
    if required and not value:
        sys.exit("환경변수 {} 가 비어 있습니다.".format(name))
    return value


@dataclass
class Config:
    table: str
    webhook: str
    total_credit: float
    expiry: Optional[dt.date]
    currency: str
    tz: str
    credit_types: list
    lookback_hours: int
    thresholds: list
    mention: str
    project_label: str

    @classmethod
    def from_env(cls):
        table = env("BQ_BILLING_TABLE", required=True).strip()
        if not TABLE_RE.match(table) or table.count(".") != 2:
            sys.exit(
                "BQ_BILLING_TABLE 형식이 올바르지 않습니다: {!r}\n"
                "형식: project.dataset.gcp_billing_export_v1_XXXXXX_XXXXXX_XXXXXX".format(table)
            )

        tz = env("REPORT_TZ", "Asia/Seoul")
        if not TZ_RE.match(tz):
            sys.exit("REPORT_TZ 형식이 올바르지 않습니다: {!r}".format(tz))

        credit_types = [t.strip().upper() for t in env("CREDIT_TYPES", "PROMOTION").split(",") if t.strip()]
        for t in credit_types:
            if not CREDIT_TYPE_RE.match(t):
                sys.exit("CREDIT_TYPES 값이 올바르지 않습니다: {!r}".format(t))

        expiry_raw = env("CREDIT_EXPIRY", "").strip()
        expiry = dt.date.fromisoformat(expiry_raw) if expiry_raw else None

        thresholds = sorted({int(x) for x in env("MENTION_THRESHOLDS", "50,80,90").split(",") if x.strip()})

        return cls(
            table=table,
            webhook=env("DISCORD_WEBHOOK_URL", required=True).strip(),
            total_credit=float(env("TOTAL_CREDIT", required=True)),
            expiry=expiry,
            currency=env("CURRENCY", "KRW"),
            tz=tz,
            credit_types=credit_types,
            lookback_hours=int(env("MENTION_LOOKBACK_HOURS", "12")),
            thresholds=thresholds,
            mention=env("MENTION_TARGET", "@everyone"),
            project_label=env("PROJECT_LABEL", "GCP"),
        )


def money(amount, currency):
    if currency in ("KRW", "JPY"):
        return "{} {:,.0f}".format(currency, amount)
    return "{} {:,.2f}".format(currency, amount)


def credit_expr(credit_types):
    types = ", ".join('"{}"'.format(t) for t in credit_types)
    return (
        "(SELECT COALESCE(SUM(c.amount), 0) FROM UNNEST(credits) c "
        "WHERE c.type IN ({}))".format(types)
    )


def fetch_totals_and_daily(client, cfg):
    credits_sql = credit_expr(cfg.credit_types)

    totals_sql = """
    SELECT
      -COALESCE(SUM(promo), 0) AS used_now,
      -COALESCE(SUM(IF(usage_start_time < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {hours} HOUR),
                       promo, 0)), 0) AS used_prev,
      COALESCE(SUM(cost), 0) AS gross_cost,
      MAX(export_time) AS last_export
    FROM (
      SELECT usage_start_time, cost, export_time, {credits} AS promo
      FROM `{table}`
    )
    """.format(hours=cfg.lookback_hours, credits=credits_sql, table=cfg.table)

    daily_sql = """
    SELECT DATE(usage_start_time, "{tz}") AS day,
           -COALESCE(SUM({credits}), 0) AS used
    FROM `{table}`
    WHERE usage_start_time >= TIMESTAMP(DATE_SUB(CURRENT_DATE("{tz}"), INTERVAL 7 DAY), "{tz}")
    GROUP BY day
    ORDER BY day
    """.format(tz=cfg.tz, credits=credits_sql, table=cfg.table)

    totals = list(client.query(totals_sql).result())[0]
    daily = {r["day"]: float(r["used"]) for r in client.query(daily_sql).result()}
    return totals, daily


def fetch_top_services(client, cfg, start, end):
    sql = """
    SELECT service.description AS service,
           -COALESCE(SUM({credits}), 0) AS used
    FROM `{table}`
    WHERE DATE(usage_start_time, "{tz}") BETWEEN "{start}" AND "{end}"
    GROUP BY service
    HAVING used > 0
    ORDER BY used DESC
    LIMIT 5
    """.format(
        credits=credit_expr(cfg.credit_types),
        table=cfg.table,
        tz=cfg.tz,
        start=start.isoformat(),
        end=end.isoformat(),
    )
    return [(r["service"], float(r["used"])) for r in client.query(sql).result()]


def progress_bar(ratio, width=20):
    filled = max(0, min(width, round(ratio * width)))
    return "█" * filled + "░" * (width - filled)


def crossed_threshold(cfg, pct_now, pct_prev):
    """이번 실행 구간에서 새로 넘어선 임계치만 돌려준다 (매번 멘션되는 것 방지)."""
    crossed = [t for t in cfg.thresholds if pct_prev < t <= pct_now]
    return max(crossed) if crossed else None


def build_embed(cfg, used, daily, top, top_label, pct_prev):
    remaining = cfg.total_credit - used
    pct = (used / cfg.total_credit * 100) if cfg.total_credit else 0.0

    today = dt.datetime.now(KST).date()
    yesterday = today - dt.timedelta(days=1)
    yday_used = daily.get(yesterday, 0.0)

    past = [v for d, v in daily.items() if d < today]
    avg = (sum(past) / len(past)) if past else 0.0

    if remaining <= 0:
        depletion_text = "이미 소진됨"
    elif avg > 0:
        days_left = remaining / avg
        depletion = today + dt.timedelta(days=int(min(days_left, 3650)))
        depletion_text = "{} (약 {:,.0f}일 뒤)".format(depletion.isoformat(), days_left)
        if cfg.expiry and depletion > cfg.expiry:
            depletion_text += "\n→ 만료일({}) 이 먼저 도래".format(cfg.expiry.isoformat())
    else:
        depletion_text = "최근 7일 사용 없음 (예측 불가)"

    if pct >= 90:
        color = 0xE74C3C
    elif pct >= 80:
        color = 0xE67E22
    elif pct >= 50:
        color = 0xF1C40F
    else:
        color = 0x2ECC71

    fields = [
        {"name": "남은 크레딧", "value": money(remaining, cfg.currency), "inline": True},
        {"name": "사용률", "value": "{:.2f}%".format(pct), "inline": True},
        {"name": "총 크레딧", "value": money(cfg.total_credit, cfg.currency), "inline": True},
        {"name": "어제 사용", "value": money(yday_used, cfg.currency), "inline": True},
        {"name": "최근 7일 평균/일", "value": money(avg, cfg.currency), "inline": True},
        {"name": "크레딧 만료일", "value": cfg.expiry.isoformat() if cfg.expiry else "-", "inline": True},
        {"name": "소진 예상", "value": depletion_text, "inline": False},
    ]

    if top:
        lines = [
            "`{}.` {} — **{}**".format(i, name, money(amount, cfg.currency))
            for i, (name, amount) in enumerate(top, 1)
        ]
        top_value = "\n".join(lines)
    else:
        top_value = "해당 기간 사용 내역 없음"
    fields.append({"name": "서비스별 지출 TOP 5 ({})".format(top_label), "value": top_value, "inline": False})

    embed = {
        "title": "💳 {} 크레딧 리포트".format(cfg.project_label),
        "description": "`{}` **{:.2f}%** 사용".format(progress_bar(pct / 100), pct),
        "color": color,
        "fields": fields,
        "footer": {"text": "Cloud Billing → BigQuery 내보내기 기준 · 최신 사용량은 수 시간 지연될 수 있음"},
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    }

    content = ""
    hit = crossed_threshold(cfg, pct, pct_prev)
    if hit is not None:
        content = "{} ⚠️ 크레딧 사용률이 **{}%** 를 넘었습니다.".format(cfg.mention, hit)
    return content, embed


def post(cfg, content, embed):
    payload = {"embeds": [embed]}
    if content:
        payload["content"] = content
        payload["allowed_mentions"] = {"parse": ["everyone"]}
    resp = requests.post(cfg.webhook, json=payload, timeout=30)
    resp.raise_for_status()
    print("디스코드 전송 완료 (HTTP {})".format(resp.status_code))


def post_no_data(cfg, reason):
    embed = {
        "title": "💳 {} 크레딧 리포트".format(cfg.project_label),
        "description": (
            "{}\n"
            "결제 내보내기를 방금 켰다면 첫 데이터가 도착하기까지 최대 24시간 걸립니다.\n"
            "그 전까지는 이 안내만 전송됩니다."
        ).format(reason),
        "color": 0x95A5A6,
        "footer": {"text": cfg.table},
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    post(cfg, "", embed)


def main():
    cfg = Config.from_env()
    client = bigquery.Client()

    try:
        totals, daily = fetch_totals_and_daily(client, cfg)
    except NotFound:
        # 내보내기를 켠 직후에는 테이블 자체가 아직 생성되지 않는다.
        post_no_data(cfg, "빌링 내보내기 테이블이 아직 생성되지 않았습니다.")
        return

    if totals["last_export"] is None:
        post_no_data(cfg, "빌링 내보내기 테이블에 아직 데이터가 없습니다.")
        return

    used = float(totals["used_now"])
    used_prev = float(totals["used_prev"])
    pct_prev = (used_prev / cfg.total_credit * 100) if cfg.total_credit else 0.0

    today = dt.datetime.now(KST).date()
    yesterday = today - dt.timedelta(days=1)

    top = fetch_top_services(client, cfg, yesterday, yesterday)
    top_label = "어제"
    if not top:
        top = fetch_top_services(client, cfg, today - dt.timedelta(days=7), today)
        top_label = "최근 7일"

    content, embed = build_embed(cfg, used, daily, top, top_label, pct_prev)
    post(cfg, content, embed)


if __name__ == "__main__":
    main()
