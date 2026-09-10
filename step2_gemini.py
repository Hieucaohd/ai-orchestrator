"""
BUOC 2 — Dieu khien Gemini qua Chrome dang dang nhap (CDP).

Cach chay:
  1. Chay launch_chrome.bat  ->  Chrome mo ra, dang nhap Gemini (lan dau)
  2. Chay:  venv\\Scripts\\python.exe step2_gemini.py "cau hoi cua ban"
"""

import sys
import time
from playwright.sync_api import sync_playwright, Page

CDP_URL = "http://localhost:9222"

# --- Selector rieng cho Gemini (day la phan hay phai chinh khi web doi giao dien) ---
URL_MATCH     = "gemini.google.com"                       # nhan dien tab Gemini
INPUT_BOX     = 'div.ql-editor[contenteditable="true"]'   # o nhap lieu (Quill editor)
SEND_BUTTON   = "gem-icon-button.send-button button"      # nut gui (chi hien khi o nhap co chu)
RESPONSE      = "model-response"                          # 1 the <model-response> = 1 luot tra loi
REPLY_TEXT    = ".model-response-text"                    # phan noi dung chu trong luot tra loi
DONE_MARKER   = "message-actions"                         # thanh nut copy/thumb -> chi co khi tra loi XONG


def find_page(browser, url_match: str) -> Page:
    """Duyet tat ca tab trong moi cua so Chrome, tim tab co URL khop."""
    for context in browser.contexts:
        for page in context.pages:
            if url_match in page.url:
                return page
    raise RuntimeError(
        f"Khong tim thay tab nao co '{url_match}'. "
        f"Ban da mo tab Gemini trong cua so Chrome (launch_chrome.bat) chua?"
    )


def count_replies(page: Page) -> int:
    """Dem so luot tra loi hien co. Goi TRUOC khi gui de biet moc so sanh."""
    return page.locator(RESPONSE).count()


def send_message(page: Page, text: str):
    """Go text vao o nhap va gui."""
    box = page.locator(INPUT_BOX).first
    box.click()
    box.fill(text)
    page.wait_for_selector(SEND_BUTTON, state="visible", timeout=10000)  # doi nut gui hien ra
    page.locator(SEND_BUTTON).first.click()


def wait_until_done(page: Page, before: int, timeout_s: int = 180):
    """
    Doi Gemini tra loi xong.
    Gemini khong co nut Stop on dinh, nen dung 2 moc:
      1) so <model-response> tang len  -> da bat dau tra loi
      2) luot tra loi moi co <message-actions> -> da tra loi xong
    """
    page.wait_for_function(
        "(n) => document.querySelectorAll('model-response').length > n",
        arg=before,
        timeout=timeout_s * 1000,
    )
    page.wait_for_function(
        """() => {
             const r = [...document.querySelectorAll('model-response')].pop();
             return !!(r && r.querySelector('message-actions'));
           }""",
        timeout=timeout_s * 1000,
    )
    time.sleep(0.5)  # cho DOM on dinh


def read_last_reply(page: Page) -> str:
    """Doc noi dung luot tra loi cuoi cung cua Gemini."""
    responses = page.locator(RESPONSE)
    count = responses.count()
    if count == 0:
        return "(chua co tra loi nao)"
    last = responses.nth(count - 1)
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

        print("[*] Dang doi Gemini tra loi ...")
        wait_until_done(page, before)

        reply = read_last_reply(page)
        print("\n===== GEMINI TRA LOI =====")
        print(reply)
        print("==========================")


if __name__ == "__main__":
    main()
