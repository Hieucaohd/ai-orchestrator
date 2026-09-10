import io
import json
import unittest
import urllib.error
from unittest.mock import patch

import talkshow


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.job = {
            "topic": "Kiểm tra **tiếng Việt**",
            "items": [
                {"role": "mc", "status": "done", "lead": "Lời dẫn",
                 "text": "Câu hỏi thứ nhất?"},
                {"role": "guest", "status": "done",
                 "text": "## Trả lời\n\n- **Giữ nguyên Markdown**"},
                {"role": "mc", "status": "done", "text": "Câu hỏi thứ hai?"},
                {"role": "guest", "status": "done", "text": "Trả lời thứ hai."},
                {"role": "mc", "status": "done", "text": "Lời kết."},
            ],
        }
        self.enterContext(patch.object(talkshow, "JOBS", {"show": self.job}))
        self.enterContext(patch.object(talkshow, "EXPORT_TOKEN", ""))

    def response(self, port=8123, **changes):
        result = {
            "id": "012345abcdef", "conversation_id": 42, "count": 5,
            "source": talkshow.EXPORT_SOURCE,
            "open_url": f"http://localhost:{port}/#import=012345abcdef",
            "expires_in": 3600,
        }
        result.update(changes)
        return json.dumps(result, ensure_ascii=False).encode("utf-8")

    def receiver(self, body):
        response = io.BytesIO(body)
        response.status = 200
        return patch.object(talkshow.urllib.request, "urlopen", return_value=response)

    def test_reader_roles_preserve_order_topic_lead_and_markdown(self):
        turns = talkshow.build_turns(self.job)
        self.assertEqual([t["speaker"] for t in turns], ["Bạn", "AI", "Bạn", "AI", "Bạn"])
        self.assertEqual(turns[0]["text"],
                         "Chủ đề: Kiểm tra **tiếng Việt**\n\nLời dẫn\n\nCâu hỏi thứ nhất?")
        self.assertEqual(turns[1]["text"], self.job["items"][1]["text"])
        self.assertEqual(turns[-1]["text"], "Lời kết.")
        self.assertEqual(self.job["items"][0]["text"], "Câu hỏi thứ nhất?")

    def test_incomplete_and_blank_items_are_not_exported(self):
        self.job["items"].extend([
            {"role": "guest", "status": "running", "text": "Chưa xong"},
            {"role": "guest", "status": "error", "text": "Lỗi"},
            {"role": "guest", "status": "done", "text": " \n "},
        ])
        self.assertEqual(len(talkshow.build_turns(self.job)), 5)

    def test_topic_alone_does_not_create_a_conversation(self):
        self.job["items"] = []
        with patch.object(talkshow.urllib.request, "urlopen") as send:
            with self.assertRaisesRegex(RuntimeError, "chua co noi dung"):
                talkshow.export_show("show", 8123)
            send.assert_not_called()

    def test_single_completed_turn_without_topic_can_be_exported(self):
        self.job["topic"] = ""
        self.job["items"] = self.job["items"][:1]
        with self.receiver(self.response(count=1)):
            result = talkshow.export_show("show", 8123)
        self.assertEqual(result["turns"], 1)

    def test_request_and_response_follow_reader_contract(self):
        with self.receiver(self.response()) as send:
            result = talkshow.export_show("show", "8123")
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:8123/api/import")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json; charset=utf-8")
        self.assertIsNone(request.get_header("X-api-key"))
        self.assertEqual(json.loads(request.data.decode("utf-8")), {
            "source": talkshow.EXPORT_SOURCE, "turns": talkshow.build_turns(self.job),
        })
        self.assertEqual(result["conversation_id"], 42)
        self.assertEqual(result["turns"], 5)
        self.assertEqual(result["open_url"], "http://localhost:8123/#import=012345abcdef")

    def test_optional_token_matches_reader_header(self):
        with patch.object(talkshow, "EXPORT_TOKEN", "test-token"):
            with self.receiver(self.response()) as send:
                talkshow.export_show("show", 8123)
        self.assertEqual(send.call_args.args[0].get_header("X-api-key"), "test-token")

    def test_default_port_matches_reader_launcher(self):
        with self.receiver(self.response(port=8002)) as send:
            talkshow.export_show("show")
        self.assertEqual(send.call_args.args[0].full_url, "http://localhost:8002/api/import")

    def test_invalid_ports_never_send_a_request(self):
        with patch.object(talkshow.urllib.request, "urlopen") as send:
            for port in ("", "abc", 0, -1, 65536, 3.5, True, "8000/path"):
                with self.subTest(port=port), self.assertRaises(ValueError):
                    talkshow.export_show("show", port)
            send.assert_not_called()

    def test_success_status_without_import_confirmation_is_rejected(self):
        bodies = [
            b"<html>Some other service</html>", b"{}", b"null", b"[]",
            self.response(count=4), self.response(count=True),
            self.response(conversation_id=None), self.response(id="bad"),
            self.response(open_url="https://example.com/"),
        ]
        for body in bodies:
            with self.subTest(body=body), self.receiver(body):
                with self.assertRaisesRegex(RuntimeError, "AI Reader"):
                    talkshow.export_show("show", 8123)

    def test_reader_errors_are_readable(self):
        for code, detail, expected in (
            (401, "Thiếu hoặc sai header X-API-Key.", "AI_READER_TOKEN"),
            (422, [{"loc": ["body", "turns"], "msg": "Too many turns"}], "Too many turns"),
        ):
            error = urllib.error.HTTPError(
                "http://localhost:8123/api/import", code, "Rejected", {},
                io.BytesIO(json.dumps({"detail": detail}).encode()),
            )
            with patch.object(talkshow.urllib.request, "urlopen", side_effect=error):
                with self.assertRaisesRegex(RuntimeError, expected):
                    talkshow.export_show("show", 8123)


if __name__ == "__main__":
    unittest.main()
