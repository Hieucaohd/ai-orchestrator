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
import io
import json
import os
import re
import secrets
import socket
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

import segno

import adapters
import dev_reload
import talkshow_prompts as P

# Bind ra ca mang noi bo de dien thoai xem duoc bien ban qua link chia se.
# An toan nho Handler.tu_choi_xa(): may khac chi mo duoc /s/<token>.
HOST = "0.0.0.0"
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
CREATE TABLE IF NOT EXISTS presets (
    id         TEXT PRIMARY KEY,
    name       TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS preset_values (
    preset_id TEXT,
    name      TEXT,
    text      TEXT,
    PRIMARY KEY (preset_id, name)
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS shares (
    show_id     TEXT PRIMARY KEY,
    token       TEXT UNIQUE,
    created_at  TEXT,
    can_control INTEGER DEFAULT 0
);
"""

# Cot them vao sau khi bang da ton tai. CREATE TABLE IF NOT EXISTS khong bo
# sung cot cho bang cu, nen phai ALTER tay.
MIGRATIONS = [
    ("shares", "can_control", "INTEGER DEFAULT 0"),
]

# Bien dung duoc trong tung mau, kem giai thich.
#
# Day la danh sach CHINH XAC nhung gi run_show truyen vao P.fill() cho tung
# mau. Viet <<TEN>> khong co trong danh sach cua mau do thi no se nam nguyen
# xi trong prompt gui di, nen giao dien canh bao ngay.
TEMPLATE_VARS = {
    "MC_SETUP": [("TOPIC", "Chủ đề buổi toạ đàm bạn gõ ở đầu trang"),
                 ("MAX", "Số ký tự tối đa cho một câu hỏi")],
    "MC_FIRST": [],
    "MC_NEXT": [("ANSWER", "Nguyên văn câu trả lời khách mời vừa đưa"),
                ("TURN", "Số thứ tự lượt hiện tại"),
                ("TOTAL", "Tổng số lượt của buổi"),
                ("CLOSING", "Câu nhắc sắp hết giờ, ghép từ mẫu MC_NEAR_END")],
    "MC_NEAR_END": [],
    "MC_CLOSE": [("ANSWER", "Câu trả lời cuối cùng của khách mời")],
    "MC_CONTINUE": [("RECAP", "Tóm tắt buổi trước (ưu tiên lời kết)"),
                    ("DONE", "Số lượt buổi trước đã chạy"),
                    ("FOCUS", "Khối chỉ dẫn riêng, dựng từ MC_CONTINUE_FOCUS"),
                    ("EXTRA", "Số lượt được chạy thêm lần này")],
    "MC_CONTINUE_FOCUS": [("NOTE", "Prompt bổ sung bạn gõ khi bấm Đào sâu thêm")],
    "MC_FOCUS_REMINDER": [("NOTE", "Cũng là prompt bổ sung đó")],
    "GUEST_SETUP": [("TOPIC", "Chủ đề buổi toạ đàm"),
                    ("QUESTION", "Câu hỏi MC vừa đặt, đã cắt cho vừa ô nhập")],
    "GUEST_NEXT": [("QUESTION", "Câu hỏi MC vừa đặt")],
    "GUEST_RESUME": [("TOPIC", "Chủ đề buổi toạ đàm"),
                     ("QUESTION", "Câu hỏi MC vừa đặt"),
                     ("RECAP", "Danh sách các câu đã hỏi ở buổi trước")],
}

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

# Mau nay gui cho AI nao. Ghi ro ra giao dien de khoi phai doan qua tien to
# MC_ / GUEST_ trong ten bien.
#
# Ba con so o cuoi khong phai prompt, nhung deu anh huong den noi dung gui
# cho mot ben cu the: MAX_QUESTION cat cau hoi truoc khi sang NotebookLM,
# hai con RECAP_* cat phan tom tat truoc khi sang ChatGPT.
TEMPLATE_TARGET = {
    "MC_SETUP": MC, "MC_FIRST": MC, "MC_NEXT": MC, "MC_NEAR_END": MC,
    "MC_CLOSE": MC, "MC_CONTINUE": MC, "MC_CONTINUE_FOCUS": MC,
    "MC_FOCUS_REMINDER": MC,
    "GUEST_SETUP": GUEST, "GUEST_NEXT": GUEST, "GUEST_RESUME": GUEST,
    "MAX_QUESTION": GUEST, "RECAP_MAX_CHARS": MC, "RECAP_MAX_ANSWER": MC,
}

PROMPT_OVERRIDES = {}   # ten -> noi dung da sua tren giao dien

# Thong bao cho nhung buoi bi cat ngang vi tat server giua chung.
INTERRUPTED = "Phiên trước bị dừng giữa chừng (đóng server hoặc mất kết nối)."


def db_init():
    # closing() de dong han ket noi. Rieng `with con:` chi commit transaction
    # chu KHONG dong file — de vay se ro ri file handle sau moi lan ghi.
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        con.executescript(SCHEMA)
        for bang, cot, kieu in MIGRATIONS:
            co = {r[1] for r in con.execute(f"PRAGMA table_info({bang})")}
            if cot not in co:
                con.execute(f"ALTER TABLE {bang} ADD COLUMN {cot} {kieu}")
                print(f"[db] da them cot {bang}.{cot}")


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


# --------------------------------------------- chia se qua mang noi bo (LAN)

def lan_ip() -> str:
    """
    Dia chi cua may trong mang noi bo, de dung lam link chia se.

    Mo mot socket UDP roi doc dia chi cua chinh no — khong he gui goi tin nao
    di, chi de he dieu hanh chon giup card mang dang dung.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# Nhung viec ma thiet bi khac duoc lam khi link chia se da bat quyen dieu
# khien. Co y KHONG cho: bat dau buoi moi, sua mau prompt, xuat du lieu, xem
# danh sach cac buoi khac — nhung thu do chi lam tu may nay.
SHARE_ACTIONS = {"extend", "retry", "pause", "stop", "review"}


def share_info(show_id: str) -> dict:
    """Ma chia se cua mot buoi kem quyen dieu khien. Chua co thi tao moi."""
    with LOCK:
        if show_id not in JOBS:
            raise KeyError("khong co buoi toa dam nao voi ma nay")

    with closing(sqlite3.connect(DB_PATH)) as con, con:
        row = con.execute(
            "SELECT token, can_control FROM shares WHERE show_id = ?",
            (show_id,)).fetchone()
        if row:
            return {"token": row[0], "control": bool(row[1])}
        token = secrets.token_urlsafe(16)
        con.execute("INSERT INTO shares VALUES (?,?,?,0)",
                    (show_id, token,
                     datetime.now().isoformat(sep=" ", timespec="seconds")))
    return {"token": token, "control": False}


def set_share_control(show_id: str, on: bool):
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        con.execute("UPDATE shares SET can_control = ? WHERE show_id = ?",
                    (1 if on else 0, show_id))


def share_by_token(token: str):
    """Tra ve (show_id, duoc_dieu_khien) hoac (None, False)."""
    if not token:
        return None, False
    with closing(sqlite3.connect(DB_PATH)) as con:
        row = con.execute(
            "SELECT show_id, can_control FROM shares WHERE token = ?",
            (token,)).fetchone()
    return (row[0], bool(row[1])) if row else (None, False)


def show_by_token(token: str):
    return share_by_token(token)[0]


def qr_svg(data: str) -> str:
    """
    Ma QR dang SVG, nhet thang vao trang duoc.

    xmldecl=False de bo khai bao <?xml?> (khong hop le giua HTML), nhung
    svgns=True de GIU lai xmlns. Thieu xmlns thi nhet vao innerHTML van chay,
    nhung luu ra file hay dung lam <img src="data:image/svg+xml..."> thi
    trinh duyet tu choi giai ma.
    """
    buf = io.BytesIO()
    segno.make(data, error="m").save(
        buf, kind="svg", scale=5, border=2, dark="#111", light="#fff",
        xmldecl=False, svgns=True, nl=False)
    return buf.getvalue().decode("utf-8")


# ----------------------------------------------------- bo mau prompt co ten

def get_setting(key: str, mac_dinh: str = "") -> str:
    with closing(sqlite3.connect(DB_PATH)) as con:
        row = con.execute("SELECT value FROM settings WHERE key = ?",
                          (key,)).fetchone()
    return row[0] if row else mac_dinh


def set_setting(key: str, value: str):
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        con.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) "
                    "DO UPDATE SET value = excluded.value", (key, value))


def preset_list() -> list:
    """Danh sach bo mau, kem bo dang dung."""
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT p.id, p.name, p.updated_at, COUNT(v.name) AS so_muc "
            "FROM presets p LEFT JOIN preset_values v ON v.preset_id = p.id "
            "GROUP BY p.id ORDER BY p.name COLLATE NOCASE").fetchall()
    return {"active": get_setting("active_preset"),
            "presets": [dict(r) for r in rows]}


def preset_save(name: str, values: dict, preset_id: str = None) -> str:
    """
    Tao moi hoac ghi de mot bo mau.

    Chi luu nhung muc KHAC ban goc — bo mau vi the tu bat kip khi ban sua
    mau mac dinh trong talkshow_prompts.py, thay vi dong bang gia tri cu.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("bo mau phai co ten")

    known = {n for n, _ in EDITABLE_TEMPLATES} | {n for n, _ in EDITABLE_NUMBERS}
    khac = {k: str(v) for k, v in values.items()
            if k in known and str(v).strip() != str(getattr(P, k)).strip()}

    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    preset_id = preset_id or uuid.uuid4().hex[:12]
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        con.execute(
            "INSERT INTO presets VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE "
            "SET name = excluded.name, updated_at = excluded.updated_at",
            (preset_id, name, now, now))
        con.execute("DELETE FROM preset_values WHERE preset_id = ?", (preset_id,))
        con.executemany("INSERT INTO preset_values VALUES (?,?,?)",
                        [(preset_id, k, v) for k, v in khac.items()])
    return preset_id


def preset_apply(preset_id: str):
    """Nap mot bo mau vao bo dang dung. preset_id rong = ve mau goc."""
    if preset_id:
        with closing(sqlite3.connect(DB_PATH)) as con:
            if not con.execute("SELECT 1 FROM presets WHERE id = ?",
                               (preset_id,)).fetchone():
                raise KeyError("khong co bo mau nao voi ma nay")
            rows = con.execute(
                "SELECT name, text FROM preset_values WHERE preset_id = ?",
                (preset_id,)).fetchall()
    else:
        rows = []

    with closing(sqlite3.connect(DB_PATH)) as con, con:
        con.execute("DELETE FROM prompts")
        con.executemany("INSERT INTO prompts VALUES (?,?)", rows)
    set_setting("active_preset", preset_id)
    load_prompts()


def preset_delete(preset_id: str):
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        con.execute("DELETE FROM preset_values WHERE preset_id = ?", (preset_id,))
        con.execute("DELETE FROM presets WHERE id = ?", (preset_id,))
    if get_setting("active_preset") == preset_id:
        set_setting("active_preset", "")


VAR_PATTERN = re.compile(r"<<([A-Z_][A-Z0-9_]*)>>")


def prompt_list() -> list:
    """
    Toan bo mau prompt kem ban goc, danh sach bien dung duoc va canh bao.

    "unknown" la nhung <<TEN>> co trong noi dung ma khong phai bien cua mau
    do — chung se nam nguyen xi trong prompt gui cho AI, nen phai bao.
    """
    out = []
    for kind, items in (("text", EDITABLE_TEMPLATES), ("number", EDITABLE_NUMBERS)):
        for name, note in items:
            default = str(getattr(P, name))
            with LOCK:
                current = PROMPT_OVERRIDES.get(name)
            text = current if current is not None else default
            bien = TEMPLATE_VARS.get(name, [])
            hop_le = {v for v, _ in bien}
            out.append({
                "name": name, "note": note, "kind": kind,
                "target": TEMPLATE_TARGET.get(name, ""),
                "default": default, "text": text,
                "changed": current is not None,
                "vars": [{"name": v, "note": n} for v, n in bien],
                "unknown": sorted(set(VAR_PATTERN.findall(text)) - hop_le),
            })
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
  .turn {
    margin-bottom: 16px;
    /* De trinh duyet bo qua bo cuc va ve cho nhung luot ngoai man hinh.
       Buoi dai vai tram luot thi day la khac biet lon nhat khi cuon. */
    content-visibility: auto;
    contain-intrinsic-size: auto 420px;
  }
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
  /* Phan da render tu markdown: tu xuong dong bang the, khong dung pre-wrap */
  .bubble .md { white-space: normal; }
  .md > :first-child { margin-top: 0; }
  .md > :last-child { margin-bottom: 0; }
  .md p { margin: 0 0 10px; }
  .md h3, .md h4, .md h5, .md h6 { margin: 16px 0 8px; line-height: 1.35; }
  .md h3 { font-size: 16px; } .md h4 { font-size: 15px; }
  .md h5, .md h6 { font-size: 14px; }
  .md ul, .md ol { margin: 0 0 10px; padding-left: 22px; }
  .md li { margin: 3px 0; }
  .md code { font-family: ui-monospace, Consolas, monospace; font-size: 12.5px;
             background: var(--line); padding: 1px 5px; border-radius: 4px; }
  .md pre { background: var(--line); padding: 10px 12px; border-radius: 8px;
            overflow-x: auto; margin: 0 0 10px; }
  .md pre code { background: none; padding: 0; }
  .md blockquote { margin: 0 0 10px; padding: 2px 0 2px 12px;
                   border-left: 3px solid var(--line); color: var(--muted); }
  .md hr { border: 0; border-top: 1px solid var(--line); margin: 14px 0; }
  .md a { color: var(--mc); }
  .mc .bubble { background: var(--mc-bg); }
  .guest .bubble { background: var(--guest-bg); margin-left: 28px; }
  .lead { font-style: italic; color: var(--muted); margin-bottom: 8px;
          padding-bottom: 8px; border-bottom: 1px dashed var(--line); }
  .waiting { color: var(--muted); font-style: italic; }
  .err .bubble { border-color: var(--err); color: var(--err); }
  button.small { height: 38px; padding: 0 16px; font-size: 13px;
                 background: transparent; color: var(--guest);
                 border: 1px solid var(--guest); }
  /* bang chia se kem ma QR */
  .share-box { display: flex; gap: 16px; align-items: flex-start; margin-top: 12px;
               padding: 14px; border-radius: 11px; background: var(--card);
               border: 1px solid var(--line); }
  .share-qr svg { display: block; width: 168px; height: 168px;
                  border-radius: 6px; background: #fff; }
  .share-info { flex: 1; min-width: 0; }
  .share-title { font-size: 13px; font-weight: 650; margin-bottom: 9px; }
  .share-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  #share-url { flex: 1 1 260px; padding: 8px 10px; height: 38px;
    border: 1px solid var(--line); border-radius: 9px; background: var(--bg);
    color: var(--text); font-family: ui-monospace, Consolas, monospace;
    font-size: 12.5px; }
  .share-note { margin-top: 10px; font-size: 12px; color: var(--muted);
                line-height: 1.55; }
  .share-note b { color: var(--text); }
  /* che do chi-xem: an het cac thu dieu khien */
  body.chi-xem header .row, body.chi-xem #share-box { display: none; }
  body.chi-xem header { padding-bottom: 14px; }
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
  .tpl-left { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .tpl-to { font-size: 11px; font-weight: 650; padding: 2px 8px;
            border-radius: 20px; white-space: nowrap; }
  .tpl-to.to-mc { background: var(--mc-bg); color: var(--mc); }
  .tpl-to.to-guest { background: var(--guest-bg); color: var(--guest); }
  .tpl-name { font-size: 13px; font-weight: 650; font-family: ui-monospace, monospace; }
  .tpl-note { font-size: 12px; color: var(--muted); }
  .tpl.dirty .tpl-name::after { content: " (đã sửa)"; color: var(--guest);
                                font-weight: 400; font-size: 11px; }
  .tpl textarea { width: 100%; min-height: 130px;
                  font-family: ui-monospace, Consolas, monospace;
                  font-size: 12.5px; line-height: 1.5; }
  .tpl input[type=number] { width: 120px; }
  /* thanh chon bo mau */
  .preset-bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
                padding: 12px; margin-bottom: 18px; border-radius: 10px;
                background: var(--card); border: 1px solid var(--line); }
  .preset-bar label { display: flex; gap: 7px; align-items: center;
                      font-size: 12.5px; color: var(--muted); }
  .preset-bar select { min-width: 200px; flex: 0 1 260px; }
  .preset-bar input[type=text] { flex: 1 1 180px; padding: 8px 10px; height: 38px;
    border: 1px solid var(--line); border-radius: 9px;
    background: var(--bg); color: var(--text); font: inherit; font-size: 13px; }
  button.danger { color: var(--err); border-color: var(--err); }
  /* cac bien dung duoc trong mau */
  .vars { display: flex; flex-wrap: wrap; gap: 6px; margin: 7px 0 4px; }
  .vars .v { font-family: ui-monospace, Consolas, monospace; font-size: 11.5px;
             padding: 2px 7px; border-radius: 5px; cursor: pointer;
             background: var(--mc-bg); color: var(--mc);
             border: 1px solid transparent; }
  .vars .v:hover { border-color: var(--mc); }
  .vars .none { font-size: 11.5px; color: var(--muted); font-style: italic; }
  .warn { margin-top: 6px; font-size: 12px; color: var(--err); }
  .sheet-actions { display: flex; gap: 10px; align-items: center;
                   position: sticky; bottom: 0; background: var(--bg);
                   padding: 12px 0 2px; border-top: 1px solid var(--line); }
  /* Thanh vach ben canh thanh cuon: moi vach la mot cau hoi cua MC.
     Ro chuot vao thanh thi vach dai ra; ro vao tung vach thi hien cau hoi. */
  .rail { position: fixed; right: 0; top: 0; bottom: 0; width: 26px;
          z-index: 14; display: none; }
  .rail.on { display: block; }
  .rail::before {            /* vung ro chuot rong hon vach cho de trung */
    content: ""; position: absolute; inset: 0;
  }
  .rail .tick {
    position: absolute; right: 7px; height: 2px; width: 11px;
    margin-top: -1px; border-radius: 2px; cursor: pointer;
    background: var(--muted); opacity: .4;
    transition: width .12s ease, opacity .12s ease, background .12s ease;
  }
  .rail .tick::after {
    /* Noi rong vung bat chuot sang NGANG cho de tro trung thanh vach.
       Tuyet doi khong noi theo chieu doc: buoi dai thi cac vach chi cach
       nhau vai pixel, noi doc la chung de len nhau va chan mat nhau. */
    content: ""; position: absolute; inset: 0 -8px 0 -14px;
  }
  .rail .tick.end { width: 17px; opacity: .6; }
  .rail:hover .tick { width: 19px; opacity: .75; }
  .rail:hover .tick.end { width: 24px; }
  .rail .tick:hover, .rail .tick.here {
    width: 26px; right: 3px; opacity: 1; height: 3px; background: var(--mc);
  }
  .rail-tip {
    position: fixed; right: 38px; max-width: 400px; padding: 9px 12px;
    background: var(--card); color: var(--text);
    border: 1px solid var(--line); border-radius: 9px;
    box-shadow: 0 4px 18px rgba(0, 0, 0, .25);
    font-size: 12.5px; line-height: 1.5; z-index: 16;
    display: none; pointer-events: none;
  }
  .rail-tip.on { display: block; }
  .rail-tip b { color: var(--mc); font-family: ui-monospace, monospace; }
  .rail-tip > div { margin-top: 5px; color: var(--muted); }

  /* Hai nut nhay dau/cuoi trang. Chi hien khi trang du dai de cuon. */
  .jump { position: fixed; right: 18px; bottom: 18px; z-index: 15;
          display: none; flex-direction: column; gap: 8px; }
  .jump.on { display: flex; }
  .jump button { width: 42px; height: 42px; padding: 0; border-radius: 50%;
                 font-size: 17px; line-height: 1; font-weight: 400;
                 background: var(--card); color: var(--muted);
                 border: 1px solid var(--line);
                 box-shadow: 0 2px 10px rgba(0, 0, 0, .18); }
  .jump button:hover { color: var(--mc); border-color: var(--mc); }
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
             inputmode="numeric" placeholder="đầu"
             title="Số thứ tự lượt bắt đầu — để trống là từ đầu">
    </label>
    <label class="export-port">đến
      <input type="number" id="export-to" min="1" step="1"
             inputmode="numeric" placeholder="cuối"
             title="Số thứ tự lượt kết thúc — để trống là tới cuối">
    </label>
    <button id="export" class="small" disabled>Xuất hội thoại</button>
    <button id="share" class="small" disabled>Chia sẻ</button>
  </div>
  <div class="share-box" id="share-box" hidden>
    <div class="share-qr" id="share-qr"></div>
    <div class="share-info">
      <div class="share-title">Quét mã hoặc mở link này trên thiết bị cùng mạng Wi-Fi</div>
      <div class="share-row">
        <input type="text" id="share-url" readonly>
        <button id="share-copy" class="small">Sao chép link</button>
        <button id="share-close" class="small">Đóng</button>
      </div>
      <label class="chk" style="margin-top:10px">
        <input type="checkbox" id="share-control">
        Cho phép bấm “Đào sâu thêm”, “Chạy tiếp”, “Tạm dừng”, “Dừng hẳn” từ link này
      </label>
      <div class="share-note">Mặc định link <b>chỉ để xem</b>. Dù có bật quyền
        trên, người mở link vẫn <b>không</b> bắt đầu được buổi mới, không sửa
        được mẫu prompt và không xuất được dữ liệu. Link chỉ dùng được khi máy
        này vẫn đang chạy server.</div>
    </div>
  </div>
  <div class="hint" id="hint"></div>
</header>
<main>
  <div id="feed" class="empty">Nhập chủ đề rồi bấm Bắt đầu.</div>
  <div id="feed-extra"></div>
</main>

<div class="jump" id="jump">
  <button id="to-top" title="Lên đầu trang" aria-label="Lên đầu trang">↑</button>
  <button id="to-bottom" title="Xuống cuối trang" aria-label="Xuống cuối trang">↓</button>
</div>

<div class="rail" id="rail"></div>
<div class="rail-tip" id="rail-tip"></div>

<div class="sheet" id="sheet">
  <div class="sheet-box">
    <h2>Bộ mẫu prompt</h2>
    <div class="sub">Một bộ mẫu gồm toàn bộ prompt gửi cho ChatGPT và
      NotebookLM. Bạn có thể tạo nhiều bộ cho nhiều kiểu toạ đàm khác nhau,
      đặt tên và chuyển qua lại. Các mốc dạng &lt;&lt;TÊN&gt;&gt; được thay
      bằng nội dung thật trước khi gửi — bấm vào tên biến để chèn vào chỗ
      con trỏ.</div>

    <div class="preset-bar">
      <label>Bộ đang dùng
        <select id="preset-pick"></select>
      </label>
      <input type="text" id="preset-name" placeholder="Tên bộ mẫu...">
      <button id="preset-save-as" class="small">Lưu thành bộ mới</button>
      <button id="preset-delete" class="small danger">Xoá bộ này</button>
    </div>

    <div id="tpl-list"></div>
    <div class="sheet-actions">
      <button id="tpl-save">Lưu vào bộ đang dùng</button>
      <button id="tpl-close" class="small">Đóng</button>
      <span class="hint" id="tpl-msg"></span>
    </div>
  </div>
</div>

<script>
// Server chen vao: null = giao dien dieu khien, {token} = che do chi-xem
const CHIASE = "__SHARE_CFG__";
let timer = null, jobId = null, exporting = false;
// Giu lai nhung gi da go o bang "Dao sau them", vi draw() ve lai ca feed
let focusDraft = "", extraDraft = 3;
// Bang duyet prompt: giu ban go do vi draw() ve lai feed moi 900ms
let reviewDraft = null, reviewSeq = -1;
// Cac vach ben thanh cuon, moi vach ung voi mot cau hoi cua MC
let railMarks = [];
// Dau van cua tung luot da ve, de biet luot nao thuc su doi
let dauVanCu = [], veLaiJobId = null;

// Cung mot nut, hai duong: tren may nay goi /api/..., con mo qua link
// chia se thi goi /s/<token>/... — ma buoi lay tu token nen khong tro
// sang buoi khac duoc.
function duongDan(viec) {
  return CHIASE ? "/s/" + CHIASE.token + "/" + viec
                : "/api/" + viec + "?id=" + jobId;
}
// Con tu dien khoang xuat ho khong, hay nguoi dung da tu go
let xuatTuDong = true;
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

function thoatHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function veMarkdown(src) {
  // Render markdown ra HTML. Cac AI gio tra ve markdown that (lay qua nut
  // Copy cua chinh trang web), neu in tho thi day dau ** va # rat kho doc.
  //
  // Thoat HTML TRUOC roi moi bien doi, nen noi dung do AI sinh ra khong the
  // chen the vao trang.
  const khoiCode = [];
  let t = thoatHtml(src).replace(/```([\\s\\S]*?)```/g, (m, code) => {
    khoiCode.push(code.replace(/^[a-zA-Z0-9]*\\n/, ""));
    return "\\u0000CODE" + (khoiCode.length - 1) + "\\u0000";
  });

  const ra = [];
  let dsThuong = null, dsSo = null;

  const dongDs = () => {
    if (dsThuong) { ra.push("<ul>" + dsThuong.join("") + "</ul>"); dsThuong = null; }
    if (dsSo) { ra.push("<ol>" + dsSo.join("") + "</ol>"); dsSo = null; }
  };

  const trongDong = (s) => s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\\*\\*([^*]+)\\*\\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\\*([^*\\n]+)\\*/g, "$1<em>$2</em>")
    .replace(/\\[([^\\]]+)\\]\\((https?:[^)\\s]+)\\)/g,
             '<a href="$2" target="_blank" rel="noopener">$1</a>');

  for (const dong of t.split("\\n")) {
    const d = dong.trim();

    const md = d.match(/^\\u0000CODE(\\d+)\\u0000$/);
    if (md) { dongDs(); ra.push("<pre><code>" + khoiCode[+md[1]] + "</code></pre>"); continue; }

    if (!d) { dongDs(); continue; }

    const h = d.match(/^(#{1,6})\\s+(.*)$/);
    if (h) {
      dongDs();
      const c = Math.min(h[1].length + 2, 6);
      ra.push("<h" + c + ">" + trongDong(h[2]) + "</h" + c + ">");
      continue;
    }
    if (/^(-{3,}|\\*{3,})$/.test(d)) { dongDs(); ra.push("<hr>"); continue; }
    if (d.startsWith("&gt; ")) {
      dongDs();
      ra.push("<blockquote>" + trongDong(d.slice(5)) + "</blockquote>");
      continue;
    }
    const g = d.match(/^[-*]\\s+(.*)$/);
    if (g) {
      if (dsSo) dongDs();
      (dsThuong = dsThuong || []).push("<li>" + trongDong(g[1]) + "</li>");
      continue;
    }
    const s = d.match(/^\\d+[.)]\\s+(.*)$/);
    if (s) {
      if (dsThuong) dongDs();
      (dsSo = dsSo || []).push("<li>" + trongDong(s[1]) + "</li>");
      continue;
    }
    dongDs();
    ra.push("<p>" + trongDong(d) + "</p>");
  }
  dongDs();
  return ra.join("");
}

function uocChieuCao(it) {
  // Doan chieu cao mot luot theo so ky tu, de bao cho trinh duyet biet phai
  // chua cho bao nhieu khi no bo qua luot nam ngoai man hinh.
  //
  // Doan cang sat thi thanh cuon va thanh vach cang dung. Doan bay thi
  // thanh cuon nhay lien tuc khi cuon qua.
  const chu = (it.text || "").length + (it.lead || "").length;
  return Math.round(90 + Math.ceil(chu / 95) * 23);
}

function veMotLuot(it, so) {
  const wrap = el("div", "turn " + it.role + (it.status === "error" ? " err" : ""));
  wrap.style.containIntrinsicSize = "auto " + uocChieuCao(it) + "px";
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
    const noi = el("div", "md");
    noi.innerHTML = veMarkdown(it.text || "");
    bubble.appendChild(noi);
  }
  wrap.appendChild(bubble);
  return wrap;
}

function dauVan(it, so) {
  // Dau van cua mot luot. Giong nhau thi khoi dung toi the DOM cua no.
  return [it.status, it.role, it.turn, so, it.seconds,
          (it.text || "").length, (it.lead || "").length].join("|");
}

function draw(job) {
  const feed = document.getElementById("feed");
  const extra = document.getElementById("feed-extra");
  document.getElementById("export").disabled = exporting ||
    !job.items.some(it => it.status === "done" && (it.text || "").trim());
  document.getElementById("share").disabled = !jobId;

  // Cho duyet prompt xay ra TRUOC khi them luot vao bien ban, nen o luot dau
  // tien items van rong. Neu thoat som o day thi bang duyet khong bao gio
  // hien ra, va buoi dung im mai vi khong ai bam Gui duoc.
  const cho_duyet = job.running && job.review;
  if (job.items.length === 0 && !cho_duyet) {
    feed.className = "empty";
    feed.textContent = "Dang cho MC mo dau...";
    extra.innerHTML = "";
    veLaiJobId = null; dauVanCu = [];
    return;
  }

  // Doi buoi khac -> lam lai tu dau. Cung buoi -> chi dung toi luot nao doi.
  //
  // Truoc day ham nay xoa sach roi dung lai ca bien ban moi lan poll: voi
  // buoi 398 luot la 2987 the DOM, ton ~150ms moi 0,9 giay nen giat ro ret.
  const doiBuoi = veLaiJobId !== jobId;
  if (doiBuoi) {
    feed.innerHTML = "";
    dauVanCu = [];
    veLaiJobId = jobId;
  }
  feed.className = "";

  // Danh so y het ham exportable() ben server: chi luot da xong va co chu.
  // Nho vay con so tren man hinh dung bang con so go vao o "xuat tu ... den".
  let stt = 0;
  const dauVanMoi = [];
  const marks = [];          // moi cau hoi cua MC -> mot vach ben thanh cuon

  for (let i = 0; i < job.items.length; i++) {
    const it = job.items[i];
    const so = (it.status === "done" && (it.text || "").trim()) ? ++stt : null;
    const sig = dauVan(it, so);
    dauVanMoi.push(sig);

    let node = feed.children[i];
    if (!node) {
      node = veMotLuot(it, so);
      feed.appendChild(node);
    } else if (dauVanCu[i] !== sig) {
      const moi = veMotLuot(it, so);
      feed.replaceChild(moi, node);
      node = moi;
    }

    // Chi cau hoi cua MC moi co vach — khach moi tra loi ngay duoi do
    if (it.role === "mc" && (it.text || "").trim()) {
      marks.push({
        el: node, num: so, closing: it.turn === 0,
        label: it.turn === 0 ? "Lời kết" : "Lượt " + it.turn,
        text: it.text.trim().slice(0, 300),
      });
    }
  }

  // Bo luot thua (xay ra khi bam Thu lai: luot hong bi go di)
  while (feed.children.length > job.items.length) {
    feed.removeChild(feed.lastChild);
  }
  dauVanCu = dauVanMoi;
  capNhatKhoangXuat(stt);

  // Cac bang phu nam o vung rieng, doi chung khong dung toi bien ban
  extra.innerHTML = "";

  // Mo qua link chia se ma chua bat quyen thi khong ve nut dieu khien nao:
  // truoc day van ve, bam vao thi server tra 403 — mot cai nut hong.
  const choDieuKhien = !CHIASE || CHIASE.control;

  // Buoi con do dang -> nut chay tiep tu dung cho hong, khong lam lai ca buoi
  if (choDieuKhien && !job.running && !job.finished) {
    const last = job.items[job.items.length - 1];
    const failed = last && last.status === "error";
    const btn = el("button", "retry",
      failed ? "Thử lại lượt này" : "Chạy tiếp buổi này");
    btn.addEventListener("click", retry);
    extra.appendChild(btn);
  }

  // Dang cho nguoi duyet prompt truoc khi gui
  if (choDieuKhien && job.running && job.review)
    extra.appendChild(reviewPanel(job.review));

  // Buoi da xong -> cho noi them luot de lam ro cho con bo ngo
  if (choDieuKhien && !job.running && job.finished)
    extra.appendChild(extendPanel());

  toggleJump();   // do dai trang vua doi, tinh lai xem con can nut cuon khong

  // Dung lai thanh vach ton kem (phai do vi tri tung luot) nen chi lam khi
  // so vach doi, khong lam moi lan poll. Doi buoi thi bat buoc phai dung
  // lai: so vach co the trung nhau nhung cac the DOM da khac het.
  if (doiBuoi || marks.length !== railMarks.length) buildRail(marks);
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
  const data = await (await fetch(duongDan("review"), {
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

function chenBien(field, ten) {
  // Chen <<TEN>> vao dung cho con tro dang dung
  const moc = "<<" + ten + ">>";
  const a = field.selectionStart, b = field.selectionEnd;
  if (a === null || a === undefined) { field.value += moc; return; }
  field.value = field.value.slice(0, a) + moc + field.value.slice(b);
  field.selectionStart = field.selectionEnd = a + moc.length;
  field.focus();
  field.dispatchEvent(new Event("input"));
}

function veMotMau(p) {
  const row = el("div", "tpl" + (p.changed ? " dirty" : ""));
  const head = el("div", "tpl-head");
  const trai = el("span", "tpl-left");
  if (p.target) {
    const to = el("span",
      "tpl-to " + (p.target === "ChatGPT" ? "to-mc" : "to-guest"),
      p.kind === "number"
        ? "ảnh hưởng " + p.target
        : "gửi cho " + p.target);
    trai.appendChild(to);
  }
  trai.appendChild(el("span", "tpl-name", p.name));
  head.appendChild(trai);
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

  // Bien dung duoc — bam vao thi chen vao cho con tro
  if (p.kind === "text") {
    const vars = el("div", "vars");
    if (p.vars.length === 0) {
      vars.appendChild(el("span", "none", "Mẫu này không có biến nào"));
    } else {
      for (const v of p.vars) {
        const chip = el("span", "v", "<<" + v.name + ">>");
        chip.title = v.note;
        chip.addEventListener("click", () => chenBien(field, v.name));
        vars.appendChild(chip);
      }
    }
    row.appendChild(vars);
  }

  row.appendChild(field);

  const canhBao = el("div", "warn");
  function kiemTraBien() {
    const hopLe = new Set((p.vars || []).map(v => v.name));
    const dung = [...field.value.matchAll(/<<([A-Z_][A-Z0-9_]*)>>/g)]
      .map(m => m[1]).filter(n => !hopLe.has(n));
    const la = [...new Set(dung)];
    canhBao.textContent = la.length
      ? "Biến không dùng được ở mẫu này: " + la.map(n => "<<" + n + ">>").join(", ")
        + " — sẽ nằm nguyên trong prompt gửi đi."
      : "";
  }
  field.addEventListener("input", kiemTraBien);
  kiemTraBien();
  row.appendChild(canhBao);

  if (p.changed) {
    const undo = el("button", "small", "Khôi phục mẫu gốc");
    undo.style.marginTop = "8px";
    undo.addEventListener("click", () => {
      field.value = field.dataset.default;
      row.classList.remove("dirty");
      kiemTraBien();
    });
    row.appendChild(undo);
  }
  return row;
}

function veDanhSachBo(data) {
  const sel = document.getElementById("preset-pick");
  sel.innerHTML = "";
  const goc = el("option", null, "Mặc định (mẫu gốc trong mã nguồn)");
  goc.value = "";
  sel.appendChild(goc);
  for (const b of data.presets) {
    const o = el("option", null,
      b.name + (b.so_muc ? "  ·  " + b.so_muc + " mục đã sửa" : "  ·  như mẫu gốc"));
    o.value = b.id;
    sel.appendChild(o);
  }
  sel.value = data.active || "";

  const ten = document.getElementById("preset-name");
  const dangDung = data.presets.find(b => b.id === data.active);
  ten.value = dangDung ? dangDung.name : "";
  document.getElementById("preset-delete").disabled = !dangDung;
}

async function loadPrompts() {
  const data = await (await fetch("/api/prompts")).json();
  const box = document.getElementById("tpl-list");
  box.innerHTML = "";
  for (const p of data.fields) box.appendChild(veMotMau(p));
  veDanhSachBo(data);
}

function thuThapMau() {
  const prompts = {};
  document.querySelectorAll("#tpl-list [data-name]").forEach(f => {
    prompts[f.dataset.name] = f.value;
  });
  return prompts;
}

async function goiApiMau(body, dangLam) {
  const msg = document.getElementById("tpl-msg");
  msg.textContent = dangLam;
  const data = await (await fetch("/api/prompts", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  })).json();
  if (data.error) { msg.textContent = "Lỗi: " + data.error; return null; }

  const box = document.getElementById("tpl-list");
  box.innerHTML = "";
  for (const p of data.fields) box.appendChild(veMotMau(p));
  veDanhSachBo(data);
  return data;
}

async function savePrompts() {
  const data = await goiApiMau(
    {action: "save", prompts: thuThapMau()}, "Đang lưu...");
  if (!data) return;
  const n = data.fields.filter(p => p.changed).length;
  document.getElementById("tpl-msg").textContent =
    "Đã lưu. " + (n ? n + " mục khác mẫu gốc." : "Tất cả đang là mẫu gốc.");
}

async function savePresetAs() {
  const ten = document.getElementById("preset-name").value.trim();
  if (!ten) {
    document.getElementById("tpl-msg").textContent =
      "Hãy đặt tên cho bộ mẫu trước khi lưu.";
    return;
  }
  const data = await goiApiMau(
    {action: "save_preset", name: ten, prompts: thuThapMau()},
    "Đang tạo bộ mẫu...");
  if (data) {
    document.getElementById("tpl-msg").textContent =
      "Đã lưu bộ mẫu “" + ten + "” và đang dùng bộ này.";
  }
}

async function applyPreset(id) {
  const data = await goiApiMau({action: "apply", id: id}, "Đang nạp bộ mẫu...");
  if (data) {
    const b = data.presets.find(x => x.id === id);
    document.getElementById("tpl-msg").textContent = b
      ? "Đang dùng bộ “" + b.name + "”."
      : "Đã quay về mẫu gốc.";
  }
}

async function deletePreset() {
  const sel = document.getElementById("preset-pick");
  const id = sel.value;
  if (!id) return;
  const ten = sel.options[sel.selectedIndex].textContent.split("  ·  ")[0];
  if (!window.confirm("Xoá bộ mẫu “" + ten + "”?")) return;
  const data = await goiApiMau({action: "delete", id: id}, "Đang xoá...");
  if (data) {
    document.getElementById("tpl-msg").textContent =
      "Đã xoá. Bộ đang dùng quay về mẫu gốc.";
  }
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
  const data = await (await fetch(duongDan("extend"), {
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
    const response = await fetch(CHIASE
      ? "/s/" + CHIASE.token + "/data"
      : "/api/status?id=" + jobId);
    if (!response.ok) return;
    job = await response.json();
  } catch (e) { return; } // Server may be restarting after a source edit.
  if (job.error && !job.items) { return; }

  // Dang cho duyet va bang duyet dung lượt do da hien san -> KHONG ve lai.
  // draw() tao lai toan bo the trong feed, ke ca o nhap prompt; vua go vua
  // bi tao lai thi mat focus va con tro nhay ve dau, khong sua noi.
  // Trong luc cho duyet server dang dung han nen cung chang co gi moi de ve.
  const shown = document.querySelector("#feed-extra .review");
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

function buildRail(marks) {
  // Moi vach dat dung vi tri cua cau hoi do tren toan trang, nen no xep
  // thang hang voi thanh cuon chu khong chia deu mot cach vo nghia.
  const rail = document.getElementById("rail");
  rail.innerHTML = "";
  railMarks = marks;

  const docH = document.documentElement.scrollHeight;
  if (!marks.length || docH <= window.innerHeight + 120) {
    rail.classList.remove("on");
    return;
  }
  rail.classList.add("on");

  for (const m of marks) {
    m.y = m.el.getBoundingClientRect().top + window.scrollY;
    const tick = el("div", "tick" + (m.closing ? " end" : ""));
    tick.style.top = (m.y / docH * 100) + "%";
    m.tick = tick;
    tick.addEventListener("mouseenter", () => showRailTip(m));
    tick.addEventListener("mouseleave", hideRailTip);
    tick.addEventListener("click", () => {
      // Do lai vi tri ngay luc bam chu khong dung so cu: trang cao dan khi
      // cuon qua (content-visibility), nen toa do luu tu luc dung vach co
      // the da lech.
      const y = m.el.getBoundingClientRect().top + window.scrollY;
      window.scrollTo({top: Math.max(0, y - 80), behavior: "smooth"});
    });
    rail.appendChild(tick);
  }
  markRailHere();
}

function showRailTip(m) {
  const tip = document.getElementById("rail-tip");
  tip.innerHTML = "";
  if (m.num !== null) {
    const b = el("b", null, "#" + m.num);
    tip.appendChild(b);
    tip.appendChild(el("span", null, "  " + m.label));
  } else {
    tip.appendChild(el("span", null, m.label));
  }
  tip.appendChild(el("div", null, m.text));
  tip.classList.add("on");

  // Ghim cho vua man hinh, khong de tran ra ngoai
  const r = m.tick.getBoundingClientRect();
  const h = tip.offsetHeight;
  const top = Math.max(8, Math.min(window.innerHeight - h - 8,
                                   r.top + r.height / 2 - h / 2));
  tip.style.top = top + "px";
}

function hideRailTip() {
  document.getElementById("rail-tip").classList.remove("on");
}

function markRailHere() {
  // To dam vach cua cau hoi dang o gan dau man hinh nhat
  if (!railMarks.length) return;
  const moc = window.scrollY + 120;
  let chon = railMarks[0];
  for (const m of railMarks) {
    if (m.y <= moc) chon = m;
  }
  for (const m of railMarks) {
    if (m.tick) m.tick.classList.toggle("here", m === chon);
  }
}

function capNhatKhoangXuat(tong) {
  // Mac dinh dien san ca khoang (1 -> het), de bam Xuat la ra toan bo.
  // Nguoi dung sua tay mot lan thi thoi khong tu dong nua, keo mat cai ho
  // vua go — ke ca khi buoi dang chay va so luot van tang dan.
  if (!xuatTuDong) return;
  const tu = document.getElementById("export-from");
  const den = document.getElementById("export-to");
  tu.value = tong ? 1 : "";
  den.value = tong || "";
  tu.max = den.max = tong || "";
}

function toggleJump() {
  // Trang ngan thi giau di cho do vuong.
  const canScroll =
    document.documentElement.scrollHeight > window.innerHeight + 120;
  document.getElementById("jump").classList.toggle("on", canScroll);
}

function scrollToTop() {
  window.scrollTo({top: 0, behavior: "smooth"});
}

function scrollToBottom() {
  window.scrollTo({top: document.documentElement.scrollHeight,
                   behavior: "smooth"});
  // Trang CAO DAN trong luc cuon: content-visibility chi doan kich thuoc cho
  // cac luot ngoai man hinh, cuon qua toi dau trinh duyet do that toi do.
  // Vi vay dich tinh luc bam bi hut, phai chinh lai cho cham day that su.
  let lan = 0;
  const chinh = () => {
    const day = document.documentElement.scrollHeight - window.innerHeight;
    if (window.scrollY < day - 4 && ++lan < 15) {
      window.scrollTo({top: day});
      setTimeout(chinh, 120);
    }
  };
  setTimeout(chinh, 500);
}

async function pauseShow() {
  if (!jobId) return;
  document.getElementById("pause").disabled = true;
  document.getElementById("hint").textContent =
    "Se tam dung sau khi xong luot hien tai...";
  await fetch(duongDan("pause"), {method: "POST"});
}

async function retry() {
  const hint = document.getElementById("hint");
  hint.textContent = "Dang chay tiep...";
  const data = await (await fetch(duongDan("retry"),
                                  {method: "POST"})).json();
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

async function shareShow() {
  if (!jobId) return;
  const hint = document.getElementById("hint");
  const btn = document.getElementById("share");
  btn.disabled = true;
  hint.textContent = "Dang tao link chia se...";
  try {
    const d = await (await fetch("/api/share?id=" + jobId,
                                 {method: "POST"})).json();
    if (d.error) { hint.textContent = "Khong tao duoc link: " + d.error; return; }
    document.getElementById("share-qr").innerHTML = d.qr;
    document.getElementById("share-url").value = d.url;
    document.getElementById("share-control").checked = !!d.control;
    document.getElementById("share-box").hidden = false;
    hint.textContent = "";
  } finally {
    btn.disabled = false;
  }
}

async function copyShareUrl() {
  const o = document.getElementById("share-url");
  o.select();
  try {
    await navigator.clipboard.writeText(o.value);
    document.getElementById("share-copy").textContent = "Đã sao chép";
    setTimeout(() => {
      document.getElementById("share-copy").textContent = "Sao chép link";
    }, 1500);
  } catch (e) {
    document.execCommand("copy");     // trinh duyet cu / khong co quyen
  }
}

async function toggleShareControl(e) {
  const hint = document.getElementById("hint");
  const on = e.target.checked ? "1" : "0";
  const d = await (await fetch(
    "/api/share/control?id=" + jobId + "&on=" + on, {method: "POST"})).json();
  if (d.error) {
    hint.textContent = "Khong doi duoc: " + d.error;
    e.target.checked = !e.target.checked;
    return;
  }
  hint.textContent = d.control
    ? "Link nay gio bam duoc Dao sau them / Chay tiep / Tam dung / Dung han."
    : "Link nay tro lai che do chi xem.";
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
  xuatTuDong = true;          // buoi khac thi dien lai ca khoang
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
  xuatTuDong = true;

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
  await fetch(duongDan("stop"), {method: "POST"});
}

document.getElementById("to-top").addEventListener("click", scrollToTop);
document.getElementById("to-bottom").addEventListener("click", scrollToBottom);
window.addEventListener("scroll", () => { toggleJump(); markRailHere(); },
                        {passive: true});
window.addEventListener("resize", () => { toggleJump(); buildRail(railMarks); });
document.getElementById("go").addEventListener("click", start);
document.getElementById("pause").addEventListener("click", pauseShow);
document.getElementById("stop").addEventListener("click", stop);
document.getElementById("export").addEventListener("click", exportShow);
document.getElementById("share").addEventListener("click", shareShow);
document.getElementById("share-copy").addEventListener("click", copyShareUrl);
document.getElementById("share-control")
        .addEventListener("change", toggleShareControl);
document.getElementById("share-close").addEventListener("click", () => {
  document.getElementById("share-box").hidden = true;
});
["export-from", "export-to"].forEach(id =>
  document.getElementById(id).addEventListener(
    "input", () => { xuatTuDong = false; }));
document.getElementById("open-prompts").addEventListener("click", async () => {
  await loadPrompts();
  document.getElementById("sheet").classList.add("on");
});
document.getElementById("tpl-close").addEventListener("click", () => {
  document.getElementById("sheet").classList.remove("on");
});
document.getElementById("tpl-save").addEventListener("click", savePrompts);
document.getElementById("preset-save-as")
        .addEventListener("click", savePresetAs);
document.getElementById("preset-delete")
        .addEventListener("click", deletePreset);
document.getElementById("preset-pick")
        .addEventListener("change", e => applyPreset(e.target.value));
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
  if (CHIASE) {
    // Che do chi-xem: khong goi /api/* (server chan tu may khac), chi doc
    // bien ban cua dung buoi duoc chia se.
    document.body.classList.add("chi-xem");
    jobId = CHIASE.token;
    await poll();
    timer = setInterval(poll, 2000);   // cham hon vi qua mang
    return;
  }
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
  toggleJump();
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

    def tai_cho(self) -> bool:
        """
        Yeu cau nay den tu chinh may nay khong?

        Server mo ra ca mang noi bo de dien thoai xem duoc bien ban, nhung
        MOI THAO TAC DIEU KHIEN chi cho phep tu may nay. Neu khong, ai trong
        mang cung bam duoc "Bat dau" va tieu han muc tai khoan AI cua ban.
        """
        return self.client_address[0] in ("127.0.0.1", "::1", "localhost")

    def tu_choi_xa(self) -> bool:
        """Chan yeu cau tu may khac. Tra ve True neu da chan."""
        if self.tai_cho():
            return False
        self._send(403, json.dumps(
            {"error": "Chức năng này chỉ dùng được trên máy chạy server. "
                      "Link chia sẻ chỉ để xem biên bản."}, ensure_ascii=False))
        return True

    def trang_chia_se(self, token: str):
        """Trang xem mot buoi, mo bang link chia se."""
        show_id, dieu_khien = share_by_token(token)
        if not show_id:
            return self._send(404, "<h1>Link chia sẻ không còn hiệu lực.</h1>",
                              "text/html; charset=utf-8")
        cfg = json.dumps({"token": token, "control": dieu_khien},
                         ensure_ascii=False)
        return self._send(200, PAGE.replace('"__SHARE_CFG__"', cfg),
                          "text/html; charset=utf-8")

    def viec_chia_se(self, token: str, viec: str):
        """
        Thiet bi khac bam nut dieu khien tren link chia se.

        Chi nhan nhung viec trong SHARE_ACTIONS, va chi khi link do da duoc
        bat quyen dieu khien tu may nay. Ma buoi lay tu token chu khong lay
        tu tham so, nen khong the tro sang buoi khac.
        """
        show_id, dieu_khien = share_by_token(token)
        if not show_id:
            return self._send(404, '{"error":"link khong con hieu luc"}')
        if not dieu_khien:
            return self._send(403, json.dumps(
                {"error": "Link này chỉ để xem. Bật “cho phép điều khiển” "
                          "ở máy chạy server nếu muốn bấm từ đây."},
                ensure_ascii=False))
        if viec not in SHARE_ACTIONS:
            return self._send(403, json.dumps(
                {"error": f"Việc “{viec}” chỉ làm được trên máy chạy server."},
                ensure_ascii=False))

        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            data = {}

        try:
            if viec == "extend":
                extend_job(show_id,
                           max(1, min(20, int(data.get("turns") or 3))),
                           data.get("focus") or "",
                           bool(data.get("review")))
            elif viec == "retry":
                retry_job(show_id)
            elif viec == "pause":
                with LOCK:
                    JOBS[show_id]["pause_requested"] = True
            elif viec == "stop":
                with LOCK:
                    JOBS[show_id]["stop_requested"] = True
            elif viec == "review":
                answer_review(show_id, data.get("action") or "send",
                              data.get("text"))
        except Exception as e:
            return self._send(400, json.dumps({"error": str(e)},
                                              ensure_ascii=False))
        return self._send(200, '{"ok":true}')

    def do_GET(self):
        path = urlparse(self.path).path

        # Link chia se: mo duoc tu bat ky thiet bi nao trong mang
        if path.startswith("/s/"):
            phan = path[3:].split("/")
            token = phan[0]
            if len(phan) == 1:
                return self.trang_chia_se(token)
            if len(phan) == 2 and phan[1] == "data":
                show_id = show_by_token(token)
                with LOCK:
                    job = JOBS.get(show_id) if show_id else None
                    body = (json.dumps(job, ensure_ascii=False) if job
                            else '{"error":"khong co buoi nay"}')
                return self._send(200 if job else 404, body)
            return self._send(404, '{"error":"khong co trang nay"}')

        if path == dev_reload.VERSION_PATH:
            return self._send(200, json.dumps({"version": dev_reload.VERSION}))

        # Tu day tro xuong la giao dien dieu khien — chi may nay dung duoc
        if self.tu_choi_xa():
            return
        if path == "/":
            return self._send(200, PAGE.replace('"__SHARE_CFG__"', "null"),
                              "text/html; charset=utf-8")
        if path == "/api/tabs":
            return self._send(200, json.dumps(tabs_status(), ensure_ascii=False))
        if path == "/api/shows":
            return self._send(200, json.dumps(list_shows(), ensure_ascii=False))
        if path == "/api/prompts":
            return self._send(200, json.dumps(
                {"fields": prompt_list(), **preset_list()}, ensure_ascii=False))
        if path == "/api/status":
            job_id = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            with LOCK:
                job = JOBS.get(job_id)
                body = json.dumps(job, ensure_ascii=False) if job else '{"error":"khong co job"}'
            return self._send(200 if job else 404, body)
        self._send(404, '{"error":"khong co trang nay"}')

    def do_POST(self):
        path = urlparse(self.path).path

        # Nut dieu khien bam tu link chia se — tu kiem tra quyen ben trong
        if path.startswith("/s/"):
            phan = path[3:].split("/")
            if len(phan) == 2:
                return self.viec_chia_se(phan[0], phan[1])
            return self._send(404, '{"error":"khong co trang nay"}')

        if self.tu_choi_xa():
            return

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
                viec = data.get("action") or "save"
                fields = data.get("prompts") or {}

                if viec == "save":              # ghi vao bo dang dung
                    for name, text in fields.items():
                        save_prompt(name, text)

                elif viec == "save_preset":     # luu thanh bo mau co ten
                    for name, text in fields.items():
                        save_prompt(name, text)
                    pid = preset_save(data.get("name"), fields,
                                      data.get("id") or None)
                    set_setting("active_preset", pid)

                elif viec == "apply":           # nap mot bo mau ra dung
                    preset_apply(data.get("id") or "")

                elif viec == "delete":
                    preset_delete(data.get("id") or "")

                else:
                    raise ValueError(f"khong hieu action {viec!r}")
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps(
                {"fields": prompt_list(), **preset_list()}, ensure_ascii=False))

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

        if path == "/api/share":
            job_id = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            try:
                tin = share_info(job_id)
                url = f"http://{lan_ip()}:{PORT}/s/{tin['token']}"
                ket_qua = {"url": url, "qr": qr_svg(url),
                           "control": tin["control"]}
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps(ket_qua, ensure_ascii=False))

        if path == "/api/share/control":
            q = parse_qs(urlparse(self.path).query)
            job_id = (q.get("id") or [""])[0]
            try:
                set_share_control(job_id, (q.get("on") or ["0"])[0] == "1")
                tin = share_info(job_id)
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)},
                                                  ensure_ascii=False))
            return self._send(200, json.dumps({"control": tin["control"]}))

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
        server = Server((HOST, PORT), Handler)
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
