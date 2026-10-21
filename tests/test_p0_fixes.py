"""P0 修复的回归测试。

这些用例对应 OPTIMIZATION_PLAN.md 的 P0-1 ~ P0-5，每一条在修复前都会失败。
运行：python3 -m unittest discover -s tests -v
"""

import contextlib
import io
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import renew_domains as rd  # noqa: E402


# ---------- 测试替身 ----------

class FakeResponse:
    def __init__(self, data=None, status=200, text=None):
        self._data = data
        self.status_code = status
        self.text = text if text is not None else (str(data) if data is not None else "")

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise rd.requests.HTTPError(f"{self.status_code} error", response=self)


class FakeSession:
    """按顺序吐出预设响应，并记录请求过的 URL。

    传入裸 dict 会自动包成 200 响应，省去测试里到处写 FakeResponse。
    """

    def __init__(self, responses):
        self.responses = responses if isinstance(responses, list) else [responses]
        self.urls = []
        self.index = 0

    def _next(self, url):
        self.urls.append(url)
        resp = self.responses[min(self.index, len(self.responses) - 1)]
        self.index += 1
        return resp if isinstance(resp, FakeResponse) else FakeResponse(resp)

    def get(self, url, **kwargs):
        return self._next(url)

    def post(self, url, **kwargs):
        return self._next(url)


def records(start, count):
    return [
        {
            "id": i,
            "full_domain": f"d{i}.example.com",
            "status": "Registered",
            "expires_at": "2027-01-01 00:00:00",
            "never_expires": 0,
        }
        for i in range(start, start + count)
    ]


def client_with(responses, **kwargs):
    """构造客户端。min_interval=0 让测试不必真的等待节流。"""
    kwargs.setdefault("min_interval", 0)
    config = rd.Config(api_key="k", api_secret="s", **kwargs)
    return rd.DnsheClient(config, list_session=FakeSession(responses),
                          renew_session=FakeSession(responses))


@contextlib.contextmanager
def capture_stderr():
    """warn() 直接写 stderr，不是 logging，所以要用重定向来断言。"""
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer):
        yield buffer


NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)
TZ = timezone.utc


# ---------- P0-1 never_expires ----------

class TestNeverExpires(unittest.TestCase):
    """P0-1：bool("0") 是 True，曾让域名被永久跳过。"""

    def decide_for(self, value):
        view = rd.inspect_domain(
            {"id": 1, "full_domain": "a.example.com", "never_expires": value,
             "expires_at": "2027-01-01 00:00:00"}, 0, NOW, TZ)
        return view, rd.decide(view, 180)

    def test_string_zero_means_renew(self):
        view, action = self.decide_for("0")
        self.assertFalse(view.never_expires)
        self.assertEqual(action, "renew")

    def test_string_false_means_renew(self):
        view, action = self.decide_for("false")
        self.assertFalse(view.never_expires)
        self.assertEqual(action, "renew")

    def test_numeric_and_string_true_mean_never(self):
        for value in (1, "1", True, "true", 2.0):
            with self.subTest(value=value):
                view, action = self.decide_for(value)
                self.assertTrue(view.never_expires)
                self.assertEqual(action, "never")

    def test_falsy_values_mean_renew(self):
        for value in (0, False, None, ""):
            with self.subTest(value=value):
                view, action = self.decide_for(value)
                self.assertFalse(view.never_expires)
                self.assertEqual(action, "renew")

    def test_unknown_value_falls_back_to_renew(self):
        """无法识别时必须按「需要续期」处理，宁可多撞一次 422。"""
        view, action = self.decide_for("maybe")
        self.assertFalse(view.never_expires)
        self.assertEqual(action, "renew")

    def test_to_bool_matrix(self):
        self.assertFalse(rd.to_bool("0"))
        self.assertFalse(rd.to_bool(" off "))
        self.assertTrue(rd.to_bool("YES"))
        self.assertTrue(rd.to_bool(1))
        self.assertFalse(rd.to_bool(0))
        self.assertTrue(rd.to_bool(None, default=True))


# ---------- P0-2 错误解析 ----------

class TestErrorHandling(unittest.TestCase):
    """P0-2：raise_for_status 曾把 error_code / message 全部丢掉。"""

    AUTH_BODY = {"success": False, "error_code": "auth_invalid_credentials",
                 "message": "Invalid API key", "details": {}}

    def test_http_401_keeps_error_code(self):
        with self.assertRaises(rd.ApiError) as ctx:
            client_with(FakeResponse(self.AUTH_BODY, 401)).fetch_subdomains()
        message = str(ctx.exception)
        self.assertIn("auth_invalid_credentials", message)
        self.assertIn("Invalid API key", message)

    def test_http_500_html_is_reported_not_swallowed(self):
        with self.assertRaises(rd.ApiError) as ctx:
            client_with(FakeResponse(None, 500, "<html>oops</html>")).fetch_subdomains()
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_http_200_with_failure_body_raises(self):
        body = {"success": False, "error_code": "internal_error", "message": "boom"}
        with self.assertRaises(rd.ApiError) as ctx:
            client_with(FakeResponse(body, 200)).fetch_subdomains()
        self.assertIn("internal_error", str(ctx.exception))

    def test_renew_422_is_returned_not_raised(self):
        """422 未到窗口是预期业务结果，必须原样交给调用方渲染。"""
        body = {"success": False, "error_code": "renewal_not_yet_available",
                "message": "域名尚未进入可续期时间窗口", "days_until_window": 3198}
        payload = client_with(FakeResponse(body, 422)).renew(1)
        self.assertEqual(payload["error_code"], "renewal_not_yet_available")

    def test_renew_422_renders_waiting_days(self):
        body = {"success": False, "error_code": "renewal_not_yet_available",
                "message": "域名尚未进入可续期时间窗口",
                "details": {"days_until_window": 3198}}
        outcome = rd.renew_one(client_with(FakeResponse(body, 422)),
                               rd.DomainView(label="x.example.com", subdomain_id=1))
        self.assertFalse(outcome.failed)  # 未到窗口不算失败
        self.assertIn("3198", outcome.line)

    def test_describe_error_reads_details_and_top_level(self):
        text = rd.describe_error({"error_code": "rate_limit_exceeded",
                                  "message": "Rate limit exceeded",
                                  "details": {"limit": 60, "reset_at": "2025-10-19 15:31:00"}})
        self.assertIn("limit=60", text)
        self.assertIn("reset_at=2025-10-19 15:31:00", text)

    def test_describe_error_adds_actionable_hint(self):
        text = rd.describe_error({"error_code": "auth_invalid_credentials",
                                  "message": "Invalid API key"})
        self.assertIn("重新创建", text)

    def test_describe_error_falls_back_to_seconds(self):
        text = rd.describe_error({"error_code": "renewal_not_yet_available",
                                  "message": "x", "seconds_until_window": 86400 * 5})
        self.assertIn("5 天", text)

    def test_redirect_is_rejected(self):
        """跨主机重定向会丢掉 X-API-Key/X-API-Secret，必须显式失败。"""
        client = client_with(FakeResponse(None, 302, ""))
        seen = {}
        original = client.list_session.get

        def spy(url, **kwargs):
            seen.update(kwargs)
            return original(url, **kwargs)

        client.list_session.get = spy
        with self.assertRaises(rd.ApiError):
            client.fetch_subdomains()
        self.assertFalse(seen.get("allow_redirects", True))


# ---------- P0-3 通知通道 ----------

class RecordingChannel:
    def __init__(self, name, ok):
        self.name = name
        self.ok = ok
        self.calls = 0

    def send(self, content):
        self.calls += 1
        return self.ok


class TestCompositeNotifier(unittest.TestCase):
    """P0-3：all(...) 短路曾让第二个通道永远收不到消息。"""

    def test_all_channels_run_even_if_first_fails(self):
        pushplus = RecordingChannel("pushplus", False)
        telegram = RecordingChannel("telegram", True)
        result = rd.CompositeNotifier([pushplus, telegram]).send("x")
        self.assertEqual(pushplus.calls, 1)
        self.assertEqual(telegram.calls, 1)  # 修复前这里是 0
        # 只要有一个通道送达就算送达（P1-9）：否则 PushPlus 成功、Telegram
        # 失败时会误报红色失败
        self.assertTrue(result)

    def test_all_channels_failed_is_failure(self):
        channels = [RecordingChannel("a", False), RecordingChannel("b", False)]
        self.assertFalse(rd.CompositeNotifier(channels).send("x"))
        self.assertEqual([c.calls for c in channels], [1, 1])

    def test_all_success(self):
        channels = [RecordingChannel("a", True), RecordingChannel("b", True)]
        self.assertTrue(rd.CompositeNotifier(channels).send("x"))
        self.assertEqual([c.calls for c in channels], [1, 1])

    def test_no_channel_is_success(self):
        self.assertTrue(rd.CompositeNotifier([]).send("x"))


# ---------- P0-4 分页 ----------

class TestPagination(unittest.TestCase):
    """P0-4：分页无法前进时静默截断，少掉的域名永不续期。"""

    def test_has_more_without_progress_raises(self):
        page = {"success": True, "count": 200, "subdomains": records(0, 200),
                "pagination": {"page": 1, "per_page": 200,
                               "has_more": True, "next_page": 1}}
        with self.assertRaises(rd.ApiError) as ctx:
            client_with(page).fetch_subdomains()
        self.assertIn("分页异常", str(ctx.exception))

    def test_two_pages_are_collected(self):
        page1 = {"success": True, "count": 200, "subdomains": records(0, 200),
                 "pagination": {"page": 1, "per_page": 200,
                                "has_more": True, "next_page": 2, "total": 250}}
        page2 = {"success": True, "count": 50, "subdomains": records(200, 50),
                 "pagination": {"page": 2, "per_page": 200, "has_more": False}}
        client = client_with([page1, page2])
        self.assertEqual(len(client.fetch_subdomains()), 250)
        self.assertIn("include_total=1", client.list_session.urls[0])

    def test_total_mismatch_warns(self):
        page1 = {"success": True, "count": 200, "subdomains": records(0, 200),
                 "pagination": {"page": 1, "per_page": 200,
                                "has_more": True, "next_page": 2, "total": 999}}
        page2 = {"success": True, "count": 50, "subdomains": records(200, 50),
                 "pagination": {"page": 2, "per_page": 200, "has_more": False}}
        with capture_stderr() as err:
            client_with([page1, page2]).fetch_subdomains()
        self.assertIn("不一致", err.getvalue())

    def test_page_count_mismatch_warns(self):
        page = {"success": True, "count": 500, "subdomains": records(0, 200),
                "pagination": {"page": 1, "per_page": 200, "has_more": False}}
        with capture_stderr() as err:
            client_with(page).fetch_subdomains()
        self.assertIn("count=500", err.getvalue())

    def test_empty_account_is_not_a_failure(self):
        empty = {"success": True, "count": 0, "subdomains": [],
                 "pagination": {"page": 1, "per_page": 200,
                                "has_more": False, "total": 0}}
        self.assertEqual(client_with(empty).fetch_subdomains(), [])

    def test_empty_list_without_total_still_raises(self):
        empty = {"success": True, "subdomains": []}
        with self.assertRaises(rd.ApiError):
            client_with(empty).fetch_subdomains()

    def test_duplicate_page_is_not_double_counted(self):
        """服务端忽略 page 参数时靠 id 去重，不会无限累积。"""
        page = {"success": True, "count": 200, "subdomains": records(0, 200),
                "pagination": {"page": 1, "per_page": 200,
                               "has_more": True, "next_page": 2, "total": 200}}
        with self.assertRaises(rd.ApiError):
            # 第二页原样返回第一页内容 → 无新记录 → 停止翻页，而不是死循环
            client_with([page, page]).fetch_subdomains()


# ---------- P0-5 status ----------

class TestDomainStatus(unittest.TestCase):
    """P0-5：请求了 status 却从不使用，暂停/过期域名在报告里看不出来。"""

    def view_for(self, status):
        return rd.inspect_domain(
            {"id": 1, "full_domain": "a.example.com", "status": status,
             "expires_at": "2027-01-01 00:00:00", "never_expires": 0}, 0, NOW, TZ)

    def test_normal_statuses_are_silent(self):
        for status in ("Registered", "active", "ACTIVE", None):
            with self.subTest(status=status):
                self.assertIsNone(self.view_for(status).status_abnormal)
                self.assertNotIn("⚠️", rd.expiry_line(self.view_for(status)))

    def test_suspended_is_flagged(self):
        self.assertEqual(self.view_for("suspended").status_abnormal, "suspended")
        self.assertIn("suspended", rd.expiry_line(self.view_for("suspended")))

    def test_expired_is_flagged_with_hint(self):
        line = rd.expiry_line(self.view_for("expired"))
        self.assertIn("expired", line)
        self.assertIn("续期窗口可能已关闭", line)

    def test_status_survives_into_renewal_lines(self):
        """续期结果行也要带状态，否则「续期成功」会掩盖 suspended。"""
        body = {"success": True, "new_expires_at": "2027-12-01 00:00:00",
                "charged_amount": 0, "remaining_days": 406}
        outcome = rd.renew_one(client_with(FakeResponse(body, 200)),
                               self.view_for("suspended"))
        self.assertIn("suspended", rd.mark(self.view_for("suspended"), outcome.line))


# ---------- 端到端 ----------

class TestRun(unittest.TestCase):
    def test_run_refreshes_expiry_after_renew(self):
        class StubClient:
            def fetch_subdomains(self):
                return [{"id": 1, "full_domain": "a.example.com", "status": "Registered",
                         "expires_at": "2026-12-01 00:00:00", "never_expires": 0}]

            def fetch_quota(self):
                return {"available": 2, "used": 7, "total": 9}

            def renew(self, subdomain_id):
                return {"success": True, "new_expires_at": "2027-12-01 00:00:00",
                        "charged_amount": 0, "remaining_days": 406}

        sent = []

        class StubNotifier:
            def send(self, content):
                sent.append(content)
                return True

        code = rd.run(rd.Config(api_key="k", api_secret="s"), StubClient(),
                      StubNotifier(), now=NOW)
        self.assertEqual(code, 0)
        self.assertIn("续期成功", sent[0])
        # 第二段应显示续期后的新到期时间，而不是旧的
        self.assertIn("2027-12-01", sent[0].split("=== 所有域名到期时间 ===")[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
