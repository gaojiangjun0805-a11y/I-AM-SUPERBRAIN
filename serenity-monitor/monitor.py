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
# 默认用 Twttr API (twitter241)。它需要两步：先用 username 换 rest_id，再拉时间线。
# 用 `or` 兜底：GitHub 未配置该 secret 时会传入空字符串。
RAPIDAPI_HOST = (os.environ.get("RAPIDAPI_HOST", "").strip() or "twitter241.p.rapidapi.com")
# 一次拉多少条
TWEET_COUNT = int(os.environ.get("TWEET_COUNT", "20"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# 用 Claude 把英文推文总结成中文（可选）。没配 key 就跳过总结，照常推原文。
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = (os.environ.get("ANTHROPIC_MODEL", "").strip() or "claude-haiku-4-5")

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
def _rapidapi_get(path, params):
    """对 RapidAPI 发一次 GET，带重试，返回解析后的 JSON。"""
    url = f"https://{RAPIDAPI_HOST}{path}"
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": RAPIDAPI_HOST,
    }
    last_err = None
    for attempt in range(4):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            last_err = e
            print(f"[rapidapi] {path} 第{attempt+1}次失败: {e}", file=sys.stderr)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"RapidAPI 请求失败: {path} -> {last_err}")


def _find_first(obj, keys):
    """在嵌套 JSON 里递归找第一个出现的指定 key 的值。"""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and isinstance(obj[k], (str, int)):
                return obj[k]
        for v in obj.values():
            r = _find_first(v, keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_first(v, keys)
            if r is not None:
                return r
    return None


def _extract_tweets(obj, found):
    """递归扫描整个 JSON 树，凡是同时含 full_text/text 和 id 的对象都当作一条推文。

    这样无论返回结构怎么嵌套都能稳定提取，不依赖具体路径。
    """
    if isinstance(obj, dict):
        text = obj.get("full_text") or obj.get("text")
        tid = (
            obj.get("id_str")
            or obj.get("rest_id")
            or obj.get("tweet_id")
            or obj.get("conversation_id_str")
        )
        # 只认那种像「推文正文对象」的：有文本、有数字 id、且有 created_at
        if (
            isinstance(text, str)
            and text.strip()
            and tid
            and str(tid).isdigit()
            and obj.get("created_at")
        ):
            tid = str(tid)
            if tid not in found:
                found[tid] = {
                    "id": tid,
                    "text": text,
                    "created_at": obj.get("created_at", ""),
                    "url": f"https://x.com/{TARGET_USERNAME}/status/{tid}",
                }
        for v in obj.values():
            _extract_tweets(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _extract_tweets(v, found)


def fetch_tweets(state=None):
    """抓取目标用户最新推文，兼容两类 RapidAPI 接口。

    - twitter241 (host 含 "241")：两步，先 /user?username= 拿 rest_id（缓存复用以省额度），
      再 /user-tweets?user= 拉时间线。
    - 其它（twitter-api45 等）：一步 /timeline.php?screenname= 直接拉。

    都用递归提取，返回 [{id, text, created_at, url}, ...]，按 id 从新到旧。
    """
    if not RAPIDAPI_KEY:
        raise RuntimeError("缺少 RAPIDAPI_KEY，请在 GitHub Secrets 中配置。")

    state = state if state is not None else {}

    if "241" in RAPIDAPI_HOST:
        # rest_id 永不变，缓存下来后续每次只需 1 次请求，省额度
        rest_id = state.get("rest_id")
        if not rest_id:
            user_data = _rapidapi_get("/user", {"username": TARGET_USERNAME})
            rest_id = _find_first(user_data, ["rest_id"]) or _find_first(
                user_data, ["id_str", "id"]
            )
            if not rest_id:
                raise RuntimeError(
                    f"没能从 /user 拿到 rest_id，返回片段: {str(user_data)[:300]}"
                )
            rest_id = str(rest_id)
            state["rest_id"] = rest_id
            print(f"[fetch] 取得并缓存 rest_id = {rest_id}")
        else:
            print(f"[fetch] 复用缓存 rest_id = {rest_id}")
        tl = _rapidapi_get("/user-tweets", {"user": rest_id, "count": str(TWEET_COUNT)})
    else:
        # twitter-api45 等：一步直接用 screenname 拉时间线
        tl = _rapidapi_get("/timeline.php", {"screenname": TARGET_USERNAME})

    found = {}
    _extract_tweets(tl, found)
    # id 越大越新，按 id 数值降序
    tweets = sorted(found.values(), key=lambda t: int(t["id"]), reverse=True)
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


def summarize_zh(text):
    """用 Claude Haiku 把推文总结成中文（他看好/推荐哪只票、逻辑是什么）。

    没配 ANTHROPIC_API_KEY 或调用失败时返回 None，调用方据此跳过中文段落。
    """
    if not ANTHROPIC_API_KEY:
        return None
    prompt = (
        "下面是 X 博主 Serenity(@aleabitoreddit，AI/半导体供应链分析) 的一条推文。"
        "用简体中文做一段简短总结，重点说清楚：他提到或看好哪只股票(用股票代码)、"
        "他的核心逻辑/理由是什么。如果只是闲聊、与投资无关，就回复『（非投资内容）』。"
        "直接给总结，不要客套话，控制在 120 字以内。\n\n推文原文：\n" + text
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        summary = "".join(parts).strip()
        return summary or None
    except requests.RequestException as e:
        print(f"[summary] Claude 总结失败: {e}", file=sys.stderr)
        return None


def build_message(tweet, tickers):
    tickers_line = "  ".join(tickers) if tickers else "（关键词命中，无明确代码）"

    def esc(s):
        for a, b in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;")):
            s = s.replace(a, b)
        return s

    text = esc(tweet["text"].strip())
    summary = summarize_zh(tweet["text"])
    summary_block = f"<b>中文总结：</b>{esc(summary)}\n\n" if summary else ""

    return (
        f"📈 <b>Serenity (@{TARGET_USERNAME}) 提到了股票</b>\n\n"
        f"<b>涉及标的：</b>{tickers_line}\n\n"
        f"{summary_block}"
        f"<b>原文：</b>{text}\n\n"
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
        tweets = fetch_tweets(state)
    except Exception as e:
        print(f"[fetch] 抓取失败: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[fetch] 抓到 {len(tweets)} 条推文")

    # 测试模式：手动触发时验证 Telegram 通不通，并把最近一条涉股推文当样例发出来。
    if os.environ.get("SEND_TEST", "").strip().lower() in ("1", "true", "yes"):
        sample = None
        for tw in tweets:  # tweets 已按新->旧排序
            related, tickers = is_stock_related(tw["text"])
            if related:
                sample = (tw, tickers)
                break
        if sample:
            tw, tickers = sample
            head = "✅ <b>测试成功：监控运行正常</b>\n下面是 Serenity 最近一条涉股推文（样例）：\n\n"
            body = build_message(tw, tickers)
            ok = send_telegram(head + body)
        else:
            ok = send_telegram(
                "✅ <b>测试成功：监控运行正常</b>\n"
                f"已能抓到 @{TARGET_USERNAME} 的推文（共 {len(tweets)} 条），"
                "最近暂无涉股内容。有新涉股推文会第一时间通知你。"
            )
        print(f"[test] Telegram 测试发送 {'成功' if ok else '失败'}")
        return


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
