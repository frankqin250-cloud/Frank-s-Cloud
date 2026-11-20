#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
华尔街 BTC 机构资金、衍生品与宏观利差全因子监控告警脚本 (V13 全基金明细增强版)
"""

import os
import re
import sys
import json
import calendar
import logging
import argparse
from datetime import datetime, date, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

# ==================== 1. 配置与告警阈值 ====================
TELEGRAM_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TG_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")
SOSOVALUE_API_KEY = os.getenv("SOSOVALUE_API_KEY", "").strip()

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor_state.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.log")

CFTC_API_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
SOSOVALUE_V1_BASE = "https://openapi.sosovalue.com/openapi/v1"
TFTC_ETF_URL = "https://www.tftc.io/bitcoin-etf-flows"

CORE_ETF_TICKERS = ("IBIT", "FBTC", "GBTC", "BTC", "BITB", "MSBT", "BTCW", "ARKB")

THRESHOLDS = {
    "IBIT_HEAVY_OUTFLOW_M": -150.0,         # IBIT 单日净流出警报（百万美元）
    "TOTAL_INFLOW_ALERT_M": 100.0,          # 全市场 ETF 单日净流入警报（百万美元）
    "MSBT_COUNTER_INFLOW_M": 20.0,          # 摩根士丹利 MSBT 逆势净流入阈值（百万美元）
    "COT_NET_CHANGE_CONTRACTS": 1000,       # CFTC 杠杆基金单周调仓张数警报
    "COT_UNWIND_EXHAUSTED_CONTRACTS": 3000, # CFTC 杠杆基金套利空头枯竭阈值
    "BASIS_EXCESS_SPREAD_ALERT": 2.0,       # CME 年化基差高于 10Y 美债 2% 警报
    "CB_PREMIUM_POSITIVE_ALERT": 0.05,      # Coinbase 溢价率高于 +0.05% 警报
    "CB_PREMIUM_NEGATIVE_ALERT": -0.08,     # Coinbase 溢价率低于 -0.08% 警报
    "DIVERGENCE_PRICE_DROP_PCT": -2.0,      # 24h 跌幅超 2% 且溢价转正触发底背离
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("BTC_Monitor")


def load_state() -> Dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_cot_date": "", "last_etf_date": "", "last_cb_premium": None, "last_cme_basis": None}


def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or "YOUR_TELEGRAM" in TELEGRAM_BOT_TOKEN:
        logger.error("未配置有效的 TELEGRAM_BOT_TOKEN")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code != 200:
            logger.error(f"Telegram 推送失败 ({resp.status_code}): {resp.text}")
            return False
        logger.info("Telegram 推送成功。")
        return True
    except Exception as e:
        logger.error(f"Telegram 网络异常: {e}")
        return False


# ==================== 2. CFTC 杠杆基金持仓模块 ====================
def fetch_cftc_btc_cot() -> Optional[Dict]:
    try:
        params = {"cftc_contract_market_code": "133741", "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": 2}
        resp = requests.get(CFTC_API_URL, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        if resp.status_code == 200 and resp.json():
            latest = resp.json()[0]
            oi = int(float(latest.get("open_interest_all", 0)))
            lev_long = int(float(latest.get("lev_money_positions_long", 0)))
            lev_short = int(float(latest.get("lev_money_positions_short", 0)))
            lev_spread = int(float(latest.get("lev_money_positions_spread", 0)))
            lev_net = lev_long - lev_short
            lev_net_chg = int(float(latest.get("change_in_lev_money_long", 0))) - int(float(latest.get("change_in_lev_money_short", 0)))
            return {
                "report_date": str(latest.get("report_date_as_yyyy_mm_dd", ""))[:10],
                "open_interest_contracts": oi,
                "open_interest_btc": oi * 5,
                "lev_long": lev_long,
                "lev_short": lev_short,
                "lev_spread": lev_spread,
                "lev_net_contracts": lev_net,
                "lev_net_btc": lev_net * 5,
                "lev_net_chg_contracts": lev_net_chg,
                "lev_net_chg_btc": lev_net_chg * 5,
            }
    except Exception as e:
        logger.error(f"CFTC API 获取失败: {e}")
    return None


# ==================== 3. 美国现货 ETF 资金流模块（SoSoValue 全基金明细版） ====================
def parse_etf_amount(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        num = float(val)
        return round(num / 1_000_000.0, 2) if abs(num) >= 100_000 else round(num, 2)

    s = str(val).strip().replace(",", "").replace("$", "").replace(" ", "")
    s = s.replace("−", "-").replace("–", "-").replace("—", "-")
    if not s or s in ("-", "--") or s.lower() in ("n/a", "null", "none"):
        return None

    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]

    mult = 1.0
    if s.lower().endswith("b"):
        mult = 1000.0
        s = s[:-1]
    elif s.lower().endswith("m"):
        mult = 1.0
        s = s[:-1]
    elif s.lower().endswith("k"):
        mult = 0.001
        s = s[:-1]

    try:
        num = float(s) * mult
        if neg:
            num = -abs(num)
        if mult == 1.0 and abs(num) >= 100_000:
            num = num / 1_000_000.0
        return round(num, 2)
    except ValueError:
        return None


def parse_row_date(date_str: str) -> Optional[date]:
    clean = re.sub(r"^[A-Za-z]+,\s*", "", date_str.strip()).replace(",", "")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(clean, fmt).date()
        except ValueError:
            continue
    return None


def _build_etf_payload(date_label: str, funds: Dict[str, float], total_val: Optional[float], source: str) -> Dict:
    known_tickers = ("IBIT", "FBTC", "GBTC", "BTC", "BITB", "ARKB", "HODL", "BRRR", "EZBC", "BTCO", "BTCW", "MSBT", "DEFI")
    calc_sum = round(sum(funds.get(t, 0.0) for t in known_tickers if t in funds), 2)
    total = round(total_val, 2) if (total_val is not None and (total_val != 0.0 or calc_sum == 0.0)) else calc_sum
    mini = round(funds.get("BTC", 0.0) + funds.get("BITB", 0.0), 2)
    return {
        "date": date_label,
        "source": source,
        "ibit": funds.get("IBIT", 0.0),
        "fbtc": funds.get("FBTC", 0.0),
        "gbtc": funds.get("GBTC", 0.0),
        "arkb": funds.get("ARKB", 0.0),
        "msbt": funds.get("MSBT", 0.0),
        "btcw": funds.get("BTCW", 0.0),
        "mini_flow": mini,
        "total": total,
    }


def _smart_extract_flow(obj: Dict) -> Tuple[Optional[float], str]:
    d_str = ""
    for k, v in obj.items():
        kl = k.lower()
        if any(dk in kl for dk in ("date", "time", "day", "dt")) and v:
            m = re.search(r"202\d-\d{2}-\d{2}", str(v))
            if m:
                d_str = m.group(0)
                break

    for k, v in obj.items():
        kl = k.lower()
        if ("inflow" in kl or "flow" in kl) and not any(ex in kl for ex in ("cum", "status", "type", "rate", "pct", "ratio")):
            if isinstance(v, dict):
                sub_val, sub_d = _smart_extract_flow(v)
                if sub_val is not None:
                    return sub_val, (sub_d or d_str)
            else:
                parsed = parse_etf_amount(v)
                if parsed is not None:
                    return parsed, d_str

    for k in ("value", "amount", "usd", "net"):
        if k in obj and obj[k] is not None and not isinstance(obj[k], (dict, list)):
            parsed = parse_etf_amount(obj[k])
            if parsed is not None:
                return parsed, d_str

    return None, d_str


def _unwrap_list(raw: Any) -> List[Dict]:
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        for key in ("data", "list", "records", "items", "rows", "etfs", "result"):
            sub = raw.get(key)
            if isinstance(sub, list):
                return [x for x in sub if isinstance(x, dict)]
            if isinstance(sub, dict):
                nested = _unwrap_list(sub)
                if nested:
                    return nested
    return []


def fetch_sosovalue_etf() -> Tuple[Optional[Dict], str]:
    if not SOSOVALUE_API_KEY:
        return None, "未检测到SOSOVALUE_API_KEY"

    headers = {
        "x-soso-api-key": SOSOVALUE_API_KEY,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    try:
        funds: Dict[str, float] = {}
        latest_date = ""
        debug_keys = ""

        # 步骤 1：批量拉取 US BTC 现货 ETF 列表
        list_url = f"{SOSOVALUE_V1_BASE}/etfs"
        r_list = requests.get(list_url, headers=headers, params={"symbol": "BTC", "country_code": "US"}, timeout=15)
        if r_list.status_code == 200:
            etf_items = _unwrap_list(r_list.json())
            if etf_items:
                debug_keys = ",".join(list(etf_items[0].keys())[:6])
            for item in etf_items:
                tk = str(item.get("ticker") or item.get("symbol") or item.get("code") or "").strip().upper()
                if not tk:
                    continue
                v, d = _smart_extract_flow(item)
                if v is not None:
                    funds[tk] = v
                    if d and d > latest_date:
                        latest_date = d

        # 步骤 2：拉取官方汇总历史（跳过 T+1 未结算空值行）
        total_val: Optional[float] = None
        sum_url = f"{SOSOVALUE_V1_BASE}/etfs/summary-history"
        r_sum = requests.get(sum_url, headers=headers, params={"symbol": "BTC", "country_code": "US", "limit": 5}, timeout=15)
        if r_sum.status_code == 200:
            sum_rows = _unwrap_list(r_sum.json())
            if sum_rows and not debug_keys:
                debug_keys = ",".join(list(sum_rows[0].keys())[:6])
            for row in sum_rows:
                v, d = _smart_extract_flow(row)
                if v is not None:
                    total_val = v
                    if d and d >= latest_date:
                        latest_date = d
                    break

        # 步骤 3：对核心基金（含 MSBT、BTCW、ARKB）逐一补齐缺失快照
        for ticker in CORE_ETF_TICKERS:
            if ticker in funds:
                continue
            snap_url = f"{SOSOVALUE_V1_BASE}/etfs/{ticker}/market-snapshot"
            r_tk = requests.get(snap_url, headers=headers, timeout=12)
            if r_tk.status_code == 200:
                tk_raw = r_tk.json()
                tk_obj = tk_raw.get("data") if isinstance(tk_raw, dict) else tk_raw
                if isinstance(tk_obj, list) and tk_obj:
                    tk_obj = tk_obj[0]
                if isinstance(tk_obj, dict):
                    v, d = _smart_extract_flow(tk_obj)
                    if v is not None:
                        funds[ticker] = v
                        if d and d > latest_date:
                            latest_date = d

        if "IBIT" in funds or total_val is not None:
            if not latest_date:
                latest_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            payload = _build_etf_payload(latest_date, funds, total_val, "SoSoValue API")
            logger.info(
                f"成功通过 SoSoValue API 获取 ETF ({latest_date}): Total={payload['total']:+.1f}M, "
                f"IBIT={payload['ibit']:+.1f}M, MSBT={payload['msbt']:+.1f}M, BTCW={payload['btcw']:+.1f}M"
            )
            return payload, "OK"

        status_hint = f"HTTP {r_list.status_code}/{r_sum.status_code}"
        return None, f"字段未匹配({status_hint}|keys={debug_keys})"
    except Exception as e:
        logger.warning(f"SoSoValue OpenAPI v1 异常: {e}")
        return None, "API请求异常"


def _fetch_tftc_backup() -> Optional[Dict]:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        resp = requests.get(TFTC_ETF_URL, headers=headers, timeout=18)
        if resp.status_code == 200 and "IBIT" in resp.text:
            soup = BeautifulSoup(resp.text, "html.parser")
            date_match = re.search(
                r"Latest Bitcoin ETF flows:\s*(?:[A-Za-z]+,\s*)?([A-Za-z]+\s+\d{1,2},\s+202\d)",
                soup.get_text(),
                re.IGNORECASE,
            )
            dt = parse_row_date(date_match.group(1)) if date_match else None
            date_label = dt.strftime("%Y-%m-%d") if dt else ""
            funds: Dict[str, float] = {}
            total_val: Optional[float] = None
            for table in soup.find_all("table"):
                t_text = table.get_text()
                if "IBIT" in t_text and "Net flow" in t_text:
                    for tr in table.find_all("tr")[1:]:
                        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                        if len(cells) >= 3:
                            ticker = cells[0].split()[0].upper()
                            val = parse_etf_amount(cells[2])
                            if val is None:
                                continue
                            if "ALL US SPOT" in cells[0].upper() or ticker == "TOTAL":
                                total_val = val
                            elif re.match(r"^[A-Z]{2,5}$", ticker):
                                funds[ticker] = val
                    if "IBIT" in funds and date_label:
                        return _build_etf_payload(date_label, funds, total_val, "TFTC")
    except Exception as e:
        logger.warning(f"TFTC 备用通道异常: {e}")
    return None


def fetch_etf_flows() -> Optional[Dict]:
    soso_res, reason = fetch_sosovalue_etf()
    if soso_res:
        return soso_res

    tftc_res = _fetch_tftc_backup()
    if tftc_res:
        tftc_res["source"] = f"TFTC | 降级: {reason}"
        return tftc_res
    return None


# ==================== 4. Coinbase 溢价与 CME 基差模块 ====================
def fetch_coinbase_premium() -> Optional[Dict]:
    try:
        r = requests.get("https://api.exchange.coinbase.com/products/BTC-USD/stats", timeout=15)
        stats = r.json()
        cb_price = float(stats["last"])
        open_24h = float(stats["open"])
        chg_24h = ((cb_price - open_24h) / open_24h) * 100.0 if open_24h > 0 else 0.0
    except Exception as e:
        logger.warning(f"Coinbase 行情失败: {e}")
        return None

    for name, url, fn in [
        ("OKX", "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT", lambda d: float(d["data"][0]["last"])),
        ("Bitfinex", "https://api-pub.bitfinex.com/v2/ticker/tBTCUST", lambda d: float(d[6])),
        ("Kraken", "https://api.kraken.com/0/public/Ticker?pair=XBTUSDT", lambda d: float(list(d["result"].values())[0]["c"][0])),
    ]:
        try:
            res = requests.get(url, timeout=12)
            if res.status_code == 200:
                off_price = fn(res.json())
                if off_price > 0:
                    prem_usd = cb_price - off_price
                    return {
                        "cb_price": cb_price,
                        "offshore_price": off_price,
                        "offshore_source": name,
                        "premium_usd": prem_usd,
                        "premium_pct": (prem_usd / off_price) * 100.0,
                        "price_chg_24h": chg_24h,
                    }
        except Exception:
            continue
    return None


def get_cme_days_to_expiry() -> int:
    today = datetime.now(timezone.utc).date()
    last_day = calendar.monthrange(today.year, today.month)[1]
    exp = date(today.year, today.month, last_day)
    while exp.weekday() != calendar.FRIDAY:
        exp = date(today.year, today.month, exp.day - 1)
    if (exp - today).days <= 10:
        ny = today.year + (1 if today.month == 12 else 0)
        nm = 1 if today.month == 12 else today.month + 1
        exp = date(ny, nm, calendar.monthrange(ny, nm)[1])
        while exp.weekday() != calendar.FRIDAY:
            exp = date(ny, nm, exp.day - 1)
    return max((exp - today).days, 14)


def fetch_yahoo_quote(symbol: str) -> Optional[float]:
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        if r.status_code == 200:
            meta = r.json()["chart"]["result"][0]["meta"]
            val = meta.get("regularMarketPrice") or meta.get("previousClose")
            return float(val) if val and float(val) > 0 else None
    except Exception:
        return None
    return None


def fetch_basis_and_treasury(spot_price: Optional[float]) -> Optional[Dict]:
    cme = fetch_yahoo_quote("BTC=F")
    spot = spot_price or fetch_yahoo_quote("BTC-USD")
    us10y = fetch_yahoo_quote("%5ETNX")
    if not (cme and spot and us10y):
        return None
    days = get_cme_days_to_expiry()
    raw_basis = ((cme - spot) / spot) * 100.0
    ann_basis = raw_basis * (365.0 / days)
    return {
        "cme_futures": cme,
        "spot_price": spot,
        "days_to_expiry": days,
        "raw_basis_pct": raw_basis,
        "annualized_basis_pct": ann_basis,
        "us10y_yield": us10y,
        "excess_spread_pct": ann_basis - us10y,
    }


# ==================== 5. 两大高频静默雷达 (--intraday & --basis) ====================
def run_intraday_coinbase_radar() -> None:
    state = load_state()
    cb = fetch_coinbase_premium()
    if not cb:
        return
    prem, chg24, prev = cb["premium_pct"], cb["price_chg_24h"], state.get("last_cb_premium")
    alerts = []
    if chg24 <= THRESHOLDS["DIVERGENCE_PRICE_DROP_PCT"] and prem > 0:
        alerts.append(f"🔥 <b>美盘主力吸筹底背离</b>：BTC 24h 下跌 {chg24:.2f}%，但 Coinbase 溢价为正 ({prem:+.3f}%)！")
    if prev is not None and prev < 0 and prem > 0:
        alerts.append(f"🔄 <b>Coinbase 溢价由负转正</b>：从 {prev:+.3f}% 翻正至 {prem:+.3f}% (+${cb['premium_usd']:.1f})！")
    if prem >= THRESHOLDS["CB_PREMIUM_POSITIVE_ALERT"] and (prev is None or prev < THRESHOLDS["CB_PREMIUM_POSITIVE_ALERT"]):
        alerts.append(f"🇺🇸 <b>美盘现货买盘强势</b>：Coinbase 溢价率突破 +0.05%，升至 {prem:+.3f}%")

    state["last_cb_premium"] = round(prem, 4)
    save_state(state)
    if not alerts:
        logger.info(f"美盘 10m 雷达静默：Coinbase 溢价 {prem:+.3f}%")
        return

    lines = ["<b>⚡ 美盘盘中异动雷达：Coinbase 现货买盘信号</b>\n"] + [f"• {a}" for a in alerts]
    lines.append(f"\n• <b>Coinbase:</b> <code>${cb['cb_price']:,.1f}</code> | <b>{cb['offshore_source']}:</b> <code>${cb['offshore_price']:,.1f}</code>")
    lines.append(f"• <b>当前溢价:</b> 🟢 <code>{prem:+.3f}%</code> (<code>{cb['premium_usd']:+.1f}</code> USD)")
    send_telegram_message("\n".join(lines))


def run_cme_basis_radar() -> None:
    state = load_state()
    cb = fetch_coinbase_premium()
    macro = fetch_basis_and_treasury(cb["cb_price"] if cb else None)
    if not macro:
        return

    curr_basis = macro["raw_basis_pct"]
    ann_basis = macro["annualized_basis_pct"]
    prev_basis = state.get("last_cme_basis")
    alerts = []

    if prev_basis is not None and prev_basis < 0 and curr_basis > 0:
        alerts.append(
            f"🔄 <b>CME 期货结束贴水（由负转正）</b>：当期升水从 {prev_basis:+.2f}% 翻正至 {curr_basis:+.2f}%（年化 {ann_basis:+.2f}%），期货做空动能撤退！"
        )
    if macro["excess_spread_pct"] >= THRESHOLDS["BASIS_EXCESS_SPREAD_ALERT"] and (prev_basis is None or prev_basis <= 0.1):
        alerts.append(
            f"💰 <b>期现套利窗口重开</b>：CME 年化基差高于 10Y 美债 {macro['excess_spread_pct']:+.2f}%，套利买盘具备进场动力！"
        )

    state["last_cme_basis"] = round(curr_basis, 4)
    save_state(state)
    if not alerts:
        logger.info(f"CME 30m 基差雷达静默：当期基差 {curr_basis:+.2f}% (年化 {ann_basis:+.2f}%)")
        return

    lines = ["<b>📈 CME 期货基差异动雷达：结构性转强信号</b>\n"] + [f"• {a}" for a in alerts]
    lines.extend([
        "",
        f"• <b>CME 期货主连:</b> <code>${macro['cme_futures']:,.1f}</code> | <b>现货基准:</b> <code>${macro['spot_price']:,.1f}</code>",
        f"• <b>当期基差:</b> 🟢 <code>{curr_basis:+.2f}%</code> | <b>年化基差:</b> <code>{ann_basis:+.2f}%</code> (10Y 美债: <code>{macro['us10y_yield']:.2f}%</code>)",
    ])
    send_telegram_message("\n".join(lines))


# ==================== 6. 常规全因子综合战报引擎 ====================
def evaluate_and_alert(force: bool = False) -> None:
    state = load_state()
    cot = fetch_cftc_btc_cot()
    etf = fetch_etf_flows()
    cb = fetch_coinbase_premium()
    macro = fetch_basis_and_treasury(cb["cb_price"] if cb else None)

    alerts, has_new = [], False
    if etf:
        if etf["date"] != state.get("last_etf_date"):
            has_new = True
            state["last_etf_date"] = etf["date"]
        if etf["ibit"] <= THRESHOLDS["IBIT_HEAVY_OUTFLOW_M"]:
            alerts.append(f"🚨 <b>IBIT 大额失血</b>：单日净流出 ${abs(etf['ibit']):.1f}M")
        if etf["total"] >= THRESHOLDS["TOTAL_INFLOW_ALERT_M"]:
            alerts.append(f"🟢 <b>ETF 整体大幅净流入</b>：单日净流入 +${etf['total']:.1f}M")
        if etf["ibit"] < -50 and (etf["mini_flow"] > 0 or etf["msbt"] >= THRESHOLDS["MSBT_COUNTER_INFLOW_M"]):
            alerts.append("⚠️ <b>机构筹码分化</b>：IBIT 大额流出，但摩根士丹利/低费率盘出现逆势净流入")

    if cot:
        if cot["report_date"] != state.get("last_cot_date"):
            has_new = True
            state["last_cot_date"] = cot["report_date"]
        if abs(cot["lev_net_chg_contracts"]) >= THRESHOLDS["COT_NET_CHANGE_CONTRACTS"]:
            d_str = "空头回补（平仓）" if cot["lev_net_chg_contracts"] > 0 else "空头加码（建仓）"
            alerts.append(f"📊 <b>华尔街套利盘调仓</b>：杠杆基金净头寸变化 {cot['lev_net_chg_contracts']:+d} 张 ({d_str})")
        if cot["lev_net_contracts"] < 0 and abs(cot["lev_net_contracts"]) <= THRESHOLDS["COT_UNWIND_EXHAUSTED_CONTRACTS"]:
            alerts.append(f"🧊 <b>期现套利空头已近枯竭</b>：杠杆基金净空头仅剩 {abs(cot['lev_net_contracts']):,} 张 ({abs(cot['lev_net_btc']):,} BTC)")

    if macro:
        prev_b = state.get("last_cme_basis")
        curr_b = macro["raw_basis_pct"]
        state["last_cme_basis"] = round(curr_b, 4)
        if prev_b is not None and prev_b < 0 and curr_b > 0:
            alerts.append(f"🔄 <b>CME 期货结束贴水</b>：基差从 {prev_b:+.2f}% 翻正至 {curr_b:+.2f}%")

    if cb:
        prem, chg24 = cb["premium_pct"], cb["price_chg_24h"]
        state["last_cb_premium"] = round(prem, 4)
        if chg24 <= THRESHOLDS["DIVERGENCE_PRICE_DROP_PCT"] and prem > 0:
            alerts.append(f"🔥 <b>美盘主力吸筹底背离</b>：BTC 24h 下跌 {chg24:.2f}%，但 Coinbase 溢价转正 ({prem:+.3f}%)")
        elif prem <= THRESHOLDS["CB_PREMIUM_NEGATIVE_ALERT"]:
            alerts.append(f"🩸 <b>美盘现货抛压沉重</b>：Coinbase 负溢价扩大至 {prem:+.3f}% (${cb['premium_usd']:.1f})")

    if not has_new and not force:
        logger.info("暂无新数据发布，跳过推送。")
        return

    msg = ["<b>📡 华尔街 BTC 全因子机构监控战报</b>\n"]
    if alerts:
        msg.extend(["<b>【核心交易信号触发】</b>"] + [f"• {x}" for x in alerts] + [""])
    if cb:
        icon = "🟢" if cb["premium_pct"] > 0 else "🔴"
        msg.extend([
            "<b>【1. 现货定价与 Coinbase 溢价】</b>",
            f"• <b>Coinbase 现货:</b> <code>${cb['cb_price']:,.1f}</code> (24h: <code>{cb['price_chg_24h']:+.2f}%</code>)",
            f"• <b>离岸基准 ({cb['offshore_source']}):</b> <code>${cb['offshore_price']:,.1f}</code>",
            f"• <b>Coinbase 溢价:</b> {icon} <code>{cb['premium_pct']:+.3f}%</code> (<code>{cb['premium_usd']:+.1f}</code> USD)\n",
        ])
    if macro:
        sp = macro["excess_spread_pct"]
        st = "套利有利可图" if sp >= 1.5 else ("微利/观望区间" if sp >= 0 else "倒挂/套利平仓压制")
        msg.extend([
            f"<b>【2. 宏观利率锚 & CME 期货基差 (参考 {macro['days_to_expiry']} 天交割)】</b>",
            f"• <b>CME 期货主连:</b> <code>${macro['cme_futures']:,.1f}</code> (当期升水 <code>{macro['raw_basis_pct']:+.2f}%</code>)",
            f"• <b>CME 年化基差:</b> <code>{macro['annualized_basis_pct']:+.2f}%</code> | <b>10Y 美债:</b> <code>{macro['us10y_yield']:.2f}%</code>",
            f"• <b>套利超额利差:</b> <code>{sp:+.2f}%</code> ({st})\n",
        ])
    if etf:
        msg.extend([
            f"<b>【3. 美国现货 ETF 资金流 ({etf['date']} | {etf['source']})】</b>",
            f"• <b>全市场总计:</b> <code>{etf['total']:+.1f}M</code> USD",
            f"• <b>贝莱德 IBIT:</b> <code>{etf['ibit']:+.1f}M</code> | <b>富达 FBTC:</b> <code>{etf['fbtc']:+.1f}M</code>",
            f"• <b>灰度 GBTC:</b> <code>{etf['gbtc']:+.1f}M</code> | <b>ARKB:</b> <code>{etf['arkb']:+.1f}M</code>",
            f"• <b>摩根士丹利 MSBT:</b> <code>{etf['msbt']:+.1f}M</code> | <b>智慧树 BTCW:</b> <code>{etf['btcw']:+.1f}M</code>",
            f"• <b>低费率迷你盘 (BTC+BITB):</b> <code>{etf['mini_flow']:+.1f}M</code>\n",
        ])
    else:
        msg.append("⚠️ <i>现货 ETF 数据源暂未更新</i>\n")
    if cot:
        nc = cot["lev_net_contracts"]
        msg.extend([
            f"<b>【4. CME 持仓 & CFTC 杠杆基金 ({cot['report_date']})】</b>",
            f"• <b>CME 总持仓 (OI):</b> {cot['open_interest_contracts']:,} 张 ({cot['open_interest_btc']:,} BTC)",
            f"• <b>杠杆基金 {'净空头' if nc < 0 else '净多头'}:</b> <code>{abs(nc):,}</code> 张 (<b>{abs(cot['lev_net_btc']):,} BTC</b>)",
            f"• <b>单周净头寸变化:</b> <code>{cot['lev_net_chg_contracts']:+d}</code> 张 ({cot['lev_net_chg_btc']:+d} BTC)",
            f"• <b>多空明细:</b> 多单 {cot['lev_long']:,} / 空单 {cot['lev_short']:,} / 展期 {cot['lev_spread']:,}",
        ])

    if send_telegram_message("\n".join(msg)):
        save_state(state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BTC 全因子机构监控脚本 V13")
    parser.add_argument("--force", action="store_true", help="强制推送全因子战报")
    parser.add_argument("--intraday", action="store_true", help="美盘 10 分钟 Coinbase 溢价静默雷达")
    parser.add_argument("--basis", action="store_true", help="30 分钟 CME 期货结束贴水静默雷达")
    parser.add_argument("--test", action="store_true", help="测试 Telegram 连通性")
    args = parser.parse_args()

    if args.test:
        send_telegram_message("✅ <b>BTC 监控节点 V13 测试成功</b>")
    elif args.intraday:
        run_intraday_coinbase_radar()
    elif args.basis:
        run_cme_basis_radar()
    else:
        evaluate_and_alert(force=args.force)
