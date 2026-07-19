"""更新官方資料並產生 GitHub Pages 完整台股監控儀表板。"""

from __future__ import annotations

import argparse
import calendar
import json
import math
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
HISTORY_PATH = BASE_DIR / "history.json"
OUTPUT_PATH = BASE_DIR / "index.html"
TAIPEI = ZoneInfo("Asia/Taipei")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Cache-Control": "no-cache",
}
TIMEOUT = 40
DATASETS = ("foreign", "institutions", "futures", "fx", "taiex", "tsmc")
SOURCES = {
    "foreign": "https://www.twse.com.tw/rwd/zh/fund/BFI82U",
    "futures": "https://www.taifex.com.tw/cht/3/futContractsDateExcel",
    "fx": "https://www.cbc.gov.tw/tw/lp-645-1-1-20.html",
    "taiex": "https://www.twse.com.tw/indicesReport/MI_5MINS_HIST",
    "tsmc": "https://www.twse.com.tw/exchangeReport/STOCK_DAY",
}
FOREIGN_FETCH_URL = "https://www.twse.com.tw/rwd/zh/fund/BFI82U"
BACKFILL_BATCH = 70


def number(value: object) -> float:
    return float(str(value).replace(",", "").replace("+", "").strip())


def roc_date(value: str) -> str:
    year, month, day = (int(part) for part in value.strip().split("/"))
    return f"{year + 1911:04d}-{month:02d}-{day:02d}"


def load_history(path: Path = HISTORY_PATH) -> dict[str, list[dict[str, object]]]:
    payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {name: list(payload.get(name, [])) for name in DATASETS}


def save_history(history: dict[str, list[dict[str, object]]], path: Path = HISTORY_PATH) -> None:
    normalized = {}
    for name in DATASETS:
        rows = history.get(name, [])
        by_key = {(str(row["date"]), str(row.get("name", ""))): row for row in rows}
        normalized[name] = [by_key[key] for key in sorted(by_key)]
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def bootstrap_from_sqlite(database: Path) -> dict[str, list[dict[str, object]]]:
    result = {name: [] for name in DATASETS}
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT dataset,data_date,record_key,cleaned_value FROM observations ORDER BY data_date,record_key"
        ).fetchall()
    for dataset, day, key, cleaned_text in rows:
        item = json.loads(cleaned_text)
        if dataset == "twse_institutional_flow":
            result["institutions"].append({"date": day, "name": key, "net_twd": int(item["net_amount_twd"])})
            if str(key).startswith("外資及陸資"):
                result["foreign"].append({"date": day, "net_twd": int(item["net_amount_twd"])})
        elif dataset == "taifex_foreign_tx":
            result["futures"].append({
                "date": day, "long": int(item["open_interest_long_contracts"]),
                "short": int(item["open_interest_short_contracts"]),
                "net_contracts": int(item["open_interest_net_contracts"]),
            })
        elif dataset == "cbc_usdtwd":
            result["fx"].append({"date": day, "close": float(item["close"])})
        elif dataset in {"twse_taiex", "twse_tsmc_price"}:
            name = "taiex" if dataset == "twse_taiex" else "tsmc"
            result[name].append({
                "date": day, "open": float(item["open"]), "high": float(item["high"]),
                "low": float(item["low"]), "close": float(item["close"]),
                "volume": int(item.get("volume_shares", 0)),
            })
    return result


def get_json(session: requests.Session, url: str, params: dict[str, str]) -> dict[str, object]:
    response = session.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def fetch_institutions(session: requests.Session, targets: list[date]) -> tuple[list[dict], list[dict]]:
    institutions, foreign = [], []
    required = {"單位名稱", "買進金額", "賣出金額", "買賣差額"}
    for target in targets:
        try:
            day_text = target.strftime("%Y%m%d")
            payload = get_json(session, FOREIGN_FETCH_URL, {
                "response": "json",
                "type": "day",
                "dayDate": day_text,
                "weekDate": day_text,
                "monthDate": day_text,
                "_": day_text,
            })
        except requests.RequestException:
            time.sleep(0.25)
            continue
        if payload.get("stat") != "OK":
            time.sleep(0.08)
            continue
        fields = list(payload.get("fields", []))
        if not required.issubset(fields) or "單位：元" not in str(payload.get("hints", "")):
            raise ValueError("TWSE 法人欄位或單位異常")
        reported = str(payload.get("date", target.strftime("%Y%m%d")))
        day = f"{reported[:4]}-{reported[4:6]}-{reported[6:]}"
        for values in payload.get("data", []):
            raw = dict(zip(fields, values))
            buy, sell, net = (int(number(raw[field])) for field in ("買進金額", "賣出金額", "買賣差額"))
            if buy - sell != net:
                raise ValueError("TWSE 法人買賣差額驗算失敗")
            name = str(raw["單位名稱"])
            institutions.append({"date": day, "name": name, "net_twd": net})
            if name.startswith("外資及陸資"):
                foreign.append({"date": day, "net_twd": net})
        time.sleep(0.08)
    return institutions, foreign


def parse_taifex(text: str) -> dict[str, object]:
    soup = BeautifulSoup(text, "lxml")
    page_text = soup.get_text(" ", strip=True)
    if "單位：口數；千元" not in page_text.replace(" ", ""):
        raise ValueError("TAIFEX 單位異常")
    matched = re.search(r"日期\s*(\d{4})/(\d{2})/(\d{2})", page_text)
    product = soup.find(string=lambda value: bool(value and value.strip() == "臺股期貨"))
    if not matched or product is None:
        raise ValueError("TAIFEX 日期或商品欄位異常")
    row = product.find_parent("tr")
    for _ in range(3):
        if row is not None and "外資" in row.get_text(" ", strip=True):
            break
        row = row.find_next_sibling("tr") if row else None
    cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td")] if row else []
    if len(cells) != 13:
        raise ValueError("TAIFEX 外資欄位數異常")
    values = [int(number(value)) for value in cells[1:]]
    long_oi, short_oi, reported_net = values[6], values[8], values[10]
    if long_oi - short_oi != reported_net:
        raise ValueError("TAIFEX 淨部位驗算失敗")
    return {"date": "-".join(matched.groups()), "long": long_oi, "short": short_oi, "net_contracts": reported_net}


def fetch_futures(session: requests.Session, targets: list[date]) -> list[dict]:
    rows = []
    for target in targets:
        try:
            response = session.get(SOURCES["futures"], params={
                "doQuery": "1", "queryType": "1", "queryDate": target.strftime("%Y/%m/%d")
            }, headers=HEADERS, timeout=TIMEOUT)
            response.raise_for_status()
            row = parse_taifex(response.text)
        except (requests.RequestException, ValueError):
            time.sleep(0.25)
            continue
        if row["date"] == target.isoformat():
            rows.append(row)
        time.sleep(0.12)
    return rows


def fetch_fx(session: requests.Session) -> list[dict]:
    rows = []
    for page in range(1, 14):
        response = session.get(f"https://www.cbc.gov.tw/tw/lp-645-1-{page}-20.html", headers=HEADERS, timeout=TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
        if soup.find(string=lambda value: bool(value and value.strip() == "NTD/USD")) is None:
            raise ValueError("CBC 匯率欄位異常")
        for tr in soup.select("table tr"):
            cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["th", "td"])]
            if len(cells) >= 2 and re.fullmatch(r"\d{4}/\d{2}/\d{2}", cells[0]):
                rows.append({"date": cells[0].replace("/", "-"), "close": number(cells[1])})
    return rows


def fetch_market_month(session: requests.Session, target: date, dataset: str) -> list[dict]:
    if dataset == "taiex":
        payload = get_json(session, SOURCES["taiex"], {"response": "json", "date": target.replace(day=1).strftime("%Y%m%d")})
        mapping = {"日期": "date", "開盤指數": "open", "最高指數": "high", "最低指數": "low", "收盤指數": "close"}
    else:
        payload = get_json(session, SOURCES["tsmc"], {
            "response": "json", "stockNo": "2330", "date": target.replace(day=1).strftime("%Y%m%d")
        })
        mapping = {"日期": "date", "開盤價": "open", "最高價": "high", "最低價": "low", "收盤價": "close", "成交股數": "volume"}
    fields = list(payload.get("fields", []))
    if payload.get("stat") != "OK" or not set(mapping).issubset(fields):
        raise ValueError(f"TWSE {dataset} 欄位異常")
    rows = []
    for values in payload.get("data", []):
        raw = dict(zip(fields, values))
        row = {alias: (roc_date(str(raw[field])) if alias == "date" else number(raw[field])) for field, alias in mapping.items()}
        if "volume" in row:
            row["volume"] = int(row["volume"])
        rows.append(row)
    return rows


def merge_rows(old: list[dict], incoming: list[dict], today: date) -> list[dict]:
    by_key = {(str(row["date"]), str(row.get("name", ""))): row for row in old}
    by_key.update({(str(row["date"]), str(row.get("name", ""))): row for row in incoming})
    month = today.month - 12
    year = today.year
    if month <= 0:
        month += 12
        year -= 1
    cutoff_day = min(today.day, calendar.monthrange(year, month)[1])
    cutoff = date(year, month, cutoff_day).isoformat()
    return [by_key[key] for key in sorted(by_key) if key[0] >= cutoff]


def update_official_data(history: dict[str, list[dict]]) -> list[str]:
    today = datetime.now(TAIPEI).date()
    cutoff = date(today.year - 1, today.month, min(today.day, calendar.monthrange(today.year - 1, today.month)[1]))
    weekdays = []
    cursor = cutoff
    while cursor <= today:
        if cursor.weekday() < 5:
            weekdays.append(cursor)
        cursor += timedelta(days=1)
    recent = [today - timedelta(days=offset) for offset in range(8, -1, -1)]
    messages = []
    with requests.Session() as session:
        try:
            existing_foreign = {str(row["date"]) for row in history["foreign"]}
            missing = [day for day in weekdays if day.isoformat() not in existing_foreign][:BACKFILL_BATCH]
            targets = sorted(set(recent + missing))
            institutions, foreign = fetch_institutions(session, targets)
            history["institutions"] = merge_rows(history["institutions"], institutions, today)
            history["foreign"] = merge_rows(history["foreign"], foreign, today)
            messages.append(f"institutions:+{len(institutions)}")
        except Exception as error:
            messages.append(f"institutions:保留舊資料({type(error).__name__})")
        jobs = {
            "futures": lambda: fetch_futures(
                session,
                sorted(set(recent + [day for day in weekdays if day.isoformat() not in {str(row['date']) for row in history['futures']}][:BACKFILL_BATCH]))
            ),
            "fx": lambda: fetch_fx(session),
            "taiex": lambda: sum(
                (fetch_market_month(session, (today.replace(day=1) - timedelta(days=offset * 28)).replace(day=1), "taiex") for offset in range(13)), []
            ),
            "tsmc": lambda: sum(
                (fetch_market_month(session, (today.replace(day=1) - timedelta(days=offset * 28)).replace(day=1), "tsmc") for offset in range(13)), []
            ),
        }
        for name, job in jobs.items():
            try:
                incoming = job()
                history[name] = merge_rows(history[name], incoming, today)
                messages.append(f"{name}:+{len(incoming)}")
            except Exception as error:
                messages.append(f"{name}:保留舊資料({type(error).__name__})")
    return messages


def rolling_sum(values: list[float], window: int) -> list[float | None]:
    return [None if i + 1 < window else sum(values[i - window + 1:i + 1]) for i in range(len(values))]


def change(values: list[float], periods: int, percent: bool = False) -> list[float | None]:
    result = []
    for i, value in enumerate(values):
        if i < periods or values[i - periods] == 0:
            result.append(None)
        else:
            delta = value / values[i - periods] - 1 if percent else value - values[i - periods]
            result.append(delta * 100 if percent else delta)
    return result


def expanding_score(values: list[float], minimum: int = 20) -> list[float | None]:
    result = []
    for i, value in enumerate(values):
        sample = values[:i + 1]
        if len(sample) < minimum:
            result.append(None)
        else:
            lower = sum(item < value for item in sample)
            equal = sum(item == value for item in sample)
            result.append(((lower + .5 * equal) / len(sample)) * 200 - 100)
    return result


def third_wednesday(day: date) -> date:
    days = [week[calendar.WEDNESDAY] for week in calendar.monthcalendar(day.year, day.month) if week[calendar.WEDNESDAY]]
    return date(day.year, day.month, days[2])


def settlement_in_window(start: date, end: date) -> date | None:
    cursor = start.replace(day=1)
    while cursor <= end:
        settlement = third_wednesday(cursor)
        if start <= settlement <= end:
            return settlement
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return None


def build_model(history: dict[str, list[dict]]) -> list[dict]:
    foreign_dates = [str(row["date"]) for row in history["foreign"]]
    foreign_values = [float(row["net_twd"]) / 100_000_000 for row in history["foreign"]]
    foreign_5d = dict(zip(foreign_dates, rolling_sum(foreign_values, 5)))
    futures_dates = [str(row["date"]) for row in history["futures"]]
    net_short = [max(-float(row["net_contracts"]), 0) for row in history["futures"]]
    short_change = dict(zip(futures_dates, change(net_short, 5)))
    short_level = dict(zip(futures_dates, net_short))
    fx_dates = [str(row["date"]) for row in history["fx"]]
    fx_change = dict(zip(fx_dates, change([float(row["close"]) for row in history["fx"]], 5, True)))
    taiex_dates = [str(row["date"]) for row in history["taiex"]]
    taiex_change = dict(zip(taiex_dates, change([float(row["close"]) for row in history["taiex"]], 5, True)))
    common = sorted(set(foreign_dates) & set(futures_dates) & set(fx_dates) & set(taiex_dates))
    raw = []
    for value in common:
        fields = (foreign_5d.get(value), short_change.get(value), fx_change.get(value), taiex_change.get(value))
        if any(item is None or not math.isfinite(float(item)) for item in fields):
            continue
        index = futures_dates.index(value)
        settlement = settlement_in_window(date.fromisoformat(futures_dates[index - 5]), date.fromisoformat(value)) if index >= 5 else None
        raw.append({"date": value, "foreign_5d": float(fields[0]), "net_short": int(short_level[value]),
                    "net_short_change_5d": int(fields[1]), "fx_change_5d": float(fields[2]),
                    "taiex_return_5d": float(fields[3]), "settlement_date": settlement.isoformat() if settlement else None})
    if len(raw) < 20:
        raise ValueError("四因子共同有效資料不足20日")
    columns = ("foreign_5d", "net_short_change_5d", "fx_change_5d", "taiex_return_5d")
    scores = [expanding_score([float(row[column]) for row in raw]) for column in columns]
    for i, row in enumerate(raw):
        values = [scores[0][i], None if scores[1][i] is None else -scores[1][i],
                  None if scores[2][i] is None else -scores[2][i], scores[3][i]]
        row["factor_scores"] = values
        row["score"] = None if any(value is None for value in values) else sum(float(value) for value in values) / 4
    return [row for row in raw if row["score"] is not None]


def state(score: float) -> str:
    return "資金面強勢偏多" if score >= 35 else "資金面偏多" if score >= 15 else "訊號分歧／中性" if score > -15 else "資金面偏空" if score > -35 else "資金壓力明顯偏空"


def render_html(history: dict[str, list[dict]], model: list[dict], messages: list[str]) -> str:
    latest = model[-1]
    payload = {"history": history, "model": model, "messages": messages}
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    updated = datetime.now(TAIPEI).strftime("%Y-%m-%d %H:%M")
    warning = f"⚠ 近5日區間包含 {latest['settlement_date']} 結算日；口數可能同時受到到期與轉倉影響。" if latest["settlement_date"] else ""
    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#07111f"><title>台股資金面與籌碼面監控</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
:root{{--bg:#07111f;--panel:#0f1f33;--line:#263b59;--text:#e6eef8;--muted:#9fb0c5;--green:#10b981;--red:#ef4444;--blue:#38bdf8;--yellow:#f59e0b;--violet:#a78bfa}}
*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(145deg,#07111f,#0b1728 55%,#101b2d);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
.shell{{display:grid;grid-template-columns:230px minmax(0,1fr);min-height:100vh}}aside{{padding:25px 15px;border-right:1px solid var(--line);background:#081321}}aside h2{{margin:0 0 4px}}.muted,.detail,.updated,.foot{{color:var(--muted)}}nav{{display:grid;gap:5px;margin-top:22px}}.nav{{text-align:left;border:0;border-radius:10px;padding:10px 12px;color:var(--text);background:transparent;font-size:15px;cursor:pointer}}.nav:hover,.nav.active{{background:#132842;color:#fff}}main{{width:min(1180px,100%);padding:24px clamp(14px,3vw,36px) 50px}}h1{{font-size:clamp(26px,5vw,40px);margin:0 0 5px}}h2{{margin-top:28px}}.mobile-nav{{display:none;width:100%;margin-bottom:18px;padding:11px;border:1px solid var(--line);border-radius:10px;background:var(--panel);color:var(--text)}}.page{{display:none}}.page.active{{display:block}}.grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:16px 0}}.grid.three{{grid-template-columns:repeat(3,minmax(0,1fr))}}.card{{min-width:0;background:rgba(15,31,51,.88);border:1px solid var(--line);border-radius:14px;padding:15px}}.label{{color:var(--muted);font-size:13px;margin-bottom:6px}}.value{{font-size:clamp(19px,3vw,29px);font-weight:600;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}}.detail{{font-size:12px;margin-top:5px}}.banner,.warning{{margin:14px 0;padding:13px 15px;border-radius:12px;border:1px solid var(--line);background:rgba(15,31,51,.75)}}.warning{{border-color:rgba(245,158,11,.5);background:rgba(245,158,11,.1)}}.chart-heading{{margin:30px 0 0;font-size:20px;font-weight:600}}.chart{{width:100%;min-height:390px;margin:0 0 24px}}.foot{{font-size:12px;line-height:1.7;border-top:1px solid var(--line);padding-top:14px;margin-top:25px}}a{{color:var(--blue)}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:10px;border-bottom:1px solid var(--line)}}th{{color:var(--muted)}}.empty{{padding:26px;border:1px dashed var(--line);border-radius:12px;color:var(--muted)}}@media(max-width:780px){{.shell{{display:block}}aside{{display:none}}main{{padding-top:16px}}.mobile-nav{{display:block}}.grid,.grid.three{{grid-template-columns:repeat(2,minmax(0,1fr))}}.chart-heading{{font-size:18px;margin-top:26px}}.chart{{min-height:360px}}}}@media(max-width:360px){{.grid,.grid.three{{grid-template-columns:1fr}}}}
</style></head><body><div class="shell"><aside><h2>📊 台股監控</h2><div class="muted">資金面 × 籌碼面</div><nav id="desktop-nav"></nav></aside><main>
<select id="mobile-nav" class="mobile-nav" aria-label="選擇分析頁面"></select>
<section class="page" data-page="overview"><h1>台股資金面與籌碼面監控</h1><div class="updated">最新共同交易日 {latest['date']}｜更新 {updated}（台北）</div><div id="overview-cards" class="grid"></div><div id="overview-banner" class="banner"></div><h2>今日市場摘要</h2><div id="overview-summary" class="grid three"></div></section>
<section class="page" data-page="model"><h1>四因子資金壓力模型</h1><div class="muted">外資現貨 × 近5日外資淨空增加 × USD/TWD × 加權指數</div><div id="model-cards" class="grid"></div>{f'<div class="warning">{warning}</div>' if warning else ''}<div id="factor-chart" class="chart"></div><div id="score-chart" class="chart"></div></section>
<section class="page" data-page="foreign"><h1>外資資金流</h1><div id="foreign-cards" class="grid"></div><div id="foreign-chart" class="chart"></div><div id="institution-chart" class="chart"></div><div id="foreign-fx-chart" class="chart"></div></section>
<section class="page" data-page="futures"><h1>期貨籌碼</h1><div id="futures-cards" class="grid"></div><div id="futures-chart" class="chart"></div><div id="futures-change-chart" class="chart"></div><div class="banner">外資淨未平倉＝外資多方未平倉口數－外資空方未平倉口數；負值標示為淨空。</div></section>
<section class="page" data-page="fx"><h1>匯率監控</h1><div id="fx-cards" class="grid three"></div><div id="fx-chart" class="chart"></div><div id="fx-market-chart" class="chart"></div><div class="banner">USD/TWD 上升＝新臺幣貶值；USD/TWD 下降＝新臺幣升值。</div></section>
<section class="page" data-page="market"><h1>台積電與大盤</h1><div id="market-cards" class="grid"></div><div id="tsmc-chart" class="chart"></div><div id="taiex-chart" class="chart"></div><div id="relative-chart" class="chart"></div></section>
<section class="page" data-page="backtest"><h1>歷史回測</h1><div id="backtest-cards" class="grid three"></div><div id="backtest-chart" class="chart"></div><div class="banner">訊號只使用當日及過去資料；未來5日報酬僅作事後驗證，不回填模型評分。</div></section>
<section class="page" data-page="quality"><h1>資料品質</h1><div id="quality-table"></div><h2>更新紀錄</h2><div id="quality-messages" class="banner"></div><div class="banner">假日與缺值不填0；金額以元保存、畫面換算億元；所有序列依日期升冪排序。</div></section>
<div class="foot">資料來源：<a href="{SOURCES['foreign']}">臺灣證券交易所法人</a>、<a href="{SOURCES['futures']}">臺灣期貨交易所</a>、<a href="{SOURCES['fx']}">中央銀行匯率</a>、<a href="{SOURCES['tsmc']}">台積電日成交</a>、<a href="{SOURCES['taiex']}">加權指數</a>。資料僅供研究，不構成投資建議。</div>
</main></div><script>
const D={data_json};const pages=[['overview','市場總覽'],['model','四因子模型'],['foreign','外資資金流'],['futures','期貨籌碼'],['fx','匯率監控'],['market','台積電與大盤'],['backtest','歷史回測'],['quality','資料品質']];
const C={{blue:'#38bdf8',red:'#ef4444',green:'#10b981',yellow:'#f59e0b',violet:'#a78bfa',muted:'#94a3b8'}};
const fmt=(n,d=1)=>(n==null||!Number.isFinite(+n)?'—':(+n).toLocaleString('zh-TW',{{minimumFractionDigits:d,maximumFractionDigits:d}}));const signed=(n,d=1)=>(+n>=0?'+':'')+fmt(n,d);
const card=(label,value,detail='')=>`<div class="card"><div class="label">${{label}}</div><div class="value">${{value}}</div>${{detail?`<div class="detail">${{detail}}</div>`:''}}</div>`;
const vals=(rows,key)=>rows.map(r=>+r[key]);const dates=rows=>rows.map(r=>r.date);const last=rows=>rows[rows.length-1];const sum=(a)=>a.reduce((x,y)=>x+(+y||0),0);const pct=(a,n)=>a.length>n?(last(a)/a[a.length-1-n]-1)*100:null;
const ma=(a,n)=>a.map((_,i)=>i+1<n?null:sum(a.slice(i-n+1,i+1))/n);const diff=(a,n=1)=>a.map((v,i)=>i<n?null:v-a[i-n]);const billion=n=>+n/1e8;
const layout=(y='')=>({{paper_bgcolor:'rgba(0,0,0,0)',plot_bgcolor:'rgba(0,0,0,0)',font:{{color:'#dbe7f5'}},margin:{{l:52,r:18,t:58,b:48}},hovermode:'x unified',xaxis:{{gridcolor:'#20314a'}},yaxis:{{title:y,gridcolor:'#20314a',zerolinecolor:'#64748b'}},legend:{{orientation:'h',x:0,y:1.12,xanchor:'left',yanchor:'bottom'}}}});
const plot=(id,traces,title,y)=>{{const node=document.getElementById(id);const heading=document.createElement('h2');heading.className='chart-heading';heading.textContent=title;node.before(heading);node.setAttribute('aria-label',title);return Plotly.newPlot(id,traces,layout(y),{{responsive:true,displaylogo:false}})}};
const H=D.history,M=D.model,F=H.foreign,U=H.futures,X=H.fx,I=H.taiex,T=H.tsmc,N=H.institutions,L=last(M);
const f5=sum(F.slice(-5).map(r=>billion(r.net_twd))),fx5=pct(vals(X,'close'),5),i5=pct(vals(I,'close'),5);
document.getElementById('overview-cards').innerHTML=card('四因子總分',signed(L.score,1),stateText(L.score))+card('USD/TWD',fmt(last(X).close,3),(fx5>0?'新臺幣貶值 ':'新臺幣升值 ')+signed(fx5,2)+'%')+card('外資最新買賣超',signed(billion(last(F).net_twd),1)+' 億')+card('外資期貨淨部位',signed(last(U).net_contracts,0)+' 口',last(U).net_contracts<0?'淨空':'淨多');
function stateText(s){{return s>=35?'資金面強勢偏多':s>=15?'資金面偏多':s>-15?'訊號分歧／中性':s>-35?'資金面偏空':'資金壓力明顯偏空'}}
document.getElementById('overview-banner').innerHTML=`<b>目前狀態：${{stateText(L.score)}}</b><br><span class="muted">外資近5日 ${{signed(f5,1)}} 億、淨空增加 ${{signed(L.net_short_change_5d,0)}} 口、USD/TWD ${{signed(fx5,2)}}%、大盤 ${{signed(i5,2)}}%。</span>`;
document.getElementById('overview-summary').innerHTML=card('正面因素',i5>0?'大盤近5日上漲':'暫無主要正面訊號')+card('負面因素',[f5<0?'外資現貨賣超':'',L.net_short_change_5d>0?'外資淨空增加':'',fx5>0?'新臺幣貶值':''].filter(Boolean).join('、')||'暫無')+card('觀察重點','外資5日累計、淨空變化與匯率是否同步');
document.getElementById('model-cards').innerHTML=card('四因子總分',signed(L.score,1),stateText(L.score))+card('外資近5日',signed(L.foreign_5d,1)+' 億')+card('近5日淨空增加',signed(L.net_short_change_5d,0)+' 口','目前淨空 '+fmt(L.net_short,0)+' 口')+card('USD/TWD近5日',signed(L.fx_change_5d,2)+'%','上升＝新臺幣貶值');
const scoreNames=['外資現貨','淨空增加','匯率','大盤指數'];plot('factor-chart',scoreNames.map((name,i)=>({{x:dates(M),y:M.map(r=>r.factor_scores[i]),name,type:'scatter',mode:'lines',line:{{color:[C.blue,C.red,C.yellow,C.violet][i],width:2}}}})),'四因子分數趨勢','分數');
plot('score-chart',[{{x:dates(M),y:vals(M,'score'),name:'總分',type:'scatter',mode:'lines+markers',line:{{color:C.blue,width:3}}}}],'四因子總分歷史','分數');
const fb=F.map(r=>billion(r.net_twd)),fc=fb.map((_,i)=>sum(fb.slice(0,i+1)));document.getElementById('foreign-cards').innerHTML=card('最新外資買賣超',signed(last(fb),1)+' 億')+card('近5日累計',signed(sum(fb.slice(-5)),1)+' 億')+card('近20日累計',signed(sum(fb.slice(-20)),1)+' 億')+card('一年累計',signed(sum(fb),1)+' 億');
plot('foreign-chart',[{{x:dates(F),y:fb,type:'bar',name:'每日',marker:{{color:fb.map(v=>v>=0?C.green:C.red)}}}},{{x:dates(F),y:fc,type:'scatter',name:'累計',yaxis:'y2',line:{{color:C.blue,width:2}}}}],'外資每日買賣超與一年累計','億元').then(()=>Plotly.relayout('foreign-chart',{{'yaxis2.overlaying':'y','yaxis2.side':'right','yaxis2.title':'累計（億元）'}}));
const inst={{}};N.forEach(r=>(inst[r.name]??=[]).push(r));const wanted=['外資及陸資(不含外資自營商)','投信','自營商(自行買賣)','自營商(避險)','合計'];plot('institution-chart',wanted.filter(k=>inst[k]).map((k,i)=>({{x:dates(inst[k]),y:inst[k].map(r=>billion(r.net_twd)).map((_,j,a)=>sum(a.slice(0,j+1))),name:k,type:'scatter',mode:'lines',line:{{color:[C.blue,C.red,C.yellow,C.green,C.violet][i]}}}})),'三大法人近一年累計買賣超','億元');
const fxBy=Object.fromEntries(X.map(r=>[r.date,r.close]));plot('foreign-fx-chart',[{{x:dates(F),y:fb,name:'外資',type:'bar',marker:{{color:C.violet}}}},{{x:dates(F),y:F.map(r=>fxBy[r.date]??null),name:'USD/TWD',type:'scatter',yaxis:'y2',line:{{color:C.yellow}}}}],'外資買賣超與 USD/TWD','億元').then(()=>Plotly.relayout('foreign-fx-chart',{{'yaxis2.overlaying':'y','yaxis2.side':'right','yaxis2.title':'USD/TWD'}}));
const unet=vals(U,'net_contracts'),ushort=vals(U,'short'),ulong=vals(U,'long');document.getElementById('futures-cards').innerHTML=card('多方未平倉',fmt(last(ulong),0)+' 口')+card('空方未平倉',fmt(last(ushort),0)+' 口')+card('淨未平倉',signed(last(unet),0)+' 口',last(unet)<0?'淨空':'淨多')+card('近5日淨空增加',signed(L.net_short_change_5d,0)+' 口');
plot('futures-chart',[{{x:dates(U),y:ulong,name:'多方',type:'scatter',line:{{color:C.green}}}},{{x:dates(U),y:ushort,name:'空方',type:'scatter',line:{{color:C.red}}}},{{x:dates(U),y:unet,name:'淨部位',type:'scatter',line:{{color:C.blue,width:3}}}}],'外資臺股期貨未平倉','口');
const ud=diff(unet);plot('futures-change-chart',[{{x:dates(U),y:ud,name:'每日變化',type:'bar',marker:{{color:ud.map(v=>v>=0?C.green:C.red)}}}}],'外資淨部位每日增減','口');
const xv=vals(X,'close');document.getElementById('fx-cards').innerHTML=card('最新 USD/TWD',fmt(last(xv),3))+card('當日方向',last(diff(xv))>0?'新臺幣貶值':last(diff(xv))<0?'新臺幣升值':'持平')+card('近5日變化',signed(fx5,2)+'%');
plot('fx-chart',[{{x:dates(X),y:xv,name:'USD/TWD',type:'scatter',line:{{color:C.blue,width:3}}}},...[5,10,20].map((n,i)=>({{x:dates(X),y:ma(xv,n),name:'MA'+n,type:'scatter',line:{{color:[C.green,C.yellow,C.violet][i]}}}}))],'USD/TWD 與移動平均','新臺幣／美元');
const iBy=Object.fromEntries(I.map(r=>[r.date,r.close]));plot('fx-market-chart',[{{x:dates(X),y:xv,name:'USD/TWD',type:'scatter',line:{{color:C.yellow}}}},{{x:dates(X),y:X.map(r=>iBy[r.date]??null),name:'加權指數',type:'scatter',yaxis:'y2',line:{{color:C.blue}}}}],'USD/TWD 與加權指數','USD/TWD').then(()=>Plotly.relayout('fx-market-chart',{{'yaxis2.overlaying':'y','yaxis2.side':'right','yaxis2.title':'加權指數'}}));
const tv=vals(T,'close'),iv=vals(I,'close');document.getElementById('market-cards').innerHTML=card('台積電',fmt(last(tv),2)+' 元',signed(pct(tv,1),2)+'%')+card('加權指數',fmt(last(iv),2),signed(pct(iv,1),2)+'%')+card('台積電20日報酬',signed(pct(tv,20),2)+'%')+card('大盤20日報酬',signed(pct(iv,20),2)+'%');
const priceTraces=(rows,v,name)=>[{{x:dates(rows),y:v,name,type:'scatter',line:{{color:C.blue,width:3}}}},...[5,10,20].map((n,i)=>({{x:dates(rows),y:ma(v,n),name:'MA'+n,type:'scatter',line:{{color:[C.green,C.yellow,C.violet][i]}}}}))];plot('tsmc-chart',priceTraces(T,tv,'台積電'),'台積電收盤與移動平均','元');plot('taiex-chart',priceTraces(I,iv,'加權指數'),'加權指數與移動平均','指數');
const common=dates(T).filter(d=>iBy[d]!=null),tBy=Object.fromEntries(T.map(r=>[r.date,r.close])),tn=common.map(d=>tBy[d]/tBy[common[0]]*100),inorm=common.map(d=>iBy[d]/iBy[common[0]]*100);plot('relative-chart',[{{x:common,y:tn,name:'台積電',type:'scatter',line:{{color:C.violet}}}},{{x:common,y:inorm,name:'加權指數',type:'scatter',line:{{color:C.blue}}}}],'台積電相對大盤強弱（起點＝100）','標準化指數');
const future5=M.map(r=>{{const idx=I.findIndex(x=>x.date===r.date);return idx>=0&&idx+5<I.length?(I[idx+5].close/I[idx].close-1)*100:null}});const pairs=M.map((r,i)=>[r.score,future5[i]]).filter(x=>x[1]!=null);const hit=pairs.filter(([s,y])=>(s>=0&&y>=0)||(s<0&&y<0)).length/pairs.length*100;const corr=(()=>{{const a=pairs.map(x=>x[0]),b=pairs.map(x=>x[1]),am=sum(a)/a.length,bm=sum(b)/b.length;return sum(a.map((x,i)=>(x-am)*(b[i]-bm)))/Math.sqrt(sum(a.map(x=>(x-am)**2))*sum(b.map(x=>(x-bm)**2)))}})();document.getElementById('backtest-cards').innerHTML=card('有效樣本',fmt(pairs.length,0)+' 日')+card('方向命中率',fmt(hit,1)+'%')+card('分數／未來5日相關',fmt(corr,2));
plot('backtest-chart',[{{x:pairs.map(x=>x[0]),y:pairs.map(x=>x[1]),type:'scatter',mode:'markers',marker:{{color:C.blue,size:8}},name:'觀測值'}}],'四因子分數與未來5日大盤報酬','未來5日報酬（%）');
document.getElementById('quality-table').innerHTML='<table><thead><tr><th>資料集</th><th>筆數</th><th>起始日</th><th>最新日</th></tr></thead><tbody>'+Object.entries(H).map(([k,v])=>`<tr><td>${{k}}</td><td>${{v.length}}</td><td>${{v[0]?.date||'—'}}</td><td>${{last(v)?.date||'—'}}</td></tr>`).join('')+'</tbody></table>';document.getElementById('quality-messages').textContent=D.messages.join('｜');
const nav=document.getElementById('desktop-nav'),sel=document.getElementById('mobile-nav');pages.forEach(([id,label])=>{{nav.insertAdjacentHTML('beforeend',`<button class="nav" data-target="${{id}}">○ ${{label}}</button>`);sel.insertAdjacentHTML('beforeend',`<option value="${{id}}">${{label}}</option>`)}});function show(id){{document.querySelectorAll('.page').forEach(x=>x.classList.toggle('active',x.dataset.page===id));document.querySelectorAll('.nav').forEach(x=>x.classList.toggle('active',x.dataset.target===id));sel.value=id;location.hash=id;setTimeout(()=>window.dispatchEvent(new Event('resize')),30)}}nav.addEventListener('click',e=>e.target.dataset.target&&show(e.target.dataset.target));sel.addEventListener('change',()=>show(sel.value));show(location.hash.slice(1)||'overview');
</script></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap-db", type=Path)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    history = bootstrap_from_sqlite(args.bootstrap_db) if args.bootstrap_db else load_history()
    messages = ["offline"] if args.offline else update_official_data(history)
    save_history(history)
    model = build_model(history)
    OUTPUT_PATH.write_text(render_html(history, model, messages), encoding="utf-8")
    latest = model[-1]
    print(json.dumps({"latest_date": latest["date"], "score": round(float(latest["score"]), 2),
                      "net_short": latest["net_short"], "net_short_change_5d": latest["net_short_change_5d"],
                      "sources": messages}, ensure_ascii=False))


if __name__ == "__main__":
    main()
