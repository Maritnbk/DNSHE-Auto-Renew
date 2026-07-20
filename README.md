# DNSHE 域名自动续期助手

本项目是一个基于 GitHub Actions 的自动化脚本，旨在利用 **DNSHE 免费域名 API**  实现子域名的自动续期，并通过 **SMTP 邮件** 推送执行结果，确保您的免费域名永不过期。

## 🌟 功能特性

- **全自动续期**：每月 1 日自动执行续期操作 。

- **多域名支持**：自动遍历账户下所有子域名进行批量续期 。

- **即时通知**：通过 SMTP 邮件推送详细报告，结果逐行显示，清晰直观。

- **安全合规**：采用 GitHub Secrets 管理密钥，不在代码中硬编码敏感信息 。

***

## 📝 更新说明

（2026-07-18）

- **智能续期**：先检查域名到期时间，仅对剩余天数不足 180 天的域名执行续期。

- **永不过期识别**：识别已设置为永不过期的域名，自动跳过续期。

- **通知优化**：推送消息分为两段——第一段展示本次续期结果，第二段汇总所有域名到期时间。

- **通知方式**：使用 SMTP 邮件发送执行报告。

***

## 🚀 快速上手

### 第一步：获取 API 密钥

1. 登录 [DNSHE](https://my.dnshe.com/) 。

2. 进入 **“免费域名”** 页面 。

3. 在底部的 **“API 管理”** 卡片中点击 **“创建 API 密钥”** 。

4. 妥善保存获取到的 `API Key` 和 `API Secret` 。



### 第二步：准备 SMTP 邮箱

准备 SMTP 服务商提供的服务器地址、端口、账号和密码（部分服务商要求使用“客户端专用密码”或“授权码”）。常见配置如下：

| 服务商 | `SMTP_HOST` | `SMTP_PORT` | 加密方式 |
| ------ | ---------- | ----------- | -------- |
| QQ 邮箱 | `smtp.qq.com` | `465` | SSL |
| 163 邮箱 | `smtp.163.com` | `465` | SSL |
| Gmail | `smtp.gmail.com` | `587` | STARTTLS |

465 端口默认使用 SSL，其他端口默认使用 STARTTLS；也可以通过 `SMTP_USE_SSL` 和 `SMTP_USE_STARTTLS` 显式指定。

### 第三步：配置 GitHub 仓库

1. **Fork 本仓库** 或将脚本及工作流文件上传至您的私有仓库。
2. 进入仓库设置：**Settings** -> **Secrets and variables** -> **Actions**。
3. 点击 **New repository secret**，依次添加以下变量：

| 变量名称               | 说明                 | 示例                |
| ------------------ | ------------------ | ----------------- |
| `DNSHE_API_KEY`    | DNSHE 的 API Key    | `cfsd_xxxxxxxxxx` |
| `DNSHE_API_SECRET` | DNSHE 的 API Secret | `yyyyyyyyyyyy`    |
| `SMTP_HOST`        | SMTP 服务器地址       | `smtp.qq.com`    |
| `SMTP_PORT`        | SMTP 服务器端口       | `465`            |
| `SMTP_USER`        | SMTP 登录账号         | `your@qq.com`    |
| `SMTP_PASSWORD`    | SMTP 密码或授权码     | `xxxxxxxx`       |
| `SMTP_FROM`        | 发件人地址（可选，默认使用 `SMTP_USER`） | `your@qq.com` |
| `SMTP_TO`          | 收件人地址，多个地址用英文逗号分隔 | `me@example.com` |
| `SMTP_USE_SSL`     | 是否使用 SSL（可选）  | `true`           |
| `SMTP_USE_STARTTLS` | 是否使用 STARTTLS（可选） | `false`       |

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
