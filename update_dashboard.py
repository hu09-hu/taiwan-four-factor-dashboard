"""更新官方資料並產生可由 GitHub Pages 顯示的四因子手機版頁面。"""

from __future__ import annotations

import argparse
import calendar
import html
import json
import math
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


BASE_DIR = Path(__file__).resolve().parent
HISTORY_PATH = BASE_DIR / "history.json"
OUTPUT_PATH = BASE_DIR / "index.html"
TAIPEI = ZoneInfo("Asia/Taipei")
HEADERS = {"User-Agent": "TaiwanFourFactorDashboard/1.0 (GitHub Pages research dashboard)"}
TIMEOUT = 40

SOURCES = {
    "foreign": "https://www.twse.com.tw/rwd/zh/fund/BFI82U",
    "futures": "https://www.taifex.com.tw/cht/3/futContractsDateExcel",
    "fx": "https://www.cbc.gov.tw/tw/lp-645-1-1-20.html",
    "taiex": "https://www.twse.com.tw/indicesReport/MI_5MINS_HIST",
}


def _number(value: str) -> float:
    return float(value.replace(",", "").replace("+", "").strip())


def _roc_date(value: str) -> str:
    year, month, day = (int(part) for part in value.strip().split("/"))
    return f"{year + 1911:04d}-{month:02d}-{day:02d}"


def load_history(path: Path = HISTORY_PATH) -> dict[str, list[dict[str, object]]]:
    if not path.exists():
        return {name: [] for name in SOURCES}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {name: list(payload.get(name, [])) for name in SOURCES}


def save_history(history: dict[str, list[dict[str, object]]], path: Path = HISTORY_PATH) -> None:
    normalized: dict[str, list[dict[str, object]]] = {}
    for name, rows in history.items():
        by_date = {str(row["date"]): row for row in rows}
        normalized[name] = [by_date[key] for key in sorted(by_date)]
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def bootstrap_from_sqlite(database: Path) -> dict[str, list[dict[str, object]]]:
    """由已驗證的本機資料庫建立 GitHub Pages 初始歷史。"""
    result = {name: [] for name in SOURCES}
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT dataset,data_date,record_key,cleaned_value FROM observations ORDER BY data_date"
        ).fetchall()
    for dataset, data_date, record_key, cleaned_text in rows:
        cleaned = json.loads(cleaned_text)
        if dataset == "twse_institutional_flow" and str(record_key).startswith("外資及陸資"):
            result["foreign"].append({"date": data_date, "net_twd": int(cleaned["net_amount_twd"])})
        elif dataset == "taifex_foreign_tx":
            result["futures"].append({"date": data_date, "net_contracts": int(cleaned["open_interest_net_contracts"])})
        elif dataset == "cbc_usdtwd":
            result["fx"].append({"date": data_date, "close": float(cleaned["close"])})
        elif dataset == "twse_taiex":
            result["taiex"].append({"date": data_date, "close": float(cleaned["close"])})
    return result


def _request_json(session: requests.Session, url: str, params: dict[str, str]) -> dict[str, object]:
    response = session.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def fetch_foreign(session: requests.Session, targets: list[date]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    fields_required = {"單位名稱", "買進金額", "賣出金額", "買賣差額"}
    for target in targets:
        payload = _request_json(
            session,
            SOURCES["foreign"],
            {"response": "json", "type": "day", "dayDate": target.strftime("%Y%m%d")},
        )
        if payload.get("stat") != "OK":
            continue
        fields = list(payload.get("fields", []))
        if not fields_required.issubset(fields) or "單位：元" not in str(payload.get("hints", "")):
            raise ValueError("TWSE 法人欄位或單位異常")
        for values in payload.get("data", []):
            raw = dict(zip(fields, values))
            if str(raw.get("單位名稱", "")).startswith("外資及陸資"):
                buy = int(_number(str(raw["買進金額"])))
                sell = int(_number(str(raw["賣出金額"])))
                net = int(_number(str(raw["買賣差額"])))
                if buy - sell != net:
                    raise ValueError("TWSE 外資買賣差額驗算失敗")
                reported = str(payload.get("date", target.strftime("%Y%m%d")))
                rows.append({"date": f"{reported[:4]}-{reported[4:6]}-{reported[6:]}", "net_twd": net})
                break
    return rows


def _parse_taifex(html_text: str) -> dict[str, object]:
    soup = BeautifulSoup(html_text, "lxml")
    page_text = soup.get_text(" ", strip=True)
    if "單位：口數；千元" not in page_text.replace(" ", ""):
        raise ValueError("TAIFEX 單位異常")
    match = re.search(r"日期\s*(\d{4})/(\d{2})/(\d{2})", page_text)
    if not match:
        raise ValueError("TAIFEX 缺少日期")
    product = soup.find(string=lambda value: bool(value and value.strip() == "臺股期貨"))
    if product is None:
        raise ValueError("TAIFEX 找不到臺股期貨")
    row = product.find_parent("tr")
    for _ in range(3):
        if row is not None and "外資" in row.get_text(" ", strip=True):
            break
        row = row.find_next_sibling("tr") if row else None
    cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td")] if row else []
    if len(cells) != 13:
        raise ValueError(f"TAIFEX 外資欄位數異常：{len(cells)}")
    values = [int(_number(value)) for value in cells[1:]]
    long_oi, short_oi, reported_net = values[6], values[8], values[10]
    if long_oi - short_oi != reported_net:
        raise ValueError("TAIFEX 淨部位驗算失敗")
    return {"date": "-".join(match.groups()), "net_contracts": reported_net}


def fetch_futures(session: requests.Session, targets: list[date]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for target in targets:
        response = session.get(
            SOURCES["futures"],
            params={"doQuery": "1", "queryType": "1", "queryDate": target.strftime("%Y/%m/%d")},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        try:
            parsed = _parse_taifex(response.text)
        except ValueError:
            continue
        if parsed["date"] == target.isoformat():
            rows.append(parsed)
    return rows


def fetch_fx(session: requests.Session) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for page in range(1, 5):
        url = f"https://www.cbc.gov.tw/tw/lp-645-1-{page}-20.html"
        response = session.get(url, headers=HEADERS, timeout=TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
        if soup.find(string=lambda value: bool(value and value.strip() == "NTD/USD")) is None:
            raise ValueError("CBC 找不到 NTD/USD 欄位")
        for row in soup.select("table tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            if len(cells) >= 2 and re.fullmatch(r"\d{4}/\d{2}/\d{2}", cells[0]):
                rows.append({"date": cells[0].replace("/", "-"), "close": _number(cells[1])})
    return rows


def fetch_taiex(session: requests.Session, target: date) -> list[dict[str, object]]:
    payload = _request_json(
        session,
        SOURCES["taiex"],
        {"response": "json", "date": target.replace(day=1).strftime("%Y%m%d")},
    )
    fields = list(payload.get("fields", []))
    required = {"日期", "收盤指數"}
    if payload.get("stat") != "OK" or not required.issubset(fields):
        raise ValueError("TWSE 加權指數欄位異常")
    rows = []
    for values in payload.get("data", []):
        raw = dict(zip(fields, values))
        rows.append({"date": _roc_date(str(raw["日期"])), "close": _number(str(raw["收盤指數"]))})
    return rows


def update_official_data(history: dict[str, list[dict[str, object]]]) -> list[str]:
    """更新最近七個曆日；個別來源失敗時保留舊資料並記錄風險。"""
    today = datetime.now(TAIPEI).date()
    targets = [today - timedelta(days=offset) for offset in range(7, -1, -1)]
    messages: list[str] = []
    with requests.Session() as session:
        fetch_jobs = (
            ("foreign", lambda: fetch_foreign(session, targets)),
            ("futures", lambda: fetch_futures(session, targets)),
            ("fx", lambda: fetch_fx(session)),
            ("taiex", lambda: fetch_taiex(session, today)),
        )
        for name, job in fetch_jobs:
            try:
                incoming = job()
                by_date = {str(row["date"]): row for row in history[name]}
                by_date.update({str(row["date"]): row for row in incoming})
                cutoff = (today - timedelta(days=110)).isoformat()
                history[name] = [by_date[key] for key in sorted(by_date) if key >= cutoff]
                messages.append(f"{name}: +{len(incoming)}")
            except Exception as error:  # 個別來源不應破壞舊的有效頁面
                messages.append(f"{name}: 保留舊資料（{type(error).__name__}）")
    return messages


def _rolling_sum(values: list[float], window: int) -> list[float | None]:
    return [None if index + 1 < window else sum(values[index - window + 1 : index + 1]) for index in range(len(values))]


def _change(values: list[float], periods: int, percent: bool = False) -> list[float | None]:
    result: list[float | None] = []
    for index, value in enumerate(values):
        if index < periods or values[index - periods] == 0:
            result.append(None)
        elif percent:
            result.append((value / values[index - periods] - 1.0) * 100.0)
        else:
            result.append(value - values[index - periods])
    return result


def _expanding_score(values: list[float], min_history: int = 20) -> list[float | None]:
    result: list[float | None] = []
    for index, value in enumerate(values):
        sample = values[: index + 1]
        if len(sample) < min_history:
            result.append(None)
            continue
        lower = sum(item < value for item in sample)
        equal = sum(item == value for item in sample)
        result.append(((lower + 0.5 * equal) / len(sample)) * 200.0 - 100.0)
    return result


def _third_wednesday(day: date) -> date:
    month = calendar.monthcalendar(day.year, day.month)
    wednesdays = [week[calendar.WEDNESDAY] for week in month if week[calendar.WEDNESDAY]]
    return date(day.year, day.month, wednesdays[2])


def _settlement_in_window(start: date, end: date) -> date | None:
    cursor = start.replace(day=1)
    while cursor <= end:
        settlement = _third_wednesday(cursor)
        if start <= settlement <= end:
            return settlement
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return None


def build_model(history: dict[str, list[dict[str, object]]]) -> list[dict[str, object]]:
    foreign_dates = [str(row["date"]) for row in history["foreign"]]
    foreign_values = [float(row["net_twd"]) / 100_000_000 for row in history["foreign"]]
    foreign_5d = dict(zip(foreign_dates, _rolling_sum(foreign_values, 5)))

    futures_dates = [str(row["date"]) for row in history["futures"]]
    net_short = [max(-float(row["net_contracts"]), 0.0) for row in history["futures"]]
    futures_change = dict(zip(futures_dates, _change(net_short, 5)))
    net_short_by_date = dict(zip(futures_dates, net_short))
    settlement_by_date: dict[str, str | None] = {}
    for index, value in enumerate(futures_dates):
        settlement = None
        if index >= 5:
            settlement = _settlement_in_window(date.fromisoformat(futures_dates[index - 5]), date.fromisoformat(value))
        settlement_by_date[value] = settlement.isoformat() if settlement else None

    fx_dates = [str(row["date"]) for row in history["fx"]]
    fx_values = [float(row["close"]) for row in history["fx"]]
    fx_change = dict(zip(fx_dates, _change(fx_values, 5, percent=True)))

    taiex_dates = [str(row["date"]) for row in history["taiex"]]
    taiex_values = [float(row["close"]) for row in history["taiex"]]
    taiex_change = dict(zip(taiex_dates, _change(taiex_values, 5, percent=True)))

    common = sorted(set(foreign_dates) & set(futures_dates) & set(fx_dates) & set(taiex_dates))
    raw = []
    for value in common:
        fields = (foreign_5d.get(value), futures_change.get(value), fx_change.get(value), taiex_change.get(value))
        if any(item is None or not math.isfinite(float(item)) for item in fields):
            continue
        raw.append(
            {
                "date": value,
                "foreign_5d": float(fields[0]),
                "net_short": int(net_short_by_date[value]),
                "net_short_change_5d": int(fields[1]),
                "fx_change_5d": float(fields[2]),
                "taiex_return_5d": float(fields[3]),
                "settlement_date": settlement_by_date[value],
            }
        )
    if len(raw) < 20:
        raise ValueError("四因子共同有效資料不足 20 日")

    factor_inputs = {
        "foreign_score": [row["foreign_5d"] for row in raw],
        "futures_score": [row["net_short_change_5d"] for row in raw],
        "fx_score": [row["fx_change_5d"] for row in raw],
        "taiex_score": [row["taiex_return_5d"] for row in raw],
    }
    factor_scores = {name: _expanding_score(values) for name, values in factor_inputs.items()}
    for index, row in enumerate(raw):
        scores = {
            "foreign_score": factor_scores["foreign_score"][index],
            "futures_score": None if factor_scores["futures_score"][index] is None else -factor_scores["futures_score"][index],
            "fx_score": None if factor_scores["fx_score"][index] is None else -factor_scores["fx_score"][index],
            "taiex_score": factor_scores["taiex_score"][index],
        }
        row.update(scores)
        row["score"] = None if any(value is None for value in scores.values()) else sum(float(value) for value in scores.values()) / 4.0
    return [row for row in raw if row["score"] is not None]


def _state(score: float) -> str:
    if score >= 35:
        return "資金面強勢偏多"
    if score >= 15:
        return "資金面偏多"
    if score > -15:
        return "訊號分歧／中性"
    if score > -35:
        return "資金面偏空"
    return "資金壓力明顯偏空"


def render_html(model: list[dict[str, object]], messages: list[str]) -> str:
    latest = model[-1]
    dates = [row["date"] for row in model]
    series = {
        "外資現貨": [round(float(row["foreign_score"]), 2) for row in model],
        "淨空增加": [round(float(row["futures_score"]), 2) for row in model],
        "匯率": [round(float(row["fx_score"]), 2) for row in model],
        "大盤指數": [round(float(row["taiex_score"]), 2) for row in model],
    }
    totals = [round(float(row["score"]), 2) for row in model]
    settlement_dates = sorted({str(row["settlement_date"]) for row in model if row["settlement_date"]})
    warning = ""
    if latest["settlement_date"]:
        warning = (
            f'<div class="warning">⚠ 近5日區間包含 {html.escape(str(latest["settlement_date"]))} 結算日；'
            "口數可能同時受契約到期與轉倉影響。</div>"
        )
    updated_at = datetime.now(TAIPEI).strftime("%Y-%m-%d %H:%M（台北）")
    state = _state(float(latest["score"]))
    data_json = json.dumps(
        {"dates": dates, "series": series, "totals": totals, "settlements": settlement_dates},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="theme-color" content="#07111f">
  <title>台股四因子資金壓力模型</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    :root {{ color-scheme: dark; --bg:#07111f; --card:#0f1f33; --line:#263b59; --text:#e6eef8; --muted:#9fb0c5; --green:#10b981; --red:#ef4444; --blue:#38bdf8; --yellow:#f59e0b; --violet:#a78bfa; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:linear-gradient(145deg,#07111f,#0b1728 55%,#101b2d); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ width:min(1100px,100%); margin:auto; padding:20px 16px 40px; }}
    h1 {{ margin:4px 0 6px; font-size:clamp(24px,6vw,42px); font-weight:600; }}
    .sub,.updated,.foot {{ color:var(--muted); }}
    .updated {{ margin:8px 0 18px; font-size:13px; }}
    .grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin:14px 0; }}
    .factors {{ grid-template-columns:repeat(4,minmax(0,1fr)); }}
    .card {{ min-width:0; background:rgba(15,31,51,.88); border:1px solid var(--line); border-radius:14px; padding:15px; }}
    .label {{ color:var(--muted); font-size:13px; margin-bottom:6px; }}
    .value {{ font-size:clamp(20px,5vw,31px); font-weight:600; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }}
    .detail {{ color:var(--muted); margin-top:5px; font-size:12px; }}
    .warning {{ margin:12px 0; padding:12px 14px; border-radius:12px; background:rgba(245,158,11,.12); border:1px solid rgba(245,158,11,.45); }}
    .chart {{ width:100%; min-height:410px; margin:14px 0 22px; }}
    .foot {{ font-size:12px; line-height:1.6; border-top:1px solid var(--line); padding-top:14px; }}
    .foot a {{ color:var(--blue); }}
    @media (max-width:720px) {{ .grid,.factors {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .chart {{ min-height:360px; }} }}
    @media (max-width:380px) {{ main {{ padding-left:11px; padding-right:11px; }} .grid,.factors {{ gap:8px; }} .card {{ padding:12px; }} }}
  </style>
</head>
<body>
<main>
  <h1>台股四因子資金壓力模型</h1>
  <div class="sub">外資現貨 × 外資近5日淨空增加 × USD/TWD × 加權指數</div>
  <div class="updated">最新共同交易日 {latest['date']}｜更新 {updated_at}</div>
  <section class="grid" aria-label="模型摘要">
    <div class="card"><div class="label">四因子總分</div><div class="value">{float(latest['score']):+.1f}</div><div class="detail">範圍 -100～+100</div></div>
    <div class="card"><div class="label">模型判定</div><div class="value">{html.escape(state)}</div></div>
    <div class="card"><div class="label">目前淨空水位</div><div class="value">{int(latest['net_short']):,}口</div><div class="detail">水位不參與計分</div></div>
  </section>
  <section class="grid factors" aria-label="四因子原始值">
    <div class="card"><div class="label">外資近5日</div><div class="value">{float(latest['foreign_5d']):+,.1f}億</div></div>
    <div class="card"><div class="label">近5日淨空增加</div><div class="value">{int(latest['net_short_change_5d']):+,}口</div></div>
    <div class="card"><div class="label">USD/TWD近5日</div><div class="value">{float(latest['fx_change_5d']):+.2f}%</div><div class="detail">上升＝新台幣貶值</div></div>
    <div class="card"><div class="label">大盤近5日</div><div class="value">{float(latest['taiex_return_5d']):+.2f}%</div></div>
  </section>
  {warning}
  <div id="factor-chart" class="chart" role="img" aria-label="四因子分數趨勢折線圖"></div>
  <div id="score-chart" class="chart" role="img" aria-label="四因子總分歷史折線圖"></div>
  <div class="foot">
    四項各25%，採只使用當日以前資料的擴張百分位；缺值不補0。正值為資金順風，負值為資金壓力。資料來源：
    <a href="{SOURCES['foreign']}">臺灣證券交易所法人</a>、
    <a href="{SOURCES['futures']}">臺灣期貨交易所</a>、
    <a href="{SOURCES['fx']}">中央銀行匯率</a>、
    <a href="{SOURCES['taiex']}">加權指數</a>。資料僅供研究，不構成投資建議。
  </div>
</main>
<script>
const data={data_json};
const colors=['#38bdf8','#ef4444','#f59e0b','#a78bfa'];
const symbols=['circle','square','diamond','triangle-up'];
const factorTraces=Object.entries(data.series).map(([name,values],i)=>({{x:data.dates,y:values,name,type:'scatter',mode:'lines+markers',line:{{color:colors[i],width:2}},marker:{{symbol:symbols[i],size:5}},hovertemplate:name+'<br>%{{x}}<br>%{{y:+.1f}}<extra></extra>'}}));
const settlementShapes=data.settlements.map(day=>({{type:'line',x0:day,x1:day,y0:0,y1:1,yref:'paper',line:{{color:'#64748b',width:1,dash:'dot'}}}}));
const base={{paper_bgcolor:'rgba(0,0,0,0)',plot_bgcolor:'rgba(0,0,0,0)',font:{{color:'#dbe7f5'}},margin:{{l:48,r:12,t:68,b:45}},hovermode:'x unified',xaxis:{{gridcolor:'#20314a'}},yaxis:{{range:[-105,105],gridcolor:'#20314a',zerolinecolor:'#64748b'}},legend:{{orientation:'h',y:1.16,x:0}}}};
Plotly.newPlot('factor-chart',factorTraces,{{...base,title:{{text:'四因子分數趨勢',x:0}},shapes:settlementShapes}},{{responsive:true,displaylogo:false}});
Plotly.newPlot('score-chart',[{{x:data.dates,y:data.totals,name:'總分',type:'scatter',mode:'lines+markers',line:{{color:'#38bdf8',width:3}},marker:{{size:5}},hovertemplate:'%{{x}}<br>總分 %{{y:+.1f}}<extra></extra>'}}],{{...base,title:{{text:'四因子總分歷史',x:0}},showlegend:false}},{{responsive:true,displaylogo:false}});
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap-db", type=Path)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    history = bootstrap_from_sqlite(args.bootstrap_db) if args.bootstrap_db else load_history()
    messages = ["offline"] if args.offline else update_official_data(history)
    save_history(history)
    model = build_model(history)
    OUTPUT_PATH.write_text(render_html(model, messages), encoding="utf-8")
    latest = model[-1]
    print(
        json.dumps(
            {
                "latest_date": latest["date"],
                "score": round(float(latest["score"]), 2),
                "net_short": latest["net_short"],
                "net_short_change_5d": latest["net_short_change_5d"],
                "sources": messages,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
