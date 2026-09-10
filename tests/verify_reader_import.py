"""Integration check against a local AI Reader checkout, using temporary storage.

Run with AI Reader's Python: python -B tests/verify_reader_import.py PATH_TO_READER
The orchestrator venv supplies Playwright. Chrome is used only in headless mode.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
READER_ROOT = Path(sys.argv[1]).resolve()
sys.path[:0] = [str(ROOT), str(READER_ROOT)]
sys.path.append(str(ROOT / "venv" / "Lib" / "site-packages"))

import socket
import tempfile
import threading
import time
from contextlib import ExitStack
from unittest.mock import patch

import uvicorn
from app import database, inbox, main as reader, storage
from playwright.sync_api import sync_playwright

import talkshow


JOB = {
    "topic": "Kiểm tra import tiếng Việt", "turns_planned": 2, "turn": 2,
    "phase": "done", "question": "", "answer": "", "running": False,
    "finished": True, "error": "", "created_at": "2026-09-10T12:00:00",
    "items": [
        {"role": "mc", "status": "done", "turn": 1,
         "lead": "Lời dẫn", "text": "Câu hỏi thứ nhất?"},
        {"role": "guest", "status": "done", "turn": 1,
         "text": "## Trả lời\n\n- **Giữ nguyên Markdown**"},
        {"role": "mc", "status": "done", "turn": 2, "text": "Câu hỏi thứ hai?"},
        {"role": "guest", "status": "done", "turn": 2, "text": "Trả lời thứ hai."},
        {"role": "mc", "status": "done", "turn": 0, "text": "Lời kết."},
    ],
}


def verify():
    with ExitStack() as stack:
        temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=ROOT)))
        stack.enter_context(patch.object(database, "DB_FILE", temporary / "conversations.db"))
        stack.enter_context(patch.object(inbox, "INBOX_DIR", temporary / "inbox"))
        stack.enter_context(patch.object(storage, "OUTPUT_DIR", temporary))
        stack.enter_context(patch.object(reader, "API_TOKEN", ""))
        stack.enter_context(patch.object(talkshow, "JOBS", {"contract-check": JOB}))
        stack.enter_context(patch.object(talkshow, "EXPORT_TOKEN", ""))
        stack.enter_context(patch.object(talkshow, "tabs_status", return_value=[
            {"name": name, "ready": True, "note": ""} for name in ("ChatGPT", "NotebookLM")
        ]))

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        reader_port = sock.getsockname()[1]
        stack.callback(sock.close)
        service = uvicorn.Server(uvicorn.Config(reader.app, log_level="error", access_log=False))
        worker = threading.Thread(target=service.run, kwargs={"sockets": [sock]}, daemon=True)
        worker.start()

        def stop_reader():
            service.should_exit = True
            worker.join(timeout=10)

        stack.callback(stop_reader)
        deadline = time.monotonic() + 10
        while not service.started:
            assert worker.is_alive() and time.monotonic() < deadline, "Reader did not start"
            time.sleep(0.02)

        app = talkshow.Server(("127.0.0.1", 0), talkshow.Handler)
        stack.callback(app.server_close)
        threading.Thread(target=app.serve_forever, daemon=True).start()
        stack.callback(app.shutdown)

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            try:
                context = browser.new_context()
                # Import does not require TTS or an external voice-list request.
                context.route("**/api/voices", lambda route: route.fulfill(json={
                    "voices": [], "count": 0, "default": "vi-VN-HoaiMyNeural",
                }))
                page_errors = []
                context.on("page", lambda page: page.on(
                    "pageerror", lambda error: page_errors.append(str(error))))
                page = context.new_page()
                page.goto(f"http://127.0.0.1:{app.server_port}")
                page.locator('#history option[value="contract-check"]').wait_for(state="attached")
                page.locator("#history").select_option("contract-check")
                page.locator("#export-port").fill(str(reader_port))
                with page.expect_response(lambda response: "/api/export?" in response.url) as pending:
                    page.locator("#export").click()
                exported = pending.value.json()
                assert exported["ok"], exported
                expected_turns = talkshow.build_turns(JOB)
                saved = database.get(exported["conversation_id"])
                assert saved["turns"] == expected_turns
                assert saved["mode"] == "chat" and saved["source"] == talkshow.EXPORT_SOURCE
                assert len(inbox.pending()) == 1, "Exporter must not consume the one-use import"
                print("PASS: actual export saves five turns in Reader SQLite and preserves pending import", flush=True)

                link = page.locator("#hint a")
                assert link.get_attribute("href") == exported["open_url"]
                with page.expect_popup() as popup_info:
                    link.click()
                imported = popup_info.value
                imported.wait_for_function('document.querySelectorAll(".pair-card").length === 3')
                pairs = imported.evaluate("readPairs()")
                assert pairs == [
                    {"user": expected_turns[0]["text"], "ai": expected_turns[1]["text"]},
                    {"user": expected_turns[2]["text"], "ai": expected_turns[3]["text"]},
                    {"user": expected_turns[4]["text"], "ai": ""},
                ], pairs
                assert imported.evaluate("currentConversationId") == exported["conversation_id"]
                assert inbox.pending() == []
                assert database.get(exported["conversation_id"])["turns"] == expected_turns
                print("PASS: Reader UI pairs questions and answers, preserving topic, lead, Markdown and Vietnamese", flush=True)

                legacy = [{"speaker": "Chủ đề", "text": JOB["topic"]}] + [
                    {"speaker": "MC (ChatGPT)" if item["role"] == "mc" else "Khách mời (NotebookLM)",
                     "text": item["text"]} for item in JOB["items"]
                ]
                broken = imported.evaluate("(turns) => turnsToPairs(turns)", legacy)
                assert len(broken) == 6 and all(not pair["ai"] for pair in broken)
                print("CONFIRMED: old payload produces six user-only cards and zero AI answers", flush=True)
                imported.evaluate("(id) => openConversation(id)", exported["conversation_id"])
                assert imported.evaluate("readPairs()") == pairs
                assert not page_errors, page_errors
                print("PASS: saved conversation reopens with identical pairs and no browser script errors", flush=True)
            finally:
                browser.close()

        with patch.object(reader, "API_TOKEN", "integration-test-token"):
            before = len(database.listing())
            try:
                talkshow.export_show("contract-check", reader_port)
            except RuntimeError as error:
                assert "401" in str(error) and "AI_READER_TOKEN" in str(error)
            else:
                raise AssertionError("Missing token was accepted")
            assert len(database.listing()) == before
            with patch.object(talkshow, "EXPORT_TOKEN", "integration-test-token"):
                authorized = talkshow.export_show("contract-check", reader_port)
            assert database.get(authorized["conversation_id"])["turns"] == talkshow.build_turns(JOB)
            print("PASS: Reader rejects missing token and accepts the exporter's X-API-Key", flush=True)
    print("Integration complete; temporary databases and servers cleaned up.", flush=True)


if __name__ == "__main__":
    verify()
