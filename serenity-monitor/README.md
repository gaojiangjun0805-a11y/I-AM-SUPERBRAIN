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
| `WECOM_WEBHOOK_KEY` | 通知二选一 | 企业微信群机器人 webhook 的 key（国内推荐，免费稳定） |
| `TELEGRAM_BOT_TOKEN` | 通知二选一 | Telegram bot token（国内需梯子，可能收不到） |
| `TELEGRAM_CHAT_ID` | 配 Telegram 时 | 你的 chat id |

> 通知渠道：企业微信 / Telegram **配哪个就发哪个，都配则都发**。国内推荐企业微信。
> 企业微信配置：在企业微信里建一个群 → 群设置 → 群机器人 → 添加 → 复制 Webhook 地址，
> 把地址里 `key=` 后面那一串填到 `WECOM_WEBHOOK_KEY`（或把整条地址填到 `WECOM_WEBHOOK_URL`）。
| `RAPIDAPI_KEY` | ✅ | RapidAPI 密钥（账号通用） |
| `RAPIDAPI_HOST` | 走免费 twitter-api45 时**必填** | 设为 `twitter-api45.p.rapidapi.com`；不填默认走 twitter241 |
| `OPENAI_API_KEY` | 选填 | 配了就给每条推文加中文总结（OpenAI 兼容接口，**国内推荐 DeepSeek**）；不配则只推英文原文 |
| `OPENAI_BASE_URL` | 选填 | 默认 `https://api.deepseek.com/v1`；通义/Kimi/智谱/OpenAI 换成各自地址 |
| `OPENAI_MODEL` | 选填 | 默认 `deepseek-chat` |
| `ANTHROPIC_API_KEY` | 选填 | 另一种总结方案（需国外网络+国外卡）；配了且没配 OPENAI 时启用 |

> 中文总结服务商对照（都填进上面的 `OPENAI_*` 三个 secret）：
> - **DeepSeek**（推荐，支付宝充值）：BASE_URL=`https://api.deepseek.com/v1`，MODEL=`deepseek-chat`，key 在 platform.deepseek.com
> - **通义千问**：BASE_URL=`https://dashscope.aliyuncs.com/compatible-mode/v1`，MODEL=`qwen-plus`
> - **Kimi**：BASE_URL=`https://api.moonshot.cn/v1`，MODEL=`moonshot-v1-8k`
> - **智谱 GLM**：BASE_URL=`https://open.bigmodel.cn/api/paas/v4`，MODEL=`glm-4-flash`
> - **OpenAI**：BASE_URL=`https://api.openai.com/v1`，MODEL=`gpt-4o-mini`（需在 platform.openai.com 充值，与 ChatGPT 会员不同）

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
