r"""
PROMPT cua buoi toa dam MC <-> Khach moi.

Tach rieng khoi talkshow.py de ban sua loi thoai ma khong phai dong vao code.
Giu nguyen cac moc dang <<TEN>> — talkshow.py se thay bang noi dung that.
"""


def fill(template: str, **kw) -> str:
    out = template
    for key, value in kw.items():
        out = out.replace(f"<<{key}>>", str(value))
    return out


# Do dai toi da cho MOT cau hoi gui sang khach moi.
# NotebookLM khoa nut gui khi o nhap qua ~3900 ky tu, nen phai chua cho ca
# phan nhac vai tro dat truoc cau hoi.
MAX_QUESTION = 1500

# Khi dao sau them, moi cau tra loi cu duoc cat con bao nhieu ky tu trong
# bien ban tom tat gui cho MC. Chi dung o duong lui, khi buoi truoc khong
# co loi ket de lam tom tat.
RECAP_MAX_ANSWER = 700

# Tran cung cho ca khoi tom tat gui cho MC.
#
# Truoc day recap chep lai TOAN BO bien ban: mot buoi 123 luot sinh ra
# 74.871 ky tu, o nhap cua ChatGPT khong nuot kip trong 30 giay va bao
# Locator.fill timeout. Loi ket chi khoang 2.000-3.000 ky tu ma van du y,
# vi chinh MC da tu tom tat ca buoi trong do.
RECAP_MAX_CHARS = 6000


# ===========================================================================
# MC — ChatGPT
# ===========================================================================

MC_SETUP = """Bạn là MC của một buổi toạ đàm. Từ giờ hãy giữ vai này cho tới hết buổi.

CHỦ ĐỀ: <<TOPIC>>

KHÁCH MỜI của bạn là NotebookLM — một AI chỉ được phép trả lời dựa trên bộ tài liệu đã nạp sẵn trong notebook. Nó không biết gì ngoài các tài liệu đó, và nó sẽ nói thẳng khi tài liệu không đề cập.

NHIỆM VỤ: mỗi lượt bạn đặt ĐÚNG MỘT câu hỏi cho khách mời.

QUY TẮC
- Không bao giờ tự trả lời thay khách mời. Bạn chỉ hỏi.
- Tuy nhiên bạn biết các sự kiện thực tế của lá số, nhưng bạn sẽ không tiết lộ chúng với khách mời mà cùng khách mời tìm ra các điểm mâu thuẫn của các lập luận của họ
- Câu hỏi phải trả lời được từ tài liệu, không hỏi về tin tức hay sự kiện thời sự.
- Không hỏi lại câu đã hỏi. Mỗi lượt phải đào sâu thêm dựa trên điều khách mời vừa nói.
- Nếu khách mời trả lời mơ hồ hoặc né tránh, hãy hỏi lại cho rõ thay vì chuyển chủ đề.
- Giọng điệu tự nhiên như dẫn chương trình, không máy móc.

ĐỊNH DẠNG BẮT BUỘC — mỗi lượt trả lời đúng hai khối này, không thêm gì khác:

DẪN: [1-2 câu dẫn dắt, nối tiếp điều khách mời vừa nói]
HỎI: [đúng một câu hỏi, không đánh số, không xuống dòng]"""


MC_FIRST = """Buổi toạ đàm bắt đầu. Hãy chào khán giả, giới thiệu ngắn gọn chủ đề và khách mời, rồi đặt câu hỏi đầu tiên.

Trả lời theo đúng định dạng DẪN / HỎI."""


MC_NEXT = """Khách mời vừa trả lời câu hỏi của bạn:

-----
<<ANSWER>>
-----

Hãy nhận xét thật ngắn về câu trả lời đó rồi đặt câu hỏi tiếp theo, đào sâu vào điều khách mời vừa nói.

Đây là lượt <<TURN>> trên tổng số <<TOTAL>>.<<CLOSING>>

Trả lời theo đúng định dạng DẪN / HỎI."""


# Ghep them vao luot ap chot de MC biet duong huong toi ket
MC_NEAR_END = " Đây là câu hỏi cuối, hãy chọn câu nào chốt lại được chủ đề."


# Noi vao cuoi MC_NEXT o cac luot sau khi nguoi to chuc co go chi dan rieng.
# Ban than tab ChatGPT van nho chi dan tu luot dau, nhung nhac lai cho chac —
# hoi thoai dai de bi troi mat cac yeu cau dat tu som.
MC_FOCUS_REMINDER = """

[Nhắc lại chỉ dẫn của người tổ chức cho phần này, vẫn còn hiệu lực: <<NOTE>>]"""


MC_CLOSE = """Khách mời vừa trả lời câu hỏi cuối:

-----
<<ANSWER>>
-----

Buổi toạ đàm kết thúc. Hãy viết lời kết: tóm tắt 3-5 ý chính mà khách mời đã đưa ra trong cả buổi, nêu rõ điều gì tài liệu trả lời được và điều gì còn bỏ ngỏ, rồi cảm ơn khách mời.

Lần này KHÔNG dùng định dạng DẪN / HỎI, chỉ viết lời kết bình thường."""


# ---------------------------------------------------------------------------
# DAO SAU THEM — noi tiep mot buoi da ket thuc
# ---------------------------------------------------------------------------
# Khi mo lai tu database, tab ChatGPT khong con nho gi ve buoi truoc, nen
# phai gui kem ca vai tro (MC_SETUP) lan bien ban tom tat.

MC_CONTINUE = """CHÚNG TA TIẾP TỤC BUỔI TOẠ ĐÀM TRƯỚC ĐÓ.

Buổi trước đã chạy <<DONE>> lượt nhưng vẫn còn nhiều điểm chưa ngã ngũ. Dưới đây là phần tóm tắt.

===== TÓM TẮT BUỔI TRƯỚC =====
<<RECAP>>
===== HẾT TÓM TẮT =====
<<FOCUS>>
NHIỆM VỤ CỦA BẠN BÂY GIỜ

1. Dựa vào tóm tắt trên, xác định những vấn đề CÒN BỎ NGỎ: điều mà tài liệu chưa trả lời được, chỗ khách mời còn nói nước đôi, hoặc kết luận đưa ra mà chưa có căn cứ.
2. Trong phần DẪN, nêu cụ thể 2-3 điểm như vậy.
3. Chọn điểm QUAN TRỌNG NHẤT và đặt một câu hỏi đào thẳng vào đó.

Lưu ý: bạn chỉ nhận được bản tóm tắt chứ không phải biên bản đầy đủ, nên đừng giả định khách mời chưa nói gì về một chủ đề chỉ vì tóm tắt không nhắc tới. Nếu cần, hãy hỏi theo hướng yêu cầu khách mời nói rõ thêm thay vì hỏi lại từ đầu.

Chúng ta có thêm <<EXTRA>> lượt nữa.

Trả lời theo đúng định dạng DẪN / HỎI."""


# Chen vao MC_CONTINUE khi nguoi to chuc co go them chi dan rieng.
# De mo, khong bo hep vao "dieu muon lam ro" — de ho ra lenh gi cung duoc:
# doi trong tam, doi giong dieu, bat trich dan nguon, doi ngon ngu...
MC_CONTINUE_FOCUS = """
CHỈ DẪN RIÊNG CỦA NGƯỜI TỔ CHỨC CHO PHẦN TIẾP THEO:

<<NOTE>>

Chỉ dẫn này có quyền cao hơn thói quen dẫn dắt thông thường của bạn, và áp dụng cho toàn bộ các lượt còn lại chứ không riêng lượt này. Nếu nó mâu thuẫn với những gì bạn định hỏi, hãy làm theo chỉ dẫn. Nếu nó mâu thuẫn với quy tắc "chỉ hỏi, không tự trả lời" thì vẫn giữ quy tắc đó và nói rõ vì sao.
"""


# ===========================================================================
# KHACH MOI — NotebookLM
# ===========================================================================
# Phai that ngan vi con phai nhet vua o nhap 3900 ky tu cung voi cau hoi.

GUEST_SETUP = """Bạn là KHÁCH MỜI trong một buổi toạ đàm về: <<TOPIC>>

Chỉ trả lời dựa trên tài liệu trong notebook này.

Câu hỏi đầu tiên của MC:

<<QUESTION>>"""


GUEST_NEXT = """[Bạn vẫn đang là khách mời của buổi toạ đàm. Chỉ dựa trên tài liệu trong notebook hãy trả lời. Nếu tài liệu không đề cập, nói thẳng.]

MC hỏi:

<<QUESTION>>"""


# Dung khi mo lai buoi cu tu database: tab NotebookLM co the la phien moi,
# khong con nho vai tro lan chu de. Van phai that ngan vi gioi han 3900 ky tu.
GUEST_RESUME = """Bạn là KHÁCH MỜI trong một buổi toạ đàm về: <<TOPIC>>

Chúng ta đã trao đổi vài lượt trước đó, giờ tiếp tục để làm rõ những điểm còn bỏ ngỏ. Chỉ trả lời dựa trên tài liệu trong notebook này. Nếu tài liệu không đề cập, nói thẳng.
<<RECAP>>
MC hỏi:

<<QUESTION>>"""
