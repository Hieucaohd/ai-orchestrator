"""
BUOC 1 — Dieu khien ChatGPT qua Chrome dang dang nhap (CDP).

Cach chay:
  1. Chay launch_chrome.bat  ->  Chrome mo ra, dang nhap ChatGPT (lan dau)
  2. Chay:  venv\\Scripts\\python.exe step1_chatgpt.py
Muc tieu: go 1 cau hoi vao ChatGPT, doi tra loi xong, in ket qua ra man hinh.
"""

import sys
import time
from playwright.sync_api import sync_playwright, Page

CDP_URL = "http://localhost:9222"

# --- Selector rieng cho ChatGPT (day la phan hay phai chinh khi web doi giao dien) ---
URL_MATCH        = "chatgpt.com"                         # nhan dien tab ChatGPT
INPUT_BOX        = "#prompt-textarea"                    # o nhap lieu (contenteditable)
SEND_BUTTON      = "button[data-testid='send-button']"   # nut gui
STOP_BUTTON      = "button[data-testid='stop-button']"   # nut dung (hien khi dang tra loi)
ASSISTANT_MSG    = "[data-message-author-role='assistant']"  # cac bong bong tra loi cua AI


def find_page(browser, url_match: str) -> Page:
    """Duyet tat ca tab trong moi cua so Chrome, tim tab co URL khop."""
    for context in browser.contexts:
        for page in context.pages:
            if url_match in page.url:
                return page
    raise RuntimeError(
        f"Khong tim thay tab nao co '{url_match}'. "
        f"Ban da mo tab ChatGPT trong cua so Chrome (launch_chrome.bat) chua?"
    )


def send_message(page: Page, text: str):
    """Go text vao o nhap va gui."""
    box = page.locator(INPUT_BOX)
    box.click()
    box.fill(text)               # dien text vao o
    page.locator(SEND_BUTTON).click()


def wait_until_done(page: Page, timeout_s: int = 120):
    """
    Doi ChatGPT tra loi xong.
    Logic: khi dang tra loi -> co nut STOP. Tra loi xong -> nut STOP bien mat.
    """
    # 1) doi nut Stop xuat hien (bat dau tra loi)
    try:
        page.locator(STOP_BUTTON).wait_for(state="visible", timeout=15000)
    except Exception:
        pass  # doi khi tra loi qua nhanh, bo qua

    # 2) doi nut Stop bien mat (tra loi xong)
    page.locator(STOP_BUTTON).wait_for(state="hidden", timeout=timeout_s * 1000)
    time.sleep(0.5)  # cho DOM on dinh


def read_last_reply(page: Page) -> str:
    """Doc noi dung bong bong tra loi cuoi cung cua ChatGPT."""
    messages = page.locator(ASSISTANT_MSG)
    count = messages.count()
    if count == 0:
        return "(chua co tra loi nao)"
    return messages.nth(count - 1).inner_text()


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

        print(f"[*] Gui cau hoi: {question!r}")
        send_message(page, question)

        print("[*] Dang doi ChatGPT tra loi ...")
        wait_until_done(page)

        reply = read_last_reply(page)
        print("\n===== CHATGPT TRA LOI =====")
        print(reply)
        print("===========================")


if __name__ == "__main__":
    main()
