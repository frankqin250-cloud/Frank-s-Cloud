#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CME CFTC 杠杆基金净空头 & 美国现货 BTC ETF 资金流监控告警脚本 (V2 云端防屏蔽版)
"""

import os
import re
import sys
import json
import logging
import argparse
from typing import Dict, List, Optional

import requests
import cloudscraper
from bs4 import BeautifulSoup

# ==================== 配置区 ====================
TELEGRAM_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TG_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor_state.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.log")

# CFTC 官方开放数据 API (TFF 金融期货持仓报告) & 网页备用源
CFTC_API_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
CFTC_HTML_URL = "https://www.cftc.gov/dea/futures/financial_lf.htm"

# Farside ETF 边缘防屏蔽源 & 直连备用源
FARSIDE_JINA_URL = "https://r.jina.ai/https://farside.co.uk/btc/"
FARSIDE_DIRECT_URL = "https://farside.co.uk/btc/"

THRESHOLDS = {
    "IBIT_HEAVY_OUTFLOW_M": -150.0,        # IBIT 单日净流出警报阈值（百万美元）
    "TOTAL_INFLOW_ALERT_M": 100.0,         # 全市场 ETF 单日净流入警报阈值（百万美元）
    "IBIT_INFLOW_STREAK_DAYS": 2,          # IBIT 连续净流入右侧反转天数阈值
    "COT_NET_CHANGE_CONTRACTS": 1000,      # CFTC 杠杆基金单周调仓张数阈值
    "COT_UNWIND_EXHAUSTED_CONTRACTS": 3000 # CFTC 杠杆基金套利空头枯竭阈值
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("ETF_COT_Monitor")


def load_state() -> Dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"读取状态文件失败，将重新初始化: {e}")
    return {"last_cot_date": "", "last_etf_date": ""}


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


# ==================== 1. CFTC COT 解析模块（双源保障） ====================
def fetch_cftc_btc_cot() -> Optional[Dict]:
    # 通道 A：优先请求 CFTC 官方结构化 JSON API（最稳定，不受网页排版影响）
    try:
        params = {
            "cftc_contract_market_code": "133741",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": 2,
        }
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(CFTC_API_URL, params=params, headers=headers, timeout=20)
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

    # 通道 B：CFTC 网页备用解析（兼容 CFTC Code # 133741 空格格式）
    try:
        resp = requests.get(CFTC_HTML_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status()
        raw_text = resp.text

        pattern = re.compile(
            r"BITCOIN\s*-\s*CHICAGO MERCANTILE EXCHANGE.*?133741.*?(?=CFTC Code #|\Z)",
            re.DOTALL | re.IGNORECASE,
        )
        match = pattern.search(raw_text)
        if not match:
            logger.error("CFTC 备用网页仍未匹配到 133741 区块。")
            return None

        block = match.group(0)
        lines = [line.strip() for line in block.splitlines() if line.strip()]

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
            logger.error("CFTC 网页持仓数字列不完整。")
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


# ==================== 2. 现货 ETF 资金流解析模块（防 403 双通道） ====================
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

    # 通道 A：通过边缘阅读节点抓取（解决 GitHub Actions 云服务器 IP 被 Cloudflare 403 拦截问题）
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
                cells = [c.strip() for c in line.split("|") if c.strip()]
                if not cells:
                    continue
                if "IBIT" in cells and "Total" in cells:
                    headers_list = cells
                    continue
                if headers_list and date_regex.match(cells[0]) and len(cells) >= len(headers_list):
                    row_dict = {"Date": cells[0]}
                    for idx in range(1, len(headers_list)):
                        row_dict[headers_list[idx]] = parse_farside_number(cells[idx])
                    daily_records.append(row_dict)

            if daily_records:
                logger.info(f"成功通过边缘防屏蔽节点获取 Farside ETF 数据 ({daily_records[-1]['Date']})")
                return build_etf_summary(daily_records)
    except Exception as e:
        logger.warning(f"边缘节点获取 ETF 数据异常，尝试直连备用通道: {e}")

    # 通道 B：直连 Farside HTML 表格（适合本地电脑运行）
    try:
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        resp = scraper.get(FARSIDE_DIRECT_URL, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        target_table = None
        for table in soup.find_all("table"):
            if "IBIT" in table.get_text() and "Total" in table.get_text():
                target_table = table
                break

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
                if headers_list and date_regex.match(cells[0]) and len(cells) >= len(headers_list):
                    row_dict = {"Date": cells[0]}
                    for idx in range(1, len(headers_list)):
                        row_dict[headers_list[idx]] = parse_farside_number(cells[idx])
                    daily_records.append(row_dict)

            if daily_records:
                logger.info(f"成功通过直连通道获取 Farside ETF 数据 ({daily_records[-1]['Date']})")
                return build_etf_summary(daily_records)
    except Exception as e:
        logger.error(f"直连通道获取 Farside ETF 数据失败: {e}")

    return None


# ==================== 3. 告警规则评估与消息组装 ====================
def evaluate_and_alert(force: bool = False) -> None:
    state = load_state()
    cot_data = fetch_cftc_btc_cot()
    etf_data = fetch_farside_etf_flows()

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
            alerts_triggered.append(f"✅ <b>右侧反转信号</b>：IBIT 已连续 {streak} 个交易日录得净流入！")
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

    if not has_new_data and not force:
        logger.info("暂无新的 ETF 或 CFTC 报告数据发布，跳过推送。")
        return

    msg_lines = ["<b>📡 华尔街 BTC 机构资金与衍生品监控战报</b>\n"]

    if alerts_triggered:
        msg_lines.append("<b>【触发信号】</b>")
        for item in alerts_triggered:
            msg_lines.append(f"• {item}")
        msg_lines.append("")

    if etf_data:
        streak = etf_data["ibit_streak_days"]
        streak_str = f"连流入 {streak} 日" if streak > 0 else (f"连流出 {abs(streak)} 日" if streak < 0 else "持平")
        msg_lines.extend([
            f"<b>【美国现货 ETF 资金流 ({etf_data['date']})】</b>",
            f"• <b>全市场总计:</b> <code>{etf_data['total']:+.1f}M</code> USD",
            f"• <b>贝莱德 IBIT:</b> <code>{etf_data['ibit']:+.1f}M</code> USD ({streak_str})",
            f"• <b>富达 FBTC:</b> <code>{etf_data['fbtc']:+.1f}M</code> | <b>灰度 GBTC:</b> <code>{etf_data['gbtc']:+.1f}M</code>",
            f"• <b>低费率迷你盘 (BTC+BITB):</b> <code>{etf_data['mini_flow']:+.1f}M</code>",
            "",
        ])
    else:
        msg_lines.append("⚠️ <i>本次未获取到现货 ETF 数据（数据源可能正在更新中）</i>\n")

    if cot_data:
        net_c = cot_data["lev_net_contracts"]
        net_label = "净空头" if net_c < 0 else "净多头"
        msg_lines.extend([
            f"<b>【CME 期货 & CFTC 持仓 ({cot_data['report_date']})】</b>",
            f"• <b>CME 总持仓 (OI):</b> {cot_data['open_interest_contracts']:,} 张 ({cot_data['open_interest_btc']:,} BTC)",
            f"• <b>杠杆基金 {net_label}:</b> <code>{abs(net_c):,}</code> 张 (<b>{abs(cot_data['lev_net_btc']):,} BTC</b>)",
            f"• <b>单周净头寸变化:</b> <code>{cot_data['lev_net_chg_contracts']:+d}</code> 张 ({cot_data['lev_net_chg_btc']:+d} BTC)",
            f"• <b>多空明细:</b> 多单 {cot_data['lev_long']:,} / 空单 {cot_data['lev_short']:,} / 套利展期 {cot_data['lev_spread']:,}",
        ])
    else:
        msg_lines.append("⚠️ <i>本次未获取到 CFTC 周度持仓报告</i>")

    final_message = "\n".join(msg_lines)
    if send_telegram_message(final_message) and has_new_data:
        save_state(state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BTC ETF & CFTC COT 监控脚本")
    parser.add_argument("--force", action="store_true", help="忽略去重状态，强制抓取并发送一次战报")
    parser.add_argument("--test", action="store_true", help="仅测试 Telegram Bot 连通性")
    args = parser.parse_args()

    if args.test:
        send_telegram_message("✅ <b>BTC 机构监控节点测试成功</b>：服务器网络与 Telegram Bot 通道正常。")
    else:
        evaluate_and_alert(force=args.force)
