#!/usr/bin/env python3
"""DNSHE 免费子域名自动续期。

接口行为依据官方文档《DNSHE免费域名API使用文档（V2.0)》:
https://my.dnshe.com/knowledgebase/13/DNSHE免费域名API使用文档V2.0.html
"""

import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

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

# 文档：per_page 上限 500，超过会被服务端截到 500。取上限可减少大账户的
# 翻页次数，也就减少了触发限流的概率。
PAGE_SIZE = 500
MAX_PAGES = 100

LIST_FIELDS = "id,subdomain,rootdomain,full_domain,status,expires_at,never_expires"

EXPIRY_FORMATS = ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d')

BENIGN_ERROR_CODES = frozenset({'renewal_not_yet_available'})

# 官方文档「Common Error Codes」里对使用者有行动指引的那几条，翻译成下一步该做什么。
ERROR_HINTS = {
    'auth_invalid_credentials': 'API Key/Secret 无效，请到 DNSHE 后台重新创建',
    'auth_ip_not_allowed': '当前出口 IP 不在该密钥的白名单内',
    'api_access_disabled': 'DNSHE 后台已关闭 API 访问',
    'quota_exceeded': '账户额度不足',
    'rate_limit_exceeded': '触发接口频率限制，请调大 DNSHE_MIN_INTERVAL',
    'provider_operation_failed': '上游 DNS 服务商操作失败，稍后重试',
    'internal_error': 'DNSHE 服务端内部错误',
    'subdomain_not_found': '域名不存在或不属于当前 API Key',
    'not_found': '资源不存在',
    'bad_request': '请求参数有误',
}

# 实测线上接口的 status 返回 "Registered"（首字母大写），而文档写的是 active/suspended/expired。
# 两种口径都按正常状态处理，其余一律在报告里显式标注。
NORMAL_STATUSES = frozenset({'active', 'registered'})

_TRUE_WORDS = frozenset({'1', 'true', 'yes', 'y', 'on', 't'})
_FALSE_WORDS = frozenset({'0', 'false', 'no', 'n', 'off', 'f', '', 'none', 'null'})


class ApiError(RuntimeError):
    """DNSHE 接口调用失败（含响应结构不符合预期）。"""


def to_bool(value, default=False, label=''):
    """把接口返回值稳妥地转成布尔。

    不能直接用 bool()：PHP 后端把 tinyint 序列化成字符串是常见行为，
    而 bool("0") / bool("false") 都是 True —— 对 never_expires 来说，
    这个误判会让域名被永久跳过、报告还显示「永不过期」，属于最坏的静默失效。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0

    text = str(value).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False

    warn(f"{label or '布尔字段'}={value!r} 无法识别，按 {default} 处理")
    return default


def parse_id(value):
    """把 id 归一成 int；无法转换时返回 None（交给上层报「缺少 id」）。

    接口有时把数字序列化成字符串（"12"），原样透传给续期接口虽然大概率
    能用，但一旦服务端做严格类型校验就会失败，且报错指向不明。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


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
    """按官方统一错误结构拼出可读信息：error_code + message + details。

    文档里 422 的 days_until_window / seconds_until_window 是「还要等多久」
    这个最有用的信息，不能只留一句「尚未进入窗口」。
    """
    if not isinstance(payload, dict):
        return fallback

    message = payload.get('message') or payload.get('error') or fallback
    code = payload.get('error_code')
    text = f"{message} [{code}]" if code else str(message)

    details = payload.get('details')
    details = details if isinstance(details, dict) else {}

    def pick(key):
        """顶层与 details 两处都找：不同错误码把字段放在不同层级。"""
        for source in (details, payload):
            if source.get(key) is not None:
                return source[key]
        return None

    extra = [f"{k}={pick(k)}" for k in ('limit', 'remaining', 'reset_at')
             if pick(k) is not None]
    if extra:
        text += f" ({', '.join(extra)})"

    days = pick('days_until_window')
    if days is not None:
        text += f"，还需等待 {days} 天"
    elif pick('seconds_until_window') is not None:
        text += f"，还需等待约 {max(1, int(pick('seconds_until_window')) // 86400)} 天"

    hint = ERROR_HINTS.get(code)
    if hint:
        text += f" —— {hint}"
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
        """解析响应体；解析不了时抛出带状态码的 ApiError（而非 HTTPError）。"""
        try:
            data = resp.json()
        except ValueError:
            raise ApiError(
                f"{what}返回非 JSON 内容 (HTTP {resp.status_code}): {(resp.text or '')[:200]}"
            )
        if not isinstance(data, dict):
            raise ApiError(f"{what}响应格式异常: {str(data)[:200]}")
        return data

    @classmethod
    def _checked(cls, resp, what):
        """统一处理一次响应：body 里的 error_code 优先于 HTTP 状态码。

        文档明确写着 error_code 是「Stable error code (recommended for
        business-side processing)」，所以不能先 raise_for_status 再解析——
        那样只会剩下一句「401 Client Error: Unauthorized」，把
        auth_invalid_credentials / rate_limit_exceeded 这类可行动信息全丢掉。
        """
        try:
            data = cls._payload(resp, what)
        except ApiError as parse_error:
            if resp.status_code >= 400:
                raise ApiError(
                    f"{what}失败 (HTTP {resp.status_code}): "
                    f"{(resp.text or '')[:200] or '空响应'}"
                )
            raise parse_error

        if is_failure_payload(data):
            raise ApiError(f"{what}返回失败: {describe_error(data)}")

        if resp.status_code >= 400:
            # 状态码是错的但 body 没带错误结构，仍然不能当成功
            raise ApiError(f"{what}失败 (HTTP {resp.status_code}): {str(data)[:200]}")
        return data

    def get(self, query, what):
        self.throttle.wait()
        resp = self.list_session.get(f"{self.config.base_url}{query}",
                                     headers=self._headers(), timeout=REQUEST_TIMEOUT,
                                     allow_redirects=False)
        return self._checked(resp, what)

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
        reported_total = None

        while page <= MAX_PAGES:
            query = (
                f"&endpoint=subdomains&action=list&fields={LIST_FIELDS}"
                f"&page={page}&per_page={PAGE_SIZE}"
            )
            # 首页顺带要总数，用于最后的完整性校验；文档提示 include_total 会
            # 多跑一次 COUNT，只在首页付这个成本。
            if page == 1:
                query += "&include_total=1"

            data = self.get(query, "域名列表")

            if 'subdomains' not in data:
                raise ApiError(
                    "域名列表响应缺少 subdomains 字段（接口可能已变更），"
                    f"原始响应: {str(data)[:200]}"
                )
            batch = data['subdomains']
            if not isinstance(batch, list):
                raise ApiError(f"subdomains 字段不是列表: {str(batch)[:200]}")

            reported_total = self._reported_total(data, reported_total, page)
            self._check_page_count(data, batch, page)

            # 服务端若忽略 page 参数会反复返回同一页，靠 id 去重防止无限累积
            fresh = [item for item in batch if not self._already_seen(item, seen_ids)]
            collected.extend(fresh)

            if not self._has_more(data, batch, fresh):
                break

            next_page = self._next_page(data.get('pagination'), page)
            if next_page is None:
                # 服务端说还有下一页，却给不出能前进的页码：宁可失败告警，
                # 也不能静默截断——漏掉的那批域名会一直不续期直到过期。
                raise ApiError(
                    f"域名列表分页异常：服务端标记 has_more=true，但第 {page} 页"
                    f"未能给出可前进的页码（pagination={str(data.get('pagination'))[:120]}），"
                    "已中止以免漏掉域名"
                )
            page = next_page
        else:
            raise ApiError(f"域名列表分页超过 {MAX_PAGES} 页，疑似接口异常，已中止")

        self._verify_total(collected, reported_total)

        if not collected:
            # 空列表极可能是凭据失效或接口变更，绝不能当作"无需续期"；
            # 但服务端明确给出 total=0 / count=0 时，那是真的没有域名。
            if reported_total == 0:
                print("账户下没有任何子域名，本次无需续期")
                return []
            raise ApiError("未获取到任何子域名，疑似凭据失效或接口变更，请人工核查")
        return collected

    @staticmethod
    def _reported_total(data, current, page=1):
        """读取服务端声明的「总数」，仅首页采信。

        pagination.total 才是总数；顶层 count 是「本页条数」（实测：第 2 页
        count=50、共 250 个域名），把它当总数会误报数量不一致，所以 count
        只在首页且没有 total 时退而求其次。
        """
        pagination = data.get('pagination')
        if isinstance(pagination, dict):
            try:
                total = int(pagination.get('total'))
            except (TypeError, ValueError):
                total = None
            if total is not None and total >= 0:
                return total
        if page == 1:
            try:
                count = int(data.get('count'))
            except (TypeError, ValueError):
                count = None
            if count is not None and count >= 0:
                return count
        return current

    @staticmethod
    def _check_page_count(data, batch, page):
        """本页声明的 count 与实际条数对不上时告警（多为服务端截断）。"""
        try:
            declared = int(data.get('count'))
        except (TypeError, ValueError):
            return
        if declared != len(batch):
            warn(f"第 {page} 页声明 count={declared}，实际返回 {len(batch)} 条，"
                 "疑似响应被截断")

    @staticmethod
    def _verify_total(collected, reported_total):
        """用服务端总数校验是否漏收，不一致时告警（不中止，避免误伤）。"""
        if reported_total is None:
            return
        if len(collected) != reported_total:
            warn(f"域名数量不一致：服务端声明 {reported_total} 个，实际收到 "
                 f"{len(collected)} 个，可能有域名未被处理，请人工核查")

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
        """提交续期。返回解析后的响应体（可能是业务失败），仅传输层问题抛异常。

        注意这里不能复用 _checked()：续期的 4xx（如 422 未到窗口）是预期内的
        业务结果，要原样交给调用方渲染，而不是当异常抛出去。
        """
        self.throttle.wait()
        resp = self.renew_session.post(
            f"{self.config.base_url}&endpoint=subdomains&action=renew",
            headers=self._headers(), json={"subdomain_id": subdomain_id},
            timeout=REQUEST_TIMEOUT, allow_redirects=False,
        )
        # 不能直接 raise_for_status：422 renewal_not_yet_available 等业务错误
        # 带着有用的 error_code，丢掉状态码之外的信息会让报告失去可读性。
        try:
            data = self._payload(resp, "续期接口")
        except ApiError as parse_error:
            # 解析失败时不要把原本更具体的 ApiError 换成 HTTPError
            if resp.status_code >= 400:
                raise ApiError(f"续期接口失败 (HTTP {resp.status_code}): "
                               f"{(resp.text or '')[:200] or '空响应'}")
            raise parse_error

        if resp.status_code >= 500 and not is_failure_payload(data):
            # 5xx 却给出成功结构，属于自相矛盾，按失败处理避免误报续期成功
            raise ApiError(f"续期接口异常 (HTTP {resp.status_code}): {str(data)[:200]}")
        return data


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
    status: Optional[str] = None

    @property
    def status_abnormal(self):
        """状态不是 active/registered 时返回原始状态，否则返回 None。

        实测线上返回的是 "Registered"（文档写 active），两种都算正常；
        其余取值（suspended/expired/...）说明域名有问题，必须让人看见。
        """
        if not self.status:
            return None
        if str(self.status).strip().lower() in NORMAL_STATUSES:
            return None
        return str(self.status)


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
        subdomain_id=parse_id(raw.get('id')),
        # 默认按 False（需要续期）处理：接口返回无法识别时，宁可多撞一次 422，
        # 也不能把真正需要续期的域名误判成永不过期而永久跳过。
        never_expires=to_bool(raw.get('never_expires', 0), default=False,
                              label=f"{label}.never_expires"),
        expires_raw=raw.get('expires_at'),
        status=raw.get('status'),
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


def decide(view, threshold_days, now=None):
    """判定该域名本次要做什么：invalid / never / skip / renew。

    判定用精确时间差，不用 days_remaining：后者是 .days 向下取整的结果，
    会把「剩余 180 天 0 小时 30 分」压成 180，再用 >= 180 判成跳过——
    而文档写的是「Renewal window: Opens 180 days before the expiration」，
    此刻窗口其实已经开了。
    """
    if view.problem:
        return 'invalid'
    if view.never_expires:
        return 'never'
    if view.expires_at is not None and now is not None:
        if view.expires_at - now > timedelta(days=threshold_days):
            return 'skip'
        return 'renew'
    # 没有 now 时退回按整数天判定（保持纯函数可测）
    if view.days_remaining is not None and view.days_remaining >= threshold_days:
        return 'skip'
    # 剩余天数未知时也尝试续期：宁可撞一次 422，也不要漏掉真要过期的域名
    return 'renew'


def days_left(view, now):
    """剩余天数，向上取整（未满一天也算一天）；已过期返回负数。"""
    if view.expires_at is None:
        return None
    delta = view.expires_at - now
    if delta.total_seconds() < 0:
        # 已过期：向下取整，避免 -0.5 天显示成 0
        return -(-delta).days if delta.seconds else delta.days
    return -((-delta) // timedelta(days=1))


def describe_remaining(view, now):
    """报告里的一句话剩余时间，过期域名显示「已过期 N 天」。"""
    if view.expires_at is None:
        return None
    delta = view.expires_at - now
    if delta.total_seconds() < 0:
        return f"已过期 {-delta.days}天"
    return f"剩余 {days_left(view, now)}天"


def status_note(view):
    """异常状态的附加说明，拼在报告行尾；正常状态返回空串。"""
    abnormal = view.status_abnormal
    if not abnormal:
        return ""
    if abnormal.lower() == 'expired':
        return "  ⚠️ 域名状态 expired（已过期，续期窗口可能已关闭）"
    return f"  ⚠️ 域名状态 {abnormal}"


def mark(view, line):
    """给一行报告追加状态说明。"""
    return f"{line}{status_note(view)}"


def expiry_line(view, now=None):
    if view.problem:
        return mark(view, f"{view.label}: 到期时间 未知（记录异常）")
    if view.never_expires:
        return mark(view, f"{view.label}: 到期时间 永久有效")
    remaining = describe_remaining(view, now) if now is not None else None
    if remaining:
        return mark(view, f"{view.label}: 到期时间 {view.expires_raw} ({remaining})")
    if view.days_remaining is not None:
        return mark(view, f"{view.label}: 到期时间 {view.expires_raw} (剩余 {view.days_remaining}天)")
    if view.expires_raw:
        return mark(view, f"{view.label}: 到期时间 {view.expires_raw} (无法解析剩余天数)")
    return mark(view, f"{view.label}: 到期时间 未知")


def format_charge(charged):
    """charged_amount 为 0 表示免费续期。"""
    try:
        if float(charged) == 0:
            return "免费"
    except (TypeError, ValueError):
        # 字段缺失或类型异常时不要把 None 直接拼进报告
        return "费用未知"
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
            # 响应不是 dict 说明接口行为已变，绝不能当成推送成功——那会让
            # 整轮运行报「一切正常」，而实际上一条通知都没发出去。
            if not isinstance(result, dict):
                warn(f"PushPlus 响应格式异常: {str(result)[:200]}")
                return False
            if result.get('code') != 200:
                warn(f"PushPlus 推送未成功 (code={result.get('code')}): {result.get('msg')}")
                return False
            print("PushPlus 推送成功")
            return True
        except Exception as e:
            warn(f"PushPlus 推送失败: {e}")
            return False


def utf16_len(text):
    """按 UTF-16 码元计长度。

    Telegram 的 4096 上限是 UTF-16 code unit，不是 Python 的字符数：
    BMP 外的 emoji（😀🎉🚀 等）在 UTF-16 里占 2 个码元，用 len() 会低估，
    切出来的消息可能超限被拒（400 MESSAGE_TOO_LONG）。
    """
    return len(text.encode('utf-16-le')) // 2


def split_message(content, limit=TELEGRAM_MESSAGE_LIMIT):
    """把超长报告切成若干条不超过 limit 个 UTF-16 码元的消息。

    优先在最近的换行处断开，避免把一行域名记录切成两半；
    单行本身就超限时才在行内硬切。
    """
    chunks = []
    remaining = content
    while utf16_len(remaining) > limit:
        # 先定位「不超过 limit 个码元」的最长前缀，再在其中找换行
        hard = _utf16_prefix_len(remaining, limit)
        cut = remaining.rfind('\n', 0, hard + 1)
        if cut <= 0:
            cut = hard
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip('\n')
    chunks.append(remaining)
    return chunks


def _utf16_prefix_len(text, limit):
    """返回不超过 limit 个 UTF-16 码元的最长前缀长度（按字符计）。"""
    used = 0
    for index, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > limit:
            return index
    return len(text)


def redact(text, secrets=()):
    """把日志文本里出现的密钥替换成占位符。

    requests 的连接异常消息会带上完整 URL，而 Telegram 的 URL 里就嵌着
    bot token（/bot<token>/sendMessage）。公开仓库的 Actions 日志人人可见，
    不脱敏等于把 token 直接写进日志。
    """
    result = str(text)
    for secret in secrets:
        # 太短的串（如测试里的单字符 token）做全局替换会把正常文本也切碎，
        # 反而毁掉错误信息。真实 token 都远超这个长度。
        if secret and len(secret) >= 8:
            result = result.replace(secret, '<已脱敏>')
    # 兜底：即使没拿到 token 值，也把 /bot<...>/ 这种形状抹掉
    result = re.sub(r'/bot[^/\s]+/', '/bot<已脱敏>/', result)
    return result


def telegram_error_detail(resp, bot_token=''):
    """提取错误响应体里的原因。

    Telegram 的 4xx/5xx 响应体带 JSON description（如「bot was blocked by
    the user」），比状态码本身有用得多；非 JSON 时退回原文片段。
    """
    try:
        payload = resp.json()
    except ValueError:
        return redact((resp.text or '')[:200], (bot_token,))
    if isinstance(payload, dict) and payload.get('description'):
        return redact(str(payload['description']), (bot_token,))
    return redact(str(payload)[:200], (bot_token,))


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
                    warn(f"Telegram 返回非 JSON 内容: "
                         f"{redact((resp.text or '')[:200], (self.bot_token,))}")
                    ok = False
                    continue
                # Telegram 约定 ok=true 为成功；ok=false 时 description 带原因。
                # 响应不是 dict 时不能直接 .get()，否则 AttributeError 会被
                # 下面的 except 吞成「推送失败」，看不出真实原因。
                if not isinstance(result, dict):
                    warn(f"Telegram 第 {index} 段响应格式异常: "
                         f"{redact(str(result)[:200], (self.bot_token,))}")
                    ok = False
                elif not result.get('ok'):
                    warn(f"Telegram 第 {index} 段推送未成功: "
                         f"{redact(str(result.get('description', result)), (self.bot_token,))}")
                    ok = False
            except Exception as e:
                # raise_for_status 抛出的 HTTPError 带着响应体，里面的
                # description 才是 403/400 的真正原因，不能只报状态码
                err_resp = getattr(e, 'response', None)
                if err_resp is not None:
                    warn(f"Telegram 第 {index} 段推送失败 "
                         f"(HTTP {err_resp.status_code}): "
                         f"{telegram_error_detail(err_resp, self.bot_token)}")
                else:
                    # 连接异常的消息里带着含 token 的 URL，必须脱敏
                    warn(f"Telegram 第 {index} 段推送失败: "
                         f"{redact(e, (self.bot_token,))}")
                ok = False
        if ok:
            print("Telegram 推送成功")
        return ok


class CompositeNotifier:
    """把报告分发给所有已配置的通知通道。

    失败语义：只要有「任一」通道送达就算送达。原先是 all(...)，PushPlus
    成功、Telegram 偶发失败时退出码也是 1，Actions 里显示红色失败——可报告
    其实已经发出去了，这是误报。全部通道都失败才算推送失败。

    - 未配置任何通道：提示后视为成功
    - 所有已配置通道都失败：返回 False，让进程以退出码 1 收场
    """

    def __init__(self, channels):
        self.channels = channels

    def send(self, content):
        if not self.channels:
            print("未配置任何通知通道，跳过推送")
            return True
        # 不能用 all(...)：all() 遇到第一个 False 就短路，后面的通道根本不会
        # 被调用。PushPlus 挂掉时 Telegram 也就收不到——恰恰是最需要备份通道
        # 的时刻。这里必须全部执行完再汇总。
        results = [channel.send(content) for channel in self.channels]
        delivered = any(results)
        if not delivered:
            warn(f"所有 {len(results)} 个通知通道均推送失败")
        elif not all(results):
            warn("部分通知通道推送失败，但报告已通过其余通道送达")
        return delivered


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
    except ApiError as e:
        # 只兜接口层错误；编程错误（AttributeError/TypeError 等）应当抛出，
        # 否则会被伪装成「请求异常」，掩盖真正的 bug。
        return RenewOutcome(f"❌ {view.label}: 请求异常 ({e})", True)
    except requests.RequestException as e:
        return RenewOutcome(f"❌ {view.label}: 网络异常 ({e})", True)

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
        expiry_lines.append(expiry_line(view, now))
        action = decide(view, config.threshold_days, now)

        if action == 'invalid':
            has_failure = True
            renewal_results.append(f"❌ {view.label}: {view.problem}")
        elif action == 'never':
            renewal_results.append(mark(view, f"⏭️ {view.label}: 已设置为永不过期，跳过续期"))
        elif action == 'skip':
            renewal_results.append(mark(view, (
                f"⏭️ {view.label}: {describe_remaining(view, now)} "
                f"> {config.threshold_days}天，尚未进入续期窗口，跳过"
            )))
        else:
            outcome = renew_one(client, view)
            renewal_results.append(mark(view, outcome.line))
            has_failure = has_failure or outcome.failed
            if outcome.new_expiry:
                # 续期成功后刷新到期清单，避免第二段显示续期前的旧时间
                view.expires_raw = outcome.new_expiry
                view.expires_at = parse_expiry(outcome.new_expiry, config.tz)
                view.days_remaining = (
                    (view.expires_at - now).days if view.expires_at else None
                )
                expiry_lines[-1] = expiry_line(view, now)

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
