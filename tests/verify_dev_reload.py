"""Exercise actual process restarts and browser refresh with isolated fake AI tabs."""

import json
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

import dev_reload


FAKE_ADAPTERS = '''import asyncio
CDP_URL = "fake Chrome for reload verification"
class Connection:
    async def stop(self):
        pass
class Adapter:
    def __init__(self, name):
        self.name = name
    def find_page(self, browser):
        return None
    def check_ready(self, page):
        pass
    async def ask(self, browser, prompt):
        if self.name == "NotebookLM":
            await asyncio.sleep(60)
        return "DẪN: Lời dẫn thử nghiệm.\\nHỎI: Câu hỏi thử nghiệm?"
BY_KEY = {name.lower(): Adapter(name) for name in ("ChatGPT", "NotebookLM")}
async def connect():
    return Connection(), None
'''


def read_version(base):
    with urllib.request.urlopen(base + dev_reload.VERSION_PATH, timeout=1) as response:
        assert response.headers["Cache-Control"] == "no-store"
        return json.load(response)["version"]


def wait_ready(base):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            return read_version(base)
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise AssertionError("Server did not become ready")


def verify():
    with tempfile.TemporaryDirectory(dir=ROOT) as folder:
        root = Path(folder)
        for name in ("talkshow.py", "talkshow_prompts.py", "dev_reload.py"):
            shutil.copyfile(ROOT / name, root / name)
        (root / "adapters.py").write_text(FAKE_ADAPTERS, encoding="utf-8")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        entry = root / "talkshow.py"
        source = entry.read_text(encoding="utf-8").replace("PORT = 8001", f"PORT = {port}")
        entry.write_text(source, encoding="utf-8")
        base = f"http://localhost:{port}"
        stop_event = threading.Event()
        supervisor = threading.Thread(
            target=dev_reload.supervise, args=(entry, ["--no-browser"], stop_event), daemon=True)
        supervisor.start()
        try:
            initial_version = wait_ready(base)
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="chrome", headless=True)
                try:
                    page = browser.new_page()
                    errors = []
                    navigations = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("framenavigated", lambda frame: navigations.append(frame.url)
                            if frame == page.main_frame else None)
                    page.goto(base)
                    page.locator("#topic").fill("Reload integration topic")
                    with page.expect_response(lambda response: "/api/start" in response.url) as pending:
                        page.locator("#go").click()
                    job_id = pending.value.json()["id"]
                    page.wait_for_function('document.querySelectorAll("#feed .turn").length === 2')
                    page.locator("#topic").fill("Draft kept across reload")
                    page.locator("#turns").fill("7")
                    page.locator("#export-port").fill("8124")

                    entry.write_text(source + "\ndef broken(\n", encoding="utf-8")
                    page.wait_for_timeout(1800)
                    assert read_version(base) == initial_version, "Syntax error restarted the server"
                    assert len(navigations) == 1
                    print("PASS: invalid Python leaves the working server and browser intact", flush=True)

                    updated = source.replace("<h1>Toạ đàm", "<h1>Toạ đàm — reload verified")
                    entry.write_text(updated, encoding="utf-8")
                    page.wait_for_function(
                        'document.querySelector("h1").textContent.includes("reload verified")',
                        timeout=15000)
                    page.wait_for_function('(id) => jobId === id', arg=job_id)
                    assert page.locator("#topic").input_value() == "Draft kept across reload"
                    assert page.locator("#turns").input_value() == "7"
                    assert page.locator("#export-port").input_value() == "8124"
                    assert page.locator("#history").input_value() == job_id
                    assert page.locator("button.retry").count() == 1
                    assert read_version(base) != initial_version
                    with closing(sqlite3.connect(root / "talkshow.db")) as database:
                        statuses = database.execute(
                            "SELECT status FROM items WHERE show_id = ? ORDER BY pos", (job_id,)
                        ).fetchall()
                    assert statuses == [("done",), ("error",)], statuses
                    page.wait_for_timeout(1500)
                    assert len(navigations) == 2, navigations
                    print("PASS: source edit restarts the server once and automatically refreshes the browser", flush=True)
                    print("PASS: selected show, draft, turns, export port and completed progress survive reload", flush=True)

                    # Runtime file writes must not cause a reload loop.
                    (root / "logs").mkdir(exist_ok=True)
                    (root / "logs" / "test.md").write_text("runtime log")
                    page.wait_for_timeout(1200)
                    assert len(navigations) == 2

                    # Imported module edits must refresh the UI too, even if HTML is unchanged.
                    version = read_version(base)
                    module = root / "talkshow_prompts.py"
                    module.write_text(module.read_text(encoding="utf-8") + "\n# reload check\n", encoding="utf-8")
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline and len(navigations) < 3:
                        page.wait_for_timeout(100)
                    assert len(navigations) == 3, navigations
                    assert read_version(base) != version
                    page.wait_for_function('(id) => jobId === id', arg=job_id)
                    assert page.locator("#topic").input_value() == "Draft kept across reload"
                    assert not errors, errors
                    print("PASS: imported module edits reload correctly; log writes do not; no browser script errors", flush=True)
                finally:
                    browser.close()
        finally:
            stop_event.set()
            supervisor.join(timeout=20)
            assert not supervisor.is_alive(), "Supervisor did not stop its child"
        try:
            read_version(base)
        except (OSError, urllib.error.URLError):
            pass
        else:
            raise AssertionError("Child server was left running")
    print("Reload verification complete; temporary files and processes cleaned up.", flush=True)


if __name__ == "__main__":
    verify()
