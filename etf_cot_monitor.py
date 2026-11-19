#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
华尔街 BTC 机构资金、衍生品与宏观利差全因子监控告警脚本 (V4 完整版)
新增特性:
  1. --intraday 美盘盘中静默雷达（仅在 Coinbase 溢价由负转正或触发吸筹底背离时推送）
  2. 修复 Farside Markdown 表格首列空白导致的 ETF 数据解析偏移问题
  3. 优化 CME 期货近月到期前 10 天自动展期计算，避免年化基差失真
"""

import os
import re
import sys
import json
import calendar
import logging
import argparse
from datetime import datetime, date, timezone
from typing import Dict, List, Optional

import requests
import cloudscraper
from bs4 import BeautifulSoup

# ==================== 1. 环境变量与阈值配置区 ====================
TELEGRAM_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TG_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor_state.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.log")

CFTC_API_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
CFTC_HTML_URL = "https://www.cftc.gov/dea/futures/financial_lf.htm"
FARSIDE_JINA_URL = "https://r.jina.ai/https://farside.co.uk/btc/"
FARSIDE_DIRECT_URL = "https://farside.co.uk/btc/"

THRESHOLDS = {
    "IBIT_HEAVY_OUTFLOW_M": -150.0,         # IBIT 单日净流出超过 1.5 亿美元触发大额失血警报
    "TOTAL_INFLOW_ALERT_M": 100.0,          # 全市场 ETF 单日净流入超过 1 亿美元触发回流警报
    "IBIT_INFLOW_STREAK_DAYS": 2,           # IBIT 连续净流入达到 2 天触发右侧反转信号
    "COT_NET_CHANGE_CONTRACTS": 1000,       # CFTC 杠杆基金单周调仓超过 1000 张触发警报
    "COT_UNWIND_EXHAUSTED_CONTRACTS": 3000, # CFTC 杠杆基金净空头低于 3000 张提示套利出清尾声
    "BASIS_EXCESS_SPREAD_ALERT": 2.0,       # CME 年化基差高于 10Y 美债 2% 以上提示套利资金回归
    "CB_PREMIUM_POSITIVE_ALERT": 0.05,      # Coinbase 溢价率高于 +0.05% 触发美盘强买盘信号
    "CB_PREMIUM_NEGATIVE_ALERT": -0.08,     # Coinbase 溢价率低于 -0.08% 触发美盘抛压警报
    "DIVERGENCE_PRICE_DROP_PCT": -2.0,      # 24h 跌幅超过 2% 且溢价大于 0% 时，触发主力吸筹底背离
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("BTC_Institutional_Monitor")


def load_state() -> Dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"读取状态文件失败，将重新初始化: {e}")
    return {"last_cot_date": "", "last_etf_date": "", "last_cb_premium": None}


def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or "YOUR_TELEGRAM" in TELEGRAM_BOT_TOKEN:
        logger.error("未配置有效的 TELEGRAM_BOT_TOKEN！")
        return False
    if not TELEGRAM_CHAT_ID or "YOUR_TELEGRAM" in TELEGRAM_CHAT_ID:
        logger.error("未配置有效的 TELEGRAM_CHAT_ID！")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code != 200:
            logger.error(f"Telegram API 返回错误 ({resp.status_code}): {resp.text}")
            return False
        logger.info("Telegram 告警推送成功。")
        return True
    except Exception as e:
        logger.error(f"Telegram 网络请求失败: {e}")
        return False


# ==================== 2. CFTC COT 持仓解析模块 ====================
def fetch_cftc_btc_cot() -> Optional[Dict]:
    try:
        params = {
            "cftc_contract_market_code": "133741",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": 2,
        }
        resp = requests.get(CFTC_API_URL, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            if data and len(data) > 0:
                latest = data[0]
                report_date = str(latest.get("report_date_as_yyyy_mm_dd", ""))[:10]
                oi = int(float(latest.get("open_interest_all", 0)))
                lev_long = int(float(latest.get("lev_money_positions_long", 0)))
                lev_short = int(float(latest.get("lev_money_positions_short", 0)))
                lev_spread = int(float(latest.get("lev_money_positions_spread", 0)))
                lev_net = lev_long - lev_short

                chg_long = int(float(latest.get("change_in_lev_money_long", 0)))
                chg_short = int(float(latest.get("change_in_lev_money_short", 0)))
                lev_net_chg = chg_long - chg_short

                logger.info(f"成功通过 CFTC 官方 API 获取报告 ({report_date})")
                return {
                    "report_date": report_date,
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
        logger.warning(f"CFTC 官方 API 请求异常，切换至网页备用解析: {e}")

    try:
        resp = requests.get(CFTC_HTML_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status()
        pattern = re.compile(
            r"BITCOIN\s*-\s*CHICAGO MERCANTILE EXCHANGE.*?133741.*?(?=CFTC Code #|\Z)",
            re.DOTALL | re.IGNORECASE,
        )
        match = pattern.search(resp.text)
        if not match:
            return None

        lines = [line.strip() for line in match.group(0).splitlines() if line.strip()]
        report_date = "Unknown"
        open_interest = 0
        pos_nums: List[int] = []
        chg_nums: List[int] = []

        for i, line in enumerate(lines):
            if "Open Interest is" in line:
                oi_m = re.search(r"Open Interest is\s+([\d,]+)", line)
                if oi_m:
                    open_interest = int(oi_m.group(1).replace(",", ""))
            if "Positions as of" in line:
                d_m = re.search(r"([A-Za-z]+\s+\d{1,2},\s+\d{4})", line)
                if d_m:
                    report_date = d_m.group(1)
            if line.startswith("All") and not pos_nums:
                nums = [int(x.replace(",", "")) for x in re.findall(r"-?[\d,]+", line)]
                if len(nums) >= 14:
                    pos_nums = nums
            if line.startswith("Changes from:") and i + 1 < len(lines) and not chg_nums:
                nums = [int(x.replace(",", "")) for x in re.findall(r"[+-]?[\d,]+", lines[i + 1])]
                if len(nums) >= 14:
                    chg_nums = nums

        if len(pos_nums) < 14:
            return None

        lev_long, lev_short, lev_spread = pos_nums[6], pos_nums[7], pos_nums[8]
        lev_net = lev_long - lev_short
        lev_net_chg = (chg_nums[6] - chg_nums[7]) if len(chg_nums) >= 14 else 0

        return {
            "report_date": report_date,
            "open_interest_contracts": open_interest,
            "open_interest_btc": open_interest * 5,
            "lev_long": lev_long,
            "lev_short": lev_short,
            "lev_spread": lev_spread,
            "lev_net_contracts": lev_net,
            "lev_net_btc": lev_net * 5,
            "lev_net_chg_contracts": lev_net_chg,
            "lev_net_chg_btc": lev_net_chg * 5,
        }
    except Exception as e:
        logger.error(f"CFTC 备用网页解析失败: {e}")
        return None


# ==================== 3. 美国现货 ETF 资金流模块（已修复列对齐） ====================
def parse_farside_number(val: str) -> float:
    s = val.strip().replace(",", "").replace("$", "")
    if not s or s == "-" or s.lower() == "n/a":
        return 0.0
    if s.startswith("(") and s.endswith(")"):
        return -float(s[1:-1])
    try:
        return float(s)
    except ValueError:
        return 0.0


def build_etf_summary(daily_records: List[Dict]) -> Dict:
    latest = daily_records[-1]
    streak_days = 0
    streak_dir = 0
    for rec in reversed(daily_records):
        ibit_val = rec.get("IBIT", 0.0)
        if ibit_val == 0:
            break
        curr_dir = 1 if ibit_val > 0 else -1
        if streak_dir == 0:
            streak_dir = curr_dir
            streak_days = 1
        elif curr_dir == streak_dir:
            streak_days += 1
        else:
            break

    mini_flow = latest.get("BTC", 0.0) + latest.get("BITB", 0.0)
    return {
        "date": latest["Date"],
        "ibit": latest.get("IBIT", 0.0),
        "fbtc": latest.get("FBTC", 0.0),
        "gbtc": latest.get("GBTC", 0.0),
        "mini_flow": round(mini_flow, 2),
        "total": latest.get("Total", 0.0),
        "ibit_streak_days": streak_days * streak_dir,
    }


def fetch_farside_etf_flows() -> Optional[Dict]:
    date_regex = re.compile(r"^\d{1,2}\s+[A-Za-z]{3}\s+\d{4}$")

    # 通道 A：边缘防屏蔽节点（保留首列空白位置，防止列错位）
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/plain, text/markdown, */*",
        }
        resp = requests.get(FARSIDE_JINA_URL, headers=headers, timeout=30)
        if resp.status_code == 200 and "IBIT" in resp.text:
            headers_list: List[str] = []
            daily_records: List[Dict] = []
            for line in resp.text.splitlines():
                if "|" not in line:
                    continue
                raw_cells = [c.strip() for c in line.strip().strip("|").split("|")]
                # 跳过 Markdown 表格分隔行 | --- | --- |
                if all(re.match(r"^:?-+:?$", c) or c == "" for c in raw_cells):
                    continue
                if "IBIT" in raw_cells and "Total" in raw_cells:
                    headers_list = raw_cells
                    continue
                if headers_list and len(raw_cells) >= len(headers_list) and date_regex.match(raw_cells[0]):
                    row_dict = {"Date": raw_cells[0]}
                    for idx in range(1, len(headers_list)):
                        col_name = headers_list[idx]
                        if col_name:
                            row_dict[col_name] = parse_farside_number(raw_cells[idx])
                    daily_records.append(row_dict)
            if daily_records:
                logger.info(f"成功通过边缘防屏蔽节点获取 Farside ETF 数据 ({daily_records[-1]['Date']})")
                return build_etf_summary(daily_records)
    except Exception as e:
        logger.warning(f"边缘节点获取 ETF 数据异常，尝试直连备用通道: {e}")

    # 通道 B：直连 Farside 表格
    try:
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        resp = scraper.get(FARSIDE_DIRECT_URL, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        target_table = next(
            (t for t in soup.find_all("table") if "IBIT" in t.get_text() and "Total" in t.get_text()),
            None,
        )
        if target_table:
            headers_list = []
            daily_records = []
            for tr in target_table.find_all("tr"):
                cells = [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
                if not cells:
                    continue
                if "IBIT" in cells and "Total" in cells:
                    headers_list = cells
                    continue
                if headers_list and len(cells) >= len(headers_list) and date_regex.match(cells[0]):
                    row_dict = {"Date": cells[0]}
                    for idx in range(1, len(headers_list)):
                        col_name = headers_list[idx]
                        if col_name:
                            row_dict[col_name] = parse_farside_number(cells[idx])
                    daily_records.append(row_dict)
            if daily_records:
                logger.info(f"成功通过直连通道获取 Farside ETF 数据 ({daily_records[-1]['Date']})")
                return build_etf_summary(daily_records)
    except Exception as e:
        logger.error(f"直连通道获取 Farside ETF 数据失败: {e}")

    return None


# ==================== 4. Coinbase 现货溢价指数模块 ====================
def fetch_coinbase_premium() -> Optional[Dict]:
    cb_price = None
    price_chg_24h = 0.0
    offshore_price = None
    offshore_source = ""

    try:
        resp = requests.get(
            "https://api.exchange.coinbase.com/products/BTC-USD/stats",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        if resp.status_code == 200:
            stats = resp.json()
            cb_price = float(stats["last"])
            open_24h = float(stats["open"])
            if open_24h > 0:
                price_chg_24h = ((cb_price - open_24h) / open_24h) * 100.0
    except Exception as e:
        logger.warning(f"Coinbase 行情获取失败: {e}")

    if not cb_price:
        return None

    offshore_endpoints = [
        ("OKX", "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT", lambda d: float(d["data"][0]["last"])),
        ("Bitfinex", "https://api-pub.bitfinex.com/v2/ticker/tBTCUST", lambda d: float(d[6])),
        ("Kraken", "https://api.kraken.com/0/public/Ticker?pair=XBTUSDT", lambda d: float(list(d["result"].values())[0]["c"][0])),
    ]

    for name, url, parser in offshore_endpoints:
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
            if r.status_code == 200:
                val = parser(r.json())
                if val > 0:
                    offshore_price = val
                    offshore_source = name
                    break
        except Exception:
            continue

    if not offshore_price:
        logger.error("所有离岸 USDT 现货行情源均请求失败。")
        return None

    premium_usd = cb_price - offshore_price
    premium_pct = (premium_usd / offshore_price) * 100.0

    logger.info(f"Coinbase 溢价率计算完成: {premium_pct:+.3f}% (CB: {cb_price:.1f} vs {offshore_source}: {offshore_price:.1f})")
    return {
        "cb_price": cb_price,
        "offshore_price": offshore_price,
        "offshore_source": offshore_source,
        "premium_usd": premium_usd,
        "premium_pct": premium_pct,
        "price_chg_24h": price_chg_24h,
    }


# ==================== 5. CME 期货年化基差 & 10Y 美债利率模块 ====================
def get_last_friday_of_month(year: int, month: int) -> date:
    last_day = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    while d.weekday() != calendar.FRIDAY:
        d = date(year, month, d.day - 1)
    return d


def get_cme_days_to_expiry() -> int:
    """计算 CME 主力合约剩余天数（距离当月交割不足 10 天时自动按次月合约展期计算）"""
    today = datetime.now(timezone.utc).date()
    expiry = get_last_friday_of_month(today.year, today.month)
    if (expiry - today).days <= 10:
        next_year = today.year + (1 if today.month == 12 else 0)
        next_month = 1 if today.month == 12 else today.month + 1
        expiry = get_last_friday_of_month(next_year, next_month)
    return max((expiry - today).days, 14)


def fetch_yahoo_quote(symbol: str) -> Optional[float]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            meta = resp.json()["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice") or meta.get("previousClose")
            if price and float(price) > 0:
                return float(price)
    except Exception as e:
        logger.warning(f"Yahoo Finance 获取 {symbol} 失败: {e}")
    return None


def fetch_basis_and_treasury(spot_price: Optional[float]) -> Optional[Dict]:
    cme_futures = fetch_yahoo_quote("BTC=F")
    if not spot_price:
        spot_price = fetch_yahoo_quote("BTC-USD")

    us10y = fetch_yahoo_quote("%5ETNX")
    if not us10y:
        try:
            r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10", timeout=15)
            if r.status_code == 200:
                for line in reversed(r.text.strip().splitlines()):
                    parts = line.split(",")
                    if len(parts) == 2 and parts[1] != "." and parts[1] != "DGS10":
                        us10y = float(parts[1])
                        break
        except Exception as e:
            logger.warning(f"FRED 10Y 美债利率备用源失败: {e}")

    if not (cme_futures and spot_price and us10y):
        return None

    days_to_expiry = get_cme_days_to_expiry()
    raw_basis_pct = ((cme_futures - spot_price) / spot_price) * 100.0
    annualized_basis_pct = raw_basis_pct * (365.0 / days_to_expiry)
    excess_spread_pct = annualized_basis_pct - us10y

    return {
        "cme_futures": cme_futures,
        "spot_price": spot_price,
        "days_to_expiry": days_to_expiry,
        "raw_basis_pct": raw_basis_pct,
        "annualized_basis_pct": annualized_basis_pct,
        "us10y_yield": us10y,
        "excess_spread_pct": excess_spread_pct,
    }


# ==================== 6. 美盘盘中静默雷达模式 (--intraday) ====================
def run_intraday_coinbase_radar() -> None:
    """仅在美盘时段每小时扫描 Coinbase 溢价：触发底背离或由负转正时立即推送，否则保持静默"""
    state = load_state()
    cb_data = fetch_coinbase_premium()
    if not cb_data:
        logger.warning("美盘盘中扫描：本次未能获取 Coinbase 溢价数据。")
        return

    prem = cb_data["premium_pct"]
    chg24 = cb_data["price_chg_24h"]
    prev_prem = state.get("last_cb_premium")

    alerts: List[str] = []

    # 1. 底背离触发：24h 下跌超过 2%，但 Coinbase 溢价大于 0%
    if chg24 <= THRESHOLDS["DIVERGENCE_PRICE_DROP_PCT"] and prem > 0:
        alerts.append(
            f"🔥 <b>美盘主力吸筹底背离</b>：BTC 24h 下跌 {chg24:.2f}%，但 Coinbase 现货溢价维持正值 ({prem:+.3f}%)！"
        )

    # 2. 溢价由负转正拐点触发
    if prev_prem is not None and prev_prem < 0 and prem > 0:
        alerts.append(
            f"🔄 <b>Coinbase 溢价由负转正拐点</b>：从 {prev_prem:+.3f}% 翻正至 {prem:+.3f}% (+${cb_data['premium_usd']:.1f})！"
        )
    elif prev_prem is None and prem > 0:
        alerts.append(
            f"🟢 <b>Coinbase 处于正溢价状态</b>：当前溢价率 {prem:+.3f}% (+${cb_data['premium_usd']:.1f})"
        )

    # 3. 强势正溢价突破 (+0.05%)
    if prem >= THRESHOLDS["CB_PREMIUM_POSITIVE_ALERT"] and (
        prev_prem is None or prev_prem < THRESHOLDS["CB_PREMIUM_POSITIVE_ALERT"]
    ):
        alerts.append(
            f"🇺🇸 <b>美盘现货买盘强势突破</b>：Coinbase 溢价率突破 +0.05%，升至 {prem:+.3f}%"
        )

    # 保存最新溢价记录供下一小时比对拐点
    state["last_cb_premium"] = round(prem, 4)
    save_state(state)

    if not alerts:
        logger.info(f"美盘盘中扫描完成：Coinbase 溢价 {prem:+.3f}%（未触发转正或底背离），保持静默。")
        return

    msg_lines = [
        "<b>⚡ 美盘盘中异动雷达：Coinbase 现货买盘信号</b>\n",
        "<b>【触发信号】</b>",
    ]
    for item in alerts:
        msg_lines.append(f"• {item}")

    msg_lines.extend([
        "",
        "<b>【实时盘面快照】</b>",
        f"• <b>Coinbase 现货:</b> <code>${cb_data['cb_price']:,.1f}</code> (24h: <code>{chg24:+.2f}%</code>)",
        f"• <b>离岸基准 ({cb_data['offshore_source']}):</b> <code>${cb_data['offshore_price']:,.1f}</code>",
        f"• <b>当前溢价率:</b> 🟢 <code>{prem:+.3f}%</code> (<code>{cb_data['premium_usd']:+.1f}</code> USD)",
    ])
    send_telegram_message("\n".join(msg_lines))


# ==================== 7. 常规全因子综合戦报引擎 ====================
def evaluate_and_alert(force: bool = False) -> None:
    state = load_state()

    cot_data = fetch_cftc_btc_cot()
    etf_data = fetch_farside_etf_flows()
    cb_data = fetch_coinbase_premium()
    spot_ref = cb_data["cb_price"] if cb_data else None
    macro_data = fetch_basis_and_treasury(spot_ref)

    alerts_triggered: List[str] = []
    has_new_data = False

    if etf_data:
        if etf_data["date"] != state.get("last_etf_date"):
            has_new_data = True
            state["last_etf_date"] = etf_data["date"]

        ibit = etf_data["ibit"]
        total = etf_data["total"]
        streak = etf_data["ibit_streak_days"]

        if ibit <= THRESHOLDS["IBIT_HEAVY_OUTFLOW_M"]:
            alerts_triggered.append(f"🚨 <b>IBIT 大额失血</b>：单日净流出 ${abs(ibit):.1f}M")
        if total >= THRESHOLDS["TOTAL_INFLOW_ALERT_M"]:
            alerts_triggered.append(f"🟢 <b>ETF 整体大幅净流入</b>：单日净流入 +${total:.1f}M")
        if streak >= THRESHOLDS["IBIT_INFLOW_STREAK_DAYS"]:
            alerts_triggered.append(f"✅ <b>右侧反转确认信号</b>：IBIT 已连续 {streak} 个交易日录得净流入！")
        if ibit < -50 and etf_data["mini_flow"] > 0:
            alerts_triggered.append("⚠️ <b>筹码结构分化</b>：巨鲸通道 IBIT 流出，但低费率迷你 ETF 保持净流入")

    if cot_data:
        if cot_data["report_date"] != state.get("last_cot_date"):
            has_new_data = True
            state["last_cot_date"] = cot_data["report_date"]

        net_contracts = cot_data["lev_net_contracts"]
        chg_contracts = cot_data["lev_net_chg_contracts"]

        if abs(chg_contracts) >= THRESHOLDS["COT_NET_CHANGE_CONTRACTS"]:
            direction = "空头回补（平仓）" if chg_contracts > 0 else "空头加码（建仓）"
            alerts_triggered.append(
                f"📊 <b>华尔街套利盘剧烈调仓</b>：杠杆基金净头寸单周变化 {chg_contracts:+d} 张 ({direction})"
            )
        if net_contracts < 0 and abs(net_contracts) <= THRESHOLDS["COT_UNWIND_EXHAUSTED_CONTRACTS"]:
            alerts_triggered.append(
                f"🧊 <b>期现套利空头已近枯竭</b>：杠杆基金净空头仅剩 {abs(net_contracts):,} 张 ({abs(cot_data['lev_net_btc']):,} BTC)"
            )

    if macro_data:
        spread = macro_data["excess_spread_pct"]
        if spread >= THRESHOLDS["BASIS_EXCESS_SPREAD_ALERT"]:
            alerts_triggered.append(
                f"💰 <b>期现套利窗口重开</b>：CME 年化基差高于 10Y 美债 {spread:+.2f}%，套利买盘具备进场动力"
            )

    if cb_data:
        prem = cb_data["premium_pct"]
        chg24 = cb_data["price_chg_24h"]
        state["last_cb_premium"] = round(prem, 4)

        if chg24 <= THRESHOLDS["DIVERGENCE_PRICE_DROP_PCT"] and prem > 0:
            alerts_triggered.append(
                f"🔥 <b>美盘主力吸筹底背离</b>：BTC 24h 下跌 {chg24:.2f}%，但 Coinbase 现货溢价逆势转正 ({prem:+.3f}%)！"
            )
        elif prem >= THRESHOLDS["CB_PREMIUM_POSITIVE_ALERT"]:
            alerts_triggered.append(
                f"🇺🇸 <b>美盘现货买盘强劲</b>：Coinbase 溢价率升至 {prem:+.3f}% (+${cb_data['premium_usd']:.1f})"
            )
        elif prem <= THRESHOLDS["CB_PREMIUM_NEGATIVE_ALERT"]:
            alerts_triggered.append(
                f"🩸 <b>美盘现货抛压沉重</b>：Coinbase 负溢价扩大至 {prem:+.3f}% (${cb_data['premium_usd']:.1f})"
            )

    if not has_new_data and not force:
        logger.info("暂无新的 ETF 或 CFTC 报告数据发布，跳过推送。")
        return

    msg_lines = ["<b>📡 华尔街 BTC 全因子机构监控战报</b>\n"]

    if alerts_triggered:
        msg_lines.append("<b>【核心交易信号触发】</b>")
        for item in alerts_triggered:
            msg_lines.append(f"• {item}")
        msg_lines.append("")

    if cb_data:
        prem_icon = "🟢" if cb_data["premium_pct"] > 0 else "🔴"
        msg_lines.extend([
            "<b>【1. 现货定价与 Coinbase 溢价】</b>",
            f"• <b>Coinbase 现货:</b> <code>${cb_data['cb_price']:,.1f}</code> (24h: <code>{cb_data['price_chg_24h']:+.2f}%</code>)",
            f"• <b>离岸基准 ({cb_data['offshore_source']}):</b> <code>${cb_data['offshore_price']:,.1f}</code>",
            f"• <b>Coinbase 溢价:</b> {prem_icon} <code>{cb_data['premium_pct']:+.3f}%</code> (<code>{cb_data['premium_usd']:+.1f}</code> USD)",
            "",
        ])

    if macro_data:
        sp = macro_data["excess_spread_pct"]
        sp_status = "套利有利可图" if sp >= 1.5 else ("微利/观望区间" if sp >= 0 else "倒挂/套利平仓压制")
        msg_lines.extend([
            f"<b>【2. 宏观利率锚 & CME 期货基差 (参考 {macro_data['days_to_expiry']} 天交割)】</b>",
            f"• <b>CME 期货主连:</b> <code>${macro_data['cme_futures']:,.1f}</code> (当期升水 <code>{macro_data['raw_basis_pct']:+.2f}%</code>)",
            f"• <b>CME 年化基差:</b> <code>{macro_data['annualized_basis_pct']:+.2f}%</code> | <b>10Y 美债:</b> <code>{macro_data['us10y_yield']:.2f}%</code>",
            f"• <b>套利超额利差:</b> <code>{sp:+.2f}%</code> ({sp_status})",
            "",
        ])

    if etf_data:
        streak = etf_data["ibit_streak_days"]
        streak_str = f"连流入 {streak} 日" if streak > 0 else (f"连流出 {abs(streak)} 日" if streak < 0 else "持平")
        msg_lines.extend([
            f"<b>【3. 美国现货 ETF 资金流 ({etf_data['date']})】</b>",
            f"• <b>全市场总计:</b> <code>{etf_data['total']:+.1f}M</code> USD",
            f"• <b>贝莱德 IBIT:</b> <code>{etf_data['ibit']:+.1f}M</code> USD ({streak_str})",
            f"• <b>富达 FBTC:</b> <code>{etf_data['fbtc']:+.1f}M</code> | <b>灰度 GBTC:</b> <code>{etf_data['gbtc']:+.1f}M</code>",
            f"• <b>低费率迷你盘 (BTC+BITB):</b> <code>{etf_data['mini_flow']:+.1f}M</code>",
            "",
        ])
    else:
        msg_lines.append("⚠️ <i>现货 ETF 数据源暂未更新</i>\n")

    if cot_data:
        net_c = cot_data["lev_net_contracts"]
        net_label = "净空头" if net_c < 0 else "净多头"
        msg_lines.extend([
            f"<b>【4. CME 持仓 & CFTC 杠杆基金 ({cot_data['report_date']})】</b>",
            f"• <b>CME 总持仓 (OI):</b> {cot_data['open_interest_contracts']:,} 张 ({cot_data['open_interest_btc']:,} BTC)",
            f"• <b>杠杆基金 {net_label}:</b> <code>{abs(net_c):,}</code> 张 (<b>{abs(cot_data['lev_net_btc']):,} BTC</b>)",
            f"• <b>单周净头寸变化:</b> <code>{cot_data['lev_net_chg_contracts']:+d}</code> 张 ({cot_data['lev_net_chg_btc']:+d} BTC)",
            f"• <b>多空明细:</b> 多单 {cot_data['lev_long']:,} / 空单 {cot_data['lev_short']:,} / 展期 {cot_data['lev_spread']:,}",
        ])
    else:
        msg_lines.append("⚠️ <i>CFTC 周度持仓报告暂未更新</i>")

    final_message = "\n".join(msg_lines)
    if send_telegram_message(final_message) and has_new_data:
        save_state(state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BTC 全因子机构监控脚本 V4")
    parser.add_argument("--force", action="store_true", help="忽略去重状态，强制抓取并发送一次战报")
    parser.add_argument("--intraday", action="store_true", help="美盘盘中静默雷达模式：仅触发溢价转正或底背离时发送")
    parser.add_argument("--test", action="store_true", help="仅测试 Telegram Bot 连通性")
    args = parser.parse_args()

    if args.test:
        send_telegram_message("✅ <b>BTC 全因子监控节点 V4 测试成功</b>：通道正常。")
    elif args.intraday:
        run_intraday_coinbase_radar()
    else:
        evaluate_and_alert(force=args.force)
