# DNSHE 域名自动续期助手

本项目是一个基于 GitHub Actions 的自动化脚本，旨在利用 **DNSHE 免费域名 API**  实现子域名的自动续期，并通过 **PushPlus / Telegram Bot** 推送执行结果，确保您的免费域名永不过期。

## 🌟 功能特性

- **全自动续期**：每月 1 日自动执行续期操作 。

- **多域名支持**：自动遍历账户下所有子域名进行批量续期 。

- **即时通知**：通过 PushPlus / Telegram Bot 推送详细报告，结果逐行显示，清晰直观。

- **安全合规**：采用 GitHub Secrets 管理密钥，不在代码中硬编码敏感信息 。

***

## 📝 更新说明

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

### 第二步（可选）：配置 Telegram Bot 通知

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

| 变量名称               | 说明                 | 示例                |
| ------------------ | ------------------ | ----------------- |
| `DNSHE_API_KEY`    | DNSHE 的 API Key    | `cfsd_xxxxxxxxxx` |
| `DNSHE_API_SECRET` | DNSHE 的 API Secret | `yyyyyyyyyyyy`    |
| `PUSHPLUS_TOKEN`   | PushPlus 的用户令牌     | `9a8b...`         |
| `PUSHPLUS_TOPIC`   | (可选) PushPlus 群组编码 | `123`             |
| `TELEGRAM_BOT_TOKEN` | (可选) Telegram Bot Token | `123456:ABC-DEF...` |
| `TELEGRAM_CHAT_ID`   | (可选) Telegram 目标聊天 ID | `123456789` |

### 第四步：启用自动化

1. 点击仓库顶部的 **Actions** 选项卡。
2. 在左侧选择 **"DNSHE Domain Auto Renew"** 工作流。
3. 点击 **Run workflow** 手动触发一次，验证配置是否正确。

***

## 📅 运行计划

- **执行频率**：每月 1 日北京时间 08:00。

- **速率限制**：脚本遵循 API 默认的 60 请求 / 分钟限制 。

- **错误处理**：若续期失败，推送消息中将包含具体的错误原因（如认证失败、资源不存在等） 。



## ⚠️ 安全建议

- **密钥保护**：切勿将 `API Secret` 上传至公开代码库 。

- **定期轮换**：建议定期在 DNSHE 后台使用 `regenerate` 操作更新密钥以增强安全性 。

- **最小权限**：建议仅为该脚本配置必要的 API 访问权限 。



***



## 🙏 致谢

本项目得以实现，特别感谢以下平台与技术的支持：

- [**DNSHE**](https://www.dnshe.com/)

- [**OpenCode**](https://github.com/nicepkg/opencode)

- [**DeepSeek**](https://platform.deepseek.com/usage)

- [**Google Gemini**](https://aistudio.google.com/)

***
