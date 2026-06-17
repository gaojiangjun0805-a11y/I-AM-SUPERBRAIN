#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
监控 X(Twitter) 博主 Serenity (@aleabitoreddit) 的推文，
检测到「推荐/讨论某只股票」相关内容时，通过 Telegram 立刻通知。

设计要点：
- 数据源：RapidAPI 上的 Twitter 抓取接口（默认 twitter-api45），有免费额度。
- 运行环境：GitHub Actions 定时任务（cron），免费长期在线。
- 去重：把已经处理过的推文 id 记录在 state/last_seen.json，避免重复通知。
- 通知：Telegram Bot。

所有密钥都通过环境变量（GitHub Secrets）读取，不写死在代码里。
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# 配置（可用环境变量覆盖）
# ---------------------------------------------------------------------------
TARGET_USERNAME = os.environ.get("TARGET_USERNAME", "aleabitoreddit")

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "").strip()
RAPIDAPI_HOST = os.environ.get("RAPIDAPI_HOST", "twitter-api45.p.rapidapi.com").strip()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# 一次最多通知多少条，避免首次运行或长时间未跑时刷屏
MAX_ALERTS_PER_RUN = int(os.environ.get("MAX_ALERTS_PER_RUN", "10"))

STATE_FILE = Path(__file__).resolve().parent / "state" / "last_seen.json"

# ---------------------------------------------------------------------------
# 选股相关性判断
# ---------------------------------------------------------------------------
# 美股 cashtag，例如 $AMD $NVDA $SIVE
CASHTAG_RE = re.compile(r"\$[A-Za-z]{1,6}(?:\.[A-Za-z])?\b")

# 投资/推荐相关关键词（中英文）。命中任意一个即视为「在聊投资」。
KEYWORDS = [
    # 英文
    "buy", "buying", "long", "longs", "position", "positions", "add", "adding",
    "accumulate", "accumulating", "entry", "entries", "my pick", "picks",
    "invest", "investing", "investment", "own ", "holding", "bought",
    "undervalued", "asymmetric", "multibagger", "bottleneck play", "trade",
    "target price", "price target", "upside", "conviction",
    # 中文
    "买入", "买进", "看多", "做多", "加仓", "建仓", "持仓", "推荐", "标的",
    "投资", "低估", "翻倍", "重仓", "上车", "布局",
]
KEYWORDS_LOWER = [k.lower() for k in KEYWORDS]


def is_stock_related(text: str):
    """返回 (是否相关, 命中的 cashtag 列表)。

    判定逻辑：出现 cashtag（最强信号）即相关；
    或文本里出现投资相关关键词也算相关。
    """
    if not text:
        return False, []
    tickers = sorted({t.upper() for t in CASHTAG_RE.findall(text)})
    if tickers:
        return True, tickers
    low = text.lower()
    for kw in KEYWORDS_LOWER:
        if kw in low:
            return True, tickers
    return False, tickers


# ---------------------------------------------------------------------------
# 状态读写（去重）
# ---------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"seen_ids": [], "last_id": None}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 只保留最近 500 条 id，防止文件无限增长
    state["seen_ids"] = state.get("seen_ids", [])[-500:]
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 抓取推文
# ---------------------------------------------------------------------------
def fetch_tweets():
    """从 RapidAPI 抓取目标用户的最新推文。

    返回标准化后的列表：[{id, text, created_at, url}, ...]，按时间新->旧。
    针对 twitter-api45 的返回结构做了适配，并做了防御性解析。
    """
    if not RAPIDAPI_KEY:
        raise RuntimeError("缺少 RAPIDAPI_KEY，请在 GitHub Secrets 中配置。")

    url = f"https://{RAPIDAPI_HOST}/timeline.php"
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": RAPIDAPI_HOST,
    }
    params = {"screenname": TARGET_USERNAME}

    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    raw = data.get("timeline") or data.get("tweets") or []
    tweets = []
    for item in raw:
        tid = str(
            item.get("tweet_id")
            or item.get("id_str")
            or item.get("id")
            or ""
        ).strip()
        text = (
            item.get("text")
            or item.get("full_text")
            or item.get("tweet")
            or ""
        )
        if not tid or not text:
            continue
        created = item.get("created_at") or item.get("date") or ""
        tweets.append(
            {
                "id": tid,
                "text": text,
                "created_at": created,
                "url": f"https://x.com/{TARGET_USERNAME}/status/{tid}",
            }
        )
    return tweets


# ---------------------------------------------------------------------------
# Telegram 通知
# ---------------------------------------------------------------------------
def send_telegram(message: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        raise RuntimeError(
            "缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID，请在 GitHub Secrets 中配置。"
        )
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    for attempt in range(4):
        try:
            r = requests.post(api, data=payload, timeout=30)
            if r.status_code == 200:
                return True
            print(f"[telegram] 发送失败 {r.status_code}: {r.text}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"[telegram] 网络错误: {e}", file=sys.stderr)
        time.sleep(2 ** attempt)
    return False


def build_message(tweet, tickers):
    tickers_line = "  ".join(tickers) if tickers else "（关键词命中，无明确代码）"
    text = tweet["text"].strip()
    # Telegram HTML 模式需要转义
    for a, b in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;")):
        text = text.replace(a, b)
    return (
        f"📈 <b>Serenity (@{TARGET_USERNAME}) 提到了股票</b>\n\n"
        f"<b>涉及标的：</b>{tickers_line}\n\n"
        f"{text}\n\n"
        f"🔗 {tweet['url']}"
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    state = load_state()
    seen = set(state.get("seen_ids", []))
    first_run = not seen  # 第一次运行（无历史）

    try:
        tweets = fetch_tweets()
    except Exception as e:
        print(f"[fetch] 抓取失败: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[fetch] 抓到 {len(tweets)} 条推文")

    # 找出未处理过的、且与股票相关的推文。时间新->旧，先翻成旧->新方便按序通知。
    new_relevant = []
    for tw in reversed(tweets):
        if tw["id"] in seen:
            continue
        related, tickers = is_stock_related(tw["text"])
        if related:
            new_relevant.append((tw, tickers))

    # 不管相不相关，都把抓到的 id 标记为已见，避免下次重复判断
    for tw in tweets:
        if tw["id"] not in seen:
            seen.add(tw["id"])
            state["seen_ids"].append(tw["id"])

    if tweets:
        state["last_id"] = tweets[0]["id"]

    if first_run:
        # 首次运行只建立基线，不把历史推文一股脑全推给你
        save_state(state)
        print("[run] 首次运行，已建立基线（不发送历史推文）。")
        return

    if not new_relevant:
        save_state(state)
        print("[run] 没有新的与股票相关的推文。")
        return

    sent = 0
    for tw, tickers in new_relevant[:MAX_ALERTS_PER_RUN]:
        msg = build_message(tw, tickers)
        if send_telegram(msg):
            sent += 1
            print(f"[notify] 已通知: {tw['id']} {tickers}")
        else:
            print(f"[notify] 通知失败: {tw['id']}", file=sys.stderr)

    save_state(state)
    print(f"[run] 完成，共通知 {sent} 条。")


if __name__ == "__main__":
    main()
