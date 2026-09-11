r"""
LOI DUNG CHUNG — dinh nghia cach dieu khien tung chatbot.

File nay khong chay truc tiep. No gom lai phan "biet cach noi chuyen voi
tung web AI" de ask_all.py (console) va web_ui.py (trinh duyet) dung chung.

Moi chatbot = 1 lop ke thua Adapter, chi can khai bao lai bo selector.
Khi web doi giao dien, sua o phan selector cua lop tuong ung la du.
"""

import asyncio
import time
from playwright.async_api import Page, async_playwright

CDP_URL = "http://localhost:9222"
DEFAULT_TIMEOUT_S = 600      # 10 phut cho moi luot tra loi

# Doi bao lau de nut Stop kip hien ra sau khi bam gui. Khong phai timeout cua
# ca luot — chi la moc nhan ra "da bat dau tra loi".
START_WAIT_MS = 30000

# Text phai dung yen bao lau moi coi la da tra loi xong. Can moc nay vi nut
# Stop cua vai site nhap nhay giua chung, neu chi dua vao no se doc phai
# cau tra loi con dang viet do dang.
QUIET_MS = 2500


class Adapter:
    """Khuon chung cho moi chatbot."""

    name = ""
    url_match = ""       # chuoi de nhan ra tab
    input_box = ""       # o nhap lieu
    send_button = ""     # nut gui
    stop_button = ""     # nut dung (hien trong luc dang tra loi)
    assistant_msg = ""   # 1 phan tu = 1 luot tra loi cua AI
    reply_text = ""      # selector con de lay chu sach ("" = lay ca bong bong)
    max_prompt = None    # gioi han so ky tu o nhap chiu duoc (None = thoai mai)
    timeout_s = DEFAULT_TIMEOUT_S   # site nao tra loi cham thi khai lai o lop con

    # ---------- tim tab ----------

    def find_page(self, browser) -> Page:
        """Duyet tat ca tab trong moi cua so Chrome, tim tab co URL khop."""
        for context in browser.contexts:
            for page in context.pages:
                if self.url_match in page.url:
                    return page
        raise RuntimeError(
            f"Khong tim thay tab {self.name} (URL chua '{self.url_match}'). "
            f"Ban da chay launch_chrome.bat va dang nhap chua?"
        )

    def check_ready(self, page: Page):
        """Kiem tra bo sung truoc khi gui. Mac dinh khong can gi them."""
        return

    # ---------- 3 thao tac chinh ----------

    async def count_replies(self, page: Page) -> int:
        """Dem so luot tra loi hien co. Goi TRUOC khi gui de biet moc so sanh."""
        return await page.locator(self.assistant_msg).count()

    async def send(self, page: Page, text: str):
        """Go text vao o nhap va gui."""
        if self.max_prompt and len(text) > self.max_prompt:
            raise RuntimeError(
                f"{self.name}: prompt dai {len(text)} ky tu, vuot gioi han "
                f"{self.max_prompt} cua o nhap — nut gui se bi khoa. "
                f"Hay rut gon truoc khi gui."
            )
        box = page.locator(self.input_box).first
        await box.click()
        await box.fill(text)
        # Nut gui cua ChatGPT va Gemini CHI xuat hien khi o nhap da co chu,
        # nen phai doi no hien ra roi moi bam.
        try:
            await page.wait_for_selector(self.send_button, state="visible",
                                         timeout=START_WAIT_MS)
        except Exception:
            raise RuntimeError(
                f"{self.name}: khong thay nut gui. Tab co the dang ket o trang thai "
                f"'dang tra loi' — hay tai lai tab do (F5) roi thu lai."
            ) from None
        await page.locator(self.send_button).first.click()

    async def wait_done(self, page: Page, before: int, timeout_s: int):
        """
        Doi tra loi xong. Mac dinh dua vao nut Stop.
        Thu tu 4 buoc nay quan trong — xem ghi chu o buoc 1.
        """
        # 1) doi nut Stop XUAT HIEN. Neu bo qua buoc nay thi buoc (3) se khop
        #    ngay lap tuc (chua co nut Stop nao = coi nhu da an) -> doc nham
        #    bong bong con rong.
        try:
            await page.locator(self.stop_button).wait_for(state="visible",
                                                          timeout=START_WAIT_MS)
        except Exception:
            pass  # doi khi tra loi qua nhanh, bo qua

        # 2) doi bong bong tra loi moi xuat hien
        await self._wait_new_reply(page, before, timeout_s)

        # 3) doi nut Stop bien mat -> da tra loi xong
        await page.locator(self.stop_button).wait_for(state="hidden", timeout=timeout_s * 1000)

        # 4) doi text dung yen
        await self._wait_stable(page, timeout_s)

    async def _wait_new_reply(self, page: Page, before: int, timeout_s: int):
        await page.wait_for_function(
            "(a) => document.querySelectorAll(a.sel).length > a.n",
            arg={"sel": self.assistant_msg, "n": before},
            timeout=timeout_s * 1000,
        )

    async def _reply_length(self, page: Page) -> int:
        """Do dai cau tra loi cuoi cung, tinh bang ky tu."""
        return await page.evaluate(
            """(a) => {
                 const list = document.querySelectorAll(a.msg);
                 const last = list[list.length - 1];
                 if (!last) return 0;
                 const body = (a.body && last.querySelector(a.body)) || last;
                 return (body.innerText || '').trim().length;
               }""",
            {"msg": self.assistant_msg, "body": self.reply_text},
        )

    async def _wait_stable(self, page: Page, timeout_s: int = DEFAULT_TIMEOUT_S):
        """
        Doi den khi cau tra loi DUNG YEN, khong chi la "da co chu".

        Chi kiem tra rong/khong-rong la chua du: nut Stop cua ChatGPT nhap nhay
        giua chung khi dang viet, nen buoc truoc do co the ket thuc som va ta
        doc phai cau tra loi moi viet duoc mot nua.

        Vong lap dem nam o day chu khong nhet vao trang web, vi
        page.wait_for_function chay trong execution context rieng — bien luu
        giua cac lan poll khong song sot qua duoc.
        """
        deadline = time.monotonic() + timeout_s
        last_len, since = -1, time.monotonic()
        while time.monotonic() < deadline:
            n = await self._reply_length(page)
            now = time.monotonic()
            if n == 0:
                last_len, since = -1, now
            elif n != last_len:
                last_len, since = n, now
            elif (now - since) * 1000 >= QUIET_MS:
                return
            await asyncio.sleep(0.3)
        raise RuntimeError(
            f"{self.name}: cau tra loi van chay sau {timeout_s}s, khong dung yen.")

    def clean(self, text: str) -> str:
        """Don rac trong text doc duoc. Mac dinh chi cat khoang trang."""
        return text.strip()

    async def read_last(self, page: Page) -> str:
        """Doc noi dung luot tra loi cuoi cung."""
        messages = page.locator(self.assistant_msg)
        count = await messages.count()
        if count == 0:
            return "(chua co tra loi nao)"
        last = messages.nth(count - 1)
        if self.reply_text:
            body = last.locator(self.reply_text)
            if await body.count() > 0:
                return self.clean(await body.first.inner_text())
        return self.clean(await last.inner_text())

    # ---------- ghep lai ----------

    async def ensure_composer(self, page: Page, timeout_s: int = 30):
        """
        Cac trang nay la SPA va thinh thoang ket: o nhap bien mat khoi DOM du
        van dang dang nhap. Gap truong hop do thi tai lai tab 1 lan — cuoc hoi
        thoai nam tren may chu nen khong mat gi.
        """
        try:
            await page.wait_for_selector(self.input_box, state="visible", timeout=5000)
            return
        except Exception:
            pass
        await page.reload(wait_until="domcontentloaded")
        try:
            await page.wait_for_selector(self.input_box, state="visible",
                                         timeout=timeout_s * 1000)
        except Exception:
            raise RuntimeError(
                f"{self.name}: khong thay o nhap ke ca sau khi tai lai tab. "
                f"Kiem tra xem tab con dang nhap khong."
            ) from None

    async def wait_idle(self, page: Page, timeout_s: int = DEFAULT_TIMEOUT_S):
        """
        Neu tab con dang tra loi do dang tu lan chay truoc thi nut gui se bi
        khoa. Doi cho xong roi moi go cau moi.
        """
        if not self.stop_button:
            return
        stop = page.locator(self.stop_button)
        try:
            if await stop.count() > 0 and await stop.first.is_visible():
                await stop.wait_for(state="hidden", timeout=timeout_s * 1000)
        except Exception:
            pass  # het gio thi cu thu gui, buoc send se bao loi ro hon

    async def ask(self, browser, text: str, timeout_s: int = None) -> str:
        """
        Gui 1 cau hoi va tra ve cau tra loi.
        Khong truyen timeout_s thi lay theo tung site (xem thuoc tinh timeout_s
        cua lop con) — NotebookLM cham hon han nen duoc cho lau hon.
        """
        timeout_s = timeout_s or self.timeout_s
        page = self.find_page(browser)
        self.check_ready(page)
        await self.ensure_composer(page)
        await self.wait_idle(page, timeout_s)
        before = await self.count_replies(page)
        await self.send(page, text)
        await self.wait_done(page, before, timeout_s)
        return await self.read_last(page)


class ChatGPT(Adapter):
    name = "ChatGPT"
    url_match = "chatgpt.com"
    input_box = "#prompt-textarea"
    # Cung 1 nut #composer-submit-button, data-testid doi giua send/stop.
    # Nut nay khong ton tai khi o nhap con trong.
    send_button = "#composer-submit-button[data-testid='send-button']"
    stop_button = "#composer-submit-button[data-testid='stop-button']"
    assistant_msg = "[data-message-author-role='assistant']"
    reply_text = ".markdown"
    # Nut nay nam trong action bar cua moi response da hoan tat. ChatGPT hien
    # ao hoa lich su chat: khi response moi xuat hien, mot response cu co the
    # bi go khoi DOM, lam tong so `assistant_msg` khong tang. Vi vay khong the
    # dung count_replies/_wait_new_reply cho rieng ChatGPT.
    completion_button = (
        "button[data-testid='copy-turn-action-button']"
        "[aria-label='Copy response']"
    )
    turn_container = "section[data-testid^='conversation-turn-']"

    async def _completed_reply_ids(self, page: Page) -> list[str]:
        """Lay ID cac response da co nut Copy tai thoi diem hien tai."""
        return await page.evaluate(
            """(a) => [...document.querySelectorAll(a.done)]
                .map((button) => {
                    const turn = button.closest(a.turn);
                    const message = turn && turn.querySelector(a.message);
                    return message && message.getAttribute('data-message-id');
                })
                .filter(Boolean)""",
            {"done": self.completion_button, "turn": self.turn_container,
             "message": self.assistant_msg},
        )

    async def _wait_completed_reply(self, page: Page, known_ids: list[str],
                                    timeout_s: int) -> str:
        """
        Doi nut Copy response cua mot message ID moi.

        So sanh ID thay vi so luong node de khong bi ket khi ChatGPT ao hoa
        DOM: no co the xoa mot response cu dung luc them response moi.
        """
        handle = await page.wait_for_function(
            """(a) => {
                const known = new Set(a.known);
                const buttons = [...document.querySelectorAll(a.done)];
                for (let i = buttons.length - 1; i >= 0; i--) {
                    const turn = buttons[i].closest(a.turn);
                    const message = turn && turn.querySelector(a.message);
                    const id = message && message.getAttribute('data-message-id');
                    if (id && !known.has(id)) return id;
                }
                return false;
            }""",
            arg={"known": known_ids, "done": self.completion_button,
                 "turn": self.turn_container, "message": self.assistant_msg},
            timeout=timeout_s * 1000,
        )
        return await handle.json_value()

    async def _read_reply_by_id(self, page: Page, reply_id: str) -> str:
        """Doc dung response vua hoan tat, khong mac dinh lay node cuoi."""
        message = page.locator(
            f'{self.assistant_msg}[data-message-id="{reply_id}"]'
        ).first
        if await message.count() == 0:
            raise RuntimeError(
                "ChatGPT: response moi da bien mat khoi DOM truoc khi doc duoc."
            )
        body = message.locator(self.reply_text)
        if await body.count() > 0:
            return self.clean(await body.first.inner_text())
        return self.clean(await message.inner_text())

    async def ask(self, browser, text: str, timeout_s: int = None) -> str:
        """Gui va nhan response ChatGPT dua tren nut Copy response."""
        timeout_s = timeout_s or self.timeout_s
        page = self.find_page(browser)
        self.check_ready(page)
        await self.ensure_composer(page)
        await self.wait_idle(page, timeout_s)
        known_ids = await self._completed_reply_ids(page)
        await self.send(page, text)
        reply_id = await self._wait_completed_reply(page, known_ids, timeout_s)
        return await self._read_reply_by_id(page, reply_id)


class Gemini(Adapter):
    name = "Gemini"
    url_match = "gemini.google.com"
    input_box = 'div.ql-editor[contenteditable="true"]'
    send_button = "gem-icon-button.send-button button"
    stop_button = ""  # Gemini khong co nut Stop voi selector on dinh
    assistant_msg = "model-response"
    reply_text = ".model-response-text"

    async def wait_done(self, page: Page, before: int, timeout_s: int):
        """
        Gemini khong co nut Stop dung duoc (aria-label doi theo ngon ngu giao
        dien), nen dung moc khac: thanh nut copy/thumb <message-actions> chi
        duoc render SAU KHI tra loi xong. Cach nay khong phu thuoc ngon ngu.
        """
        await self._wait_new_reply(page, before, timeout_s)
        await page.wait_for_function(
            """() => {
                 const r = [...document.querySelectorAll('model-response')].pop();
                 return !!(r && r.querySelector('message-actions'));
               }""",
            timeout=timeout_s * 1000,
        )
        await self._wait_stable(page, timeout_s)


class NotebookLM(Adapter):
    name = "NotebookLM"
    # notebooklm.google.com hien chuyen huong sang notebook.google.com,
    # nen chi khop chuoi "notebook" cho ca 2 truong hop.
    url_match = "notebook"
    input_box = "textarea.query-box-input"
    send_button = "button.submit-button"
    stop_button = "button.stop-button"
    assistant_msg = "chat-message:has(.to-user-message-inner-content)"
    reply_text = ".message-text-content"
    # Thanh nut o cuoi message (Luu vao ghi chu / Sao chep / thumb) chi duoc
    # render SAU KHI response hoan tat. NotebookLM khong gan message-id len
    # <chat-message>, nen danh dau cac cap hoi-dap dang co truoc khi gui roi
    # doi mot cap moi co thanh nut nay.
    #
    # Truoc day dung "button.xap-copy-to-clipboard" nhung NotebookLM da doi
    # giao dien: selector do khop 0 phan tu, nen khong bao gio nhan ra la da
    # tra loi xong va ca buoi treo cho toi khi het gio. Dung the
    # <mat-card-actions> vi no khong phu thuoc ngon ngu giao dien — aria-label
    # cua nut Copy o day la tieng Viet ("Sao chep").
    completion_button = "mat-card-actions.message-actions"
    reply_pair = ".chat-message-pair"
    before_marker = "data-ai-orchestrator-before"
    # Do duoc bang cach nhi phan: qua ~3900 ky tu la nut gui bi khoa cung.
    # De 3800 cho co bien an toan.
    max_prompt = 3800
    # NotebookLM phai doc lai toan bo nguon trong notebook truoc khi tra loi
    # nen cham hon han 3 site kia. Cho no 20 phut.
    timeout_s = 1200

    NOISE = {"more_horiz", "expand_more", "keep_pin", "Thoughts",
             "thumb_up", "thumb_down", "copy_all"}

    def check_ready(self, page: Page):
        """Khung chat chi ton tai ben trong 1 notebook cu the."""
        if "/notebook/" not in page.url:
            raise RuntimeError(
                f"Tab NotebookLM dang o trang danh sach ({page.url}), chua co khung chat. "
                f"Hay mo 1 notebook cu the (URL dang .../notebook/<id>) roi thu lai."
            )

    def clean(self, text: str) -> str:
        """
        Bo chu thich nguon chen giua cau. NotebookLM gan nut trich dan vao
        trong doan van, doc innerText se ra nhung dong rac dang so thu tu
        hoac ten icon.
        """
        lines = []
        for line in text.splitlines():
            s = line.strip()
            if not s or s.isdigit() or s in self.NOISE:
                continue
            lines.append(s)
        return " ".join(lines).replace(" .", ".").replace(" ,", ",").strip()

    async def _mark_existing_pairs(self, page: Page) -> str:
        """Danh dau cac cap hoi-dap hien co de nhan ra cap moi sau khi gui."""
        token = str(time.monotonic_ns())
        await page.evaluate(
            """(a) => document.querySelectorAll(a.pair).forEach(
                (pair) => pair.setAttribute(a.marker, a.token))""",
            {"pair": self.reply_pair, "marker": self.before_marker,
             "token": token},
        )
        return token

    async def _wait_completed_message(self, page: Page, token: str,
                                      timeout_s: int):
        """Doi message moi co nut Copy, ke ca khi tong so node khong tang."""
        handle = await page.wait_for_function(
            """(a) => {
                const pairs = [...document.querySelectorAll(a.pair)];
                for (let i = pairs.length - 1; i >= 0; i--) {
                    const pair = pairs[i];
                    if (pair.getAttribute(a.marker) === a.token) continue;
                    const messages = [...pair.querySelectorAll(a.message)];
                    const completed = messages.find(
                        (message) => message.querySelector(a.done));
                    if (completed) return completed;
                }
                return false;
            }""",
            arg={"pair": self.reply_pair,
                 "marker": self.before_marker, "token": token,
                 "message": self.assistant_msg,
                 "done": self.completion_button},
            timeout=timeout_s * 1000,
        )
        message = handle.as_element()
        if message is None:
            raise RuntimeError("NotebookLM: khong xac dinh duoc response moi.")
        return message

    async def _read_message_handle(self, message) -> str:
        """Doc response tu dung ElementHandle vua co nut Copy."""
        paras = await message.query_selector_all(
            ".message-text-content .paragraph")
        if paras:
            out = [self.clean(await para.inner_text()) for para in paras]
            return "\n".join(x for x in out if x).strip()

        body = await message.query_selector(self.reply_text)
        if body is None:
            raise RuntimeError("NotebookLM: response moi khong co noi dung chu.")
        return self.clean(await body.inner_text())

    async def ask(self, browser, text: str, timeout_s: int = None) -> str:
        """Gui va nhan response NotebookLM dua tren nut Copy."""
        timeout_s = timeout_s or self.timeout_s
        page = self.find_page(browser)
        self.check_ready(page)
        await self.ensure_composer(page)
        await self.wait_idle(page, timeout_s)
        token = await self._mark_existing_pairs(page)
        await self.send(page, text)
        message = await self._wait_completed_message(page, token, timeout_s)
        return await self._read_message_handle(message)

    async def read_last(self, page: Page) -> str:
        """Ghep tung doan van de bo duoc phan Thoughts o dau bong bong."""
        messages = page.locator(self.assistant_msg)
        count = await messages.count()
        if count == 0:
            return "(chua co tra loi nao)"
        last = messages.nth(count - 1)

        paras = last.locator(".message-text-content .paragraph")
        n = await paras.count()
        if n > 0:
            out = [self.clean(await paras.nth(i).inner_text()) for i in range(n)]
            return "\n".join(x for x in out if x).strip()
        return self.clean(await last.locator(self.reply_text).first.inner_text())


class Claude(Adapter):
    name = "Claude"
    url_match = "claude.ai"
    input_box = 'div[contenteditable="true"][data-testid="chat-input"]'
    send_button = 'button[data-testid="chat-input-send"]'
    stop_button = 'button[aria-label="Stop response"]'
    assistant_msg = "div.font-claude-response"
    reply_text = ".standard-markdown"  # bo khoi "Thought for Xs"


ALL = [ChatGPT(), Gemini(), NotebookLM(), Claude()]
BY_KEY = {a.name.lower(): a for a in ALL}


def pick(keys) -> list:
    """Chon adapter theo ten. keys rong -> lay tat ca."""
    if not keys:
        return list(ALL)
    chosen = []
    for k in keys:
        ad = BY_KEY.get(k.strip().lower())
        if ad is None:
            raise KeyError(k)
        if ad not in chosen:
            chosen.append(ad)
    return chosen


async def connect():
    """Gan vao Chrome dang chay. Tra ve (playwright, browser) de con dong lai."""
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    return pw, browser


async def ask_all(browser, question: str, targets=None,
                  timeout_s: int = None, on_event=None) -> list:
    """
    Gui cung 1 cau hoi cho nhieu AI SONG SONG.
    on_event(name, status, text) duoc goi khi mot AI bat dau / xong / loi.
    Tra ve list dict: {name, ok, seconds, reply}.
    """
    targets = targets or list(ALL)

    async def one(ad: Adapter):
        t0 = time.perf_counter()
        if on_event:
            on_event(ad.name, "running", "")
        try:
            reply = await ad.ask(browser, question, timeout_s)
            ok = True
        except Exception as e:
            reply = f"(LOI: {type(e).__name__}: {str(e)[:300]})"
            ok = False
        result = {"name": ad.name, "ok": ok,
                  "seconds": round(time.perf_counter() - t0, 1), "reply": reply}
        if on_event:
            on_event(ad.name, "done" if ok else "error", reply)
        return result

    return await asyncio.gather(*(one(a) for a in targets))
