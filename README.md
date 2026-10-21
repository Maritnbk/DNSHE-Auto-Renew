# DNSHE 域名自动续期助手

本项目是一个基于 GitHub Actions 的自动化脚本，旨在利用 **DNSHE 免费域名 API**  实现子域名的自动续期，并通过 **PushPlus / Telegram Bot** 推送执行结果，确保您的免费域名永不过期。

## 🌟 功能特性

- **全自动续期**：每月 1 日自动执行续期操作 。

- **多域名支持**：自动遍历账户下所有子域名进行批量续期 。

- **即时通知**：通过 PushPlus / Telegram Bot 推送详细报告，结果逐行显示，清晰直观。

- **安全合规**：采用 GitHub Secrets 管理密钥，不在代码中硬编码敏感信息 。

***

## 📝 更新说明

（2026-10-21）

- **修复静默误判**：`never_expires` 为字符串 `"0"` 时曾被当成「永不过期」而永久跳过续期，现按正确语义解析。

- **错误信息可读**：保留官方 `error_code` 并给出处理建议（此前 401 只显示一句 `Unauthorized`）；未到续期窗口时会提示还需等待多少天。

- **通知不再短路**：此前 PushPlus 失败会导致 Telegram 完全不被调用，现已修复。

- **分页不再静默截断**：服务端分页异常时改为显式报错，并用接口返回的总数校验是否漏收域名。

- **域名状态可见**：被暂停 / 已过期的域名会在报告中标注。

- **阈值边界修正**：剩余正好 180 天时不再被误判为「跳过」。

- **日志脱敏**：推送失败的日志不再泄漏 Telegram Bot Token。

- **运行频率**：cron 错开到非整点执行，避免 GitHub Actions 高负载时被延迟或丢弃。

（2026-08-21）

- **通知渠道扩展**：新增 Telegram Bot 通知方式，可与 PushPlus 并存，未配置则自动跳过。

- **长报告适配**：超过 Telegram 单条消息长度上限时自动分段发送，内容不丢失。

（2026-07-18）

- **智能续期**：先检查域名到期时间，仅对剩余天数不足 180 天的域名执行续期。

- **永不过期识别**：识别已设置为永不过期的域名，自动跳过续期。

- **通知优化**：推送消息分为两段——第一段展示本次续期结果，第二段汇总所有域名到期时间。

***

## 🚀 快速上手

### 第一步：获取 API 密钥

1. 登录 [DNSHE](https://my.dnshe.com/) 。

2. 进入 **“免费域名”** 页面 。

3. 在底部的 **“API 管理”** 卡片中点击 **“创建 API 密钥”** 。

4. 妥善保存获取到的 `API Key` 和 `API Secret` 。



### 第二步：获取推送 Token

1. 访问 [PushPlus 官网](https://www.pushplus.plus/)。
2. 登录并获取您的 **Token**。
3. （可选）若需推送到群组，请创建一个群组并记录 **群组编码 (Topic)**。

### （可选）：配置 Telegram Bot 通知

若希望同时通过 Telegram 接收报告：

1. 在 Telegram 中找到 [@BotFather](https://t.me/BotFather)，发送 `/newbot` 创建机器人，保存获得的 **Bot Token**。
2. 向您的机器人发送任意一条消息（必须先发起过对话，机器人才能向您推送）。
3. 获取 **Chat ID**：浏览器访问 `https://api.telegram.org/bot<你的Token>/getUpdates`，在返回的 JSON 中找到 `chat.id`；也可使用 [@userinfobot](https://t.me/userinfobot) 查询。
4. 将两个值分别填入 Secrets 的 `TELEGRAM_BOT_TOKEN` 与 `TELEGRAM_CHAT_ID`。

> 通知方式可任选或并存：只配 PushPlus、只配 Telegram、或两者都配均可；都未配置时脚本仅跳过推送，不影响续期执行。

### 第三步：配置 GitHub 仓库

1. **Fork 本仓库** 或将脚本及工作流文件上传至您的私有仓库。
2. 进入仓库设置：**Settings** -> **Secrets and variables** -> **Actions**。
3. 点击 **New repository secret**，依次添加以下变量：

| 变量名称                 | 说明                      | 示例                  |
| -------------------- | ----------------------- | ------------------- |
| `DNSHE_API_KEY`      | DNSHE 的 API Key         | `cfsd_xxxxxxxxxx`   |
| `DNSHE_API_SECRET`   | DNSHE 的 API Secret      | `yyyyyyyyyyyy`      |
| `PUSHPLUS_TOKEN`     | PushPlus 的用户令牌          | `9a8b...`           |
| `PUSHPLUS_TOPIC`     | (可选) PushPlus 群组编码      | `123`               |
| `TELEGRAM_BOT_TOKEN` | (可选) Telegram Bot Token | `123456:ABC-DEF...` |
| `TELEGRAM_CHAT_ID`   | (可选) Telegram 目标聊天 ID   | `123456789`         |

> 只要有一个通知通道送达即视为成功；所有通道都失败才会让工作流标红。

#### 可选配置（Repository variables，非敏感）

在 **Settings** -> **Secrets and variables** -> **Actions** -> **Variables** 中添加，不配则用默认值：

| 变量名称                         | 默认值                                               | 说明                                     |
| ---------------------------- | ------------------------------------------------- | -------------------------------------- |
| `DNSHE_RENEW_THRESHOLD_DAYS` | `180`                                             | 剩余天数不超过该值时才续期（官方窗口为到期前 180 天）          |
| `DNSHE_MIN_INTERVAL`         | `2`                                               | 相邻两次 API 调用的最小间隔（秒）                    |
| `DNSHE_TZ_OFFSET`            | `8`                                               | 解析到期时间用的时区偏移。**非中国时区用户务必修改**，否则剩余天数会算错 |
| `DNSHE_API_BASE_URL`         | `https://api005.dnshe.com/index.php?m=domain_hub` | 接口地址，一般无需修改                            |

### 第四步：启用自动化

> ⚠️ **Fork 之后定时任务默认是关闭的**，必须手动启用，否则它永远不会自己运行。
> 这是「配置完以为在跑、其实一次都没跑过」最常见的原因。

1. 点击仓库顶部的 **Actions** 选项卡。
2. 若看到提示，点击 **I understand my workflows, go ahead and enable them**。
3. 在左侧选择 **"DNSHE Domain Auto Renew"** 工作流，点击 **Enable workflow**（若已启用则跳过）。
4. 点击 **Run workflow** 手动触发一次，验证配置是否正确。

***

## 📅 运行计划

- **执行频率**：每月 1 日北京时间 08:23。刻意避开整点——GitHub 官方文档说明 `schedule` 在高负载时会延迟甚至丢弃任务，而「每个整点」正是高负载时段。

- **速率限制**：官方文档对默认额度有两处不同表述（30 次/分与 60 次/分），脚本按 2 秒间隔保守节流，两种口径下都安全。如需调整请改 `DNSHE_MIN_INTERVAL`。

- **错误处理**：续期失败时，推送消息会带上官方 `error_code` 与可读原因（如认证失败、额度不足、尚未进入续期窗口还需等待多少天）。

- **⚠️ 长期不动的仓库会被自动停用**：GitHub 会在公开仓库 60 天无活动后自动禁用定时工作流。建议每两个月手动 Run 一次，或给仓库点个 star / 提交一次改动来保活。

- **报告内容**：推送分两段——第一段是本次续期结果，第二段列出全部域名的到期时间。域名较多时 Telegram 会自动分成多条消息发送。

- **时区**：脚本按 `DNSHE_TZ_OFFSET`（默认 UTC+8）解读接口返回的到期时间。接口返回的是不带时区的本地时间字符串，**如果你的账号不在 UTC+8，必须改这个值**，否则「剩余天数」会整体偏移。

## ⚠️ 安全建议

- **密钥保护**：切勿将 `API Secret` 上传至公开代码库 。

- **定期轮换**：建议定期在 DNSHE 后台使用 `regenerate` 操作更新密钥以增强安全性 。

- **最小权限**：建议仅为该脚本配置必要的 API 访问权限 。

- **日志脱敏**：推送失败时打印的异常信息会自动抹掉 Bot Token，可安全地贴到 issue 里。

***

## 🧪 本地运行

```bash
pip install -r requirements.txt

export DNSHE_API_KEY=...
export DNSHE_API_SECRET=...
export PUSHPLUS_TOKEN=...                            # 可选
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...   # 可选

python renew_domains.py
```

> 注意：`key.txt`、`.env` 等本地凭据文件已在 `.gitignore` 中排除，请勿提交。

***

## 🙏 致谢

本项目得以实现，特别感谢以下平台与技术的支持：

- [**DNSHE**](https://www.dnshe.com/)

- [**DeepSeek**](https://platform.deepseek.com/usage)
