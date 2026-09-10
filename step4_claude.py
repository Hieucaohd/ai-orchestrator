"""
BUOC 4 — Dieu khien Claude qua Chrome dang dang nhap (CDP).

Cach chay:
  1. Chay launch_chrome.bat  ->  Chrome mo ra, dang nhap Claude (lan dau)
  2. Chay:  venv\\Scripts\\python.exe step4_claude.py "cau hoi cua ban"
"""

import sys
import time
from playwright.sync_api import sync_playwright, Page

CDP_URL = "http://localhost:9222"

# --- Selector rieng cho Claude (day la phan hay phai chinh khi web doi giao dien) ---
URL_MATCH     = "claude.ai"                                          # nhan dien tab Claude
INPUT_BOX     = 'div[contenteditable="true"][data-testid="chat-input"]'  # o nhap lieu
SEND_BUTTON   = 'button[data-testid="chat-input-send"]'              # nut gui
STOP_BUTTON   = 'button[aria-label="Stop response"]'                 # nut dung (hien khi dang tra loi)
ASSISTANT_MSG = "div.font-claude-response"                           # cac bong bong tra loi cua AI
REPLY_TEXT    = ".standard-markdown"                                 # chi lay phan tra loi, bo khoi "Thought for Xs"


def find_page(browser, url_match: str) -> Page:
    """Duyet tat ca tab trong moi cua so Chrome, tim tab co URL khop."""
    for context in browser.contexts:
        for page in context.pages:
            if url_match in page.url:
                return page
    raise RuntimeError(
        f"Khong tim thay tab nao co '{url_match}'. "
        f"Ban da mo tab Claude trong cua so Chrome (launch_chrome.bat) chua?"
    )


def count_replies(page: Page) -> int:
    """Dem so bong bong tra loi hien co. Goi TRUOC khi gui de biet moc so sanh."""
    return page.locator(ASSISTANT_MSG).count()


def send_message(page: Page, text: str):
    """Go text vao o nhap va gui."""
    box = page.locator(INPUT_BOX).first
    box.click()
    box.fill(text)
    page.locator(SEND_BUTTON).first.click()


def wait_until_done(page: Page, before: int, timeout_s: int = 180):
    """
    Doi Claude tra loi xong.
    Logic: 1) doi nut Stop xuat hien  -> da bat dau tra loi
           2) doi bong bong tra loi moi xuat hien
           3) doi nut Stop bien mat   -> da tra loi xong
           4) doi text render ra      -> tranh doc phai bong bong con rong
    """
    # 1) doi nut Stop xuat hien. QUAN TRONG: neu bo qua buoc nay thi buoc (3)
    #    se khop ngay lap tuc (chua co nut Stop nao = da "hidden") -> doc nham.
    try:
        page.locator(STOP_BUTTON).wait_for(state="visible", timeout=15000)
    except Exception:
        pass  # doi khi tra loi qua nhanh, bo qua

    # 2) doi bong bong tra loi moi
    page.wait_for_function(
        "(args) => document.querySelectorAll(args.sel).length > args.n",
        arg={"sel": ASSISTANT_MSG, "n": before},
        timeout=timeout_s * 1000,
    )

    # 3) doi nut Stop bien mat
    page.locator(STOP_BUTTON).wait_for(state="hidden", timeout=timeout_s * 1000)

    # 4) doi noi dung thuc su hien ra
    page.wait_for_function(
        """() => {
             const r = [...document.querySelectorAll('div.font-claude-response')].pop();
             if (!r) return false;
             const body = r.querySelector('.standard-markdown') || r;
             return (body.innerText || '').trim().length > 0;
           }""",
        timeout=30000,
    )
    time.sleep(0.5)  # cho DOM on dinh


def read_last_reply(page: Page) -> str:
    """Doc noi dung bong bong tra loi cuoi cung cua Claude."""
    messages = page.locator(ASSISTANT_MSG)
    count = messages.count()
    if count == 0:
        return "(chua co tra loi nao)"
    last = messages.nth(count - 1)
    body = last.locator(REPLY_TEXT)
    text = body.first.inner_text() if body.count() > 0 else last.inner_text()
    return text.strip()


def main():
    question = "Xin chao! Hay tu gioi thieu ban trong 1 cau ngan gon."
    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])

    with sync_playwright() as p:
        print(f"[*] Ket noi toi Chrome tai {CDP_URL} ...")
        browser = p.chromium.connect_over_cdp(CDP_URL)

        page = find_page(browser, URL_MATCH)
        print(f"[*] Da tim thay tab: {page.url}")
        page.bring_to_front()

        before = count_replies(page)
        print(f"[*] Gui cau hoi: {question!r}")
        send_message(page, question)

        print("[*] Dang doi Claude tra loi ...")
        wait_until_done(page, before)

        reply = read_last_reply(page)
        print("\n===== CLAUDE TRA LOI =====")
        print(reply)
        print("==========================")


if __name__ == "__main__":
    main()
