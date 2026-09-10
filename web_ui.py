r"""
BANG DIEU KHIEN (web) — mo trang localhost co o nhap va 4 cot tra loi.

Cach chay:
  1. Chay launch_chrome.bat, dang nhap du 4 site, mo san 1 notebook trong NotebookLM
  2. Chay:  venv\Scripts\python.exe web_ui.py
  3. Mo http://localhost:8000

Chi dung thu vien co san cua Python, khong can cai them gi ngoai playwright.

Cach hoat dong:
  - 1 luong chay san event loop asyncio, giu ket noi Playwright toi Chrome
  - HTTP server nhan cau hoi, day viec vao loop do, tra ve ngay 1 ma job
  - Trang web hoi lai /api/status theo chu ky de cap nhat tung cot
"""

import asyncio
import json
import threading
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import adapters

PORT = 8000
LOG_DIR = Path(__file__).parent / "logs"

LOOP = None       # event loop chay o luong nen
PW = None         # giu tham chieu, neu de bi thu gom thi ket noi dut
BROWSER = None    # ket noi Playwright toi Chrome
JOBS = {}         # job_id -> trang thai
LOCK = threading.Lock()


# ---------------------------------------------------------------- nen (async)

def start_background_loop():
    """Dung 1 event loop rieng o luong nen va giu ket noi Chrome trong do."""
    global LOOP, PW, BROWSER
    LOOP = asyncio.new_event_loop()
    threading.Thread(target=LOOP.run_forever, daemon=True).start()

    async def _connect():
        global PW, BROWSER
        PW, BROWSER = await adapters.connect()

    asyncio.run_coroutine_threadsafe(_connect(), LOOP).result(timeout=30)


def tabs_status() -> list:
    """Tab nao dang mo san, tab nao thieu."""
    out = []
    for ad in adapters.ALL:
        try:
            page = ad.find_page(BROWSER)
            ad.check_ready(page)
            out.append({"name": ad.name, "ready": True, "note": ""})
        except Exception as e:
            out.append({"name": ad.name, "ready": False, "note": str(e)[:200]})
    return out


def save_log(question: str, results: list):
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"web-{datetime.now():%Y%m%d}.md"
    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n## [{datetime.now():%H:%M:%S}] {question}\n")
        for r in results:
            f.write(f"\n### {r['name']} ({r['seconds']}s)\n\n{r['reply']}\n")


def start_job(question: str, target_keys: list) -> str:
    """Tao 1 job va day vao event loop nen. Tra ve job_id ngay lap tuc."""
    targets = adapters.pick(target_keys)
    job_id = uuid.uuid4().hex[:12]

    with LOCK:
        JOBS[job_id] = {
            "question": question,
            "done": False,
            "models": {a.name: {"status": "pending", "reply": "", "seconds": None}
                       for a in targets},
        }

    def on_event(name, status, text):
        with LOCK:
            slot = JOBS[job_id]["models"][name]
            slot["status"] = status
            if text:
                slot["reply"] = text

    async def run():
        results = await adapters.ask_all(BROWSER, question, targets, on_event=on_event)
        with LOCK:
            for r in results:
                JOBS[job_id]["models"][r["name"]].update(
                    status="done" if r["ok"] else "error",
                    reply=r["reply"], seconds=r["seconds"])
            JOBS[job_id]["done"] = True
        save_log(question, results)

    asyncio.run_coroutine_threadsafe(run(), LOOP)
    return job_id


# ---------------------------------------------------------------- HTTP server

PAGE = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ai-orchestrator</title>
<style>
  :root {
    --bg: #f6f7f9; --card: #fff; --line: #e3e6ea; --text: #1b1f24;
    --muted: #6b7480; --accent: #2b6cb0; --ok: #1a7f4b; --err: #c0392b;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #14171a; --card: #1c2024; --line: #2c3238; --text: #e6e9ec;
      --muted: #98a2ad; --accent: #6aa9e9; --ok: #4ec98a; --err: #ff8a7a;
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 15px/1.6 system-ui, "Segoe UI", sans-serif; }
  header { padding: 18px 22px 12px; border-bottom: 1px solid var(--line); }
  h1 { margin: 0 0 12px; font-size: 17px; font-weight: 650; letter-spacing: .2px; }
  .row { display: flex; gap: 10px; align-items: flex-start; }
  textarea {
    flex: 1; min-height: 62px; padding: 10px 12px; resize: vertical;
    border: 1px solid var(--line); border-radius: 9px;
    background: var(--card); color: var(--text); font: inherit;
  }
  textarea:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  button.send {
    padding: 10px 22px; height: 62px; border: 0; border-radius: 9px;
    background: var(--accent); color: #fff; font: inherit; font-weight: 600;
    cursor: pointer;
  }
  button.send:disabled { opacity: .5; cursor: default; }
  .picks { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 14px;
           font-size: 13px; color: var(--muted); }
  .picks label { display: flex; gap: 6px; align-items: center; cursor: pointer; }
  .picks .off { color: var(--err); }
  main { padding: 16px 22px 40px; }
  .grid { display: grid; gap: 14px; grid-template-columns: repeat(4, 1fr); }
  @media (max-width: 1100px) { .grid { grid-template-columns: repeat(2, 1fr); } }
  @media (max-width: 620px)  { .grid { grid-template-columns: 1fr; } }
  .col { background: var(--card); border: 1px solid var(--line);
         border-radius: 11px; overflow: hidden; display: flex; flex-direction: column; }
  .col h2 { margin: 0; padding: 10px 13px; font-size: 13px; font-weight: 650;
            border-bottom: 1px solid var(--line);
            display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  .chip { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 20px;
          border: 1px solid var(--line); color: var(--muted); white-space: nowrap; }
  .chip.running { color: var(--accent); border-color: var(--accent); }
  .chip.done { color: var(--ok); border-color: var(--ok); }
  .chip.error { color: var(--err); border-color: var(--err); }
  .body { padding: 13px; white-space: pre-wrap; word-wrap: break-word;
          font-size: 14px; min-height: 90px; }
  .body.empty { color: var(--muted); font-style: italic; }
  .body.error { color: var(--err); }
  .hint { margin-top: 10px; font-size: 12.5px; color: var(--muted); }
</style>
</head>
<body>
<header>
  <h1>ai-orchestrator — gui 1 cau hoi cho 4 AI cung luc</h1>
  <div class="row">
    <textarea id="q" placeholder="Nhap cau hoi... (Ctrl+Enter de gui)"></textarea>
    <button class="send" id="go">Gui</button>
  </div>
  <div class="picks" id="picks"></div>
  <div class="hint" id="hint"></div>
</header>
<main><div class="grid" id="grid"></div></main>

<script>
const NAMES = ["ChatGPT", "Gemini", "NotebookLM", "Claude"];
let timer = null;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

async function loadTabs() {
  const tabs = await (await fetch("/api/tabs")).json();
  const picks = document.getElementById("picks");
  picks.innerHTML = "";
  let offNote = "";
  for (const t of tabs) {
    const lab = el("label", t.ready ? "" : "off");
    const cb = el("input");
    cb.type = "checkbox";
    cb.value = t.name.toLowerCase();
    cb.checked = t.ready;
    cb.disabled = !t.ready;
    lab.appendChild(cb);
    lab.appendChild(el("span", null, t.name + (t.ready ? "" : " (chua san sang)")));
    picks.appendChild(lab);
    if (!t.ready && !offNote) offNote = t.name + ": " + t.note;
  }
  document.getElementById("hint").textContent = offNote;
}

function chosen() {
  return [...document.querySelectorAll("#picks input:checked")].map(c => c.value);
}

function drawGrid(models) {
  const grid = document.getElementById("grid");
  grid.innerHTML = "";
  for (const name of NAMES) {
    const m = models[name];
    if (!m) continue;
    const col = el("div", "col");
    const h = el("h2");
    h.appendChild(el("span", null, name));
    let label = "cho...";
    if (m.status === "running") label = "dang tra loi";
    else if (m.status === "done") label = m.seconds != null ? m.seconds + "s" : "xong";
    else if (m.status === "error") label = "loi";
    h.appendChild(el("span", "chip " + m.status, label));
    col.appendChild(h);

    let cls = "body";
    let text = m.reply;
    if (!text) { cls += " empty"; text = m.status === "running" ? "dang cho tra loi..." : "chua co"; }
    if (m.status === "error") cls += " error";
    col.appendChild(el("div", cls, text));
    grid.appendChild(col);
  }
}

async function poll(id) {
  const job = await (await fetch("/api/status?id=" + id)).json();
  drawGrid(job.models);
  if (job.done) {
    clearInterval(timer);
    timer = null;
    document.getElementById("go").disabled = false;
  }
}

async function send() {
  const q = document.getElementById("q").value.trim();
  const targets = chosen();
  if (!q || targets.length === 0) return;
  document.getElementById("go").disabled = true;
  const res = await fetch("/api/ask", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({question: q, targets: targets})
  });
  const data = await res.json();
  if (data.error) {
    document.getElementById("hint").textContent = data.error;
    document.getElementById("go").disabled = false;
    return;
  }
  if (timer) clearInterval(timer);
  await poll(data.id);
  timer = setInterval(() => poll(data.id), 700);
}

document.getElementById("go").addEventListener("click", send);
document.getElementById("q").addEventListener("keydown", e => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) send();
});
loadTabs();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # tat log tung request cho do roi terminal

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")

        if path == "/api/tabs":
            return self._send(200, json.dumps(tabs_status(), ensure_ascii=False))

        if path == "/api/status":
            qs = parse_qs(urlparse(self.path).query)
            job_id = (qs.get("id") or [""])[0]
            with LOCK:
                job = JOBS.get(job_id)
                body = json.dumps(job, ensure_ascii=False) if job else '{"error":"khong co job"}'
            return self._send(200 if job else 404, body)

        self._send(404, '{"error":"khong co trang nay"}')

    def do_POST(self):
        if urlparse(self.path).path != "/api/ask":
            return self._send(404, '{"error":"khong co trang nay"}')

        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            question = (data.get("question") or "").strip()
            targets = data.get("targets") or []
            if not question:
                raise ValueError("cau hoi rong")
            job_id = start_job(question, targets)
        except Exception as e:
            return self._send(400, json.dumps({"error": f"{type(e).__name__}: {e}"},
                                              ensure_ascii=False))
        self._send(200, json.dumps({"id": job_id}))


def main():
    print(f"[*] Ket noi toi Chrome tai {adapters.CDP_URL} ...")
    start_background_loop()

    for t in tabs_status():
        mark = "san sang" if t["ready"] else "CHUA SAN SANG"
        print(f"    {t['name']:<11} {mark}")

    url = f"http://localhost:{PORT}"
    print(f"\n[*] Mo {url}   (Ctrl+C de dung)")
    webbrowser.open(url)

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Da dung.")


if __name__ == "__main__":
    main()
