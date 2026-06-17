# Serenity 股票推文监控

自动监控 X(Twitter) 博主 **Serenity (@aleabitoreddit)** 的推文，
一旦检测到他在「推荐股票 / 讨论投资哪只标的」，立刻通过 **Telegram** 通知你。

## 它是怎么工作的

```
GitHub Actions 定时任务(每15分钟)  →  RapidAPI 抓取他的最新推文
   →  检测是否提到股票($代码 或 买入/看多/推荐等关键词)
   →  命中就用 Telegram Bot 推到你手机
   →  用 state/last_seen.json 记录已通知过的，避免重复
```

- **免费**：GitHub Actions + RapidAPI 免费额度 + Telegram Bot 都不花钱。
- **延迟**：约 15 分钟级（cron 最小间隔限制），不是真·秒级实时。想更快可把 cron 改成 `*/5`。

## ⚠️ 必读：定时任务只在默认分支生效

GitHub 的 cron 定时任务**只会从仓库默认分支（main/master）上的 workflow 文件触发**。
所以这套代码现在在 `claude/stock-recommendation-alerts-j2bjck` 分支上，
**你需要把它合并到默认分支，cron 才会真正开始跑。** 合并前可以先在 Actions 页面用
「Run workflow」手动触发测试。

## 配置步骤（约 10 分钟）

你需要准备 3 个密钥，填到仓库的 **Settings → Secrets and variables → Actions → New repository secret**。

### 1. Telegram Bot Token（`TELEGRAM_BOT_TOKEN`）
1. 在 Telegram 里搜索 **@BotFather**，发送 `/newbot`，按提示给 bot 起名字。
2. 创建成功后它会给你一串 token，形如 `123456789:ABCdef...`，这就是 `TELEGRAM_BOT_TOKEN`。

### 2. 你的 Chat ID（`TELEGRAM_CHAT_ID`）
1. 先给你刚建的 bot 随便发一条消息（点开 bot → Start → 发个 “hi”）。
2. 浏览器打开：`https://api.telegram.org/bot<你的TOKEN>/getUpdates`
3. 在返回的 JSON 里找到 `"chat":{"id":...}`，那个数字就是 `TELEGRAM_CHAT_ID`。

### 3. RapidAPI Key（`RAPIDAPI_KEY`）+ 选接口
注册 https://rapidapi.com （免费），然后选一个 Twitter 接口订阅。代码**同时兼容两类**：

| 接口 | host | 免费档 | 说明 |
|---|---|---|---|
| **twitter-api45**（作者 alexanderxbx，**推荐免费**） | `twitter-api45.p.rapidapi.com` | 有 $0 档 | 一步用用户名直接拉推文，最省额度 |
| Twttr API / twitter241（davethebeast） | `twitter241.p.rapidapi.com` | Basic $1/月 | 两步：先换 rest_id 再拉时间线 |

- 走**免费**就订阅 **twitter-api45** 的 $0 档，并把 `RAPIDAPI_HOST` 设为 `twitter-api45.p.rapidapi.com`。
- 订阅后在接口端点页右侧 Code Snippet 里复制 `X-RapidAPI-Key`，作为 `RAPIDAPI_KEY`。
  注意：RapidAPI 的 key 是**整个账号通用**的，订阅哪个接口都是同一串。

> ⚠️ 免费档额度低（常见 100~500 次/月），所以 workflow 默认只在美股活跃时段每 30 分钟查一次。
> 务必按你套餐的「Requests / Month」上限调整 `.github/workflows/serenity-monitor.yml` 里的 cron（文件里有说明）。
>
> 实现说明：代码用**递归扫描**从返回 JSON 里提取推文，不依赖固定路径，对两类接口都适用；
> 换别的接口改 `monitor.py` 的 `fetch_tweets()` 即可。

## 需要配置的 Secrets 一览

| Secret 名 | 必填 | 说明 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | Telegram bot token |
| `TELEGRAM_CHAT_ID` | ✅ | 你的 chat id |
| `RAPIDAPI_KEY` | ✅ | RapidAPI 密钥（账号通用） |
| `RAPIDAPI_HOST` | 走免费 twitter-api45 时**必填** | 设为 `twitter-api45.p.rapidapi.com`；不填默认走 twitter241 |

## 本地测试

```bash
pip install -r serenity-monitor/requirements.txt
export RAPIDAPI_KEY=xxx
export TELEGRAM_BOT_TOKEN=xxx
export TELEGRAM_CHAT_ID=xxx
python serenity-monitor/monitor.py
```

第一次运行只会「建立基线」（记录当前推文但不发送历史），之后只通知**新出现**的股票相关推文。

## 调整命中规则

- 想只在出现明确股票代码（`$AMD` 这种）时才通知：把 `monitor.py` 里 `is_stock_related`
  的关键词分支去掉，只保留 cashtag 判断。
- 想加/减关键词：改 `KEYWORDS` 列表即可（已支持中英文）。
