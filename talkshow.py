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
CREATE TABLE IF NOT EXISTS prompts (
    name TEXT PRIMARY KEY,
    text TEXT
);
"""

# Cac mau prompt cho phep sua tren giao dien. Ten trung voi bien trong
# talkshow_prompts.py — khong co ban sua thi lay thang tu do ra.
EDITABLE_TEMPLATES = [
    ("MC_SETUP", "Vai trò MC — gửi ở lượt đầu và mỗi khi mở lại buổi cũ"),
    ("MC_FIRST", "Lời mở đầu buổi toạ đàm"),
    ("MC_NEXT", "Mỗi lượt hỏi tiếp"),
    ("MC_NEAR_END", "Ghép thêm vào lượt áp chót"),
    ("MC_CLOSE", "Yêu cầu MC viết lời kết"),
    ("MC_CONTINUE", "Prompt đào sâu khi chạy tiếp buổi đã xong"),
    ("MC_CONTINUE_FOCUS", "Khối chứa chỉ dẫn riêng bạn gõ khi đào sâu"),
    ("MC_FOCUS_REMINDER", "Nhắc lại chỉ dẫn đó ở các lượt sau"),
    ("GUEST_SETUP", "Vai trò khách mời — lượt đầu"),
    ("GUEST_NEXT", "Khách mời, các lượt sau"),
    ("GUEST_RESUME", "Khách mời khi mở lại buổi cũ"),
]

# Cac con so chinh duoc tren giao dien
EDITABLE_NUMBERS = [
    ("MAX_QUESTION", "Độ dài tối đa một câu hỏi gửi sang khách mời (ký tự)"),
    ("RECAP_MAX_CHARS", "Trần cho cả khối tóm tắt buổi trước"),
    ("RECAP_MAX_ANSWER", "Cắt mỗi câu trả lời cũ còn bao nhiêu, ở đường lui"),
]

PROMPT_OVERRIDES = {}   # ten -> noi dung da sua tren giao dien

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
            # Doc lai tu dia -> tab AI gan nhu chac chan da la phien chat khac,
            # nen lan chay tiep dau tien phai nhac lai vai tro va bien ban.
            "needs_recap": True, "guest_recap": True, "focus": "",
            "review_mode": False, "review": None, "review_seq": 0,
            "paused": False, "pause_requested": False,
            "created_at": s["created_at"],
            "items": by_show.get(s["id"], []),
        }
    return len(shows)


# ------------------------------------------------------- mau prompt sua duoc

def tpl(name: str) -> str:
    """
    Lay mau prompt dang dung: uu tien ban da sua tren giao dien, khong co
    thi lay mac dinh trong talkshow_prompts.py.

    Khong ghi de thang vao module P, de nut "Khoi phuc mau goc" luc nao
    cung con ban goc de quay ve.
    """
    with LOCK:
        override = PROMPT_OVERRIDES.get(name)
    return override if override is not None else getattr(P, name)


def num(name: str) -> int:
    """Nhu tpl() nhung cho cac con so."""
    with LOCK:
        override = PROMPT_OVERRIDES.get(name)
    if override is not None:
        try:
            return int(str(override).strip())
        except ValueError:
            pass          # ai do go bay vao o so — dung mac dinh cho an toan
    return getattr(P, name)


def load_prompts():
    with closing(sqlite3.connect(DB_PATH)) as con:
        rows = con.execute("SELECT name, text FROM prompts").fetchall()
    known = {n for n, _ in EDITABLE_TEMPLATES} | {n for n, _ in EDITABLE_NUMBERS}
    with LOCK:
        PROMPT_OVERRIDES.clear()
        PROMPT_OVERRIDES.update({n: t for n, t in rows if n in known})
    return len(PROMPT_OVERRIDES)


def save_prompt(name: str, text: str):
    """Luu ban sua. Text trung y het mac dinh thi xoa han cho gon."""
    known = {n for n, _ in EDITABLE_TEMPLATES} | {n for n, _ in EDITABLE_NUMBERS}
    if name not in known:
        raise KeyError(f"khong co mau prompt ten {name!r}")

    text = str(text)
    same = text.strip() == str(getattr(P, name)).strip()
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        if same:
            con.execute("DELETE FROM prompts WHERE name = ?", (name,))
        else:
            con.execute("INSERT INTO prompts VALUES (?,?) ON CONFLICT(name) "
                        "DO UPDATE SET text = excluded.text", (name, text))
    with LOCK:
        PROMPT_OVERRIDES.pop(name, None)
        if not same:
            PROMPT_OVERRIDES[name] = text


def prompt_list() -> list:
    """Toan bo mau prompt kem ban goc, de giao dien so sanh va khoi phuc."""
    out = []
    for kind, items in (("text", EDITABLE_TEMPLATES), ("number", EDITABLE_NUMBERS)):
        for name, note in items:
            default = str(getattr(P, name))
            with LOCK:
                current = PROMPT_OVERRIDES.get(name)
            out.append({"name": name, "note": note, "kind": kind,
                        "default": default,
                        "text": current if current is not None else default,
                        "changed": current is not None})
    return out


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
    if len(question) <= num("MAX_QUESTION"):
        return question
    cut = question[:num("MAX_QUESTION")]
    # cat o dau cham cuoi cung cho khoi cut giua cau
    dot = max(cut.rfind("."), cut.rfind("?"), cut.rfind("!"))
    if dot > num("MAX_QUESTION") // 2:
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
        JOBS[job_id]["review"] = None
        JOBS[job_id]["paused"] = False
        JOBS[job_id]["pause_requested"] = False
        job = json.loads(json.dumps(JOBS[job_id], ensure_ascii=False))
    save_job(job_id)
    save_log(job)


def hold_show(job_id: str):
    """
    Tam dung theo yeu cau: giu nguyen phase va so luot, KHONG danh dau la
    loi va cung khong ket thuc buoi. Nut "Chay tiep buoi nay" se hien ra.
    """
    with LOCK:
        job = JOBS[job_id]
        job["running"] = False
        job["paused"] = True
        job["pause_requested"] = False
        job["review"] = None
    save_job(job_id)


def pause_show(job_id: str, error: str):
    """
    Dung vi loi NHUNG chua ket thuc: giu nguyen phase va so luot, de bam
    "Thu lai" la chay tiep tu dung cho vua hong chu khong lam lai tu dau.
    """
    with LOCK:
        job = JOBS[job_id]
        job["running"] = False
        job["error"] = error
        job["review"] = None
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

def exportable(items: list) -> list:
    """
    Danh so tung cau hoi / cau tra loi, tinh tu 1.

    Chi nhung luot da xong va co chu moi duoc danh so — luot hong hay dang
    chay thi bo qua, vi chung cung khong xuat di dau. Giao dien va phan xuat
    deu goi ham nay nen con so ban nhin thay tren trang dung bang con so ban
    go vao o "xuat tu ... den ...".

    Tra ve list (so_thu_tu, item).
    """
    out = []
    for it in items:
        if it["status"] == "done" and (it.get("text") or "").strip():
            out.append((len(out) + 1, it))
    return out


def build_turns(job: dict, first: int = None, last: int = None) -> list:
    """
    Doi buoi toa dam sang cac cap Ban/AI cua giao dien Chat AI trong AI Reader.

    Loi dan cua MC va cau hoi duoc gop lai thanh 1 luot, vi ngoai doi MC noi
    lien mach chu khong tach ra. Cac luot hong hoac dang chay thi bo qua.

    first/last la khoang so thu tu muon xuat (tinh tu 1, lay ca hai dau).
    De trong thi xuat het.
    """
    turns = []
    for num, it in exportable(job["items"]):
        if first is not None and num < first:
            continue
        if last is not None and num > last:
            continue
        if it["role"] == "mc":
            lead = (it.get("lead") or "").strip()
            text = it["text"].strip()
            turns.append({"speaker": "Bạn",
                          "text": f"{lead}\n\n{text}" if lead else text})
        else:
            turns.append({"speaker": "AI", "text": it["text"].strip()})

    # Chu de di cung luot mo dau, khong tao them mot nguoi noi/cap rong.
    # Van ghi ca khi xuat tu giua chung, de ben kia con biet dang ban gi.
    topic = (job.get("topic") or "").strip()
    if turns and topic:
        turns[0]["text"] = f"Chủ đề: {topic}\n\n{turns[0]['text']}"
    return turns


def export_show(job_id: str, port=DEFAULT_EXPORT_PORT,
                first: int = None, last: int = None) -> dict:
    """Gui buoi toa dam sang service khac. Goi tu phia server chu khong tu
    trinh duyet, de khoi vuong CORS.

    first/last gioi han khoang so thu tu muon xuat; de trong thi xuat het."""
    if not re.fullmatch(r"[0-9]{1,5}", str(port)) or not 1 <= int(port) <= 65535:
        raise ValueError("Port service phải là số nguyên từ 1 đến 65535.")
    if first is not None and last is not None and first > last:
        raise ValueError("Số bắt đầu phải nhỏ hơn hoặc bằng số kết thúc.")

    target = f"http://localhost:{int(port)}/api/import"
    with LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise KeyError("khong co buoi toa dam nao voi ma nay")
        tong = len(exportable(job["items"]))
        payload = {"source": EXPORT_SOURCE,
                   "turns": build_turns(job, first, last)}

    if not payload["turns"]:
        if tong == 0:
            raise RuntimeError("buoi nay chua co noi dung gi de xuat")
        raise RuntimeError(
            f"khoang {first}-{last} khong co luot nao. "
            f"Buoi nay danh so tu 1 den {tong}.")

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


class ShowStopped(Exception):
    """Nguoi dung bam Dung han — ket thuc buoi."""


class ShowPaused(Exception):
    """
    Nguoi dung bam Tam dung — giu nguyen tien do de con chay tiep.

    Khac ShowStopped o cho khong danh dau finished, nen nut "Chay tiep buoi
    nay" hien ra va buoi tiep tuc dung luot dang do, khong phai dao sau lai
    tu dau.
    """


async def gate(job_id: str, who: str, label: str, prompt: str) -> str:
    """
    Che do duyet: dung lai, dua prompt len giao dien cho nguoi dung xem va
    sua, roi moi gui di. Tra ve prompt cuoi cung (co the da bi sua tay).

    Khong bat che do thi di thang, khong ton them gi.

    Vong cho nam o day va poll moi 0.3s vi nguoi bam nut o luong HTTP khac,
    khong dung chung event loop voi ham nay.
    """
    with LOCK:
        job = JOBS[job_id]
        if not job.get("review_mode"):
            return prompt
        job["review"] = {"seq": job.get("review_seq", 0) + 1, "who": who,
                         "label": label, "prompt": prompt,
                         "edited": prompt, "action": None}
        job["review_seq"] = job["review"]["seq"]

    while True:
        await asyncio.sleep(0.3)
        with LOCK:
            job = JOBS[job_id]
            review = job.get("review")
            if job["stop_requested"]:
                job["review"] = None
                raise ShowStopped
            if job.get("pause_requested"):
                job["review"] = None
                raise ShowPaused
            if review and review["action"] == "send":
                job["review"] = None
                return review["edited"]


def answer_review(job_id: str, action: str, text: str = None):
    """Nguoi dung bam Gui hoac Dung o bang duyet prompt."""
    with LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise KeyError("khong co buoi toa dam nao voi ma nay")
        if action == "stop":
            job["stop_requested"] = True
            return
        if action == "pause":
            job["pause_requested"] = True
            return
        review = job.get("review")
        if not review:
            raise RuntimeError("khong co prompt nao dang cho duyet")
        if text is not None:
            review["edited"] = text
        review["action"] = "send"


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
                if job.get("pause_requested"):
                    raise ShowPaused
                phase = job["phase"]
                turn = job["turn"]
                turns = job["turns_planned"]
                topic = job["topic"]
                question = job["question"]
                answer = job["answer"]
                # Buoi mo lai tu database, hoac vua bam "Dao sau them" -> phai
                # nhac lai vai tro va bien ban vi tab AI khong con nho gi.
                needs_recap = job.get("needs_recap", False)
                guest_recap = job.get("guest_recap", False)
                focus = job.get("focus", "")
                past = (list(job["items"])
                        if (needs_recap or guest_recap) else [])

            if phase == "mc":
                if needs_recap:
                    focus_block = (P.fill(tpl("MC_CONTINUE_FOCUS"), NOTE=focus)
                                   if focus else "")
                    prompt = (P.fill(tpl("MC_SETUP"), TOPIC=topic, MAX=num("MAX_QUESTION"))
                              + "\n\n---\n\n"
                              + P.fill(tpl("MC_CONTINUE"), RECAP=build_recap(past),
                                       DONE=done_turns(past), FOCUS=focus_block,
                                       EXTRA=max(1, turns - turn + 1)))
                elif turn == 1:
                    prompt = (P.fill(tpl("MC_SETUP"), TOPIC=topic, MAX=num("MAX_QUESTION"))
                              + "\n\n---\n\n" + tpl("MC_FIRST"))
                else:
                    near_end = tpl("MC_NEAR_END") if turn == turns else ""
                    prompt = P.fill(tpl("MC_NEXT"), ANSWER=answer, TURN=turn,
                                    TOTAL=turns, CLOSING=near_end)
                    if focus:
                        prompt += P.fill(tpl("MC_FOCUS_REMINDER"), NOTE=focus)

                # Duyet TRUOC khi them luot vao bien ban, de trong luc cho
                # nguoi duyet khong co luot nao lo lung o trang thai "dang
                # tra loi".
                prompt = await gate(job_id, MC, f"MC · lượt {turn}", prompt)

                idx = add_item(job_id, role="mc", name=MC, turn=turn,
                               lead="", text="", status="running", seconds=None)
                t0 = time.perf_counter()
                reply = await ask(MC, prompt)
                lead, asked = split_mc(reply)
                update_item(job_id, idx, lead=lead, text=asked, status="done",
                            seconds=round(time.perf_counter() - t0, 1))
                with LOCK:
                    # Xoa co SAU khi da hoi xong, de neu luot nay hong thi lan
                    # thu lai van con duoc nhac lai bien ban.
                    JOBS[job_id].update(question=asked, phase="guest",
                                        needs_recap=False)
                save_job(job_id)

            elif phase == "guest":
                sent = clip_question(question)
                if turn == 1:
                    prompt = P.fill(tpl("GUEST_SETUP"), TOPIC=topic, QUESTION=sent)
                elif guest_recap:
                    # Tinh xem con thua bao nhieu cho de nhet phan nhac lai,
                    # roi moi dung — o nhap cua khach moi co gioi han cung.
                    base = P.fill(tpl("GUEST_RESUME"), TOPIC=topic,
                                  QUESTION=sent, RECAP="")
                    cap = adapters.BY_KEY[GUEST.lower()].max_prompt or 10 ** 9
                    prompt = P.fill(
                        tpl("GUEST_RESUME"), TOPIC=topic, QUESTION=sent,
                        RECAP=build_guest_recap(past, cap - len(base) - 80))
                else:
                    prompt = P.fill(tpl("GUEST_NEXT"), QUESTION=sent)

                prompt = await gate(job_id, GUEST, f"Khách mời · lượt {turn}",
                                    prompt)

                idx = add_item(job_id, role="guest", name=GUEST, turn=turn,
                               lead="", text="", status="running", seconds=None)
                t0 = time.perf_counter()
                said = await ask(GUEST, prompt)
                update_item(job_id, idx, text=said, status="done",
                            seconds=round(time.perf_counter() - t0, 1))
                with LOCK:
                    JOBS[job_id].update(
                        answer=said, turn=turn + 1, guest_recap=False,
                        phase="mc" if turn < turns else "closing")
                save_job(job_id)

            elif phase == "closing":
                prompt = await gate(job_id, MC, "MC · lời kết",
                                    P.fill(tpl("MC_CLOSE"), ANSWER=answer))

                idx = add_item(job_id, role="mc", name=MC, turn=0,
                               lead="", text="", status="running", seconds=None)
                t0 = time.perf_counter()
                said = await ask(MC, prompt)
                update_item(job_id, idx, text=said, status="done",
                            seconds=round(time.perf_counter() - t0, 1))
                with LOCK:
                    JOBS[job_id]["phase"] = "finished"
                save_job(job_id)

            else:
                break

        end_show(job_id)
    except ShowStopped:
        end_show(job_id)          # bam Dung han — ket thuc, khong phai loi
    except ShowPaused:
        hold_show(job_id)         # bam Tam dung — con chay tiep duoc
    except Exception as e:
        pause_show(job_id, f"{type(e).__name__}: {e}")


def start_job(topic: str, turns: int, review_mode: bool = False) -> str:
    job_id = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[job_id] = {
            "topic": topic, "turns_planned": turns,
            "turn": 1, "phase": "mc",   # mc -> guest -> mc ... -> closing
            "question": "", "answer": None,
            "running": True, "finished": False, "error": None,
            "stop_requested": False, "items": [],
            # Buoi moi: luot 1 da gui MC_SETUP roi nen khong can nhac lai
            "needs_recap": False, "guest_recap": False, "focus": "",
            "review_mode": review_mode, "review": None, "review_seq": 0,
            "paused": False, "pause_requested": False,
            "created_at": datetime.now().isoformat(sep=" ", timespec="minutes"),
        }
    save_job(job_id)
    asyncio.run_coroutine_threadsafe(run_show(job_id), LOOP)
    return job_id


def trim(text: str, limit: int) -> str:
    """Cat bot cho vua han muc, uu tien cat o cuoi cau cho khoi dut giua chung."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind(". "), cut.rfind("\n"), cut.rfind("? "))
    if stop > limit // 2:
        cut = cut[:stop + 1]
    return cut.rstrip() + "\n[...phần sau đã lược bớt cho gọn]"


def build_recap(items: list) -> str:
    """
    Tom tat buoi truoc de nhac cho MC khi dao sau them.

    Can thiet vi mo lai buoi cu tu database thi tab ChatGPT thuong da la
    phien chat khac, khong con nho gi.

    UU TIEN LOI KET: chinh MC da tom tat ca buoi trong do, va MC_CLOSE con
    bat no neu ro "dieu gi con bo ngo" — dung thu ta can. Loi ket chi
    ~2.000-3.000 ky tu, trong khi chep lai ca bien ban co the len toi
    75.000 ky tu va lam o nhap ChatGPT timeout.

    Buoi bi cat ngang thi khong co loi ket, luc do moi lui ve lay vai luot
    cuoi — cung cat cho vua tran.
    """
    done = [it for it in items
            if it["status"] == "done" and (it.get("text") or "").strip()]

    closing = next((it["text"].strip() for it in reversed(done)
                    if it["role"] == "mc" and it["turn"] == 0), None)
    if closing:
        return ("Lời kết của buổi trước — MC đã tự tóm tắt toàn bộ và nêu rõ "
                "phần nào còn bỏ ngỏ:\n\n" + trim(closing, num("RECAP_MAX_CHARS")))

    # Duong lui: buoi chua kip chot, lay tu luot cuoi nguoc len cho day tran
    blocks = []
    for it in done:
        if it["role"] == "mc":
            blocks.append(f"[Lượt {it['turn']}] MC hỏi: {it['text'].strip()}")
        else:
            blocks.append(f"[Lượt {it['turn']}] Khách mời đáp: "
                          f"{trim(it['text'].strip(), num("RECAP_MAX_ANSWER"))}\n")
    if not blocks:
        return "(buổi trước chưa có nội dung nào)"

    kept, used = [], 0
    for block in reversed(blocks):
        if used + len(block) > num("RECAP_MAX_CHARS") and kept:
            break
        kept.insert(0, block)
        used += len(block)

    head = ("Buổi trước bị dừng giữa chừng nên chưa có lời kết. "
            f"Đây là {len(kept)} phần cuối của cuộc trao đổi:\n\n")
    return head + "\n".join(kept)


def build_guest_recap(items: list, budget: int) -> str:
    """
    Nhac cho khach moi biet buoi truoc da hoi nhung gi, de no khoi tra loi
    lai y cu.

    Chi liet ke CAU HOI chu khong chep lai cau tra loi: o nhap cua NotebookLM
    chi chua duoc ~3900 ky tu, phai danh cho cau hoi moi. Neu van khong vua
    thi bo dan cau cu nhat.
    """
    if budget < 150:
        return ""

    asked = [(it.get("text") or "").strip() for it in items
             if it["role"] == "mc" and it["turn"] != 0 and it["status"] == "done"]
    lines = [f"- {q[:160].rstrip()}..." if len(q) > 160 else f"- {q}"
             for q in asked if q]
    if not lines:
        return ""

    head = "\nMC đã hỏi những câu này rồi, đừng trả lời lặp lại các ý đó:\n"
    while lines and len(head) + sum(len(x) + 1 for x in lines) > budget:
        lines.pop(0)
    return head + "\n".join(lines) + "\n" if lines else ""


def done_turns(items: list) -> int:
    """So luot da hoi-dap tron ven (khong tinh loi ket)."""
    return len({it["turn"] for it in items
                if it["role"] == "guest" and it["status"] == "done"})


def extend_job(job_id: str, extra: int, focus: str = "",
               review_mode: bool = False):
    """
    Chay tiep mot buoi DA KET THUC de dao sau them.

    Khac retry_job: retry lam lai dung luot vua hong cua mot buoi con do
    dang; con ham nay noi them luot moi vao buoi da xong, va bat MC doc lai
    toan bo bien ban truoc khi hoi tiep.
    """
    with LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise KeyError("khong co buoi toa dam nao voi ma nay")
        if job["running"]:
            raise RuntimeError("buoi nay dang chay")
        if not any(it["status"] == "done" for it in job["items"]):
            raise RuntimeError("buoi nay chua co noi dung gi de dao sau")

        # turn dang tro toi luot ke tiep, nen tru 1 ra la so luot da chay
        job["turns_planned"] = job["turn"] - 1 + extra
        job.update(phase="mc", finished=False, error=None,
                   stop_requested=False, running=True,
                   needs_recap=True,      # MC phai duoc nhac lai ca buoi truoc
                   guest_recap=True,      # khach moi phai duoc nhac lai vai tro
                   focus=(focus or "").strip(),
                   review_mode=review_mode, review=None,
                   paused=False, pause_requested=False)

    save_job(job_id)
    asyncio.run_coroutine_threadsafe(run_show(job_id), LOOP)


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
        job["review"] = None
        job["paused"] = False
        job["pause_requested"] = False

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
  #export-from, #export-to { width: 74px; height: 38px; }
  select { flex: 1; min-width: 260px; padding: 8px 10px; border: 1px solid var(--line);
    border-radius: 9px; background: var(--card); color: var(--text);
    font: inherit; font-size: 13px; }
  button { padding: 10px 20px; height: 58px; border: 0; border-radius: 9px;
           font: inherit; font-weight: 600; cursor: pointer; }
  #go { background: var(--mc); color: #fff; }
  #pause { background: transparent; color: var(--text);
           border: 1px solid var(--line); }
  #stop { background: transparent; color: var(--err); border: 1px solid var(--err); }
  button:disabled { opacity: .45; cursor: default; }
  .hint { margin-top: 10px; font-size: 12.5px; color: var(--muted); }
  .hint b { color: var(--err); font-weight: 600; }
  main { padding: 18px 22px 60px; max-width: 900px; margin: 0 auto; }
  .turn { margin-bottom: 16px; }
  .who { font-size: 12px; font-weight: 650; margin-bottom: 5px;
         display: flex; gap: 8px; align-items: center; }
  .who .t { color: var(--muted); font-weight: 400; }
  .who .stt { font-family: ui-monospace, Consolas, monospace; font-weight: 700;
              background: var(--line); color: var(--muted);
              padding: 1px 6px; border-radius: 5px; font-size: 11px; }
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
  label.chk { display: flex; gap: 7px; align-items: center; font-size: 13px;
              color: var(--muted); cursor: pointer; height: 38px; }
  /* bang duyet prompt truoc khi gui */
  .review { margin-top: 22px; padding: 15px 16px; border-radius: 11px;
            border: 2px solid var(--mc); background: var(--card); }
  .review-title { font-size: 14px; font-weight: 650; margin-bottom: 4px;
                  color: var(--mc); }
  .review-note { font-size: 12.5px; color: var(--muted); margin-bottom: 10px; }
  .review textarea { width: 100%; min-height: 320px; margin-bottom: 10px;
                     font-family: ui-monospace, Consolas, monospace;
                     font-size: 12.5px; line-height: 1.5; }
  /* khu sua mau prompt */
  .sheet { position: fixed; inset: 0; background: rgba(0,0,0,.45);
           display: none; z-index: 20; overflow-y: auto; padding: 30px 16px; }
  .sheet.on { display: block; }
  .sheet-box { max-width: 880px; margin: 0 auto; background: var(--bg);
               border: 1px solid var(--line); border-radius: 13px; padding: 20px 22px; }
  .sheet h2 { margin: 0 0 4px; font-size: 16px; }
  .sheet .sub { font-size: 12.5px; color: var(--muted); margin-bottom: 16px; }
  .tpl { margin-bottom: 16px; padding-bottom: 14px;
         border-bottom: 1px solid var(--line); }
  .tpl-head { display: flex; justify-content: space-between; align-items: baseline;
              gap: 10px; margin-bottom: 5px; }
  .tpl-name { font-size: 13px; font-weight: 650; font-family: ui-monospace, monospace; }
  .tpl-note { font-size: 12px; color: var(--muted); }
  .tpl.dirty .tpl-name::after { content: " (đã sửa)"; color: var(--guest);
                                font-weight: 400; font-size: 11px; }
  .tpl textarea { width: 100%; min-height: 130px;
                  font-family: ui-monospace, Consolas, monospace;
                  font-size: 12.5px; line-height: 1.5; }
  .tpl input[type=number] { width: 120px; }
  .sheet-actions { display: flex; gap: 10px; align-items: center;
                   position: sticky; bottom: 0; background: var(--bg);
                   padding: 12px 0 2px; border-top: 1px solid var(--line); }
  .extend { margin-top: 22px; padding: 15px 16px; border-radius: 11px;
            border: 1px dashed var(--line); background: var(--card); }
  .extend-title { font-size: 14px; font-weight: 650; margin-bottom: 6px; }
  .extend-note { font-size: 12.5px; color: var(--muted); margin-bottom: 12px; }
  .extend textarea { width: 100%; min-height: 68px; margin-bottom: 10px; }
  .extend .row { align-items: center; }
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
    <button id="pause" disabled>Tạm dừng</button>
    <button id="stop" disabled>Dừng hẳn</button>
  </div>
  <div class="row" style="margin-top:10px">
    <label class="chk"><input type="checkbox" id="review">
      Duyệt prompt trước khi gửi</label>
    <button id="open-prompts" class="small">Mẫu prompt</button>
  </div>
  <div class="row" style="margin-top:10px">
    <select id="history"></select>
    <label class="export-port">Port service (localhost)
      <input type="number" id="export-port" value="__EXPORT_PORT__"
             min="1" max="65535" step="1" inputmode="numeric" required>
    </label>
    <label class="export-port">Xuất từ
      <input type="number" id="export-from" min="1" step="1"
             inputmode="numeric" placeholder="đầu">
    </label>
    <label class="export-port">đến
      <input type="number" id="export-to" min="1" step="1"
             inputmode="numeric" placeholder="cuối">
    </label>
    <button id="export" class="small" disabled>Xuất hội thoại</button>
  </div>
  <div class="hint" id="hint"></div>
</header>
<main><div id="feed" class="empty">Nhập chủ đề rồi bấm Bắt đầu.</div></main>

<div class="sheet" id="sheet">
  <div class="sheet-box">
    <h2>Mẫu prompt</h2>
    <div class="sub">Sửa ở đây sẽ áp dụng cho các lượt sau, lưu vào talkshow.db
      nên còn nguyên sau khi khởi động lại. Giữ nguyên các mốc dạng
      &lt;&lt;TÊN&gt;&gt; — chương trình thay chúng bằng nội dung thật trước khi gửi.</div>
    <div id="tpl-list"></div>
    <div class="sheet-actions">
      <button id="tpl-save">Lưu</button>
      <button id="tpl-close" class="small">Đóng</button>
      <span class="hint" id="tpl-msg"></span>
    </div>
  </div>
</div>

<script>
let timer = null, jobId = null, exporting = false;
// Giu lai nhung gi da go o bang "Dao sau them", vi draw() ve lai ca feed
let focusDraft = "", extraDraft = 3;
// Bang duyet prompt: giu ban go do vi draw() ve lai feed moi 900ms
let reviewDraft = null, reviewSeq = -1;
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
  // Cho duyet prompt xay ra TRUOC khi them luot vao bien ban, nen o luot dau
  // tien items van rong. Neu thoat som o day thi bang duyet khong bao gio
  // hien ra, va buoi dung im mai vi khong ai bam Gui duoc.
  const cho_duyet = job.running && job.review;
  if (job.items.length === 0 && !cho_duyet) {
    feed.className = "empty";
    feed.textContent = "Dang cho MC mo dau...";
    return;
  }
  // Danh so y het ham exportable() ben server: chi luot da xong va co chu.
  // Nho vay con so tren man hinh dung bang con so go vao o "xuat tu ... den".
  let stt = 0;
  for (const it of job.items) {
    const so = (it.status === "done" && (it.text || "").trim()) ? ++stt : null;

    const wrap = el("div", "turn " + it.role + (it.status === "error" ? " err" : ""));
    const who = el("div", "who");
    const label = it.role === "mc"
      ? (it.turn === 0 ? "MC — lời kết" : "MC · lượt " + it.turn)
      : "Khách mời · lượt " + it.turn;
    if (so !== null) who.appendChild(el("span", "stt", "#" + so));
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

  // Dang cho nguoi duyet prompt truoc khi gui
  if (job.running && job.review) feed.appendChild(reviewPanel(job.review));

  // Buoi da xong -> cho noi them luot de lam ro cho con bo ngo
  if (!job.running && job.finished) feed.appendChild(extendPanel());
}

function reviewPanel(rv) {
  // draw() ve lai ca feed moi 900ms, nen phai giu lai nhung gi dang go do
  if (rv.seq !== reviewSeq) { reviewSeq = rv.seq; reviewDraft = null; }

  const box = el("div", "review");
  box.dataset.seq = rv.seq;      // de poll() biet bang nay da dung roi
  box.appendChild(el("div", "review-title",
    "Chờ bạn duyệt — " + rv.label + " (" + rv.who + ")"));
  box.appendChild(el("div", "review-note",
    "Đây là prompt sắp gửi. Sửa trực tiếp trong ô rồi bấm Gửi đi. " +
    "Sửa ở đây chỉ áp dụng cho lượt này; muốn đổi hẳn thì sửa ở Mẫu prompt."));

  const ta = el("textarea");
  ta.value = reviewDraft !== null ? reviewDraft : rv.prompt;
  ta.addEventListener("input", e => { reviewDraft = e.target.value; });
  box.appendChild(ta);

  const row = el("div", "row");
  const send = el("button", null, "Gửi đi");
  send.style.height = "40px";
  send.addEventListener("click", () => answerReview("send", ta.value));
  const hold = el("button", "small", "Tạm dừng");
  hold.addEventListener("click", () => answerReview("pause"));
  const halt = el("button", "small", "Dừng hẳn");
  halt.style.color = "var(--err)";
  halt.style.borderColor = "var(--err)";
  halt.addEventListener("click", () => answerReview("stop"));
  const chars = el("span", "review-note",
    ta.value.length + " ký tự");
  chars.style.marginBottom = "0";
  ta.addEventListener("input", e => {
    chars.textContent = e.target.value.length + " ký tự";
  });
  row.appendChild(send);
  row.appendChild(hold);
  row.appendChild(halt);
  row.appendChild(chars);
  box.appendChild(row);
  return box;
}

async function answerReview(action, text) {
  const data = await (await fetch("/api/review", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({id: jobId, action: action, text: text})
  })).json();
  if (data.error) {
    document.getElementById("hint").textContent = "Loi: " + data.error;
    return;
  }
  reviewDraft = null;
  await poll();
}

async function loadPrompts() {
  const list = await (await fetch("/api/prompts")).json();
  const box = document.getElementById("tpl-list");
  box.innerHTML = "";
  for (const p of list) {
    const row = el("div", "tpl" + (p.changed ? " dirty" : ""));
    const head = el("div", "tpl-head");
    head.appendChild(el("span", "tpl-name", p.name));
    head.appendChild(el("span", "tpl-note", p.note));
    row.appendChild(head);

    let field;
    if (p.kind === "number") {
      field = el("input");
      field.type = "number";
      field.min = 1;
    } else {
      field = el("textarea");
    }
    field.value = p.text;
    field.dataset.name = p.name;
    field.dataset.default = p.default;
    row.appendChild(field);

    if (p.changed) {
      const undo = el("button", "small", "Khôi phục mẫu gốc");
      undo.style.marginTop = "8px";
      undo.addEventListener("click", () => {
        field.value = field.dataset.default;
        row.classList.remove("dirty");
      });
      row.appendChild(undo);
    }
    box.appendChild(row);
  }
}

async function savePrompts() {
  const msg = document.getElementById("tpl-msg");
  const prompts = {};
  document.querySelectorAll("#tpl-list [data-name]").forEach(f => {
    prompts[f.dataset.name] = f.value;
  });
  msg.textContent = "Dang luu...";
  const data = await (await fetch("/api/prompts", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({prompts: prompts})
  })).json();
  if (data.error) { msg.textContent = "Loi: " + data.error; return; }
  const n = data.filter(p => p.changed).length;
  msg.textContent = "Da luu. " + (n ? n + " mau khac mac dinh." : "Tat ca dang la mau goc.");
  await loadPrompts();
}

function extendPanel() {
  const box = el("div", "extend");
  box.appendChild(el("div", "extend-title", "Đào sâu thêm"));
  box.appendChild(el("div", "extend-note",
    "Buổi này đã kết thúc. Chạy thêm lượt để làm rõ những điểm còn bỏ ngỏ — " +
    "MC sẽ được đọc lại toàn bộ biên bản ở trên, tự tìm chỗ khách mời còn " +
    "trả lời chung chung hoặc mâu thuẫn, rồi hỏi thẳng vào đó. " +
    "Bạn có thể viết thêm chỉ dẫn riêng cho lần tiếp tục này ở ô dưới."));

  const ta = el("textarea");
  ta.id = "focus";
  ta.rows = 3;
  // PAGE la chuoi Python thuong, KHONG phai raw string: viet \\n o day thi
  // Python nuot mat dau \\ va nhet xuong dong that vao giua chuoi JS, lam vo
  // ca khoi <script>. Phai escape doi.
  ta.placeholder =
    "Prompt bổ sung cho lần tiếp tục này (tuỳ chọn) — ví dụ:\\n" +
    "· Tập trung vào cung Quan Lộc, bỏ qua phần tính cách\\n" +
    "· Bắt khách mời trích dẫn đúng tên tài liệu cho mỗi khẳng định";
  ta.value = focusDraft;
  ta.addEventListener("input", e => { focusDraft = e.target.value; });
  box.appendChild(ta);

  const row = el("div", "row");
  const lab = el("label", "turns", "thêm mấy lượt");
  const num = el("input");
  num.type = "number"; num.id = "extraTurns";
  num.value = extraDraft; num.min = 1; num.max = 20;
  num.addEventListener("input", e => { extraDraft = e.target.value; });
  lab.appendChild(num);
  row.appendChild(lab);

  const btn = el("button", "retry", "Đào sâu thêm");
  btn.style.marginTop = "0";
  btn.addEventListener("click", extendShow);
  row.appendChild(btn);
  box.appendChild(row);
  return box;
}

async function extendShow() {
  if (!jobId) return;
  const hint = document.getElementById("hint");
  const extra = parseInt(document.getElementById("extraTurns").value, 10) || 3;
  hint.textContent = "Dang chay tiep de dao sau...";
  const data = await (await fetch("/api/extend", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({id: jobId, turns: extra, focus: focusDraft,
                          review: document.getElementById("review").checked})
  })).json();
  if (data.error) { hint.textContent = "Khong dao sau duoc: " + data.error; return; }
  focusDraft = "";
  document.getElementById("go").disabled = true;
  document.getElementById("pause").disabled = false;
  document.getElementById("stop").disabled = false;
  await poll();
  if (!timer) timer = setInterval(poll, 900);
}

async function poll() {
  let job;
  try {
    const response = await fetch("/api/status?id=" + jobId);
    if (!response.ok) return;
    job = await response.json();
  } catch (e) { return; } // Server may be restarting after a source edit.
  if (job.error && !job.items) { return; }

  // Dang cho duyet va bang duyet dung lượt do da hien san -> KHONG ve lai.
  // draw() tao lai toan bo the trong feed, ke ca o nhap prompt; vua go vua
  // bi tao lai thi mat focus va con tro nhay ve dau, khong sua noi.
  // Trong luc cho duyet server dang dung han nen cung chang co gi moi de ve.
  const shown = document.querySelector("#feed .review");
  if (shown && job.review && Number(shown.dataset.seq) === job.review.seq) return;

  draw(job);
  if (!job.running) {
    clearInterval(timer);
    timer = null;
    document.getElementById("go").disabled = false;
    document.getElementById("pause").disabled = true;
    document.getElementById("stop").disabled = true;
    const hint = document.getElementById("hint");
    if (job.error) {
      hint.innerHTML = "<b>Loi:</b> " + job.error +
        " — bam nut o cuoi trang de chay tiep tu dung cho hong.";
    } else if (job.paused) {
      hint.textContent = "Đã tạm dừng. Bấm “Chạy tiếp buổi này” " +
        "ở cuối trang để chạy tiếp từ đúng lượt đang dở.";
    } else if (job.finished) {
      hint.textContent = "Xong. Bien ban da luu trong logs/ va trong talkshow.db.";
    }
  }
}

async function pauseShow() {
  if (!jobId) return;
  document.getElementById("pause").disabled = true;
  document.getElementById("hint").textContent =
    "Se tam dung sau khi xong luot hien tai...";
  await fetch("/api/pause?id=" + jobId, {method: "POST"});
}

async function retry() {
  const hint = document.getElementById("hint");
  hint.textContent = "Dang chay tiep...";
  const data = await (await fetch("/api/retry?id=" + jobId, {method: "POST"})).json();
  if (data.error) { hint.textContent = "Khong chay tiep duoc: " + data.error; return; }
  document.getElementById("go").disabled = true;
  document.getElementById("pause").disabled = false;
  document.getElementById("stop").disabled = false;
  await poll();
  if (!timer) timer = setInterval(poll, 900);
}

async function exportShow() {
  if (!jobId || exporting) return;
  if (!exportPortInput.reportValidity()) return;
  const port = exportPortInput.valueAsNumber;
  rememberExportPort();

  // O trong = xuat het. Server tu hieu tham so rong.
  const tu = document.getElementById("export-from").value.trim();
  const den = document.getElementById("export-to").value.trim();
  if (tu && den && Number(tu) > Number(den)) {
    document.getElementById("hint").textContent =
      "So bat dau phai nho hon hoac bang so ket thuc.";
    return;
  }

  const hint = document.getElementById("hint");
  const btn = document.getElementById("export");
  hint.textContent = "Dang xuat...";
  exporting = true;
  btn.disabled = true;
  try {
    const url = "/api/export?id=" + jobId + "&port=" + port +
                "&from=" + encodeURIComponent(tu) + "&to=" + encodeURIComponent(den);
    const data = await (await fetch(url, {method: "POST"})).json();
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
  document.getElementById("pause").disabled = !job.running;
  document.getElementById("stop").disabled = !job.running;
  draw(job);
  const hint = document.getElementById("hint");
  if (job.running) {
    hint.textContent = "Dang chay...";
    timer = setInterval(poll, 900);
  } else if (job.finished) hint.textContent = "Buoi nay da xong.";
  else if (job.paused) {
    hint.textContent = "Buổi này đang tạm dừng — bấm “Chạy tiếp buổi này” " +
      "ở cuối trang để chạy tiếp từ đúng lượt đang dở.";
  } else hint.textContent = "Buoi nay con do dang — bam nut o cuoi trang de chay tiep.";
}

async function start() {
  const topic = document.getElementById("topic").value.trim();
  if (!topic) return;
  const turns = parseInt(document.getElementById("turns").value, 10) || 4;
  document.getElementById("go").disabled = true;
  document.getElementById("pause").disabled = false;
  document.getElementById("stop").disabled = false;
  document.getElementById("hint").textContent = "Dang chay...";

  const res = await fetch("/api/start", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({topic: topic, turns: turns,
                          review: document.getElementById("review").checked})
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
document.getElementById("pause").addEventListener("click", pauseShow);
document.getElementById("stop").addEventListener("click", stop);
document.getElementById("export").addEventListener("click", exportShow);
document.getElementById("open-prompts").addEventListener("click", async () => {
  await loadPrompts();
  document.getElementById("sheet").classList.add("on");
});
document.getElementById("tpl-close").addEventListener("click", () => {
  document.getElementById("sheet").classList.remove("on");
});
document.getElementById("tpl-save").addEventListener("click", savePrompts);
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
        if path == "/api/prompts":
            return self._send(200, json.dumps(prompt_list(), ensure_ascii=False))
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

        if path == "/api/pause":
            job_id = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            with LOCK:
                if job_id in JOBS:
                    JOBS[job_id]["pause_requested"] = True
            return self._send(200, '{"ok":true}')

        if path == "/api/retry":
            job_id = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            try:
                retry_job(job_id)
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)},
                                                  ensure_ascii=False))
            return self._send(200, '{"ok":true}')

        if path == "/api/prompts":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
                for name, text in (data.get("prompts") or {}).items():
                    save_prompt(name, text)
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps(prompt_list(), ensure_ascii=False))

        if path == "/api/review":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
                answer_review((data.get("id") or "").strip(),
                              data.get("action") or "send",
                              data.get("text"))
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)},
                                                  ensure_ascii=False))
            return self._send(200, '{"ok":true}')

        if path == "/api/extend":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
                extend_job((data.get("id") or "").strip(),
                           max(1, min(20, int(data.get("turns") or 3))),
                           data.get("focus") or "",
                           bool(data.get("review")))
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)},
                                                  ensure_ascii=False))
            return self._send(200, '{"ok":true}')

        if path == "/api/export":
            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            job_id = (query.get("id") or [""])[0]
            port = query.get("port", [DEFAULT_EXPORT_PORT])[0]

            def so(ten):
                """Doc so thu tu tu URL; de trong hoac khong phai so -> None."""
                raw = (query.get(ten) or [""])[0].strip()
                return int(raw) if raw.isdigit() and int(raw) > 0 else None

            try:
                result = export_show(job_id, port, so("from"), so("to"))
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
            turns = max(1, min(50, int(data.get("turns") or 4)))
            if not topic:
                raise ValueError("chua co chu de")
            job_id = start_job(topic, turns, bool(data.get("review")))
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
        k = load_prompts()
        print(f"[*] Da doc {n} buoi toa dam cu tu {DB_PATH.name}"
              + (f", {k} mau prompt da sua tay" if k else ""))
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
