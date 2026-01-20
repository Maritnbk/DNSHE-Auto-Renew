
# DNSHE 域名自动续期助手

本项目是一个基于 GitHub Actions 的自动化脚本，旨在利用 **DNSHE 免费域名 API**  实现子域名的自动续期，并通过 **PushPlus** 推送执行结果，确保您的免费域名永不过期。

## 🌟 功能特性

* **全自动续期**：每月 1 日自动执行续期操作 。
* **多域名支持**：自动遍历账户下所有子域名进行批量续期 。
* **即时通知**：通过 PushPlus 推送详细报告，结果逐行显示，清晰直观。
* **安全合规**：采用 GitHub Secrets 管理密钥，不在代码中硬编码敏感信息 。

---

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

### 第三步：配置 GitHub 仓库

1. **Fork 本仓库** 或将脚本及工作流文件上传至您的私有仓库。
2. 进入仓库设置：**Settings** -> **Secrets and variables** -> **Actions**。
3. 点击 **New repository secret**，依次添加以下变量：

| 变量名称 | 说明 | 示例 |
| --- | --- | --- |
| `DNSHE_API_KEY` | DNSHE 的 API Key | `cfsd_xxxxxxxxxx` |
| `DNSHE_API_SECRET` | DNSHE 的 API Secret | `yyyyyyyyyyyy` |
| `PUSHPLUS_TOKEN` | PushPlus 的用户令牌 | `9a8b...` |
| `PUSHPLUS_TOPIC` | (可选) PushPlus 群组编码 | `123` |

### 第四步：启用自动化

1. 点击仓库顶部的 **Actions** 选项卡。
2. 在左侧选择 **"DNSHE Domain Auto Renew"** 工作流。
3. 点击 **Run workflow** 手动触发一次，验证配置是否正确。

---

## 📅 运行计划

* **执行频率**：每月 1 日北京时间 08:00。
* **速率限制**：脚本遵循 API 默认的 60 请求 / 分钟限制 。
* **错误处理**：若续期失败，推送消息中将包含具体的错误原因（如认证失败、资源不存在等） 。



## ⚠️ 安全建议

* **密钥保护**：切勿将 `API Secret` 上传至公开代码库 。
* **定期轮换**：建议定期在 DNSHE 后台使用 `regenerate` 操作更新密钥以增强安全性 。
* **最小权限**：建议仅为该脚本配置必要的 API 访问权限 。


---


## 🙏 致谢

本项目得以实现，特别感谢以下平台与技术的支持：

* **[DNSHE](https://www.dnshe.com/)**：提供简单、快速、免费的域名注册服务及免费域名 API 服务，支持全类型 DNS 记录解析、DNS 记录管理及自动化续期功能。


* **[Google Gemini](https://gemini.google.com/)**：提供智能化的代码编写建议、脚本逻辑优化以及文档排版支持。

---

