"""P1 修复的回归测试。

对应 OPTIMIZATION_PLAN.md 的 P1-1 ~ P1-8（P1-2 / P1-7 在 P0 阶段已完成，
其用例留在 test_p0_fixes.py）。运行：python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import renew_domains as rd  # noqa: E402

try:  # 作为包运行：python -m unittest discover -s tests -t .
    from .test_p0_fixes import (
        FakeResponse, FakeSession, capture_stderr, client_with,
    )
except ImportError:  # pytest / discover -s tests 时按顶层模块导入
    from test_p0_fixes import (
        FakeResponse, FakeSession, capture_stderr, client_with,
    )

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
TZ = timezone(timedelta(hours=8))
TOKEN = "123456789:AAFakeTokenForTestingOnly_xxxxxxxx"


def view_for(expires, status="Registered", never=0):
    return rd.inspect_domain(
        {"id": 1, "full_domain": "a.example.com", "status": status,
         "expires_at": expires, "never_expires": never}, 0, NOW, TZ)


# ---------- P1-1 阈值边界 ----------

class TestThresholdBoundary(unittest.TestCase):
    """P1-1：.days 截断 + >= 让「正好 180 天」被误判为跳过。"""

    def test_exactly_180_days_is_renewable(self):
        expires = (NOW + timedelta(days=180)).astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(rd.decide(view_for(expires), 180, NOW), "renew")

    def test_just_over_180_days_is_skipped(self):
        expires = (NOW + timedelta(days=180, hours=1)).astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(rd.decide(view_for(expires), 180, NOW), "skip")

    def test_just_under_180_days_is_renewable(self):
        expires = (NOW + timedelta(days=179, hours=23)).astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(rd.decide(view_for(expires), 180, NOW), "renew")

    def test_expired_domain_still_attempts_renew(self):
        expires = (NOW - timedelta(days=3)).astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(rd.decide(view_for(expires), 180, NOW), "renew")

    def test_decide_without_now_falls_back_to_days(self):
        view = view_for((NOW + timedelta(days=200)).astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual(rd.decide(view, 180), "skip")


class TestRemainingDisplay(unittest.TestCase):
    """过期域名不该显示「剩余 -2天」。"""

    def test_future_rounds_up(self):
        expires = (NOW + timedelta(days=179, hours=17)).astimezone(TZ)
        view = view_for(expires.strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual(rd.days_left(view, NOW), 180)

    def test_expired_shows_elapsed(self):
        view = view_for((NOW - timedelta(days=2)).astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual(rd.describe_remaining(view, NOW), "已过期 2天")
        self.assertNotIn("-", rd.describe_remaining(view, NOW))

    def test_future_shows_remaining(self):
        view = view_for((NOW + timedelta(days=30)).astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S"))
        self.assertTrue(rd.describe_remaining(view, NOW).startswith("剩余 30天"))

    def test_expiry_line_uses_new_wording(self):
        view = view_for((NOW - timedelta(days=5)).astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S"))
        self.assertIn("已过期 5天", rd.expiry_line(view, NOW))


# ---------- P1-4 UTF-16 分段 ----------

class TestTelegramSplitting(unittest.TestCase):
    """P1-4：Telegram 按 UTF-16 码元计数，BMP 外 emoji 占 2 个。"""

    def test_astral_emoji_does_not_overflow(self):
        content = ("❌ test.example.com: 失败 😀🎉🚀\n") * 400
        for chunk in rd.split_message(content):
            self.assertLessEqual(rd.utf16_len(chunk), 4096)

    def test_content_is_preserved(self):
        content = ("行内容 😀\n" * 3000)
        chunks = rd.split_message(content)
        self.assertEqual("".join(chunks).replace("\n", ""), content.replace("\n", ""))

    def test_single_long_line_is_hard_cut(self):
        content = "😀" * 3000  # 6000 码元，无换行
        chunks = rd.split_message(content)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(rd.utf16_len(chunk), 4096)
        self.assertEqual("".join(chunks), content)

    def test_short_message_is_untouched(self):
        self.assertEqual(rd.split_message("hello"), ["hello"])

    def test_utf16_len_counts_astral_as_two(self):
        self.assertEqual(rd.utf16_len("a"), 1)
        self.assertEqual(rd.utf16_len("😀"), 2)
        self.assertEqual(rd.utf16_len("✅"), 1)


# ---------- P1-5 脱敏 ----------

class TestRedaction(unittest.TestCase):
    """P1-5：requests 的异常消息里带含 bot token 的 URL。"""

    def test_token_is_removed(self):
        text = f"Max retries exceeded with url: /bot{TOKEN}/sendMessage"
        cleaned = rd.redact(text, (TOKEN,))
        self.assertNotIn(TOKEN, cleaned)
        self.assertIn("<已脱敏>", cleaned)

    def test_fallback_pattern_without_known_token(self):
        cleaned = rd.redact(f"/bot{TOKEN}/sendMessage")
        self.assertNotIn(TOKEN, cleaned)

    def test_telegram_connection_error_does_not_leak(self):
        class Boom:
            def post(self, *args, **kwargs):
                raise ConnectionError(f"url: /bot{TOKEN}/sendMessage")

        notifier = rd.TelegramNotifier(TOKEN, "123", session=Boom())
        with capture_stderr() as err:
            self.assertFalse(notifier.send("hi"))
        self.assertNotIn(TOKEN, err.getvalue())


# ---------- P1-8 响应格式异常 ----------

class TestMalformedNotifierResponses(unittest.TestCase):
    """P1-8：非 dict 响应曾被判成功（PushPlus）或抛出 AttributeError。"""

    def test_pushplus_non_dict_is_failure(self):
        notifier = rd.PushPlusNotifier("t", session=FakeSession(FakeResponse([], 200)))
        self.assertFalse(notifier.send("x"))

    def test_pushplus_missing_code_is_failure(self):
        notifier = rd.PushPlusNotifier("t", session=FakeSession(FakeResponse({"msg": "?"}, 200)))
        self.assertFalse(notifier.send("x"))

    def test_telegram_non_dict_is_failure_without_traceback(self):
        notifier = rd.TelegramNotifier("t", "c", session=FakeSession(FakeResponse([], 200)))
        with capture_stderr() as err:
            self.assertFalse(notifier.send("x"))
        self.assertIn("格式异常", err.getvalue())
        self.assertNotIn("has no attribute", err.getvalue())

    def test_telegram_ok_true_is_success(self):
        notifier = rd.TelegramNotifier(
            "t", "c", session=FakeSession(FakeResponse({"ok": True}, 200)))
        self.assertTrue(notifier.send("x"))


# ---------- P1-9 退出码语义 ----------

class TestPartialNotificationDelivery(unittest.TestCase):
    """P1-9：一个通道送达即算送达，避免误报红色失败。"""

    def test_one_of_two_success_is_ok(self):
        class Ch:
            def __init__(self, ok):
                self.ok = ok

            def send(self, content):
                return self.ok

        self.assertTrue(rd.CompositeNotifier([Ch(False), Ch(True)]).send("x"))
        self.assertFalse(rd.CompositeNotifier([Ch(False), Ch(False)]).send("x"))

    def test_run_returns_zero_when_report_delivered_once(self):
        class StubClient:
            def fetch_subdomains(self):
                return [{"id": 1, "full_domain": "a.example.com", "status": "Registered",
                         "expires_at": "2027-06-01 00:00:00", "never_expires": 0}]

            def fetch_quota(self):
                return {"available": 1, "used": 1, "total": 2}

        class Ch:
            def __init__(self, ok):
                self.ok = ok

            def send(self, content):
                return self.ok

        config = rd.Config(api_key="k", api_secret="s")
        notifier = rd.CompositeNotifier([Ch(False), Ch(True)])
        self.assertEqual(rd.run(config, StubClient(), notifier, now=NOW), 0)

        all_failed = rd.CompositeNotifier([Ch(False), Ch(False)])
        self.assertEqual(rd.run(config, StubClient(), all_failed, now=NOW), 1)


# ---------- P1-3 空账户（P0-4 已实现，此处锁定语义） ----------

class TestEmptyAccount(unittest.TestCase):
    def test_zero_total_is_success_not_error(self):
        empty = {"success": True, "count": 0, "subdomains": [],
                 "pagination": {"page": 1, "per_page": 200, "has_more": False, "total": 0}}
        self.assertEqual(client_with(empty).fetch_subdomains(), [])

    def test_run_with_empty_account_returns_zero(self):
        class StubClient:
            def fetch_subdomains(self):
                return []

            def fetch_quota(self):
                return {"available": 5, "used": 0, "total": 5}

        class StubNotifier:
            def send(self, content):
                return True

        code = rd.run(rd.Config(api_key="k", api_secret="s"), StubClient(),
                      StubNotifier(), now=NOW)
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
