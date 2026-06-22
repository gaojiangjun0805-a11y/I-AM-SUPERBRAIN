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

# 企业微信群机器人（国内可用，免费）。填 webhook 的 key 或完整 url 任一即可。
WECOM_KEY = os.environ.get("WECOM_WEBHOOK_KEY", "").strip()
WECOM_URL = os.environ.get("WECOM_WEBHOOK_URL", "").strip()
if not WECOM_URL and WECOM_KEY:
    WECOM_URL = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM_KEY}"

# Bark（iPhone 推送，国内直连、秒到、免费）。装 Bark App，复制它给的 key 填进来。
BARK_KEY = os.environ.get("BARK_KEY", "").strip()
BARK_SERVER = (os.environ.get("BARK_SERVER", "").strip() or "https://api.day.app").rstrip("/")

# 用大模型把英文推文总结成中文（可选）。没配 key 就跳过总结，照常推原文。
# 方案一（推荐，国内可直连/支付宝充值）：OpenAI 兼容接口（DeepSeek / 通义 / Kimi / 智谱 / OpenAI 本身）
#   配 OPENAI_API_KEY，并按所选服务商设置 OPENAI_BASE_URL 和 OPENAI_MODEL。
# 方案二：Anthropic Claude（需国外网络+国外卡）。配 ANTHROPIC_API_KEY 即可。
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = (os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.deepseek.com/v1")
OPENAI_MODEL = (os.environ.get("OPENAI_MODEL", "").strip() or "deepseek-chat")

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


def send_wecom(text: str):
    """发到企业微信群机器人（纯文本）。"""
    if not WECOM_URL:
        return False
    for attempt in range(4):
        try:
            r = requests.post(
                WECOM_URL,
                json={"msgtype": "text", "text": {"content": text}},
                timeout=30,
            )
            if r.status_code == 200 and r.json().get("errcode") == 0:
                return True
            print(f"[wecom] 发送失败: {r.status_code} {r.text}", file=sys.stderr)
        except (requests.RequestException, ValueError) as e:
            print(f"[wecom] 网络错误: {e}", file=sys.stderr)
        time.sleep(2 ** attempt)
    return False


def notify_all(tg_html: str, plain_text: str, bark_url: str = None):
    """发到所有已配置的渠道（Bark + 企业微信 + Telegram）。任一成功即视为成功。"""
    results = []
    if BARK_KEY:
        # 标题取首行，正文取其余，点通知可跳转推文链接
        lines = plain_text.split("\n", 1)
        title = lines[0].strip() or "Serenity 股票提醒"
        body = lines[1].strip() if len(lines) > 1 else plain_text
        results.append(send_bark(title, body, bark_url))
    if WECOM_URL:
        results.append(send_wecom(plain_text))
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        results.append(send_telegram(tg_html))
    if not results:
        print("[notify] 未配置任何通知渠道（Bark/企业微信/Telegram）", file=sys.stderr)
        return False
    return any(results)


def send_bark(title: str, body: str, url: str = None):
    """发到 Bark（iPhone 推送）。"""
    if not BARK_KEY:
        return False
    payload = {"title": title, "body": body, "group": "Serenity"}
    if url:
        payload["url"] = url
    for attempt in range(4):
        try:
            r = requests.post(f"{BARK_SERVER}/{BARK_KEY}", json=payload, timeout=30)
            if r.status_code == 200 and r.json().get("code") == 200:
                return True
            print(f"[bark] 发送失败: {r.status_code} {r.text}", file=sys.stderr)
        except (requests.RequestException, ValueError) as e:
            print(f"[bark] 网络错误: {e}", file=sys.stderr)
        time.sleep(2 ** attempt)
    return False


SUMMARY_PROMPT = (
    "下面是 X 博主 Serenity(@aleabitoreddit，AI/半导体供应链分析) 的一条推文。"
    "用简体中文做一段简短总结，重点说清楚：他提到或看好哪只股票(用股票代码)、"
    "他的核心逻辑/理由是什么。如果只是闲聊、与投资无关，就回复『（非投资内容）』。"
    "直接给总结，不要客套话，控制在 120 字以内。\n\n推文原文：\n"
)


def _summarize_openai(text):
    """调用 OpenAI 兼容接口（DeepSeek / 通义 / Kimi / 智谱 / OpenAI）。"""
    r = requests.post(
        f"{OPENAI_BASE_URL.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": OPENAI_MODEL,
            "max_tokens": 400,
            "messages": [{"role": "user", "content": SUMMARY_PROMPT + text}],
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def _summarize_anthropic(text):
    """调用 Anthropic Claude 接口。"""
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
            "messages": [{"role": "user", "content": SUMMARY_PROMPT + text}],
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip()


def summarize_zh(text):
    """把推文总结成中文（他看好/推荐哪只票、逻辑是什么）。

    优先用 OpenAI 兼容接口（国内可用），其次 Anthropic。
    都没配 key 或调用失败时返回 None，调用方据此跳过中文段落。
    """
    try:
        if OPENAI_API_KEY:
            return _summarize_openai(text) or None
        if ANTHROPIC_API_KEY:
            return _summarize_anthropic(text) or None
    except (requests.RequestException, KeyError, IndexError) as e:
        print(f"[summary] 总结失败: {e}", file=sys.stderr)
    return None


def build_messages(tweet, tickers):
    """生成两种格式：(Telegram 用 HTML, 企业微信用纯文本)。中文总结只算一次。"""
    tickers_line = "  ".join(tickers) if tickers else "（关键词命中，无明确代码）"
    summary = summarize_zh(tweet["text"])
    raw = tweet["text"].strip()

    # 企业微信：纯文本
    plain_summary = f"中文总结：{summary}\n\n" if summary else ""
    plain = (
        f"📈 Serenity (@{TARGET_USERNAME}) 提到了股票\n\n"
        f"涉及标的：{tickers_line}\n\n"
        f"{plain_summary}"
        f"原文：{raw}\n\n"
        f"🔗 {tweet['url']}"
    )

    # Telegram：HTML（需转义）
    def esc(s):
        for a, b in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;")):
            s = s.replace(a, b)
        return s

    tg_summary = f"<b>中文总结：</b>{esc(summary)}\n\n" if summary else ""
    tg = (
        f"📈 <b>Serenity (@{TARGET_USERNAME}) 提到了股票</b>\n\n"
        f"<b>涉及标的：</b>{tickers_line}\n\n"
        f"{tg_summary}"
        f"<b>原文：</b>{esc(raw)}\n\n"
        f"🔗 {tweet['url']}"
    )
    return tg, plain


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
            tg, plain = build_messages(tw, tickers)
            head_tg = "✅ <b>测试成功：监控运行正常</b>\n下面是 Serenity 最近一条涉股推文（样例）：\n\n"
            head_plain = "✅ 测试成功：监控运行正常\n下面是 Serenity 最近一条涉股推文（样例）：\n\n"
            ok = notify_all(head_tg + tg, head_plain + plain)
        else:
            tip = (
                f"已能抓到 @{TARGET_USERNAME} 的推文（共 {len(tweets)} 条），"
                "最近暂无涉股内容。有新涉股推文会第一时间通知你。"
            )
            ok = notify_all(
                "✅ <b>测试成功：监控运行正常</b>\n" + tip,
                "✅ 测试成功：监控运行正常\n" + tip,
            )
        print(f"[test] 测试推送 {'成功' if ok else '失败'}")
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
        tg, plain = build_messages(tw, tickers)
        if notify_all(tg, plain, bark_url=tw["url"]):
            sent += 1
            print(f"[notify] 已通知: {tw['id']} {tickers}")
        else:
            print(f"[notify] 通知失败: {tw['id']}", file=sys.stderr)

    save_state(state)
    print(f"[run] 完成，共通知 {sent} 条。")


if __name__ == "__main__":
    main()
