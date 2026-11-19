#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
华尔街 BTC 机构资金、衍生品与宏观利差全因子监控告警脚本 (V5 完整修复版)
更新说明:
  1. 新增对云端 IP 友好的 SatsIntel ETF 资金流主通道，彻底解决 Farside 403 导致的 ETF 数据缺失
  2. 保留 --intraday 美盘盘中静默雷达（仅在 Coinbase 溢价由负转正或触发吸筹底背离时推送）
  3. 保留 CME 期货自动展期基差测算与 CFTC 官方 JSON API 持仓监控
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

# 数据源接口地址
CFTC_API_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
CFTC_HTML_URL = "https://www.cftc.gov/dea/futures/financial_lf.htm"

ETF_PRIMARY_URL = "https://www.satsintel.io/en/etfs/flows"
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
        logger.info("Telegram 战报推送成功。")
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


# ==================== 3. 美国现货 ETF 资金流模块（多源云端免拦截） ====================
def parse_flow_number(val: str) -> float:
    """统一清洗各类财务符号: '−$209.5M' / '(209.5)' / '+$7.9M' / '-' -> float"""
    s = val.strip().replace(",", "").replace("$", "").replace("M", "").replace("m", "")
    s = s.replace("−", "-").replace("–", "-").replace("—", "-")
    if not s or s == "-" or s.lower() == "n/a":
        return 0.0
    if s.startswith("(") and s.endswith(")"):
        return -float(s[1:-1])
    try:
        return float(s)
    except ValueError:
        return 0.0


def build_etf_summary(daily_records: List[Dict], latest_first: bool = False) -> Dict:
    """
    daily_records: 按日期排序的每日字典列表
    latest_first: True 表示 index 0 是最新一天（如 SatsIntel），False 表示 index -1 是最新一天（如 Farside）
    """
    ordered = daily_records if latest_first else list(reversed(daily_records))
    latest = ordered[0]

    streak_days = 0
    streak_dir = 0
    for rec in ordered:
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
    headers_browser = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    # 通道 A（主通道）：SatsIntel 静态 SSR 表格（不封锁 GitHub 云端 IP）
    try:
        resp = requests.get(ETF_PRIMARY_URL, headers=headers_browser, timeout=25)
        if resp.status_code == 200 and "IBIT" in resp.text:
            soup = BeautifulSoup(resp.text, "html.parser")
            for table in soup.find_all("table"):
                t_text = table.get_text()
                if "IBIT" not in t_text or "FBTC" not in t_text:
                    continue
                rows = table.find_all("tr")
                col_headers: List[str] = []
                records: List[Dict] = []

                for tr in rows:
                    cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                    if not cells:
                        continue
                    # 匹配表头行
                    if any("IBIT" in c for c in cells) and any("FBTC" in c for c in cells):
                        col_headers = []
                        for c in cells:
                            m = re.search(r"\b(IBIT|FBTC|GBTC|BITB|ARKB|HODL|BRRR|EZBC|BTCO|BTCW|DEFI|BTC|Total|Net)\b", c)
                            col_headers.append(m.group(1) if m else c)
                        continue

                    if col_headers and len(cells) >= 5:
                        date_str = cells[0]
                        # 识别常见日期格式 (如 "Nov 17, 2026" / "2026-11-17" / "17 Nov 2026" / "Tue, Nov 17")
                        if re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\d{4}-\d{2}-\d{2})\b", date_str):
                            row_dict = {"Date": date_str}
                            for idx in range(1, min(len(col_headers), len(cells))):
                                key = col_headers[idx]
                                if key in ("Net", "Total"):
                                    key = "Total"
                                row_dict[key] = parse_flow_number(cells[idx])
                            # 若表格未单独列 Total 列，则自动求和所有已识别 ETF 列
                            if "Total" not in row_dict:
                                row_dict["Total"] = round(
                                    sum(v for k, v in row_dict.items() if k != "Date" and isinstance(v, float)), 2
                                )
                            records.append(row_dict)

                if records:
                    # 判断表格是最新日期在第一行还是最后一行
                    latest_first = True
                    if len(records) >= 2:
                        # SatsIntel 默认最新日期在最上方
                        latest_first = True
                    logger.info(f"成功通过 SatsIntel 主通道获取 ETF 数据 ({records[0]['Date']})")
                    return build_etf_summary(records, latest_first=latest_first)
        else:
            logger.warning(f"SatsIntel 主通道返回状态码: {resp.status_code}")
    except Exception as e:
        logger.warning(f"SatsIntel 主通道解析异常，切换至备用通道: {e}")

    # 通道 B（备用）：Farside 直连 / 边缘节点
    date_regex = re.compile(r"^\d{1,2}\s+[A-Za-z]{3}\s+\d{4}$")
    try:
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        resp = scraper.get(FARSIDE_DIRECT_URL, timeout=25)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            target_table = next(
                (t for t in soup.find_all("table") if "IBIT" in t.get_text() and "Total" in t.get_text()),
                None,
            )
            if target_table:
                headers_list: List[str] = []
                daily_records: List[Dict] = []
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
                                row_dict[col_name] = parse_flow_number(cells[idx])
                        daily_records.append(row_dict)
                if daily_records:
                    logger.info(f"成功通过 Farside 备用通道获取 ETF 数据 ({daily_records[-1]['Date']})")
                    return build_etf_summary(daily_records, latest_first=False)
        else:
            logger.warning(f"Farside 备用通道被屏蔽 (HTTP {resp.status_code})")
    except Exception as e:
        logger.error(f"Farside 备用通道异常: {e}")

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
            price = meta.get("regularMarketPrice
