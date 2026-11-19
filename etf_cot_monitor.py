#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CME CFTC 杠杆基金净空头 & 美国现货 BTC ETF 资金流监控告警脚本
数据源:
  1. CFTC TFF Report: https://www.cftc.gov/dea/futures/financial_lf.htm
  2. Farside BTC ETF Flow: https://farside.co.uk/btc/
"""

import os
import re
import sys
import json
import logging
import argparse
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests
import cloudscraper
from bs4 import BeautifulSoup

# ==================== 配置区（建议使用环境变量覆盖） ====================
TELEGRAM_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TG_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor_state.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.log")

CFTC_URL = "https://www.cftc.gov/dea/futures/financial_lf.htm"
FARSIDE_URL = "https://farside.co.uk/btc/"

# 告警阈值配置
THRESHOLDS = {
    # IBIT 单日净流出超过该值（百万美元）触发大额失血警报
    "IBIT_HEAVY_OUTFLOW_M": -150.0,
    # 全市场 ETF 单日净流入超过该值（百万美元）触发买盘回流警报
    "TOTAL_INFLOW_ALERT_M": 100.0,
    # IBIT 连续净流入达到该天数，触发“套利/抛压止血右侧信号”
    "IBIT_INFLOW_STREAK_DAYS": 2,
    # CFTC 杠杆基金净空头单周变化超过该张数（1张 = 5 BTC）触发剧烈调仓警报
    "COT_NET_CHANGE_CONTRACTS": 1000,
    # CFTC 杠杆基金剩余净空头低于该张数，提示“套利平仓已近枯竭”
    "COT_UNWIND_EXHAUSTED_CONTRACTS": 3000,
}

# ==================== 日志初始化 ====================
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
    if "YOUR_TELEGRAM" in TELEGRAM_BOT_TOKEN or "YOUR_TELEGRAM" in TELEGRAM_CHAT_ID:
        logger.error("未配置有效的 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID！")
        print("\n--- 模拟推送消息预览 ---\n" + text + "\n------------------------\n")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        logger.info("Telegram 告警推送成功。")
        return True
    except Exception as e:
        logger.error(f"Telegram 推送失败: {e}")
        return False


# ==================== 1. CFTC COT 解析模块 ====================
def fetch_cftc_btc_cot() -> Optional[Dict]:
    """
    抓取并解析 CFTC Traders in Financial Futures (financial_lf.htm)
    锁定标准合约: BITCOIN - CHICAGO MERCANTILE EXCHANGE (CFTC Code #133741, 5 BTC/张)
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36"
    }
    try:
        resp = requests.get(CFTC_URL, headers=headers, timeout=30)
        resp.raise_for_status()
        raw_text = resp.text
    except Exception as e:
        logger.error(f"获取 CFTC 报告失败: {e}")
        return None

    # 精确匹配标准 CME 比特币合约区块（排除 MICRO BITCOIN）
    pattern = re.compile(
        r"(?m)^BITCOIN - CHICAGO MERCANTILE EXCHANGE\s+CFTC Code #133741.*?(?=\n[A-Z0-9\s\-/]+CFTC Code #|\Z)",
        re.DOTALL,
    )
    match = pattern.search(raw_text)
    if not match:
        logger.error("未能在 CFTC 报告中匹配到标准 BITCOIN (#133741) 区块。")
        return None

    block = match.group(0)
    lines = [line.strip() for line in block.splitlines() if line.strip()]

    # 提取报告截止日期 (如: Positions as of November 10, 2026)
    report_date = "Unknown"
    open_interest = 0
    pos_nums: List[int] = []
    chg_nums: List[int] = []

    for i, line in enumerate(lines):
        if "Open Interest is" in line:
            oi_match = re.search(r"Open Interest is\s+([\d,]+)", line)
            if oi_match:
                open_interest = int(oi_match.group(1).replace(",", ""))

        if line.startswith("Positions") or "Futures Only Positions as of" in line:
            date_match = re.search(r"([A-Za-z]+\s+\d{1,2},\s+\d{4})", line)
            if date_match:
                report_date = date_match.group(1)

        # 提取 Positions 数值行（包含 14 个整数：Dealer[3], AssetMgr[3], LevFunds[3], Other[3], NonRep[2]）
        if line.startswith("All") and not pos_nums:
            nums = [int(x.replace(",", "")) for x in re.findall(r"-?[\d,]+", line)]
            if len(nums) >= 14:
                pos_nums = nums

        # 兼容部分格式中数字直接在 Positions 下一行的情况
        if line == "Positions" and i + 1 < len(lines) and not pos_nums:
            nums = [int(x.replace(",", "")) for x in re.findall(r"-?[\d,]+", lines[i + 1])]
            if len(nums) >= 14:
                pos_nums = nums

        # 提取 Changes from 周度变化数值行
        if line.startswith("Changes from:") and i + 1 < len(lines) and not chg_nums:
            nums = [int(x.replace(",", "")) for x in re.findall(r"[+-]?[\d,]+", lines[i + 1])]
            if len(nums) >= 14:
                chg_nums = nums

    if len(pos_nums) < 14:
        logger.error("CFTC 持仓数字列解析不完整，请检查网页格式。")
        return None

    # TFF 报告列索引: [6]=Lev_Long, [7]=Lev_Short, [8]=Lev_Spreading
    lev_long = pos_nums[6]
    lev_short = pos_nums[7]
    lev_spread = pos_nums[8]
    lev_net = lev_long - lev_short  # 负值代表净空头

    lev_net_chg = 0
    if len(chg_nums) >= 14:
        lev_net_chg = chg_nums[6] - chg_nums[7]

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


# ==================== 2. Farside 现货 ETF 解析模块 ====================
def parse_farside_number(val: str) -> float:
    """将 Farside 表格单元格转为浮点数，例如 '(209.5)' -> -209.5, '-' -> 0.0"""
    s = val.strip().replace(",", "")
    if not s or s == "-" or s.lower() == "n/a":
        return 0.0
    if s.startswith("(") and s.endswith(")"):
        return -float(s[1:-1])
    try:
        return float(s)
    except ValueError:
        return 0.0


def fetch_farside_etf_flows() -> Optional[Dict]:
    """抓取并解析 Farside Investors 美国现货 BTC ETF 每日资金流"""
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "linux", "mobile": False}
    )
    try:
        resp = scraper.get(FARSIDE_URL, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"获取 Farside ETF 数据失败: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    target_table = None
    for table in soup.find_all("table"):
        text = table.get_text()
        if "IBIT" in text and "Total" in text:
            target_table = table
            break

    if not target_table:
        logger.error("未在 Farside 页面找到包含 IBIT 的数据表。")
        return None

    rows = target_table.find_all("tr")
    headers: List[str] = []
    daily_records: List[Dict] = []

    date_regex = re.compile(r"^\d{1,2}\s+[A-Za-z]{3}\s+\d{4}$")

    for tr in rows:
        cells = [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
        if not cells:
            continue

        # 识别表头行
        if "IBIT" in cells and "Total" in cells:
            headers = cells
            continue

        # 仅保留日期格式的有效交易日行（自动排除底部的 Total / Average / Maximum 汇总行）
        if headers and date_regex.match(cells[0]) and len(cells) >= len(headers):
            row_dict = {"Date": cells[0]}
            for idx in range(1, len(headers)):
                ticker = headers[idx]
                row_dict[ticker] = parse_farside_number(cells[idx])
            daily_records.append(row_dict)

    if not daily_records:
        logger.error("Farside ETF 有效交易日记录为空。")
        return None

    latest = daily_records[-1]

    # 计算 IBIT 连续净流入或净流出天数
    streak_days = 0
    streak_dir = 0  # 1=净流入, -1=净流出
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

    # 迷你低费率 ETF 合并流向 (Grayscale BTC + Bitwise BITB)
    mini_flow = latest.get("BTC", 0.0) + latest.get("BITB", 0.0)

    return {
        "date": latest["Date"],
        "ibit": latest.get("IBIT", 0.0),
        "fbtc": latest.get("FBTC", 0.0),
        "gbtc": latest.get("GBTC", 0.0),
        "mini_flow": round(mini_flow, 2),
        "total": latest.get("Total", 0.0),
        "ibit_streak_days": streak_days * streak_dir,  # 正数为连涨天数，负数为连跌天数
    }


# ==================== 3. 告警规则评估与消息组装 ====================
def evaluate_and_alert(force: bool = False) -> None:
    state = load_state()
    cot_data = fetch_cftc_btc_cot()
    etf_data = fetch_farside_etf_flows()

    alerts_triggered: List[str] = []
    has_new_data = False

    # --- 检查 ETF 数据 ---
    if etf_data:
        is_new_etf = etf_data["date"] != state.get("last_etf_date")
        if is_new_etf:
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

    # --- 检查 CFTC COT 数据 ---
    if cot_data:
        is_new_cot = cot_data["report_date"] != state.get("last_cot_date")
        if is_new_cot:
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

    # 仅在有新数据更新，或用户手动指定 --force 时发送通知
    if not has_new_data and not force:
        logger.info("暂无新的 ETF 或 CFTC 报告数据发布，跳过推送。")
        return

    # 构建 Telegram 战报
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

    final_message = "\n".join(msg_lines)
    if send_telegram_message(final_message) or force:
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
