"""P2 修复的回归测试。

对应 OPTIMIZATION_PLAN.md 的 P2-7（小问题合集）。P2-4 / P2-8 按需求跳过。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import renew_domains as rd  # noqa: E402

try:
    from .test_p0_fixes import FakeResponse, FakeSession, capture_stderr
except ImportError:
    from test_p0_fixes import FakeResponse, FakeSession, capture_stderr


class TestParseId(unittest.TestCase):
    """P2-7：id 为字符串时原样透传，服务端严格校验会失败。"""

    def test_int_passthrough(self):
        self.assertEqual(rd.parse_id(42), 42)

    def test_numeric_string_is_converted(self):
        self.assertEqual(rd.parse_id("42"), 42)
        self.assertIsInstance(rd.parse_id("42"), int)

    def test_whitespace_is_stripped(self):
        self.assertEqual(rd.parse_id(" 42 "), 42)

    def test_invalid_returns_none(self):
        for value in (None, "", "abc", {}, [], True):
            with self.subTest(value=value):
                self.assertIsNone(rd.parse_id(value))

    def test_view_reports_missing_id(self):
        from datetime import datetime, timezone
        view = rd.inspect_domain({"full_domain": "a.example.com", "id": "not-a-number"},
                                 0, datetime(2026, 10, 21, tzinfo=timezone.utc), timezone.utc)
        self.assertEqual(view.problem, "响应缺少 id 字段，无法续期")


class TestFormatCharge(unittest.TestCase):
    """P2-7：charged_amount 缺失时曾渲染成「消耗 None 积分」。"""

    def test_zero_is_free(self):
        self.assertEqual(rd.format_charge(0), "免费")
        self.assertEqual(rd.format_charge(0.0), "免费")
        self.assertEqual(rd.format_charge("0"), "免费")

    def test_missing_is_unknown_not_none(self):
        self.assertEqual(rd.format_charge(None), "费用未知")
        self.assertNotIn("None", rd.format_charge(None))

    def test_garbage_is_unknown(self):
        self.assertEqual(rd.format_charge("abc"), "费用未知")

    def test_positive_amount_is_shown(self):
        self.assertIn("9.9", rd.format_charge(9.9))


class TestRenewOneExceptionScope(unittest.TestCase):
    """P2-7：裸 except 会把编程错误伪装成「请求异常」。"""

    def test_api_error_is_caught(self):
        class Boom:
            def renew(self, sid):
                raise rd.ApiError("接口炸了")

        outcome = rd.renew_one(Boom(), rd.DomainView(label="a.example.com", subdomain_id=1))
        self.assertTrue(outcome.failed)
        self.assertIn("请求异常", outcome.line)

    def test_request_exception_is_caught(self):
        class Boom:
            def renew(self, sid):
                raise rd.requests.ConnectionError("网络不通")

        outcome = rd.renew_one(Boom(), rd.DomainView(label="a.example.com", subdomain_id=1))
        self.assertTrue(outcome.failed)
        self.assertIn("网络异常", outcome.line)

    def test_programming_error_propagates(self):
        class Boom:
            def renew(self, sid):
                raise AttributeError("这是 bug，不该被吞")

        with self.assertRaises(AttributeError):
            rd.renew_one(Boom(), rd.DomainView(label="a.example.com", subdomain_id=1))


class TestPageSize(unittest.TestCase):
    """P2-7：per_page 用文档上限 500，减少大账户翻页次数。"""

    def test_page_size_is_documented_max(self):
        self.assertEqual(rd.PAGE_SIZE, 500)

    def test_request_uses_page_size(self):
        from test_p0_fixes import records
        page = {"success": True, "count": 500, "subdomains": records(0, 500),
                "pagination": {"page": 1, "per_page": 500, "has_more": False, "total": 500}}
        client = rd.DnsheClient(rd.Config(api_key="k", api_secret="s", min_interval=0),
                                list_session=FakeSession(page))
        client.fetch_subdomains()
        self.assertIn("per_page=500", client.list_session.urls[0])


class TestNoUnusedImports(unittest.TestCase):
    """P2-7：确认模块不再导入未使用的 Dict。"""

    def test_dict_not_imported(self):
        self.assertFalse(hasattr(rd, "Dict"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
