import unittest
from unittest.mock import Mock, patch

import requests

import renew_domains as rd


class FakeResponse:
    def __init__(self, json_data=None, text="", status_code=200, raise_http=False):
        self._json_data = json_data
        self.text = text
        self.status_code = status_code
        self._raise_http = raise_http

    def raise_for_status(self):
        if self._raise_http:
            raise requests.HTTPError("http error", response=self)

    def json(self):
        if self._json_data is None:
            raise ValueError("not json")
        return self._json_data


class TestWeComNotifier(unittest.TestCase):
    def test_skip_when_webhook_not_configured(self):
        session = Mock()
        notifier = rd.WeComNotifier("", session=session)
        self.assertTrue(notifier.send("hello"))
        session.post.assert_not_called()

    def test_success_response(self):
        session = Mock()
        session.post.return_value = FakeResponse(json_data={"errcode": 0, "errmsg": "ok"})
        notifier = rd.WeComNotifier("https://example.test/webhook", session=session)

        self.assertTrue(notifier.send("report"))
        session.post.assert_called_once_with(
            "https://example.test/webhook",
            json={"msgtype": "text", "text": {"content": "report"}},
            timeout=rd.REQUEST_TIMEOUT,
        )

    def test_errcode_non_zero(self):
        session = Mock()
        session.post.return_value = FakeResponse(json_data={"errcode": 93000, "errmsg": "bad webhook"})
        notifier = rd.WeComNotifier("https://example.test/webhook", session=session)
        self.assertFalse(notifier.send("report"))

    def test_http_error_and_network_error(self):
        with self.subTest("http error"):
            session = Mock()
            session.post.return_value = FakeResponse(
                json_data={"errcode": 40013, "errmsg": "invalid"},
                status_code=400,
                raise_http=True,
            )
            notifier = rd.WeComNotifier("https://example.test/webhook", session=session)
            self.assertFalse(notifier.send("report"))

        with self.subTest("network error"):
            session = Mock()
            session.post.side_effect = requests.RequestException("network down")
            notifier = rd.WeComNotifier("https://example.test/webhook", session=session)
            self.assertFalse(notifier.send("report"))

    def test_long_message_all_segments_success(self):
        content = "a" * 18 + "\n" + "b" * 18 + "\n" + "c" * 18
        session = Mock()
        session.post.side_effect = [
            FakeResponse(json_data={"errcode": 0, "errmsg": "ok"}),
            FakeResponse(json_data={"errcode": 0, "errmsg": "ok"}),
            FakeResponse(json_data={"errcode": 0, "errmsg": "ok"}),
        ]
        notifier = rd.WeComNotifier("https://example.test/webhook", session=session)

        with patch.object(rd, "WECOM_MESSAGE_LIMIT_BYTES", 20):
            self.assertTrue(notifier.send(content))

        self.assertEqual(session.post.call_count, 3)
        sent_contents = [call.kwargs["json"]["text"]["content"] for call in session.post.call_args_list]
        self.assertEqual(sent_contents, ["a" * 18, "b" * 18, "c" * 18])
        for item in sent_contents:
            self.assertLessEqual(len(item.encode("utf-8")), 20)

    def test_long_message_partial_failure(self):
        content = "a" * 18 + "\n" + "b" * 18
        session = Mock()
        session.post.side_effect = [
            FakeResponse(json_data={"errcode": 0, "errmsg": "ok"}),
            FakeResponse(json_data={"errcode": 93000, "errmsg": "failed"}),
        ]
        notifier = rd.WeComNotifier("https://example.test/webhook", session=session)

        with patch.object(rd, "WECOM_MESSAGE_LIMIT_BYTES", 20):
            self.assertFalse(notifier.send(content))
        self.assertEqual(session.post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
