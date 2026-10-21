# DNSHE-Auto-Renew 代码审查与优化方案

> 审查对象：`Maritnbk/DNSHE-Auto-Renew` @ `8c57742`（2026-08-21）
> 对照文档：DNSHE Free Domain API User Guide V2.0 + 《DNSHE Free Domain Renewal and API Command Renewal Methods》
> 审查方式：通读全部 770 行代码 + 逐条对照文档 + 本地可复现的缺陷验证 + 对线上 API 的只读探测（无效凭据，未触碰任何真实数据）

---

## 一、总体评价

先说结论：**这个项目的工程质量明显高于同类 GitHub Actions 脚本**。作者已经处理了很多别人会踩的坑：

- 续期接口不重试读超时和 5xx（避免重复扣积分），只重试 connect 错误和 429 —— 这个幂等性判断是专业水准；
- 分页强制翻完、按 id 去重、空列表不当成"无需续期"；
- 单个域名记录异常不中断整轮遍历；
- 推送失败也计入失败退出码（"报告发不出去等于没有告警通道"）；
- `configure_streams()` 处理 Windows GBK 终端的 emoji 崩溃。

**因此下面的问题不是"这脚本写得很烂"，而是"在一个已经相当扎实的基础上，还有一批会在特定条件下静默出错的地方"。** 其中最严重的几个缺陷有个共同特征：**不报错、不告警，只是悄悄做错事**——这恰恰是自动续期脚本最不能容忍的失效模式。

问题按"会不会真的害你丢域名"排序，共 30 项。

---

## 二、P0：会造成静默错误 / 实际损失（建议优先修）

### P0-1 `never_expires` 用 `bool()` 转换，字符串 `"0"` 会被判成"永不过期"

`renew_domains.py:406`

```python
never_expires=bool(raw.get('never_expires', 0)),
```

Python 里 `bool("0")` 和 `bool("false")` 都是 `True`。实测：

| 接口返回值 | 脚本判定 | 后果 |
|---|---|---|
| `0` / `False` / `null` | 需续期 ✅ | 正常 |
| `1` / `True` | 永不过期 ✅ | 正常 |
| `"0"` | **永不过期** ❌ | **该续期的不续期，域名到期静默消失** |
| `"false"` | **永不过期** ❌ | 同上 |

文档里 `never_expires` 的类型在不同响应示例中并不统一（list 接口示例里甚至没给这个字段，renew 响应里是 `0`）。PHP 后端把 DB 的 `tinyint` 序列化成字符串是常见行为，`"0"` 完全可能出现。一旦出现，**这个域名会被永久跳过，报告里还会显示"已设置为永不过期，跳过续期"，看起来一切正常**。

**修复**：写一个 `to_bool()` 显式处理 `"0"/"false"/"no"/"off"/空串/None`，对无法识别的值给出警告并按"需要续期"处理（保守方向）。

---

### P0-2 `raise_for_status()` 把官方统一错误结构整个丢掉

`renew_domains.py:248`（list/get）与 `renew_domains.py:356`（renew）

文档明确写了：

> `error_code` — Stable error code (**recommended for business-side processing**)

但代码先 `raise_for_status()` 再解析 body，HTTP 4xx/5xx 时异常信息只剩 URL 和状态码。实测（本地构造 401）：

```
当前输出：401 Client Error: Unauthorized for url: https://api005.dnshe.com/index.php?m=domain_hub&endpoint=subdomains&action=list&fields=...
期望输出：域名列表返回失败: Invalid API key [auth_invalid_credentials]
```

而 `renew()` 里更糟（`renew_domains.py:353-357`）：先 `_payload()` 解析失败抛 `ApiError`，然后 `except ApiError: resp.raise_for_status()` 用 HTTPError **覆盖**掉原本更有信息量的 ApiError。实测 500 + 非 JSON 时，报出来的是 `500 Client Error: Unauthorized`（"Unauthorized"还是上一条测试的残留文本，说明信息已经完全错位）。

**线上验证**：我用无效凭据打了真实接口，`401` 的响应体确实是标准结构：

```json
{"error":"Invalid API key","success":false,"error_code":"auth_invalid_credentials","message":"Invalid API key","details":{}}
```

**结论：body 里一直有可用信息，是客户端自己丢的。**

**修复**：先解析 body → 有 `error_code`/`success:false` 就抛带 code 的 `ApiError` → 解析不了才退回 `raise_for_status()`。文档里 14 个 error_code（`auth_invalid_credentials` / `auth_ip_not_allowed` / `rate_limit_exceeded` / `quota_exceeded` / `provider_operation_failed` …）可以映射成人话提示。

---

### P0-3 `CompositeNotifier` 用 `all()` 短路，PushPlus 挂了 Telegram 就永远收不到

`renew_domains.py:608`

```python
return all(channel.send(content) for channel in self.channels)
```

`all()` 遇到第一个 `False` 立即返回，后面的通道**根本不会被调用**。实测：

```
pushplus.send called -> False
CompositeNotifier result: False
telegram called? False      ← 第二个通道被跳过
```

这直接违背 README 的承诺：「通知方式可任选或并存：只配 PushPlus、只配 Telegram、或两者都配均可」。**双通道用户在最需要备份通知的时候（主通道故障），备份通道恰好不会触发。**

**修复**：改成先全量执行再汇总 `ok = all([...])`。

---

### P0-4 分页异常时静默截断，少掉的域名永远不会被续期

`renew_domains.py:292-298` + `_next_page()` `renew_domains.py:334-341`

`_next_page()` 在 `next_page <= current` 时返回 `None`，主循环 `break` —— **静默退出，不报错**。而 `_has_more()` 明明已经返回 `True`（服务端说还有更多）。

实测：构造 `has_more=true, next_page=1` 的响应，200 条记录：

```
返回 200 条，服务端声称 has_more=True 但 next_page=1 -> 静默截断，未报错
```

这与代码自己在 `fetch_subdomains()` docstring 里写的担忧完全一致（"那正是『报告显示一切正常但域名实际过期』这类故障的温床"），但兜底没兜住。

另外文档提供了两个可用的一致性校验，代码都没用：
- 响应顶层 `count`（本页条数）
- `pagination.total`（`include_total=1` 时返回总数）

**修复**：
1. `has_more` 为真但页码无法前进 → 抛 `ApiError`（宁可失败告警，不要静默少续）；
2. 首页请求带 `include_total=1`，拿到 `total` 后与实际收集条数比对，不一致就告警；
3. `count` 与实际 `len(batch)` 不一致时告警。

---

### P0-5 请求了 `status` 字段却从不使用，已暂停/已过期域名被当作正常域名处理

`renew_domains.py:39` 的 `LIST_FIELDS` 里包含 `status`，但全文再无引用（grep 只命中 retry 相关的 `status=` 参数）。实测：

```
status=suspended 的记录被判定为: renew （报告里看不出它已被暂停）
```

**后果**：域名被 DNSHE 暂停（`suspended`）或已过期（`expired`）时，脚本照常尝试续期，成功与否都只显示"续期成功/失败"，**报告里完全看不出这个域名处于异常状态**。而 `suspended` 往往意味着上游 Cloudflare zone 出了问题，需要人工介入。

**修复**：`DomainView` 增加 `status` 字段，报告中对非 `active` 的域名显式标注（如 `⚠️ 状态 suspended`），并对 `expired` 给出"已过期，续期窗口可能已关闭"的提示。

---

## 三、P1：正确性 / 健壮性（建议本轮一并处理）

### P1-1 阈值判定有 off-by-one，且依赖 `.days` 截断

`renew_domains.py:429`

```python
if view.days_remaining is not None and view.days_remaining >= threshold_days:
    return 'skip'
```

`days_remaining = (expires_at - now).days` 是向下取整。于是：

| 实际剩余 | `.days` | 判定 | 正确？ |
|---|---|---|---|
| 180 天 0 小时 | 180 | skip | ❌ 窗口已开启却跳过 |
| 180 天 23 小时 | 180 | skip | ❌ 同上 |
| 179 天 23 小时 | 179 | renew | ✅ |

文档写的是"Renewal window: Opens **180 days before** the domain expiration date"，即 `剩余 <= 180天` 就该续。实测确认：

```
expires=2027-04-19 17:30:00 days=180 decide=skip   ← 已进入窗口却跳过
```

**影响**：由于每月只跑一次、窗口有 180 天，实际漏续风险很低（下个月会补上）。但一旦叠加 P1-6（workflow 被自动禁用 60 天），就可能真的错过。**修复**：改用 `expires_at - now <= timedelta(days=threshold)` 精确比较，并统一用 `ceil` 展示天数。

### P1-2 官方 422 响应里的 `days_until_window` 被丢弃

`renew_domains.py:205-210` 的 `details` 只取了 `limit` / `remaining` / `reset_at`。

Issue #2 里维护者贴出的真实响应是：

```json
{"error":"renewal not yet available","seconds_until_window":10526683,
 "days_until_window":122,"error_code":"renewal_not_yet_available",
 "message":"域名尚未进入可续期时间窗口"}
```

实测当前渲染：

```
当前渲染: 域名尚未进入可续期时间窗口 [renewal_not_yet_available]
期望包含: 还需 122 天
```

**这正是 issue #2 那位用户提出的诉求，而数据一直在响应里，只是没被读出来。** 修复成本一行。

### P1-3 合法的空账户被当成故障

`renew_domains.py:302-304`：`collected` 为空即抛错。实测一个正常返回 `{"success":true,"count":0,"subdomains":[]}` 的账户：

```
合法空账户被当成故障: 未获取到任何子域名，疑似凭据失效或接口变更，请人工核查
```

告警语义没错（空列表确实常意味着凭据失效），但**当响应明确带 `count: 0` 时应当区分**：这是"账户下确实没有域名"，不是故障。否则新用户第一次运行就收到红色告警。

### P1-4 Telegram 分段按 Python 字符数切，但 Telegram 按 UTF-16 码元计数

`renew_domains.py:510-525`。BMP 外字符（emoji 如 😀🎉🚀，含部分彩色 emoji）在 UTF-16 里占 2 个码元。实测：

```
py_len=4069  utf16=4399  -> 超限 4096，Telegram 会返回 400 MESSAGE_TOO_LONG
最大 UTF-16 长度: 4399
```

脚本自己在注释里写"报告含 emoji"，而 emoji 恰恰是计数的坑。当前用的 `✅❌⏭️⚠️` 都在 BMP 内，所以**目前不触发**；但域名备注、接口返回的 message 里一旦出现用户可控的 emoji 就会炸。修复：按 `len(s.encode('utf-16-le')) // 2` 切分。

### P1-5 Telegram Bot Token 会泄漏进 Actions 日志

`renew_domains.py:586`

```python
warn(f"Telegram 第 {index} 段推送失败: {e}")
```

`requests` 的连接异常消息里带完整 URL。实测：

```
Telegram 第 1 段推送失败: HTTPSConnectionPool(host='api.telegram.org', port=443):
Max retries exceeded with url: /bot123456:AAH-fake-secret-token/sendMessage (...)
```

**公开仓库的 Actions 日志所有访客可见**（fork 场景下更是如此）。有 `response` 的分支（`:581-584`）已经处理好了，只有无 response 的分支会泄漏。

**修复**：对异常文本做 token 替换脱敏（`re.sub(r'bot[^/]+/', 'bot<redacted>/', msg)`）。

### P1-6 工作流注释"错峰"未做，且未提示 fork 后定时任务默认禁用

`.github/workflows/renew.yml:8` 使用 `cron: '0 0 1 * *'`，**整点**。GitHub 官方文档原文：

> The schedule event can be delayed during periods of high loads of GitHub Actions workflow runs. High load times include **the start of every hour**. If the load is sufficiently high enough, **some queued jobs may be dropped**.

更关键的是同一页：

> **When a public repository is forked, scheduled workflows are disabled by default.** In a public repository, scheduled workflows are automatically disabled when no repository activity has occurred in 60 days.

README 第一步就是"Fork 本仓库"，却**完全没有提到要去 Actions 页面手动 Enable**。这是"按 README 配完、以为在跑、其实一次都没跑过"的头号原因。60 天无活动自动禁用这条，对一个只改一次就不动的续期仓库更是定时炸弹。

**修复**：
- cron 改为非整点（如 `23 1 1 * *`，北京 09:23），并在注释里说明原因；
- README 增加醒目步骤："Fork 后必须到 Actions 页点击 *I understand my workflows, go ahead and enable them*"；
- README 增加"每 60 天回来 commit 一次或手动 Run 一次"的维护提示。

### P1-7 自定义鉴权头在跨主机重定向时被剥离，且错误信息极具误导性

`renew_domains.py:246-247`。实测：302 跳转到另一台主机后，`X-API-Key` / `X-API-Secret` **不再发送**（requests 的安全行为）。

由于 `DNSHE_API_BASE_URL` 可配置，用户填错地址（比如填了带尾斜杠或旧域名）时，会得到 401 而不是"地址不对"。加上 P0-2 的信息丢失，最终报出来的是 `401 Client Error: Unauthorized for url: ...`——用户会以为是密钥错了，去反复重新生成密钥。

**修复**：`allow_redirects=False`，遇到 3xx 直接报"接口地址发生了重定向，请检查 DNSHE_API_BASE_URL"。

### P1-8 推送响应非 dict 时两个通道行为不一致

实测：

```
PushPlus 返回 [] 时: True      ← 判成功
Telegram 返回 [] 时: False     ← 至少还判了失败，但日志里是 "'list' object has no attribute 'get'"
```

`PushPlusNotifier` 的 `isinstance(result, dict)` 判断失败后直接落到 `print("PushPlus 推送成功")`（`renew_domains.py:500-504` 逻辑：`if isinstance(...) and code != 200` 为 False → 继续往下走成功分支）。**响应格式异常被当成成功。**

**修复**：两个 notifier 统一：非 dict 一律判失败并记录原文片段。

---

## 四、P2：工程化与可维护性

### P2-1 代码支持 4 个可选环境变量，但工作流一个都没透传

`Config.from_env()` 支持 `DNSHE_RENEW_THRESHOLD_DAYS`、`DNSHE_MIN_INTERVAL`、`DNSHE_TZ_OFFSET`、`DNSHE_API_BASE_URL`，但 `renew.yml` 和 README **都没有出现这几个名字**（已 grep 确认）。用户在 Actions 里配了也不会生效。

**修复**：workflow 增加 `vars.*` 透传（repository variables 不是 secret，适合放阈值这类非敏感配置），README 补表格。

### P2-2 依赖不锁定，Python 3.9 已 EOL

`renew.yml:24` 是裸 `pip install requests`，每次运行装最新版；`renew.yml:21` 用 3.9（2025-10 已停止安全支持）。`urllib3` 的重试语义在 2.x 有过变化，不锁定意味着某天可能因上游升级行为改变。

**修复**：加 `requirements.txt`（`requests>=2.32,<3`、`urllib3>=2,<3`），`pip install -r`；Python 升到 3.12 或 3.13（已用 `ast.parse(feature_version=(3,9))` 验证语法向后兼容，升级无风险）。

### P2-3 零测试、零 lint、零 CI

770 行里有大量**天然可测的纯函数**：`parse_expiry` / `decide` / `split_message` / `describe_error` / `parse_tz_offset` / `Throttle` / `format_charge` / `_has_more` / `_next_page`。本次审查中 P0-1、P0-3、P0-4、P1-2、P1-4 全部是用十几行脚本复现的——**这些本该是单元测试**。

**修复**：加 `tests/test_renew_domains.py`（pytest），覆盖上述边界；加 `ruff`；加一个 `ci.yml` 在 push/PR 时跑 lint + test。这也让后续重构有安全网。

### P2-4 单文件 770 行

`Config` / `DnsheClient` / 三个 Notifier / 报告渲染 / 主流程混在一起。不算错，但拆分后（`config.py` / `client.py` / `notify.py` / `report.py` / `main.py`）可读性和可测性都会更好。

**建议**：这一项**放到最后、可选**。它改动面最大、收益最"软"，且会让 diff 难以 review。如果只想要行为改进，可以完全跳过。

### P2-5 无 LICENSE

70 stars / 86 forks 的公开项目没有许可证文件。法律上默认"保留所有权利"，别人 fork 后使用存在风险。建议加 MIT。

### P2-6 README 若干问题

- **步骤编号重复**：出现两次"第二步"（`README.md:49` 和 `README.md:55`）；
- **速率限制表述与官方文档矛盾**：README 写"60 请求/分钟"，而官方文档开头写 "Default **30** requests/minute"，后面的 Rate Limiting 章节又写 "Default Limit: **60** requests/minute"——**文档自己前后矛盾**。脚本 `DEFAULT_MIN_INTERVAL = 2`（30 次/分钟）在两种口径下都安全，但 README 不该复述一个不确定的数字，应改为"以官方为准，脚本按 2 秒间隔保守节流"；
- 未说明 `DNSHE_TZ_OFFSET` 默认 UTC+8（非中国时区用户会看到错误的天数）；
- 未说明"报告第二段列出全部域名到期时间"这一行为，以及域名多时 Telegram 会分段。

### P2-7 小问题合集

| 位置 | 问题 |
|---|---|
| `renew_domains.py:14` | `Dict` 导入未使用 |
| `renew_domains.py:454` | `format_charge(None)` → "消耗 **None** 积分" |
| `renew_domains.py:402-407` | `id` 为字符串 `"12"` 时原样透传给 `subdomain_id`，未做 int 归一 |
| `renew_domains.py:419` | 已过期域名 `days_remaining` 为负数，报告里显示"剩余 -2天"，措辞别扭（应显示"已过期 2 天"） |
| `renew_domains.py:659` | `renew_one` 捕获裸 `Exception`，会把编程错误也伪装成"请求异常" |
| `renew_domains.py:305` | `fetch_subdomains` 用 `MAX_PAGES=100 × 200` 兜底，但没利用文档的 `per_page` 上限 500 |

### P2-8 运行频率建议（值得单独讨论）

当前**每月 1 日跑一次**。由于脚本已有"剩余 >= 180 天则跳过"的幂等判断，**提高频率几乎没有额外成本**（每轮只多几次 list 请求），但能显著提升鲁棒性：

- 单次运行失败（API 502、runner 抽风）不用再等一个月；
- 降低"workflow 被 60 天规则禁用"造成的损失；
- 域名一旦进入 180 天窗口，7 天内必定被处理。

建议改为**每周一次**（`23 1 * * 1`）。这是行为变更，需要你确认。

---

## 五、建议的执行计划

分三批，每批可独立提交、独立回滚。

### 第 1 批：P0 静默错误（核心，约 150 行改动）

| # | 改动 | 涉及位置 |
|---|---|---|
| 1 | `to_bool()` 解析 `never_expires`，未知值按"需续期"并告警 | `:406` |
| 2 | 统一错误解析：先读 body 的 `error_code`/`message`/`details`，再退回状态码；修掉 `renew()` 里 HTTPError 覆盖 ApiError 的问题；error_code 映射人话 | `:232-260`, `:343-357` |
| 3 | `CompositeNotifier` 改为全量执行再汇总 | `:608` |
| 4 | 分页无法前进时抛错；用 `include_total` + `count` 做一致性校验 | `:262-341` |
| 5 | `DomainView` 增加 `status`，报告中标注非 active 状态 | `:39`, `:396-445` |

**验证**：为每项补单元测试，复现修复前的失败、修复后通过。

### 第 2 批：P1 健壮性（约 120 行改动）

| # | 改动 |
|---|---|
| 6 | 阈值改为 `timedelta` 精确比较 |
| 7 | `describe_error` 支持 `days_until_window` / `seconds_until_window` |
| 8 | 空账户（`count == 0`）不再当故障 |
| 9 | Telegram 分段改按 UTF-16 码元计数 |
| 10 | 日志中脱敏 bot token |
| 11 | `allow_redirects=False`，3xx 明确报错 |
| 12 | 两个 notifier 统一处理非 dict 响应 |
| 13 | 工作流 cron 错峰 + Python 升级 + 依赖锁定 + `vars` 透传 |

### 第 3 批：工程化（约 250 行新增，主要是测试）

| # | 改动 |
|---|---|
| 14 | `tests/` + pytest 用例（覆盖本次全部复现项） |
| 15 | `ruff` 配置 + `ci.yml`（push/PR 跑 lint + test） |
| 16 | `requirements.txt` |
| 17 | README 重写受影响章节（fork 启用步骤、可选变量、速率限制表述、步骤编号） |
| 18 | `LICENSE`（MIT） |
| 19 | *可选*：拆分为多模块 |

---

## 六、需要你拍板的几点

1. **执行范围**：只做第 1 批（最小必要），还是 1+2，还是 1+2+3 全做？
2. **运行频率**：是否把每月一次改为每周一次？（P2-8，行为变更）
3. **cron 时间**：改到非整点错峰（如北京时间 09:23），还是保持 08:00 整点？
4. **代码结构**：是否接受拆分为多文件？（P2-4，改动面最大、review 成本最高，我建议先不做）
5. **LICENSE**：加 MIT 是否可以？

确认后我按批次执行，每批完成后给出 diff 摘要和测试结果。
