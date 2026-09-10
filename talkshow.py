r"""
TOA DAM — trang web dieu phoi ChatGPT (MC) hoi, NotebookLM (khach moi) tra loi.

Vong lap:
  MC dat cau hoi  ->  cat lay phan "HOI:"  ->  gui sang khach moi
  Khach moi tra loi  ->  gui nguyen van ve cho MC  ->  MC hoi tiep
  ... lap den het so luot, roi MC viet loi ket.

Cach chay:
  1. Chay launch_chrome.bat, dang nhap ChatGPT va NotebookLM
  2. Mo san notebook chua tai lieu can noi ve
  3. Chay:  venv\Scripts\python.exe talkshow.py
  4. Mo http://localhost:8001

Mac dinh tu tai lai server va trang web khi sua ma nguon.
Them --no-reload de tat, --no-browser de khong mo tab luc khoi dong.

Chi dung thu vien co san cua Python.

Luu y: NotebookLM khoa nut gui khi o nhap qua ~3900 ky tu, nen cau hoi cua MC
duoc cat bot truoc khi chuyen sang. Xem clip_question().
"""

if __name__ == "__main__":
    from dev_reload import launch
    launch(__file__)

import asyncio
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import adapters
import dev_reload
import talkshow_prompts as P

PORT = 8001
LOG_DIR = Path(__file__).parent / "logs"
DB_PATH = Path(__file__).parent / "talkshow.db"

MC = "ChatGPT"
GUEST = "NotebookLM"

# Port mac dinh cua service nhan hoi thoai, co the doi ngay tren giao dien.
DEFAULT_EXPORT_PORT = 8002
EXPORT_SOURCE = "ai-orchestrator toa dam"
EXPORT_TOKEN = os.environ.get("AI_READER_TOKEN", "").strip()

LOOP = None       # event loop chay o luong nen
PW = None         # giu tham chieu, neu de bi thu gom thi ket noi dut
BROWSER = None
JOBS = {}
LOCK = threading.Lock()


# ---------------------------------------------------------------- luu tru

SCHEMA = """
CREATE TABLE IF NOT EXISTS shows (
    id            TEXT PRIMARY KEY,
    topic         TEXT,
    turns_planned INTEGER,
    turn          INTEGER,
    phase         TEXT,
    question      TEXT,
    answer        TEXT,
    finished      INTEGER,
    error         TEXT,
    created_at    TEXT,
    updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS items (
    show_id  TEXT,
    pos      INTEGER,
    role     TEXT,
    name     TEXT,
    turn     INTEGER,
    lead     TEXT,
    text     TEXT,
    status   TEXT,
    seconds  REAL,
    PRIMARY KEY (show_id, pos)
);
"""

# Thong bao cho nhung buoi bi cat ngang vi tat server giua chung.
INTERRUPTED = "Phiên trước bị dừng giữa chừng (đóng server hoặc mất kết nối)."


def db_init():
    # closing() de dong han ket noi. Rieng `with con:` chi commit transaction
    # chu KHONG dong file — de vay se ro ri file handle sau moi lan ghi.
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        con.executescript(SCHEMA)


def save_job(job_id: str):
    """
    Ghi ca buoi xuong SQLite. Chep du lieu ra ngoai LOCK truoc roi moi ghi,
    de khong giu LOCK trong luc doi o dia.

    Moi buoi nhieu nhat vai chuc luot nen ghi de toan bo cho don gian,
    khoi phai theo doi tung dong da doi hay chua.
    """
    with LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return
        row = (job_id, job["topic"], job["turns_planned"], job["turn"],
               job["phase"], job["question"], job["answer"],
               int(job["finished"]), job["error"],
               job["created_at"], datetime.now().isoformat(timespec="seconds"))
        items = [(job_id, pos, it["role"], it["name"], it["turn"],
                  it.get("lead", ""), it.get("text", ""), it["status"],
                  it.get("seconds"))
                 for pos, it in enumerate(job["items"])]

    with closing(sqlite3.connect(DB_PATH)) as con, con:
        con.execute("""
            INSERT INTO shows VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              topic=excluded.topic, turns_planned=excluded.turns_planned,
              turn=excluded.turn, phase=excluded.phase,
              question=excluded.question, answer=excluded.answer,
              finished=excluded.finished, error=excluded.error,
              updated_at=excluded.updated_at
        """, row)
        con.execute("DELETE FROM items WHERE show_id = ?", (job_id,))
        con.executemany("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?)", items)


def load_jobs():
    """
    Doc lai cac buoi cu luc khoi dong. Buoi nao chua xong ma cung khong co
    loi tuc la bi cat ngang -> danh dau de nut "Chay tiep" hien ra.
    """
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.row_factory = sqlite3.Row
        shows = con.execute("SELECT * FROM shows").fetchall()
        rows = con.execute("SELECT * FROM items ORDER BY show_id, pos").fetchall()

    by_show = {}
    for r in rows:
        by_show.setdefault(r["show_id"], []).append(
            {"role": r["role"], "name": r["name"], "turn": r["turn"],
             "lead": r["lead"], "text": r["text"], "status": r["status"],
             "seconds": r["seconds"]})

    for s in shows:
        finished = bool(s["finished"])
        error = s["error"]
        if not finished and not error:
            error = INTERRUPTED
        JOBS[s["id"]] = {
            "topic": s["topic"], "turns_planned": s["turns_planned"],
            "turn": s["turn"], "phase": s["phase"],
            "question": s["question"] or "", "answer": s["answer"],
            "running": False,          # tien trinh chay no da chet roi
            "finished": finished, "error": error,
            "stop_requested": False,
            "created_at": s["created_at"],
            "items": by_show.get(s["id"], []),
        }
    return len(shows)


def list_shows() -> list:
    with LOCK:
        rows = [{"id": jid, "topic": j["topic"], "created_at": j["created_at"],
                 "finished": j["finished"], "error": j["error"],
                 "turns": len(j["items"])}
                for jid, j in JOBS.items()]
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return rows


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


def stop_background_loop():
    if LOOP is None or not LOOP.is_running():
        return

    async def disconnect():
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if PW is not None:
            await PW.stop()  # Disconnect Playwright; keep the user's Chrome open.

    try:
        asyncio.run_coroutine_threadsafe(disconnect(), LOOP).result(timeout=5)
    finally:
        LOOP.call_soon_threadsafe(LOOP.stop)
    for job_id, job in list(JOBS.items()):
        if job["running"]:
            pause_show(job_id, INTERRUPTED)


def tabs_status() -> list:
    out = []
    for name in (MC, GUEST):
        ad = adapters.BY_KEY[name.lower()]
        try:
            ad.check_ready(ad.find_page(BROWSER))
            out.append({"name": name, "ready": True, "note": ""})
        except Exception as e:
            out.append({"name": name, "ready": False, "note": str(e)[:200]})
    return out


def split_mc(text: str) -> tuple:
    """
    Tach cau tra loi cua MC thanh (loi dan, cau hoi).
    MC duoc yeu cau tra loi dang 'DAN: ... / HOI: ...'. Neu no khong theo
    dung dinh dang thi lay ca doan lam cau hoi, con hon la treo.
    """
    lead, question = "", ""

    m = re.search(r"^\s*DẪN\s*:\s*(.+?)(?=^\s*HỎI\s*:|\Z)", text,
                  flags=re.MULTILINE | re.DOTALL | re.IGNORECASE)
    if m:
        lead = m.group(1).strip()

    m = re.search(r"^\s*HỎI\s*:\s*(.+)", text,
                  flags=re.MULTILINE | re.DOTALL | re.IGNORECASE)
    if m:
        question = m.group(1).strip()

    if not question:
        question = text.strip()
    return lead, question


def clip_question(question: str) -> str:
    """Cat cau hoi cho vua o nhap cua khach moi."""
    if len(question) <= P.MAX_QUESTION:
        return question
    cut = question[:P.MAX_QUESTION]
    # cat o dau cham cuoi cung cho khoi cut giua cau
    dot = max(cut.rfind("."), cut.rfind("?"), cut.rfind("!"))
    if dot > P.MAX_QUESTION // 2:
        cut = cut[:dot + 1]
    return cut.rstrip() + " [câu hỏi đã được rút gọn cho vừa ô nhập]"


def add_item(job_id: str, **fields) -> int:
    with LOCK:
        items = JOBS[job_id]["items"]
        items.append(fields)
        return len(items) - 1


def update_item(job_id: str, index: int, **fields):
    with LOCK:
        JOBS[job_id]["items"][index].update(fields)


def end_show(job_id: str):
    """Buoi toa dam ket thuc han — luu bien ban."""
    with LOCK:
        JOBS[job_id]["running"] = False
        JOBS[job_id]["finished"] = True
        job = json.loads(json.dumps(JOBS[job_id], ensure_ascii=False))
    save_job(job_id)
    save_log(job)


def pause_show(job_id: str, error: str):
    """
    Dung vi loi NHUNG chua ket thuc: giu nguyen phase va so luot, de bam
    "Thu lai" la chay tiep tu dung cho vua hong chu khong lam lai tu dau.
    """
    with LOCK:
        job = JOBS[job_id]
        job["running"] = False
        job["error"] = error
        items = job["items"]
        if items and items[-1]["status"] == "running":
            items[-1].update(status="error", text=f"(LỖI: {error})")
    save_job(job_id)


def save_log(job: dict):
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"toadam-{datetime.now():%Y%m%d-%H%M%S}.md"
    lines = [f"# Toạ đàm: {job['topic']}", f"\n_{datetime.now():%Y-%m-%d %H:%M}_\n"]
    for it in job["items"]:
        who = "MC (ChatGPT)" if it["role"] == "mc" else "Khách mời (NotebookLM)"
        lines.append(f"\n## Lượt {it['turn']} — {who}\n")
        if it.get("lead"):
            lines.append(f"_{it['lead']}_\n")
        lines.append(it.get("text", ""))
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------- xuat di

def build_turns(job: dict) -> list:
    """
    Doi buoi toa dam sang cac cap Ban/AI cua giao dien Chat AI trong AI Reader.

    Loi dan cua MC va cau hoi duoc gop lai thanh 1 luot, vi ngoai doi MC noi
    lien mach chu khong tach ra. Cac luot hong hoac dang chay thi bo qua.
    """
    turns = []
    for it in job["items"]:
        if it["status"] != "done":
            continue
        text = (it.get("text") or "").strip()
        if not text:
            continue
        if it["role"] == "mc":
            lead = (it.get("lead") or "").strip()
            turns.append({"speaker": "Bạn",
                          "text": f"{lead}\n\n{text}" if lead else text})
        else:
            turns.append({"speaker": "AI", "text": text})

    # Chu de di cung luot mo dau, khong tao them mot nguoi noi/cap rong.
    topic = (job.get("topic") or "").strip()
    if turns and topic:
        turns[0]["text"] = f"Chủ đề: {topic}\n\n{turns[0]['text']}"
    return turns


def export_show(job_id: str, port=DEFAULT_EXPORT_PORT) -> dict:
    """Gui ca buoi toa dam sang service khac. Goi tu phia server chu khong
    tu trinh duyet, de khoi vuong CORS."""
    if not re.fullmatch(r"[0-9]{1,5}", str(port)) or not 1 <= int(port) <= 65535:
        raise ValueError("Port service phải là số nguyên từ 1 đến 65535.")
    target = f"http://localhost:{int(port)}/api/import"
    with LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise KeyError("khong co buoi toa dam nao voi ma nay")
        payload = {"source": EXPORT_SOURCE, "turns": build_turns(job)}

    if not payload["turns"]:
        raise RuntimeError("buoi nay chua co noi dung gi de xuat")

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if EXPORT_TOKEN:
        headers["X-API-Key"] = EXPORT_TOKEN
    req = urllib.request.Request(target, data=body, method="POST",
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            try:
                reply = json.loads(res.read())
            except (ValueError, UnicodeError):
                raise RuntimeError(
                    f"{target} không trả về JSON của AI Reader. "
                    "Hãy kiểm tra ô Port service.") from None
            if (not isinstance(reply, dict)
                    or not isinstance(reply.get("id"), str)
                    or not re.fullmatch(r"[0-9a-f]{12}", reply["id"])
                    or type(reply.get("conversation_id")) is not int
                    or reply["conversation_id"] <= 0
                    or type(reply.get("count")) is not int
                    or reply["count"] != len(payload["turns"])
                    or reply.get("open_url") !=
                       f"http://localhost:{int(port)}/#import={reply['id']}"):
                raise RuntimeError(
                    f"{target} trả về dữ liệu không khớp API import của AI Reader. "
                    "Hãy kiểm tra service và hội thoại đã lưu trước khi xuất lại.")
            return {"ok": True, "status": res.status, "target": target,
                    "turns": reply["count"], "id": reply["id"],
                    "conversation_id": reply["conversation_id"],
                    "open_url": reply["open_url"]}
    except urllib.error.HTTPError as e:
        detail = e.read(4096).decode("utf-8", "replace").strip()
        try:
            error = json.loads(detail)
            if isinstance(error, dict) and "detail" in error:
                detail = error["detail"]
                if isinstance(detail, list):
                    detail = "; ".join(str(item.get("msg", item))
                                       if isinstance(item, dict) else str(item)
                                       for item in detail)
        except ValueError:
            pass
        if e.code == 401:
            detail = (f"{detail} Hãy đặt biến môi trường AI_READER_TOKEN khớp "
                      "với token của AI Reader rồi khởi động lại talkshow.py.")
        raise RuntimeError(f"{target} tra ve loi {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"khong ket noi duoc toi {target} ({e.reason}). "
            f"Hãy kiểm tra service đã chạy và ô Port service đã đúng chưa.") from None


async def ask(name: str, prompt: str) -> str:
    # Khong truyen timeout — de moi adapter dung han muc rieng cua no
    # (NotebookLM cham hon han nen duoc cho lau hon, xem adapters.py).
    ad = adapters.BY_KEY[name.lower()]
    return await ad.ask(BROWSER, prompt)


async def run_show(job_id: str):
    """
    Vong lap chinh cua buoi toa dam.

    Tien do (dang o luot may, dang cho ai noi) nam trong chinh job chu khong
    phai bien cuc bo, nen ham nay goi lai duoc sau khi loi: no doc phase ra va
    chay tiep tu dung cho vua hong.
    """
    try:
        while True:
            with LOCK:
                job = JOBS[job_id]
                if job["stop_requested"]:
                    break
                phase = job["phase"]
                turn = job["turn"]
                turns = job["turns_planned"]
                topic = job["topic"]
                question = job["question"]
                answer = job["answer"]

            if phase == "mc":
                if turn == 1:
                    prompt = (P.fill(P.MC_SETUP, TOPIC=topic, MAX=P.MAX_QUESTION)
                              + "\n\n---\n\n" + P.MC_FIRST)
                else:
                    near_end = P.MC_NEAR_END if turn == turns else ""
                    prompt = P.fill(P.MC_NEXT, ANSWER=answer, TURN=turn,
                                    TOTAL=turns, CLOSING=near_end)

                idx = add_item(job_id, role="mc", name=MC, turn=turn,
                               lead="", text="", status="running", seconds=None)
                t0 = time.perf_counter()
                reply = await ask(MC, prompt)
                lead, asked = split_mc(reply)
                update_item(job_id, idx, lead=lead, text=asked, status="done",
                            seconds=round(time.perf_counter() - t0, 1))
                with LOCK:
                    JOBS[job_id].update(question=asked, phase="guest")
                save_job(job_id)

            elif phase == "guest":
                sent = clip_question(question)
                prompt = (P.fill(P.GUEST_SETUP, TOPIC=topic, QUESTION=sent)
                          if turn == 1 else
                          P.fill(P.GUEST_NEXT, QUESTION=sent))

                idx = add_item(job_id, role="guest", name=GUEST, turn=turn,
                               lead="", text="", status="running", seconds=None)
                t0 = time.perf_counter()
                said = await ask(GUEST, prompt)
                update_item(job_id, idx, text=said, status="done",
                            seconds=round(time.perf_counter() - t0, 1))
                with LOCK:
                    JOBS[job_id].update(
                        answer=said, turn=turn + 1,
                        phase="mc" if turn < turns else "closing")
                save_job(job_id)

            elif phase == "closing":
                idx = add_item(job_id, role="mc", name=MC, turn=0,
                               lead="", text="", status="running", seconds=None)
                t0 = time.perf_counter()
                said = await ask(MC, P.fill(P.MC_CLOSE, ANSWER=answer))
                update_item(job_id, idx, text=said, status="done",
                            seconds=round(time.perf_counter() - t0, 1))
                with LOCK:
                    JOBS[job_id]["phase"] = "finished"
                save_job(job_id)

            else:
                break

        end_show(job_id)
    except Exception as e:
        pause_show(job_id, f"{type(e).__name__}: {e}")


def start_job(topic: str, turns: int) -> str:
    job_id = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[job_id] = {
            "topic": topic, "turns_planned": turns,
            "turn": 1, "phase": "mc",   # mc -> guest -> mc ... -> closing
            "question": "", "answer": None,
            "running": True, "finished": False, "error": None,
            "stop_requested": False, "items": [],
            "created_at": datetime.now().isoformat(sep=" ", timespec="minutes"),
        }
    save_job(job_id)
    asyncio.run_coroutine_threadsafe(run_show(job_id), LOOP)
    return job_id


def retry_job(job_id: str):
    """Bo luot bi hong roi chay tiep tu dung buoc do."""
    with LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise KeyError("khong co buoi toa dam nao voi ma nay")
        if job["running"]:
            raise RuntimeError("dang chay, chua can thu lai")
        if job["finished"]:
            raise RuntimeError("buoi toa dam da ket thuc roi")

        items = job["items"]
        if items and items[-1]["status"] == "error":
            items.pop()          # bo luot hong di, lat nua lam lai dung luot do
        job["error"] = None
        job["stop_requested"] = False
        job["running"] = True

    asyncio.run_coroutine_threadsafe(run_show(job_id), LOOP)


# ---------------------------------------------------------------- HTTP server

PAGE = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Toạ đàm</title>
<style>
  :root {
    --bg: #f6f7f9; --card: #fff; --line: #e3e6ea; --text: #1b1f24;
    --muted: #6b7480; --mc: #2b6cb0; --guest: #1a7f4b; --err: #c0392b;
    --mc-bg: #eaf2fb; --guest-bg: #e8f5ee;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #14171a; --card: #1c2024; --line: #2c3238; --text: #e6e9ec;
      --muted: #98a2ad; --mc: #6aa9e9; --guest: #4ec98a; --err: #ff8a7a;
      --mc-bg: #1b2733; --guest-bg: #17281f;
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 15px/1.65 system-ui, "Segoe UI", sans-serif; }
  header { padding: 18px 22px 14px; border-bottom: 1px solid var(--line);
           position: sticky; top: 0; background: var(--bg); z-index: 5; }
  h1 { margin: 0 0 12px; font-size: 17px; font-weight: 650; }
  h1 small { font-weight: 400; color: var(--muted); font-size: 13px; }
  .row { display: flex; gap: 10px; align-items: flex-start; flex-wrap: wrap; }
  textarea {
    flex: 1; min-width: 280px; min-height: 58px; padding: 10px 12px;
    resize: vertical; border: 1px solid var(--line); border-radius: 9px;
    background: var(--card); color: var(--text); font: inherit;
  }
  textarea:focus { outline: 2px solid var(--mc); outline-offset: -1px; }
  label.turns { display: flex; flex-direction: column; gap: 4px;
                font-size: 12px; color: var(--muted); }
  input[type=number] { width: 78px; padding: 9px 10px; border: 1px solid var(--line);
    border-radius: 9px; background: var(--card); color: var(--text); font: inherit; }
  .export-port { display: flex; gap: 6px; align-items: center;
                 font-size: 13px; color: var(--muted); }
  #export-port { width: 96px; height: 38px; }
  select { flex: 1; min-width: 260px; padding: 8px 10px; border: 1px solid var(--line);
    border-radius: 9px; background: var(--card); color: var(--text);
    font: inherit; font-size: 13px; }
  button { padding: 10px 20px; height: 58px; border: 0; border-radius: 9px;
           font: inherit; font-weight: 600; cursor: pointer; }
  #go { background: var(--mc); color: #fff; }
  #stop { background: transparent; color: var(--err); border: 1px solid var(--err); }
  button:disabled { opacity: .45; cursor: default; }
  .hint { margin-top: 10px; font-size: 12.5px; color: var(--muted); }
  .hint b { color: var(--err); font-weight: 600; }
  main { padding: 18px 22px 60px; max-width: 900px; margin: 0 auto; }
  .turn { margin-bottom: 16px; }
  .who { font-size: 12px; font-weight: 650; margin-bottom: 5px;
         display: flex; gap: 8px; align-items: center; }
  .who .t { color: var(--muted); font-weight: 400; }
  .mc .who { color: var(--mc); }
  .guest .who { color: var(--guest); }
  .bubble { padding: 12px 15px; border-radius: 11px; white-space: pre-wrap;
            word-wrap: break-word; border: 1px solid var(--line); }
  .mc .bubble { background: var(--mc-bg); }
  .guest .bubble { background: var(--guest-bg); margin-left: 28px; }
  .lead { font-style: italic; color: var(--muted); margin-bottom: 8px;
          padding-bottom: 8px; border-bottom: 1px dashed var(--line); }
  .waiting { color: var(--muted); font-style: italic; }
  .err .bubble { border-color: var(--err); color: var(--err); }
  button.small { height: 38px; padding: 0 16px; font-size: 13px;
                 background: transparent; color: var(--guest);
                 border: 1px solid var(--guest); }
  button.retry { display: block; margin-top: 12px; height: auto;
                 padding: 8px 16px; font-size: 13px; font-weight: 600;
                 background: var(--err); color: #fff; }
  .empty { color: var(--muted); text-align: center; padding: 50px 0; }
</style>
</head>
<body>
<header>
  <h1>Toạ đàm <small>— ChatGPT dẫn chương trình, NotebookLM là khách mời</small></h1>
  <div class="row">
    <textarea id="topic" placeholder="Chủ đề buổi toạ đàm... (ví dụ: cách luận giải cung Mệnh trong Tử Vi theo các tài liệu trong notebook)"></textarea>
    <label class="turns">số lượt hỏi
      <input type="number" id="turns" value="4" min="1" max="12">
    </label>
    <button id="go">Bắt đầu</button>
    <button id="stop" disabled>Dừng</button>
  </div>
  <div class="row" style="margin-top:10px">
    <select id="history"></select>
    <label class="export-port">Port service (localhost)
      <input type="number" id="export-port" value="__EXPORT_PORT__"
             min="1" max="65535" step="1" inputmode="numeric" required>
    </label>
    <button id="export" class="small" disabled>Xuất hội thoại</button>
  </div>
  <div class="hint" id="hint"></div>
</header>
<main><div id="feed" class="empty">Nhập chủ đề rồi bấm Bắt đầu.</div></main>

<script>
let timer = null, jobId = null, exporting = false;
const exportPortInput = document.getElementById("export-port");
const EXPORT_PORT_KEY = "talkshow.exportPort";
try {
  const savedPort = localStorage.getItem(EXPORT_PORT_KEY);
  if (savedPort !== null) {
    exportPortInput.value = savedPort;
    if (!exportPortInput.checkValidity()) exportPortInput.value = exportPortInput.defaultValue;
  }
} catch (e) { /* Trinh duyet co the chan localStorage. */ }

function rememberExportPort() {
  if (!exportPortInput.checkValidity()) return;
  try {
    localStorage.setItem(EXPORT_PORT_KEY, String(exportPortInput.valueAsNumber));
  } catch (e) { /* Van cho phep xuat khi khong luu duoc tuy chon. */ }
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

async function loadTabs() {
  const tabs = await (await fetch("/api/tabs")).json();
  const bad = tabs.filter(t => !t.ready);
  const hint = document.getElementById("hint");
  if (bad.length === 0) {
    hint.innerHTML = "ChatGPT va NotebookLM da san sang.";
    document.getElementById("go").disabled = false;
  } else {
    hint.innerHTML = "<b>Chua san sang:</b> " +
      bad.map(t => t.name + " — " + t.note).join(" | ");
    document.getElementById("go").disabled = true;
  }
}

function draw(job) {
  const feed = document.getElementById("feed");
  feed.className = "";
  feed.innerHTML = "";
  document.getElementById("export").disabled = exporting ||
    !job.items.some(it => it.status === "done" && (it.text || "").trim());
  if (job.items.length === 0) {
    feed.className = "empty";
    feed.textContent = "Dang cho MC mo dau...";
    return;
  }
  for (const it of job.items) {
    const wrap = el("div", "turn " + it.role + (it.status === "error" ? " err" : ""));
    const who = el("div", "who");
    const label = it.role === "mc"
      ? (it.turn === 0 ? "MC — lời kết" : "MC · lượt " + it.turn)
      : "Khách mời · lượt " + it.turn;
    who.appendChild(el("span", null, label));
    who.appendChild(el("span", "t",
      it.status === "running" ? "đang trả lời..." :
      (it.seconds != null ? it.seconds + "s" : "")));
    wrap.appendChild(who);

    const bubble = el("div", "bubble");
    if (it.lead) bubble.appendChild(el("div", "lead", it.lead));
    if (it.status === "running" && !it.text) {
      bubble.appendChild(el("div", "waiting", "..."));
    } else {
      bubble.appendChild(el("div", null, it.text));
    }
    wrap.appendChild(bubble);
    feed.appendChild(wrap);
  }

  // Buoi con do dang -> nut chay tiep tu dung cho hong, khong lam lai ca buoi
  if (!job.running && !job.finished) {
    const last = job.items[job.items.length - 1];
    const failed = last && last.status === "error";
    const btn = el("button", "retry",
      failed ? "Thử lại lượt này" : "Chạy tiếp buổi này");
    btn.addEventListener("click", retry);
    feed.appendChild(btn);
  }
}

async function poll() {
  let job;
  try {
    const response = await fetch("/api/status?id=" + jobId);
    if (!response.ok) return;
    job = await response.json();
  } catch (e) { return; } // Server may be restarting after a source edit.
  if (job.error && !job.items) { return; }
  draw(job);
  if (!job.running) {
    clearInterval(timer);
    timer = null;
    document.getElementById("go").disabled = false;
    document.getElementById("stop").disabled = true;
    const hint = document.getElementById("hint");
    if (job.error) {
      hint.innerHTML = "<b>Loi:</b> " + job.error +
        " — bam nut o cuoi trang de chay tiep tu dung cho hong.";
    } else if (job.finished) {
      hint.textContent = "Xong. Bien ban da luu trong logs/ va trong talkshow.db.";
    }
  }
}

async function retry() {
  const hint = document.getElementById("hint");
  hint.textContent = "Dang chay tiep...";
  const data = await (await fetch("/api/retry?id=" + jobId, {method: "POST"})).json();
  if (data.error) { hint.textContent = "Khong chay tiep duoc: " + data.error; return; }
  document.getElementById("go").disabled = true;
  document.getElementById("stop").disabled = false;
  await poll();
  if (!timer) timer = setInterval(poll, 900);
}

async function exportShow() {
  if (!jobId || exporting) return;
  if (!exportPortInput.reportValidity()) return;
  const port = exportPortInput.valueAsNumber;
  rememberExportPort();
  const hint = document.getElementById("hint");
  const btn = document.getElementById("export");
  hint.textContent = "Dang xuat...";
  exporting = true;
  btn.disabled = true;
  try {
    const data = await (await fetch("/api/export?id=" + jobId + "&port=" + port,
                                    {method: "POST"})).json();
    if (data.error) {
      hint.textContent = "Xuất thất bại: " + data.error;
    } else {
      hint.textContent = "Đã nhập " + data.turns + " lượt vào AI Reader (hội thoại #" +
        data.conversation_id + "). ";
      const link = el("a", null, "Mở hội thoại trong AI Reader");
      link.href = data.open_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      hint.appendChild(link);
    }
  } catch (e) {
    hint.textContent = "Xuat that bai: " + e;
  } finally {
    exporting = false;
    btn.disabled = false;
  }
}

async function loadHistory() {
  const shows = await (await fetch("/api/shows")).json();
  const sel = document.getElementById("history");
  sel.innerHTML = "";
  sel.appendChild(el("option", null, shows.length
    ? "— mở lại buổi trước (" + shows.length + ") —" : "— chưa có buổi nào —"));
  for (const s of shows) {
    const mark = s.finished ? "✓" : "⏸";
    const o = el("option", null,
      mark + "  " + s.created_at + "  ·  " + s.topic.slice(0, 60));
    o.value = s.id;
    sel.appendChild(o);
  }
}

async function openShow(id) {
  if (!id) return;
  if (timer) { clearInterval(timer); timer = null; }
  const response = await fetch("/api/status?id=" + id);
  if (!response.ok) return;
  const job = await response.json();
  jobId = id;
  document.getElementById("history").value = id;
  document.getElementById("topic").value = job.topic;
  document.getElementById("turns").value = job.turns_planned;
  document.getElementById("go").disabled = job.running;
  document.getElementById("stop").disabled = !job.running;
  draw(job);
  const hint = document.getElementById("hint");
  if (job.running) {
    hint.textContent = "Dang chay...";
    timer = setInterval(poll, 900);
  } else if (job.finished) hint.textContent = "Buoi nay da xong.";
  else hint.textContent = "Buoi nay con do dang — bam nut o cuoi trang de chay tiep.";
}

async function start() {
  const topic = document.getElementById("topic").value.trim();
  if (!topic) return;
  const turns = parseInt(document.getElementById("turns").value, 10) || 4;
  document.getElementById("go").disabled = true;
  document.getElementById("stop").disabled = false;
  document.getElementById("hint").textContent = "Dang chay...";

  const res = await fetch("/api/start", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({topic: topic, turns: turns})
  });
  const data = await res.json();
  if (data.error) {
    document.getElementById("hint").textContent = data.error;
    document.getElementById("go").disabled = false;
    return;
  }
  jobId = data.id;
  await poll();
  timer = setInterval(poll, 900);
  loadHistory();
}

async function stop() {
  if (!jobId) return;
  document.getElementById("stop").disabled = true;
  document.getElementById("hint").textContent = "Se dung sau khi xong luot hien tai...";
  await fetch("/api/stop?id=" + jobId, {method: "POST"});
}

document.getElementById("go").addEventListener("click", start);
document.getElementById("stop").addEventListener("click", stop);
document.getElementById("export").addEventListener("click", exportShow);
exportPortInput.addEventListener("change", rememberExportPort);
document.getElementById("history").addEventListener("change", e => openShow(e.target.value));

window.addEventListener("beforedevreload", () => {
  try {
    sessionStorage.setItem("talkshow.reloadView", JSON.stringify({
      jobId, topic: document.getElementById("topic").value,
      turns: document.getElementById("turns").value,
      exportPort: exportPortInput.value, scrollY: window.scrollY
    }));
  } catch (e) { /* Reload still works when browser storage is blocked. */ }
});

async function initialize() {
  let view = null;
  try {
    view = JSON.parse(sessionStorage.getItem("talkshow.reloadView"));
    sessionStorage.removeItem("talkshow.reloadView");
  } catch (e) { /* No saved view available. */ }
  await Promise.allSettled([loadTabs(), loadHistory()]);
  if (view) {
    if (view.jobId) await openShow(view.jobId);
    document.getElementById("topic").value = view.topic;
    document.getElementById("turns").value = view.turns;
    exportPortInput.value = view.exportPort;
    requestAnimationFrame(() => window.scrollTo(0, view.scrollY));
  }
}
initialize();
</script>
</body>
</html>
""".replace("__EXPORT_PORT__", str(DEFAULT_EXPORT_PORT))
PAGE = dev_reload.inject_browser_reload(PAGE)


class Server(ThreadingHTTPServer):
    # ThreadingHTTPServer mac dinh bat allow_reuse_address. Tren Windows dieu
    # do cho phep MOT server thu hai bind trung cong ma khong bao loi gi ca —
    # server cu van nuot het request, con server moi chay khong. Tat di de
    # chay trung cong la bao loi ngay.
    allow_reuse_address = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == dev_reload.VERSION_PATH:
            return self._send(200, json.dumps({"version": dev_reload.VERSION}))
        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/tabs":
            return self._send(200, json.dumps(tabs_status(), ensure_ascii=False))
        if path == "/api/shows":
            return self._send(200, json.dumps(list_shows(), ensure_ascii=False))
        if path == "/api/status":
            job_id = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            with LOCK:
                job = JOBS.get(job_id)
                body = json.dumps(job, ensure_ascii=False) if job else '{"error":"khong co job"}'
            return self._send(200 if job else 404, body)
        self._send(404, '{"error":"khong co trang nay"}')

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/stop":
            job_id = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            with LOCK:
                if job_id in JOBS:
                    JOBS[job_id]["stop_requested"] = True
            return self._send(200, '{"ok":true}')

        if path == "/api/retry":
            job_id = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            try:
                retry_job(job_id)
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)},
                                                  ensure_ascii=False))
            return self._send(200, '{"ok":true}')

        if path == "/api/export":
            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            job_id = (query.get("id") or [""])[0]
            port = query.get("port", [DEFAULT_EXPORT_PORT])[0]
            try:
                result = export_show(job_id, port)
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps(result, ensure_ascii=False))

        if path != "/api/start":
            return self._send(404, '{"error":"khong co trang nay"}')

        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            topic = (data.get("topic") or "").strip()
            turns = max(1, min(12, int(data.get("turns") or 4)))
            if not topic:
                raise ValueError("chua co chu de")
            job_id = start_job(topic, turns)
        except Exception as e:
            return self._send(400, json.dumps(
                {"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        self._send(200, json.dumps({"id": job_id}))


def main():
    try:
        server = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"\n[!] Cong {PORT} dang bi mot tien trinh khac giu.")
        print(f"    Co the ban dang chay san 1 talkshow.py o cua so khac —")
        print(f"    hay tat no di (Ctrl+C) roi chay lai, neu khong ban se van")
        print(f"    thay giao dien cu.")
        raise SystemExit(1)

    try:
        db_init()
        n = load_jobs()
        print(f"[*] Da doc {n} buoi toa dam cu tu {DB_PATH.name}")
        print(f"[*] Ket noi toi Chrome tai {adapters.CDP_URL} ...")
        start_background_loop()
        for t in tabs_status():
            print(f"    {t['name']:<11} {'san sang' if t['ready'] else 'CHUA SAN SANG — ' + t['note']}")

        url = f"http://localhost:{PORT}"
        print(f"\n[*] Mo {url}   (Ctrl+C de dung)")
        if dev_reload.should_open_browser():
            webbrowser.open(url)
        dev_reload.listen_for_shutdown(server.shutdown)
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Da dung.")
    finally:
        server.server_close()
        stop_background_loop()


if __name__ == "__main__":
    main()
