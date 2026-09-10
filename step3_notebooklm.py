"""
BUOC 3 — Dieu khien NotebookLM qua Chrome dang dang nhap (CDP).

Cach chay:
  1. Chay launch_chrome.bat  ->  Chrome mo ra, dang nhap NotebookLM (lan dau)
  2. MO SAN 1 NOTEBOOK (URL phai dang .../notebook/<id>), vi khung chat chi
     ton tai ben trong 1 notebook cu the — trang danh sach notebook thi khong co.
  3. Chay:  venv\\Scripts\\python.exe step3_notebooklm.py "cau hoi cua ban"
"""

import sys
import time
from playwright.sync_api import sync_playwright, Page

CDP_URL = "http://localhost:9222"

# --- Selector rieng cho NotebookLM (day la phan hay phai chinh khi web doi giao dien) ---
# Luu y: notebooklm.google.com hien chuyen huong sang notebook.google.com,
# nen chi khop chuoi "notebook" cho ca 2 truong hop.
URL_MATCH     = "notebook"                                      # nhan dien tab NotebookLM
INPUT_BOX     = "textarea.query-box-input"                      # o nhap cau hoi
SEND_BUTTON   = "button.submit-button"                          # nut gui (mui ten ->)
STOP_BUTTON   = "button.stop-button"                            # nut dung (hien khi dang tra loi)
ASSISTANT_MSG = "chat-message:has(.to-user-message-inner-content)"  # bong bong tra loi cua AI
REPLY_TEXT    = ".message-text-content"                         # khoi chu trong bong bong
REPLY_PARA    = ".message-text-content .paragraph"              # tung doan van (bo phan "Thoughts")


def find_page(browser, url_match: str) -> Page:
    """Duyet tat ca tab trong moi cua so Chrome, tim tab co URL khop."""
    for context in browser.contexts:
        for page in context.pages:
            if url_match in page.url:
                return page
    raise RuntimeError(
        f"Khong tim thay tab nao co '{url_match}'. "
        f"Ban da mo tab NotebookLM trong cua so Chrome (launch_chrome.bat) chua?"
    )


def check_notebook_opened(page: Page):
    """Khung chat chi co khi dang o trong 1 notebook cu the."""
    if "/notebook/" not in page.url:
        raise RuntimeError(
            f"Tab NotebookLM dang o '{page.url}' — day la trang danh sach, chua co khung chat. "
            f"Hay mo 1 notebook cu the (URL dang .../notebook/<id>) roi chay lai."
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
    Doi NotebookLM tra loi xong.
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
             const m = [...document.querySelectorAll('chat-message')]
                       .filter(e => e.querySelector('.to-user-message-inner-content')).pop();
             if (!m) return false;
             const body = m.querySelector('.message-text-content') || m;
             return (body.innerText || '').trim().length > 0;
           }""",
        timeout=30000,
    )
    time.sleep(0.5)  # cho DOM on dinh


def clean(text: str) -> str:
    """
    Bo cac chu thich nguon chen giua cau.
    NotebookLM gan nut trich dan vao trong doan van, khi doc innerText chung
    hien ra thanh nhung dong rac dang "1", "2" hoac "more_horiz".
    """
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s == "more_horiz" or s.isdigit():
            continue
        lines.append(s)
    return " ".join(lines).replace(" .", ".").replace(" ,", ",").strip()


def read_last_reply(page: Page) -> str:
    """Doc noi dung bong bong tra loi cuoi cung cua NotebookLM."""
    messages = page.locator(ASSISTANT_MSG)
    count = messages.count()
    if count == 0:
        return "(chua co tra loi nao)"
    last = messages.nth(count - 1)

    # Uu tien ghep tung doan van -> bo duoc phan "Thoughts / expand_more" o dau
    paras = last.locator(REPLY_PARA)
    n = paras.count()
    if n > 0:
        return "\n".join(clean(paras.nth(i).inner_text()) for i in range(n)).strip()
    return clean(last.locator(REPLY_TEXT).first.inner_text())


def main():
    question = "Xin chao! Hay tu gioi thieu ban trong 1 cau ngan gon."
    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])

    with sync_playwright() as p:
        print(f"[*] Ket noi toi Chrome tai {CDP_URL} ...")
        browser = p.chromium.connect_over_cdp(CDP_URL)

        page = find_page(browser, URL_MATCH)
        check_notebook_opened(page)
        print(f"[*] Da tim thay tab: {page.url}")
        page.bring_to_front()

        before = count_replies(page)
        print(f"[*] Gui cau hoi: {question!r}")
        send_message(page, question)

        print("[*] Dang doi NotebookLM tra loi ...")
        wait_until_done(page, before)

        reply = read_last_reply(page)
        print("\n===== NOTEBOOKLM TRA LOI =====")
        print(reply)
        print("==============================")


if __name__ == "__main__":
    main()
