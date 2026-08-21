#!/usr/bin/env python3
"""DNSHE 免费子域名自动续期。

接口行为依据官方文档《DNSHE免费域名API使用文档（V2.0)》:
https://my.dnshe.com/knowledgebase/13/DNSHE免费域名API使用文档V2.0.html
"""

import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_BASE_URL = "https://api005.dnshe.com/index.php?m=domain_hub"

PUSHPLUS_URL = "https://www.pushplus.plus/send"

TELEGRAM_API_BASE = "https://api.telegram.org"

TELEGRAM_MESSAGE_LIMIT = 4096

REQUEST_TIMEOUT = (10, 30)

DEFAULT_THRESHOLD_DAYS = 180

DEFAULT_MIN_INTERVAL = 2

DEFAULT_TZ_OFFSET_HOURS = 8.0

PAGE_SIZE = 200
MAX_PAGES = 100

LIST_FIELDS = "id,subdomain,rootdomain,full_domain,status,expires_at,never_expires"

EXPIRY_FORMATS = ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d')

BENIGN_ERROR_CODES = frozenset({'renewal_not_yet_available'})


class ApiError(RuntimeError):
    """DNSHE 接口调用失败（含响应结构不符合预期）。"""


def configure_streams():
    """报告含 emoji，非 UTF-8 终端（如 Windows GBK）下 print 会抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is not None:
            try:
                reconfigure(encoding='utf-8', errors='replace')
            except ValueError:
                pass


def warn(message):
    print(f"警告: {message}", file=sys.stderr)


def parse_tz_offset(raw, default_hours=DEFAULT_TZ_OFFSET_HOURS):
    """解析形如 '+8' / '8' / '-5' / '+5.5' 的时区偏移，非法值回退到默认。"""
    if raw is None or not str(raw).strip():
        return timezone(timedelta(hours=default_hours))
    try:
        hours = float(str(raw).strip())
    except ValueError:
        warn(f"DNSHE_TZ_OFFSET={raw!r} 不是合法数字，回退到 UTC+{default_hours:g}")
        return timezone(timedelta(hours=default_hours))
    if not -24 < hours < 24:
        warn(f"DNSHE_TZ_OFFSET={raw!r} 超出范围，回退到 UTC+{default_hours:g}")
        return timezone(timedelta(hours=default_hours))
    return timezone(timedelta(hours=hours))


def env_number(env, name, default, cast):
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return cast(str(raw).strip())
    except ValueError:
        warn(f"{name}={raw!r} 不是合法数值，回退到默认值 {default}")
        return default


def format_offset(tz):
    """把 tzinfo 渲染成 'UTC+8' 这类可读文本，用于在报告里显式声明时区假设。"""
    total_minutes = int(tz.utcoffset(None).total_seconds() // 60)
    sign = '+' if total_minutes >= 0 else '-'
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")


@dataclass(frozen=True)
class Config:
    api_key: str = ""
    api_secret: str = ""
    pushplus_token: str = ""
    pushplus_topic: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    base_url: str = DEFAULT_BASE_URL
    threshold_days: int = DEFAULT_THRESHOLD_DAYS
    min_interval: float = DEFAULT_MIN_INTERVAL
    tz: timezone = field(default_factory=lambda: timezone(timedelta(hours=DEFAULT_TZ_OFFSET_HOURS)))

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env

        def text(name):
            return (env.get(name) or '').strip()

        return cls(
            api_key=text('DNSHE_API_KEY'),
            api_secret=text('DNSHE_API_SECRET'),
            pushplus_token=text('PUSHPLUS_TOKEN'),
            pushplus_topic=text('PUSHPLUS_TOPIC'),
            telegram_bot_token=text('TELEGRAM_BOT_TOKEN'),
            telegram_chat_id=text('TELEGRAM_CHAT_ID'),
            base_url=text('DNSHE_API_BASE_URL') or DEFAULT_BASE_URL,
            threshold_days=env_number(env, 'DNSHE_RENEW_THRESHOLD_DAYS',
                                      DEFAULT_THRESHOLD_DAYS, int),
            min_interval=env_number(env, 'DNSHE_MIN_INTERVAL',
                                    DEFAULT_MIN_INTERVAL, float),
            tz=parse_tz_offset(env.get('DNSHE_TZ_OFFSET')),
        )

    def missing_credentials(self):
        """返回缺失的必需环境变量名，用于启动时 fail-fast。"""
        return [name for name, value in (('DNSHE_API_KEY', self.api_key),
                                        ('DNSHE_API_SECRET', self.api_secret))
                if not value]


class Throttle:
    """保证相邻两次 API 调用间隔不小于 min_interval，避免撞 60 次/分钟限流。"""

    def __init__(self, min_interval, sleep=time.sleep, clock=time.monotonic):
        self.min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._last = None

    def wait(self):
        if self.min_interval <= 0:
            return
        if self._last is not None:
            remaining = self.min_interval - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


def build_session(retry):
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session


def read_retry():
    """只读请求可以放心对连接错误、读超时、5xx、429 全面重试。"""
    return Retry(total=4, connect=3, read=3, status=3,
                 status_forcelist=(429, 500, 502, 503, 504),
                 allowed_methods=frozenset(['GET']),
                 backoff_factor=2, respect_retry_after_header=True)


def renew_retry():
    """续期会扣积分且不保证幂等，只重试确定没有产生副作用的情形。

    connect 错误发生在请求发出之前，429 是服务端明确拒收，两者都安全。
    读超时和 5xx 不重试——服务端可能已经扣过积分，重试会重复消耗。
    """
    return Retry(total=3, connect=3, read=0, status=2,
                 status_forcelist=(429,),
                 allowed_methods=frozenset(['POST']),
                 backoff_factor=2, respect_retry_after_header=True)


def notify_retry():
    """重复推送只是轻微骚扰，收不到通知才是真问题，所以推送重试更激进。"""
    return Retry(total=3, connect=2, read=2, status=2,
                 status_forcelist=(429, 500, 502, 503, 504),
                 allowed_methods=frozenset(['POST']),
                 backoff_factor=1, respect_retry_after_header=True)


def describe_error(payload, fallback='未知错误'):
    """按官方统一错误结构拼出可读信息：error_code + message + details。"""
    if not isinstance(payload, dict):
        return fallback

    message = payload.get('message') or payload.get('error') or fallback
    code = payload.get('error_code')
    text = f"{message} [{code}]" if code else str(message)

    details = payload.get('details')
    if isinstance(details, dict):
        extra = [f"{k}={details[k]}" for k in ('limit', 'remaining', 'reset_at')
                 if details.get(k) is not None]
        if extra:
            text += f" ({', '.join(extra)})"
    return text


def is_failure_payload(payload):
    return payload.get('success') is False or bool(payload.get('error_code'))


class DnsheClient:
    def __init__(self, config, throttle=None, list_session=None, renew_session=None):
        self.config = config
        self.throttle = throttle or Throttle(config.min_interval)
        self.list_session = list_session or build_session(read_retry())
        self.renew_session = renew_session or build_session(renew_retry())

    def _headers(self):
        return {
            "X-API-Key": self.config.api_key,
            "X-API-Secret": self.config.api_secret,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _payload(resp, what):
        try:
            data = resp.json()
        except ValueError:
            raise ApiError(
                f"{what}返回非 JSON 内容 (HTTP {resp.status_code}): {(resp.text or '')[:200]}"
            )
        if not isinstance(data, dict):
            raise ApiError(f"{what}响应格式异常: {str(data)[:200]}")
        return data

    def get(self, query, what):
        self.throttle.wait()
        resp = self.list_session.get(f"{self.config.base_url}{query}",
                                     headers=self._headers(), timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = self._payload(resp, what)
        if is_failure_payload(data):
            raise ApiError(f"{what}返回失败: {describe_error(data)}")
        return data

    def fetch_quota(self):
        """查询账户积分。返回 quota 字典，失败由调用方决定是否致命。"""
        data = self.get("&endpoint=quota", "积分接口")
        quota = data.get('quota')
        if not isinstance(quota, dict):
            raise ApiError("积分接口响应缺少 quota 字段")
        return quota

    def fetch_subdomains(self):
        """逐页拉取全部子域名。

        list 接口 per_page 默认 200，不分页会静默丢掉后面的域名 —— 那正是
        「报告显示一切正常但域名实际过期」这类故障的温床，所以必须翻完。
        """
        collected: List[Any] = []
        seen_ids = set()
        page = 1

        while page <= MAX_PAGES:
            data = self.get(
                f"&endpoint=subdomains&action=list&fields={LIST_FIELDS}"
                f"&page={page}&per_page={PAGE_SIZE}",
                "域名列表",
            )

            if 'subdomains' not in data:
                raise ApiError(
                    "域名列表响应缺少 subdomains 字段（接口可能已变更），"
                    f"原始响应: {str(data)[:200]}"
                )
            batch = data['subdomains']
            if not isinstance(batch, list):
                raise ApiError(f"subdomains 字段不是列表: {str(batch)[:200]}")

            # 服务端若忽略 page 参数会反复返回同一页，靠 id 去重防止无限累积
            fresh = [item for item in batch if not self._already_seen(item, seen_ids)]
            collected.extend(fresh)

            if not self._has_more(data, batch, fresh):
                break

            next_page = self._next_page(data.get('pagination'), page)
            if next_page is None:
                break
            page = next_page
        else:
            raise ApiError(f"域名列表分页超过 {MAX_PAGES} 页，疑似接口异常，已中止")

        if not collected:
            # 空列表极可能是凭据失效或接口变更，绝不能当作"无需续期"
            raise ApiError("未获取到任何子域名，疑似凭据失效或接口变更，请人工核查")
        return collected

    @staticmethod
    def _already_seen(item, seen_ids):
        """按 id 去重；没有 id 的记录一律保留，交给后续逐条校验去报错。"""
        if not isinstance(item, dict):
            return False
        item_id = item.get('id')
        if item_id is None:
            return False
        if item_id in seen_ids:
            return True
        seen_ids.add(item_id)
        return False

    @staticmethod
    def _has_more(data, batch, fresh):
        """判断是否还有下一页。

        文档只在「分页响应」里描述 pagination 字段，基础响应可能没有。
        缺字段时不能想当然认为已经取完 —— 那会静默截断域名列表。
        改用「本页是否装满」来推断，并要求本页至少有新记录才继续翻。
        """
        pagination = data.get('pagination')
        if isinstance(pagination, dict) and 'has_more' in pagination:
            return bool(pagination['has_more'])
        return len(batch) >= PAGE_SIZE and bool(fresh)

    @staticmethod
    def _next_page(pagination, current):
        """取下一页页码，异常值回退到 current+1；无法前进则返回 None。"""
        raw = pagination.get('next_page') if isinstance(pagination, dict) else None
        try:
            candidate = current + 1 if raw is None else int(raw)
        except (TypeError, ValueError):
            candidate = current + 1
        return candidate if candidate > current else None

    def renew(self, subdomain_id):
        """提交续期。返回解析后的响应体（可能是业务失败），仅传输层问题抛异常。"""
        self.throttle.wait()
        resp = self.renew_session.post(
            f"{self.config.base_url}&endpoint=subdomains&action=renew",
            headers=self._headers(), json={"subdomain_id": subdomain_id},
            timeout=REQUEST_TIMEOUT,
        )
        # 不能直接 raise_for_status：422 renewal_not_yet_available 等业务错误
        # 带着有用的 error_code，丢掉状态码之外的信息会让报告失去可读性。
        try:
            return self._payload(resp, "续期接口")
        except ApiError:
            resp.raise_for_status()
            raise


@dataclass
class DomainView:
    """一条子域名记录的规范化视图，便于纯函数判定与测试。"""
    label: str
    subdomain_id: Optional[int] = None
    never_expires: bool = False
    expires_raw: Optional[str] = None
    expires_at: Optional[datetime] = None
    days_remaining: Optional[int] = None
    problem: Optional[str] = None


def parse_expiry(value, tz):
    """解析到期时间，返回带时区的 datetime；失败返回 None（不抛异常）。

    不带时区的字符串按 tz 解读；自带偏移的（ISO 8601）尊重其偏移。
    """
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip()
    for fmt in EXPIRY_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=tz)
        except ValueError:
            continue

    iso_text = text[:-1] + '+00:00' if text.endswith('Z') else text
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError:
        warn(f"无法解析到期时间 {value!r}")
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def inspect_domain(raw, index, now, tz):
    """把接口返回的一条记录转成 DomainView，任何字段异常都不得中断遍历。"""
    position = f"<第{index + 1}条记录>"
    if not isinstance(raw, dict):
        return DomainView(label=position, problem=f"记录格式异常: {str(raw)[:80]}")

    label = raw.get('full_domain') or raw.get('subdomain') or position
    view = DomainView(
        label=label,
        subdomain_id=raw.get('id'),
        never_expires=bool(raw.get('never_expires', 0)),
        expires_raw=raw.get('expires_at'),
    )

    if view.subdomain_id is None:
        view.problem = "响应缺少 id 字段，无法续期"
        return view

    if view.never_expires:
        return view

    view.expires_at = parse_expiry(view.expires_raw, tz)
    if view.expires_at is not None:
        view.days_remaining = (view.expires_at - now).days
    return view


def decide(view, threshold_days):
    """判定该域名本次要做什么：invalid / never / skip / renew。"""
    if view.problem:
        return 'invalid'
    if view.never_expires:
        return 'never'
    if view.days_remaining is not None and view.days_remaining >= threshold_days:
        return 'skip'
    # 剩余天数未知时也尝试续期：宁可撞一次 422，也不要漏掉真要过期的域名
    return 'renew'


def expiry_line(view):
    if view.problem:
        return f"{view.label}: 到期时间 未知（记录异常）"
    if view.never_expires:
        return f"{view.label}: 到期时间 永久有效"
    if view.days_remaining is not None:
        return f"{view.label}: 到期时间 {view.expires_raw} (剩余 {view.days_remaining}天)"
    if view.expires_raw:
        return f"{view.label}: 到期时间 {view.expires_raw} (无法解析剩余天数)"
    return f"{view.label}: 到期时间 未知"


def format_charge(charged):
    """charged_amount 为 0 表示免费续期。"""
    try:
        if float(charged) == 0:
            return "免费"
    except (TypeError, ValueError):
        pass
    return f"消耗 {charged} 积分"


def quota_line(quota):
    """把 quota 响应渲染成一行摘要，积分见底时给出显式警示。"""
    available = quota.get('available')
    parts = [f"可用 {available}" if available is not None else None,
             f"已用 {quota.get('used')}" if quota.get('used') is not None else None,
             f"总额 {quota.get('total')}" if quota.get('total') is not None else None]
    summary = "，".join(p for p in parts if p) or str(quota)[:120]

    try:
        if available is not None and float(available) <= 0:
            return f"⚠️ 账户积分：{summary}（积分已耗尽，付费续期将失败）"
    except (TypeError, ValueError):
        pass
    return f"账户积分：{summary}"


class PushPlusNotifier:
    def __init__(self, token, topic='', session=None):
        self.token = token
        self.topic = topic
        self.session = session or build_session(notify_retry())

    def send(self, content):
        """返回是否成功。本方法绝不抛异常，否则会吞掉它要上报的错误。"""
        if not self.token:
            print("未配置 PushPlus Token，跳过推送")
            return True

        data = {
            "token": self.token,
            "title": "DNSHE 域名自动续期报告",
            "content": content,
            "template": "txt",
            "topic": self.topic,
        }
        try:
            resp = self.session.post(PUSHPLUS_URL, json=data, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            try:
                result = resp.json()
            except ValueError:
                warn(f"PushPlus 返回非 JSON 内容: {(resp.text or '')[:200]}")
                return False
            if isinstance(result, dict) and result.get('code') != 200:
                warn(f"PushPlus 推送未成功 (code={result.get('code')}): {result.get('msg')}")
                return False
            print("PushPlus 推送成功")
            return True
        except Exception as e:
            warn(f"PushPlus 推送失败: {e}")
            return False


def split_message(content, limit=TELEGRAM_MESSAGE_LIMIT):
    """把超长报告切成若干条不超过 limit 的消息。

    优先在最近的换行处断开，避免把一行域名记录切成两半；
    单行本身就超限时才在行内硬切。
    """
    chunks = []
    remaining = content
    while len(remaining) > limit:
        cut = remaining.rfind('\n', 0, limit + 1)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip('\n')
    chunks.append(remaining)
    return chunks


class TelegramNotifier:
    """通过 Telegram Bot API 推送，纯文本发送，超长报告自动分段顺序发送。

    不用 parse_mode：报告含 emoji、域名和接口错误原文，Markdown 转义
    易漏易错且没有收益。
    """

    def __init__(self, bot_token, chat_id, session=None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.session = session or build_session(notify_retry())

    def send(self, content):
        """返回是否全部发送成功。本方法绝不抛异常，否则会吞掉它要上报的错误。"""
        if not self.bot_token or not self.chat_id:
            print("未配置 Telegram Bot Token 或 Chat ID，跳过推送")
            return True

        url = f"{TELEGRAM_API_BASE}/bot{self.bot_token}/sendMessage"
        ok = True
        for index, chunk in enumerate(split_message(content), start=1):
            data = {"chat_id": self.chat_id, "text": chunk}
            try:
                resp = self.session.post(url, json=data, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                try:
                    result = resp.json()
                except ValueError:
                    warn(f"Telegram 返回非 JSON 内容: {(resp.text or '')[:200]}")
                    ok = False
                    continue
                # Telegram 约定 ok=true 为成功；ok=false 时 description 带原因
                if not (isinstance(result, dict) and result.get('ok')):
                    warn(f"Telegram 第 {index} 段推送未成功: {result.get('description', result)}")
                    ok = False
            except Exception as e:
                warn(f"Telegram 第 {index} 段推送失败: {e}")
                ok = False
        if ok:
            print("Telegram 推送成功")
        return ok


class CompositeNotifier:
    """把报告分发给所有已配置的通知通道。

    - 未配置任何通道：提示后视为成功（与原先无 PushPlus token 时一致）
    - 任一已配置通道失败：返回 False，让进程以退出码 1 收场——
      报告发不出去等于没有告警通道
    """

    def __init__(self, channels):
        self.channels = channels

    def send(self, content):
        if not self.channels:
            print("未配置任何通知通道，跳过推送")
            return True
        return all(channel.send(content) for channel in self.channels)


def build_notifier(config):
    """按 Config 装配所有已配置的通知通道，供主流程与异常兜底共用。"""
    channels = []
    if config.pushplus_token:
        channels.append(PushPlusNotifier(config.pushplus_token, config.pushplus_topic))
    if config.telegram_bot_token and config.telegram_chat_id:
        channels.append(TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id))
    elif config.telegram_bot_token or config.telegram_chat_id:
        warn("TELEGRAM_BOT_TOKEN 与 TELEGRAM_CHAT_ID 需同时配置，已忽略不完整的 Telegram 配置")
    return CompositeNotifier(channels)


def build_message(config, quota_text, renewal_results, expiry_lines, has_failure):
    parts = []
    if quota_text:
        parts.append(quota_text)
        parts.append("")

    parts.append("=== 本次续期结果 ===")
    if renewal_results:
        parts.extend(renewal_results)
    else:
        parts.append(f"（所有域名剩余天数 >= {config.threshold_days}天，本次无需续期）")

    if has_failure:
        parts.append("")
        parts.append("⚠️ 存在失败项，请查看 GitHub Actions 日志并人工处理")

    parts.append("")
    parts.append("=== 所有域名到期时间 ===")
    parts.extend(expiry_lines)
    parts.append("")
    parts.append(f"（共 {len(expiry_lines)} 个域名；到期时间按 {format_offset(config.tz)} 解读）")
    return "\n".join(parts)


@dataclass
class RenewOutcome:
    """一次续期尝试的结果。new_expiry 非空时可用于刷新到期时间清单。"""
    line: str
    failed: bool
    new_expiry: Optional[str] = None


def renew_one(client, view):
    """续期单个域名。"""
    try:
        payload = client.renew(view.subdomain_id)
    except Exception as e:
        return RenewOutcome(f"❌ {view.label}: 请求异常 ({e})", True)

    if payload.get('success'):
        new_expiry = payload.get('new_expires_at') or ''
        charge = format_charge(payload.get('charged_amount', 0))
        remaining = payload.get('remaining_days')
        tail = f"，剩余 {remaining}天" if remaining is not None else ""
        shown = new_expiry or '未知'
        return RenewOutcome(
            f"✅ {view.label}: 续期成功 (新到期 {shown}{tail}, {charge})",
            False,
            new_expiry or None,
        )

    detail = describe_error(payload)
    if payload.get('error_code') in BENIGN_ERROR_CODES:
        # 尚未进入续期窗口，属预期结果，不计入失败
        return RenewOutcome(f"⏭️ {view.label}: 暂不可续期 ({detail})", False)
    return RenewOutcome(f"❌ {view.label}: 续期失败 ({detail})", True)


def run(config, client, notifier, now=None):
    """执行一轮续期，返回进程退出码：0 全部正常，1 存在需人工关注的问题。"""
    now = now or datetime.now(timezone.utc)

    try:
        subdomains = client.fetch_subdomains()
    except Exception as e:
        message = f"❌ 获取域名列表失败，本次未执行任何续期: {e}"
        print(message, file=sys.stderr)
        notifier.send(message)
        return 1

    # 积分只是辅助信息，查不到不该让整轮续期失败
    try:
        quota_text = quota_line(client.fetch_quota())
    except Exception as e:
        quota_text = f"账户积分：查询失败（{e}）"

    renewal_results: List[str] = []
    expiry_lines: List[str] = []
    has_failure = False

    for index, raw in enumerate(subdomains):
        view = inspect_domain(raw, index, now, config.tz)
        expiry_lines.append(expiry_line(view))
        action = decide(view, config.threshold_days)

        if action == 'invalid':
            has_failure = True
            renewal_results.append(f"❌ {view.label}: {view.problem}")
        elif action == 'never':
            renewal_results.append(f"⏭️ {view.label}: 已设置为永不过期，跳过续期")
        elif action == 'skip':
            renewal_results.append(
                f"⏭️ {view.label}: 剩余 {view.days_remaining}天 "
                f">= {config.threshold_days}天，跳过续期"
            )
        else:
            outcome = renew_one(client, view)
            renewal_results.append(outcome.line)
            has_failure = has_failure or outcome.failed
            if outcome.new_expiry:
                # 续期成功后刷新到期清单，避免第二段显示续期前的旧时间
                view.expires_raw = outcome.new_expiry
                view.expires_at = parse_expiry(outcome.new_expiry, config.tz)
                view.days_remaining = (
                    (view.expires_at - now).days if view.expires_at else None
                )
                expiry_lines[-1] = expiry_line(view)

    message = build_message(config, quota_text, renewal_results, expiry_lines, has_failure)
    print(message)
    # 推送失败同样算失败：报告发不出去等于没有告警通道
    pushed = notifier.send(message)
    return 0 if (not has_failure and pushed) else 1


def main():
    configure_streams()
    config = Config.from_env()
    notifier = build_notifier(config)

    missing = config.missing_credentials()
    if missing:
        message = (
            f"❌ 缺少必需的环境变量: {', '.join(missing)}\n"
            "请在仓库 Settings -> Secrets and variables -> Actions 中配置后重试。"
        )
        print(message, file=sys.stderr)
        notifier.send(message)
        return 1

    return run(config, DnsheClient(config), notifier)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        configure_streams()
        detail = traceback.format_exc()
        print(detail, file=sys.stderr)
        try:
            build_notifier(Config.from_env()).send(
                f"⚠️ 续期脚本异常中断，本次续期未完成\n\n{detail[-1500:]}")
        except Exception:
            pass
        sys.exit(1)
