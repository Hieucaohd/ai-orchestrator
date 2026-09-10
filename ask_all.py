r"""
BANG DIEU KHIEN (console) — go 1 cau hoi, gui cho ca 4 AI CUNG LUC.

Cach chay:
  1. Chay launch_chrome.bat, dang nhap du 4 site, mo san 1 notebook trong NotebookLM
  2. Chay:  venv\Scripts\python.exe ask_all.py

Lenh trong console:
  /all                 gui cho tat ca (mac dinh)
  /only claude gemini  chi gui cho nhung AI duoc liet ke
  /who                 xem dang gui cho nhung ai
  /quit                thoat

Moi phien duoc luu lai thanh 1 file markdown trong thu muc logs/.
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path

import adapters

LOG_DIR = Path(__file__).parent / "logs"

# Mau cho terminal. Windows Terminal / VS Code deu hieu ANSI.
DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
GREEN, RED, CYAN = "\033[32m", "\033[31m", "\033[36m"


def setup_console():
    """Bat UTF-8 cho stdout, neu khong tieng Viet se loi tren Windows."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def open_log() -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.md"
    path.write_text(f"# Phien {datetime.now():%Y-%m-%d %H:%M:%S}\n", encoding="utf-8")
    return path


def append_log(path: Path, question: str, results: list):
    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n## {question}\n")
        for r in results:
            f.write(f"\n### {r['name']} ({r['seconds']}s)\n\n{r['reply']}\n")


def print_reply(r: dict):
    mark = f"{GREEN}OK{RESET}" if r["ok"] else f"{RED}LOI{RESET}"
    print(f"\n{BOLD}{CYAN}== {r['name']} =={RESET} {mark} {DIM}{r['seconds']}s{RESET}")
    print(r["reply"])


async def run_turn(browser, question: str, targets: list, log: Path):
    """Gui 1 cau hoi cho cac AI da chon, in ket qua theo thu tu ai xong truoc."""
    names = ", ".join(a.name for a in targets)
    print(f"{DIM}[*] Dang gui cho: {names} ...{RESET}")

    def on_event(name, status, _text):
        if status == "running":
            print(f"{DIM}    -> {name}: dang cho{RESET}")

    results = await adapters.ask_all(browser, question, targets, on_event=on_event)
    for r in results:
        print_reply(r)

    append_log(log, question, results)
    print(f"\n{DIM}[*] Da luu vao {log.name}{RESET}")
    return results


def handle_command(line: str, targets: list):
    """
    Xu ly cac lenh bat dau bang '/'.
    Tra ve (targets_moi, con_chay_tiep).
    """
    parts = line.split()
    cmd = parts[0].lower()

    if cmd in ("/quit", "/exit"):
        return targets, False

    if cmd == "/all":
        targets = list(adapters.ALL)
        print(f"{DIM}[*] Se gui cho tat ca: {', '.join(a.name for a in targets)}{RESET}")

    elif cmd == "/only":
        if len(parts) < 2:
            print(f"{RED}[!] Vi du: /only claude gemini{RESET}")
        else:
            try:
                targets = adapters.pick(parts[1:])
                print(f"{DIM}[*] Se gui cho: {', '.join(a.name for a in targets)}{RESET}")
            except KeyError as e:
                print(f"{RED}[!] Khong co AI ten {e}. Chon trong: "
                      f"{', '.join(adapters.BY_KEY)}{RESET}")

    elif cmd == "/who":
        print(f"{DIM}[*] Dang gui cho: {', '.join(a.name for a in targets)}{RESET}")

    else:
        print(f"{RED}[!] Lenh la. Co: /all  /only  /who  /quit{RESET}")

    return targets, True


async def main():
    setup_console()
    log = open_log()

    print(f"{DIM}[*] Ket noi toi Chrome tai {adapters.CDP_URL} ...{RESET}")
    pw, browser = await adapters.connect()

    # Bao ngay tab nao thieu, thay vi de den luc gui moi bao loi
    ready, missing = [], []
    for ad in adapters.ALL:
        try:
            ad.find_page(browser)
            ready.append(ad.name)
        except RuntimeError:
            missing.append(ad.name)
    print(f"[*] Tab san sang: {GREEN}{', '.join(ready) or 'khong co'}{RESET}")
    if missing:
        print(f"[*] Thieu tab:    {RED}{', '.join(missing)}{RESET}")

    targets = list(adapters.ALL)
    print(f"{DIM}    Go cau hoi roi Enter. Lenh: /all  /only  /who  /quit{RESET}")

    try:
        while True:
            try:
                line = (await asyncio.to_thread(input, f"\n{BOLD}> {RESET}")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue

            if line.startswith("/"):
                targets, keep_going = handle_command(line, targets)
                if not keep_going:
                    break
                continue

            await run_turn(browser, line, targets, log)
    finally:
        # Chi ngat ket noi, KHONG goi browser.close() — se dong luon cua so
        # Chrome cua ban cung tat ca tab dang dang nhap.
        await pw.stop()
        print(f"\n{DIM}[*] Da thoat. Nhat ky: {log}{RESET}")


if __name__ == "__main__":
    asyncio.run(main())
