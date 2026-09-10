r"""
HOI DONG TRANH LUAN TU VI — orchestrator cho 4 AI.

4 AI khong noi truc tiep voi nhau. File nay dong vai "phong hop + thu ky":
giu bien ban chung, sau moi vong dua y kien cua cac AI cho nhau doc.

VAI TRO
  ChatGPT     vong 1 la Luan su, tu vong 2 tro thanh MODERATOR (giu Shared State)
  Gemini      Independent Analyst — xay mo hinh giai thich doc lap
  Claude      Challenger / Red Team — tim cach chung minh ket luan hien tai sai
  NotebookLM  Evidence Auditor — chi doi chieu voi nguon trong notebook

CHUAN BI
  1. Chay launch_chrome.bat, dang nhap du 4 site
  2. Mo san notebook chua tai lieu Tu Vi trong NotebookLM
  3. Dan lá số (dang chu) vao case/la_so.md
  4. Cat cac giai doan su kien vao case/reveal/<ten>.md

CHAY (moi lenh la 1 vong, chay xong doc ket qua roi moi chay tiep)
  venv\Scripts\python.exe council.py setup           gui prompt nen + vai tro
  venv\Scripts\python.exe council.py r1              vong 1: phan tich doc lap
  venv\Scripts\python.exe council.py r2              vong 2: phan bien cheo
  venv\Scripts\python.exe council.py predict 2020-2022   du doan kin
  venv\Scripts\python.exe council.py reveal  2020-2022   cong bo su kien that
  venv\Scripts\python.exe council.py redteam         vong 4: red team
  venv\Scripts\python.exe council.py verdict         vong 5: ket luan
  venv\Scripts\python.exe council.py status          xem dang o dau

Tat ca bien ban nam trong council_state/.
"""

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import adapters
import council_prompts as P

ROOT = Path(__file__).parent
CASE_DIR = ROOT / "case"
REVEAL_DIR = CASE_DIR / "reveal"
STATE_DIR = ROOT / "council_state"
ROUNDS_DIR = STATE_DIR / "rounds"
SEALED_DIR = STATE_DIR / "sealed"

MODERATOR = "ChatGPT"
PANEL = ["Gemini", "Claude", "NotebookLM"]   # thanh vien tranh luan tu vong 2
EVERYONE = [MODERATOR] + PANEL

MAX_QUOTE = 6000     # cat bot bien ban moi AI khi gui lai, tranh prompt qua dai

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
GREEN, RED, CYAN, YELLOW = "\033[32m", "\033[31m", "\033[36m", "\033[33m"


# --------------------------------------------------------------- tien ich

def setup_console():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def load_state() -> dict:
    path = STATE_DIR / "state.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"done": [], "last_tag": None, "started": None}


def save_state(state: dict):
    STATE_DIR.mkdir(exist_ok=True)
    (STATE_DIR / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def mark_done(state: dict, tag: str):
    if tag not in state["done"]:
        state["done"].append(tag)
    state["last_tag"] = tag
    save_state(state)


def read_case() -> str:
    """Doc lá số. Bao loi ro neu ban chua dan noi dung vao."""
    path = CASE_DIR / "la_so.md"
    if not path.exists():
        raise SystemExit(f"[!] Chua co {path}. Chay 'council.py init' truoc.")
    text = path.read_text(encoding="utf-8").strip()
    if len(text) < 200 or "DÁN LÁ SỐ" in text:
        raise SystemExit(
            f"[!] {path} van con la file mau. Hay dan noi dung lá số (dang chu) vao do.")
    return text


def round_dir(tag: str) -> Path:
    d = ROUNDS_DIR / tag
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_answer(tag: str, name: str, text: str):
    (round_dir(tag) / f"{name}.md").write_text(text, encoding="utf-8")


def read_answer(tag: str, name: str) -> str:
    path = ROUNDS_DIR / tag / f"{name}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def read_shared_state() -> str:
    path = STATE_DIR / "shared_state.md"
    return path.read_text(encoding="utf-8") if path.exists() else "(chưa có Shared State)"


def others_transcript(tag: str, exclude: str) -> str:
    """Ghep bien ban cua cac AI KHAC o vong tag, moi ban cat bot cho gon."""
    blocks = []
    for name in EVERYONE:
        if name == exclude:
            continue
        text = read_answer(tag, name)
        if not text:
            continue
        if len(text) > MAX_QUOTE:
            text = text[:MAX_QUOTE] + "\n\n[... đã cắt bớt cho gọn ...]"
        blocks.append(f"----- {name} nói -----\n{text}")
    return "\n\n".join(blocks) if blocks else "(chưa có phát biểu nào)"


def append_transcript(title: str, blocks: dict):
    STATE_DIR.mkdir(exist_ok=True)
    with (STATE_DIR / "transcript.md").open("a", encoding="utf-8") as f:
        f.write(f"\n\n# {title}  ({datetime.now():%Y-%m-%d %H:%M})\n")
        for name, text in blocks.items():
            f.write(f"\n## {name}\n\n{text}\n")


def parse_sections(text: str) -> dict:
    """Tach cac khoi dang '=== TEN ===' trong cau tra loi cua Moderator."""
    parts = re.split(r"^\s*=+\s*(.+?)\s*=+\s*$", text, flags=re.MULTILINE)
    if len(parts) < 3:
        return {}
    out = {}
    for i in range(1, len(parts) - 1, 2):
        out[parts[i].strip().upper()] = parts[i + 1].strip()
    return out


# --------------------------------------------------------------- gui/nhan

async def ask_many(browser, jobs: dict) -> dict:
    """Gui prompt rieng cho tung AI, chay song song."""
    async def one(name: str, prompt: str):
        ad = adapters.BY_KEY[name.lower()]
        reminder = P.ROLE_REMINDER.get(name)
        if reminder:
            prompt = f"{reminder}\n\n{prompt}"
        t0 = time.perf_counter()
        print(f"{DIM}    -> {name}: dang gui ({len(prompt)} ky tu) ...{RESET}")
        try:
            reply = await ad.ask(browser, prompt)
            ok = True
        except Exception as e:
            reply = f"(LOI: {type(e).__name__}: {e})"
            ok = False
        dt = round(time.perf_counter() - t0, 1)
        mark = f"{GREEN}xong{RESET}" if ok else f"{RED}LOI{RESET}"
        print(f"    <- {name}: {mark} {DIM}{dt}s, {len(reply)} ky tu{RESET}")
        return name, ok, reply

    results = await asyncio.gather(*(one(n, p) for n, p in jobs.items()))
    return {name: {"ok": ok, "reply": reply} for name, ok, reply in results}


async def ask_one(browser, name: str, prompt: str) -> str:
    return (await ask_many(browser, {name: prompt}))[name]["reply"]


def store(tag: str, answers: dict) -> dict:
    """Luu cau tra loi ra file va vao transcript."""
    texts = {}
    for name, r in answers.items():
        save_answer(tag, name, r["reply"])
        texts[name] = r["reply"]
    append_transcript(tag, texts)
    failed = [n for n, r in answers.items() if not r["ok"]]
    if failed:
        print(f"{RED}[!] Loi o: {', '.join(failed)} — xem council_state/rounds/{tag}/{RESET}")
    return texts


async def moderator_update(browser, tag: str):
    """Cho Moderator doc ca vong vua roi, tao Shared State + cau hoi rieng."""
    print(f"{DIM}[*] Moderator dang tong hop Shared State ...{RESET}")
    answers = "\n\n".join(
        f"----- {n} nói -----\n{read_answer(tag, n)[:MAX_QUOTE]}"
        for n in EVERYONE if read_answer(tag, n))

    prompt = P.fill(P.MOD_UPDATE, ANSWERS=answers, PREV_STATE=read_shared_state())
    reply = await ask_one(browser, MODERATOR, prompt)

    save_answer(tag, "_moderator_sync", reply)
    sections = parse_sections(reply)

    shared = sections.get("SHARED STATE", reply)
    (STATE_DIR / "shared_state.md").write_text(shared, encoding="utf-8")
    (round_dir(tag) / "_shared_state.md").write_text(shared, encoding="utf-8")

    questions = {}
    for name in PANEL:
        key = f"HỎI {name.upper()}"
        if key in sections:
            questions[name] = sections[key]
    (round_dir(tag) / "_questions.json").write_text(
        json.dumps(questions, ensure_ascii=False, indent=1), encoding="utf-8")

    if not sections:
        print(f"{YELLOW}[!] Moderator khong tra loi dung dinh dang. Da dung ca "
              f"cau tra loi lam Shared State, cau hoi rieng se de trong.{RESET}")
    return questions


def load_questions(tag: str) -> dict:
    path = ROUNDS_DIR / tag / "_questions.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------- cac vong

async def cmd_setup(browser, state, _args):
    la_so = read_case()
    jobs = {n: P.fill(P.SETUP, BASE=P.BASE, ROLE=P.ROLES[n], LA_SO=la_so)
            for n in EVERYONE}
    store("setup", await ask_many(browser, jobs))
    state["started"] = datetime.now().isoformat(timespec="seconds")
    mark_done(state, "setup")
    print(f"\n{GREEN}[OK]{RESET} Da giao vai tro. Tiep theo: council.py r1")


async def cmd_r1(browser, state, _args):
    """Vong 1: ca 4 phan tich doc lap, KHONG ai biet y kien cua ai."""
    jobs = {n: P.ROUND1 for n in EVERYONE}
    store("r1", await ask_many(browser, jobs))

    # ChatGPT nop bai xong moi doi vai thanh Moderator
    print(f"{DIM}[*] Chuyen ChatGPT sang vai Moderator ...{RESET}")
    await ask_one(browser, MODERATOR, P.MOD_SWITCH)

    await moderator_update(browser, "r1")
    mark_done(state, "r1")
    print(f"\n{GREEN}[OK]{RESET} Xem council_state/shared_state.md roi chay: council.py r2")


async def cmd_r2(browser, state, _args):
    """Vong 2: 3 thanh vien doc bien ban vong 1 va phan bien cheo."""
    shared = read_shared_state()
    questions = load_questions("r1")
    jobs = {
        n: P.fill(P.ROUND2,
                  SHARED_STATE=shared,
                  OTHERS=others_transcript("r1", exclude=n),
                  QUESTIONS=questions.get(n, "(Moderator không có câu hỏi riêng)"))
        for n in PANEL
    }
    store("r2", await ask_many(browser, jobs))
    await moderator_update(browser, "r2")
    mark_done(state, "r2")
    print(f"\n{GREEN}[OK]{RESET} Tiep theo: council.py predict <ten-giai-doan>")


def reveal_file(slug: str) -> Path:
    path = REVEAL_DIR / f"{slug}.md"
    if not path.exists():
        available = ", ".join(p.stem for p in REVEAL_DIR.glob("*.md")) or "(chua co file nao)"
        raise SystemExit(f"[!] Khong thay {path}\n    Cac giai doan dang co: {available}")
    return path


def period_title(slug: str) -> str:
    """Chi doc DONG TIEU DE cua file su kien — khong doc noi dung ben trong."""
    for line in reveal_file(slug).read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("#"):
            return line.lstrip("#").strip()
    return slug


async def cmd_predict(browser, state, args):
    """Du doan kin: hoi TRUOC khi cong bo su kien that."""
    slug = args.period
    title = period_title(slug)   # chi lay tieu de, KHONG lo noi dung
    print(f"{DIM}[*] Giai doan: {title}{RESET}")

    jobs = {n: P.fill(P.PREDICT, PERIOD=title,
                      ROLE_TASK=P.PREDICT_ROLE_TASK.get(n, ""))
            for n in PANEL}
    texts = store(f"predict-{slug}", await ask_many(browser, jobs))

    SEALED_DIR.mkdir(parents=True, exist_ok=True)
    sealed = SEALED_DIR / slug
    sealed.mkdir(exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    for name, text in texts.items():
        (sealed / f"{name}.md").write_text(
            f"<!-- niêm phong lúc {stamp} -->\n\n{text}", encoding="utf-8")

    mark_done(state, f"predict-{slug}")
    print(f"\n{GREEN}[OK]{RESET} Da niem phong du doan vao {sealed}")
    print(f"     Tiep theo: council.py reveal {slug}")


async def cmd_reveal(browser, state, args):
    """Cong bo su kien that, bat tung AI tu cham diem du doan cua chinh no."""
    slug = args.period
    events = reveal_file(slug).read_text(encoding="utf-8").strip()
    sealed = SEALED_DIR / slug
    if not sealed.exists():
        raise SystemExit(f"[!] Chua co du doan kin cho '{slug}'. "
                         f"Chay 'council.py predict {slug}' truoc.")

    jobs = {}
    for n in PANEL:
        path = sealed / f"{n}.md"
        if not path.exists():
            continue
        jobs[n] = P.fill(P.REVEAL, EVENTS=events,
                         SEALED=path.read_text(encoding="utf-8"))

    tag = f"reveal-{slug}"
    store(tag, await ask_many(browser, jobs))
    await moderator_update(browser, tag)
    mark_done(state, tag)
    print(f"\n{GREEN}[OK]{RESET} Tiep theo: council.py redteam (hoac predict giai doan sau)")


async def cmd_redteam(browser, state, _args):
    jobs = {n: P.fill(P.REDTEAM, SHARED_STATE=read_shared_state()) for n in PANEL}
    store("redteam", await ask_many(browser, jobs))
    await moderator_update(browser, "redteam")
    mark_done(state, "redteam")
    print(f"\n{GREEN}[OK]{RESET} Tiep theo: council.py verdict")


async def cmd_verdict(browser, state, _args):
    last = state.get("last_tag") or "redteam"
    prompt = P.fill(P.VERDICT,
                    SHARED_STATE=read_shared_state(),
                    OTHERS=others_transcript(last, exclude=MODERATOR))
    reply = await ask_one(browser, MODERATOR, prompt)
    save_answer("verdict", MODERATOR, reply)
    append_transcript("verdict", {MODERATOR: reply})
    (STATE_DIR / "verdict.md").write_text(reply, encoding="utf-8")
    mark_done(state, "verdict")
    print(f"\n{GREEN}[OK]{RESET} Ket luan cuoi: council_state/verdict.md")


# --------------------------------------------------------------- khong can Chrome

def cmd_init(_args):
    """Tao san khung thu muc va file mau."""
    CASE_DIR.mkdir(exist_ok=True)
    REVEAL_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(exist_ok=True)

    la_so = CASE_DIR / "la_so.md"
    if not la_so.exists():
        la_so.write_text(
            "# Lá số\n\n"
            "DÁN LÁ SỐ Ở ĐÂY (dạng chữ, không phải ảnh).\n\n"
            "Cần có: giờ/ngày/tháng/năm sinh, giới tính, mệnh cục,\n"
            "12 cung với chính tinh - phụ tinh - sát tinh, Tuần/Triệt,\n"
            "Tứ Hóa, và bảng đại vận.\n", encoding="utf-8")
        print(f"  tao {la_so}")

    mau = REVEAL_DIR / "_mau.md"
    if not mau.exists():
        mau.write_text(
            "# Giai đoạn 2020-2022 (18-20 tuổi)\n\n"
            "Dòng tiêu đề phía trên là thứ DUY NHẤT các AI được thấy ở bước predict.\n"
            "Toàn bộ phần dưới chỉ được gửi ở bước reveal.\n\n"
            "| Ngày dương | Sự kiện |\n"
            "| --- | --- |\n"
            "| 10/10/2020 | ... |\n", encoding="utf-8")
        print(f"  tao {mau}")

    print(f"\n{GREEN}[OK]{RESET} Da tao khung. Viec cua ban:")
    print(f"  1. Dan lá số vao {la_so}")
    print(f"  2. Cat su kien theo tung giai doan vao {REVEAL_DIR}/<ten>.md")
    print(f"     (theo dung mau cua _mau.md — dong '# ...' la tieu de khong lo noi dung)")
    print(f"  3. Chay: council.py setup")


def cmd_status(_args):
    state = load_state()
    print(f"{BOLD}Hoi dong tranh luan — trang thai{RESET}")
    print(f"  bat dau : {state.get('started') or '(chua setup)'}")
    print(f"  da xong : {', '.join(state['done']) or '(chua co vong nao)'}")
    print(f"  vong cuoi: {state.get('last_tag') or '-'}")

    if REVEAL_DIR.exists():
        slugs = [p.stem for p in REVEAL_DIR.glob("*.md") if not p.stem.startswith("_")]
        print(f"  giai doan co san: {', '.join(slugs) or '(chua co)'}")
    if SEALED_DIR.exists():
        for d in sorted(SEALED_DIR.iterdir()):
            if d.is_dir():
                print(f"  du doan da niem phong [{d.name}]: "
                      f"{', '.join(p.stem for p in d.glob('*.md'))}")
    ss = STATE_DIR / "shared_state.md"
    if ss.exists():
        print(f"\n{DIM}--- Shared State hien tai (200 ky tu dau) ---{RESET}")
        print(ss.read_text(encoding="utf-8")[:200].strip() + " ...")


# --------------------------------------------------------------- main

NEEDS_BROWSER = {
    "setup": cmd_setup, "r1": cmd_r1, "r2": cmd_r2,
    "predict": cmd_predict, "reveal": cmd_reveal,
    "redteam": cmd_redteam, "verdict": cmd_verdict,
}


async def run_with_browser(fn, args):
    state = load_state()
    print(f"{DIM}[*] Ket noi toi Chrome tai {adapters.CDP_URL} ...{RESET}")
    pw, browser = await adapters.connect()
    try:
        missing = []
        for name in EVERYONE:
            ad = adapters.BY_KEY[name.lower()]
            try:
                ad.check_ready(ad.find_page(browser))
            except Exception as e:
                missing.append(f"{name}: {str(e)[:120]}")
        if missing:
            print(f"{RED}[!] Chua san sang:{RESET}")
            for m in missing:
                print(f"    {m}")
            raise SystemExit(1)

        await fn(browser, state, args)
    finally:
        # Chi ngat ket noi, KHONG dong Chrome cua ban
        await pw.stop()


def main():
    setup_console()
    parser = argparse.ArgumentParser(
        description="Hoi dong 4 AI tranh luan ve mot la so Tu Vi.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="tao khung thu muc case/ va file mau")
    sub.add_parser("status", help="xem dang o vong nao")
    sub.add_parser("setup", help="gui prompt nen + vai tro cho 4 AI")
    sub.add_parser("r1", help="vong 1: phan tich doc lap")
    sub.add_parser("r2", help="vong 2: phan bien cheo")
    p_pred = sub.add_parser("predict", help="du doan kin cho 1 giai doan")
    p_pred.add_argument("period", help="ten file trong case/reveal/ (khong co .md)")
    p_rev = sub.add_parser("reveal", help="cong bo su kien that cua giai doan do")
    p_rev.add_argument("period", help="ten file trong case/reveal/ (khong co .md)")
    sub.add_parser("redteam", help="vong 4: red team")
    sub.add_parser("verdict", help="vong 5: ket luan cuoi")

    args = parser.parse_args()

    if args.cmd == "init":
        return cmd_init(args)
    if args.cmd == "status":
        return cmd_status(args)

    asyncio.run(run_with_browser(NEEDS_BROWSER[args.cmd], args))


if __name__ == "__main__":
    main()
