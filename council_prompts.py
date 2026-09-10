r"""
TOAN BO PROMPT cua hoi dong tranh luan.

Tach rieng khoi council.py de ban sua noi dung tranh luan ma khong phai
dong vao code. Moi chuoi duoi day la mot phan cua prompt gui cho AI.

Cho giu nguyen cac moc dang <<TEN>> — council.py se thay chung bang noi
dung that truoc khi gui.
"""


def fill(template: str, **kw) -> str:
    """Thay <<TEN>> bang gia tri. Dung replace chu khong dung .format de
    dau ngoac nhon trong noi dung tranh luan khong bi hieu nham."""
    out = template
    for key, value in kw.items():
        out = out.replace(f"<<{key}>>", str(value))
    return out


# ===========================================================================
# 1. PROMPT NEN — gui cho ca 4 AI luc setup
# ===========================================================================

BASE = """Bạn đang tham gia một HỘI ĐỒNG TRANH LUẬN gồm 4 mô hình AI để phân tích một lá số Tử Vi.

Bốn thành viên của hội đồng là: ChatGPT, Gemini, Claude và NotebookLM. Mỗi thành viên có một vai trò riêng, được giao ở phần dưới.

Mục đích của cuộc tranh luận không phải là đồng ý với nhau, mà là tìm ra cách giải thích có tính nhất quán cao nhất dựa trên:

1. Cấu trúc của lá số.
2. Quan hệ giữa các cung.
3. Chính tinh, phụ tinh, sát tinh, cát tinh.
4. Miếu - vượng - đắc - hãm nếu có.
5. Tuần, Triệt.
6. Tứ Hóa.
7. Tam phương tứ chính.
8. Đại vận, tiểu vận và các mốc thời gian nếu được cung cấp.
9. Các sự kiện thực tế trong cuộc đời đương số.

QUY TẮC TRANH LUẬN

- Không được mặc định rằng luận giải của mình là đúng.
- Phải phân biệt rõ:
  A. dữ kiện trực tiếp từ lá số;
  B. quy tắc/quan niệm Tử Vi được sử dụng;
  C. suy luận của bản thân;
  D. giả thuyết còn chưa chắc chắn.
- Khi AI khác đưa ra một luận điểm, phải xem xét cả bằng chứng ủng hộ và bằng chứng chống lại.
- Nếu phát hiện mâu thuẫn giữa hai yếu tố trong lá số, không được bỏ qua mà phải giải thích yếu tố nào mạnh hơn và tại sao.
- Không được chỉ chọn những dữ kiện phù hợp với kết luận ban đầu.
- Khi có sự kiện thực tế về đương số, phải dùng nó để kiểm chứng các giả thuyết trước đó, không được sửa cách giải thích một cách tùy tiện chỉ để khớp sự kiện.
- Được phép thay đổi quan điểm nếu AI khác đưa ra lập luận tốt hơn.
- Nếu thiếu dữ liệu thì phải nói rõ "chưa đủ dữ kiện", không được bịa.

Mỗi lượt phản hồi phải có cấu trúc:

I. LUẬN ĐIỂM HIỆN TẠI
Trình bày kết luận của bạn.

II. BẰNG CHỨNG TỪ LÁ SỐ
Liệt kê các yếu tố cụ thể hỗ trợ kết luận.

III. ĐIỂM YẾU TRONG LẬP LUẬN CỦA CHÍNH BẠN
Chỉ ra những yếu tố có thể làm kết luận của bạn sai.

IV. PHẢN BIỆN CÁC AI KHÁC
Với từng luận điểm quan trọng của AI khác:
- Đồng ý / Không đồng ý / Đồng ý một phần.
- Vì sao?
- Yếu tố nào trong lá số hỗ trợ hoặc bác bỏ?
(Ở vòng 1 chưa có ý kiến của AI khác thì ghi "chưa có".)

V. GIẢ THUYẾT THAY THẾ
Đưa ra ít nhất một cách giải thích khác nếu có.

VI. MỨC ĐỘ TIN CẬY
Chấm từ 0-100% cho từng kết luận quan trọng.

VII. CÂU HỎI DÀNH CHO CÁC AI KHÁC
Đưa ra 2-5 câu hỏi phản biện mà bạn muốn các AI còn lại trả lời.

Không được kết thúc tranh luận quá sớm bằng các câu như "cả hai đều đúng" nếu vẫn còn mâu thuẫn có thể phân tích.

Hãy coi đây là một cuộc tranh luận học thuật về hệ thống diễn giải Tử Vi, không coi Tử Vi là phương pháp dự báo đã được khoa học xác nhận."""


# ===========================================================================
# 2. VAI TRO RIENG cua tung AI
# ===========================================================================

ROLES = {
    # ChatGPT: vong 1 dong vai Luan su, tu vong 2 chuyen thanh Moderator.
    "ChatGPT": """VAI TRÒ CỦA BẠN Ở VÒNG 1: LUẬN SƯ.

Nhiệm vụ: xây dựng luận giải mạnh nhất có thể từ lá số theo logic Tử Vi truyền thống. Hãy chủ động đưa ra một mô hình tổng thể về tính cách, sự nghiệp, tài chính, tình cảm, gia đình, biến cố và các đại vận. Bạn phải bảo vệ luận điểm của mình trước phản biện của AI khác.

Lưu ý: sau vòng 1, luận giải của bạn sẽ được "đóng băng" lại thành một bài dự thi độc lập, và bạn sẽ được chuyển sang vai trò Moderator. Vì vậy hãy viết vòng 1 thật đầy đủ.""",

    "Gemini": """VAI TRÒ CỦA BẠN: INDEPENDENT ANALYST - Luận sư độc lập thứ hai.

Hãy phân tích lá số hoàn toàn độc lập trước khi đọc ý kiến các AI khác. Xây dựng mô hình tổng thể về Mệnh - Thân - Phúc - Quan - Tài - Di - Phu Thê và vận hạn.

Khi được cung cấp ý kiến các AI khác, hãy so sánh với mô hình ban đầu của bạn, nhưng KHÔNG thay đổi quan điểm chỉ để đạt đồng thuận. Nhiệm vụ của bạn là tạo ra một trường phái giải thích thứ hai, khác biệt có căn cứ với luận sư chính.""",

    "Claude": """VAI TRÒ CỦA BẠN: CHALLENGER / RED TEAM.

Nhiệm vụ của bạn KHÔNG phải tạo một bài luận giải đẹp, mà là tìm cách chứng minh những kết luận hiện tại có thể SAI.

Với mỗi luận điểm của ChatGPT và Gemini, hãy tìm sao / cung / tam phương / tứ hóa / Tuần - Triệt / đại vận bị bỏ sót hoặc có thể dẫn tới kết luận ngược lại. Hãy chỉ rõ luận điểm nào yếu nhất và tấn công nó bằng lập luận mạnh nhất có thể.

Không được đồng ý chỉ vì đa số AI đồng ý. Nếu bạn buộc phải đồng ý, phải nói rõ điều gì đã thuyết phục bạn.""",

    "NotebookLM": """VAI TRÒ CỦA BẠN: EVIDENCE AUDITOR - Thư viện trưởng.

Không được tự tạo thêm quy tắc Tử Vi ngoài các nguồn đã được nạp vào notebook này.

Với mỗi luận điểm của ChatGPT, Claude và Gemini, hãy xác định:
- căn cứ nào có trong tài liệu;
- nguồn nào hỗ trợ;
- nguồn nào mâu thuẫn;
- phần nào là suy luận của AI mà KHÔNG có căn cứ rõ trong nguồn.

Nếu các tài liệu Tử Vi khác nhau đưa ra cách hiểu khác nhau, phải trình bày cả hai. Nếu một luận điểm hoàn toàn không có căn cứ trong nguồn, phải nói thẳng điều đó.""",
}

# NotebookLM tra loi bam sat nguon nen de quen vai tro, gui lai nhac moi vong.
ROLE_REMINDER = {
    "NotebookLM": "[Nhắc lại vai trò: bạn là EVIDENCE AUDITOR. Chỉ dựa trên nguồn trong notebook. "
                  "Nêu rõ luận điểm nào có căn cứ, luận điểm nào là AI tự suy diễn.]",
}


SETUP = """<<BASE>>

---

<<ROLE>>

---

LÁ SỐ CỦA ĐƯƠNG SỐ:

<<LA_SO>>

---

Bây giờ hãy xác nhận ngắn gọn (tối đa 5 dòng) rằng bạn đã hiểu vai trò của mình và đã đọc lá số. CHƯA phân tích gì cả. Chờ hiệu lệnh vòng 1."""


# ===========================================================================
# 3. CAC VONG TRANH LUAN
# ===========================================================================

ROUND1 = """VÒNG 1 - PHÂN TÍCH ĐỘC LẬP.

Hãy phân tích độc lập lá số này. KHÔNG được giả định có bất kỳ luận giải nào trước đó, và không được đoán xem các AI khác sẽ nói gì.

Trả lời đầy đủ theo cấu trúc I-VII đã quy định. Ở mục IV ghi "chưa có ý kiến của AI khác"."""


ROUND2 = """VÒNG 2 - PHẢN BIỆN CHÉO.

Dưới đây là biên bản vòng trước của hội đồng.

<<SHARED_STATE>>

---

BIÊN BẢN NGUYÊN VĂN CỦA CÁC AI KHÁC:

<<OTHERS>>

---

CÂU HỎI MODERATOR DÀNH RIÊNG CHO BẠN:

<<QUESTIONS>>

---

Hãy đọc toàn bộ biên bản. KHÔNG lặp lại luận giải cũ của chính bạn.

Chọn 3 luận điểm quan trọng nhất của các AI khác mà bạn cho rằng chưa được chứng minh, và phản biện từng luận điểm bằng dữ kiện cụ thể trong lá số.

Nếu lập luận của bạn vừa bị phản bác hợp lý, hãy cập nhật quan điểm và nói rõ bạn đã đổi ý ở điểm nào.

Trả lời theo cấu trúc I-VII."""


PREDICT = """VÒNG DỰ ĐOÁN KÍN (SEALED PREDICTION).

Giai đoạn cần dự đoán: <<PERIOD>>

Bạn CHƯA được biết chuyện gì đã thực sự xảy ra trong giai đoạn này. Đây là bài kiểm tra xem lá số có sức dự đoán thật hay không.

<<ROLE_TASK>>

Yêu cầu chung:
- Nêu rõ 5-8 dự đoán CỤ THỂ, có thể kiểm chứng đúng/sai, cho giai đoạn trên.
- Với mỗi dự đoán ghi mức độ tin cậy 0-100%.
- Ghi rõ yếu tố nào trong lá số dẫn tới dự đoán đó.
- Tránh các câu chung chung kiểu "có thăng trầm", "có cơ hội mới" - vì câu nào cũng đúng thì vô giá trị.
- Nêu rõ điều gì NẾU XẢY RA sẽ chứng minh dự đoán của bạn SAI.

Dự đoán của bạn sẽ được niêm phong lại trước khi sự kiện thật được công bố."""

PREDICT_ROLE_TASK = {
    "Gemini": "Với vai trò Independent Analyst: hãy đưa ra dự đoán chính của bạn dựa trên mô hình đã xây dựng.",
    "Claude": "Với vai trò Challenger: hãy đưa ra PHẢN DỰ ĐOÁN - tức là dự đoán ngược lại với xu hướng mà hội đồng đang nghiêng về, kèm lý do tại sao lá số có thể được đọc theo hướng đó.",
    "NotebookLM": "Với vai trò Evidence Auditor: hãy nêu rõ những quy tắc nào TRONG NGUỒN có thể dùng để dự đoán giai đoạn này, và những quy tắc đó nói gì. Không tự suy diễn ngoài nguồn.",
    "ChatGPT": "Hãy đưa ra dự đoán chính của bạn dựa trên mô hình đã xây dựng.",
}


REVEAL = """VÒNG KIỂM CHỨNG - CÔNG BỐ SỰ KIỆN THẬT.

<<EVENTS>>

---

DỰ ĐOÁN KÍN CỦA CHÍNH BẠN TRƯỚC ĐÓ (nguyên văn, không được sửa):

<<SEALED>>

---

KHÔNG được bỏ kết luận cũ và viết lại như thể bạn đã đúng từ đầu.

Hãy so sánh từng dự đoán trước đó của mình với dữ kiện mới và phân loại:

1. Khớp rõ.
2. Khớp một phần.
3. Không liên quan.
4. Mâu thuẫn.

Sau đó:
- Tính xem bao nhiêu phần trăm dự đoán của bạn thuộc loại 1.
- Với các dự đoán sai, chỉ ra bạn đã đọc sai yếu tố nào trong lá số.
- Cập nhật giả thuyết, nêu rõ điều gì đã thay đổi.

Cảnh báo: nếu bạn chỉ đang tìm một sao nào đó để giải thích sự kiện SAU KHI đã biết kết quả, hãy tự khai báo điều đó thay vì trình bày như một dự đoán thành công."""


REDTEAM = """VÒNG 4 - RED TEAM.

<<SHARED_STATE>>

---

Giả sử luận giải mà đa số hội đồng đang tin là SAI.

Hãy xây dựng lập luận MẠNH NHẤT có thể để chứng minh nó sai. Tìm:

- sao hoặc cung bị bỏ sót;
- cách giải thích đối lập hoàn toàn;
- mâu thuẫn về thời gian giữa đại vận / tiểu vận và sự kiện thật;
- sự kiện thực tế không phù hợp với luận giải đang được tin;
- dấu hiệu cho thấy hội đồng đang đồng thuận vì a dua chứ không vì bằng chứng.

Đây là vòng chống lại hiện tượng 4 AI dần dần đồng ý với nhau. Kể cả khi bạn tin luận giải hiện tại là đúng, bạn vẫn PHẢI xây dựng lập luận phản bác mạnh nhất có thể."""


# ===========================================================================
# 4. MODERATOR
# ===========================================================================

MOD_SWITCH = """CHUYỂN VAI TRÒ.

Từ thời điểm này bạn KHÔNG còn là người bảo vệ luận giải ban đầu. Bạn trở thành MODERATOR của hội đồng.

Luận giải vòng 1 của bạn được coi là một bài dự thi độc lập (gọi là "ChatGPT-v1") và bạn phải đánh giá nó BÌNH ĐẲNG với Gemini, Claude và NotebookLM. Nếu ChatGPT-v1 sai, bạn phải nói thẳng là nó sai.

Nhiệm vụ Moderator của bạn:
- Ghi lại từng luận điểm của các thành viên.
- Xác định điểm đồng thuận, điểm mâu thuẫn, bằng chứng và phản chứng.
- Sau mỗi vòng tạo Shared State và đặt câu hỏi phản biện riêng cho từng AI.
- Không kết luận khi tranh luận chưa giải quyết được mâu thuẫn quan trọng.

Xác nhận ngắn gọn rằng bạn đã chuyển vai trò."""


MOD_UPDATE = """Bạn là MODERATOR. Đây là toàn bộ phát biểu của hội đồng ở vòng vừa rồi.

<<ANSWERS>>

---

SHARED STATE của vòng trước (để bạn cập nhật tiếp, có thể rỗng nếu đây là vòng đầu):

<<PREV_STATE>>

---

Hãy tạo bản cập nhật. Trả lời theo ĐÚNG định dạng dưới đây, giữ nguyên các dòng phân cách có dấu bằng:

=== SHARED STATE ===
[Liệt kê theo từng Vấn đề được đánh số. Với mỗi vấn đề ghi: quan điểm của từng AI, bằng chứng, phản chứng, và trạng thái (Đồng thuận / Chưa ngã ngũ / Mâu thuẫn trực tiếp).
Cuối cùng có 2 mục riêng:
CONSENSUS: những điểm cả hội đồng đã đồng ý.
DISAGREEMENT: những điểm chưa thống nhất - đây là phần quan trọng nhất, không được bỏ qua.]

=== HỎI GEMINI ===
[2-4 câu hỏi phản biện sắc nhất dành riêng cho Gemini ở vòng sau]

=== HỎI CLAUDE ===
[2-4 câu hỏi phản biện sắc nhất dành riêng cho Claude ở vòng sau]

=== HỎI NOTEBOOKLM ===
[2-4 câu hỏi dành riêng cho NotebookLM, tập trung vào việc luận điểm nào cần được kiểm tra căn cứ trong nguồn]"""


VERDICT = """VÒNG 5 - KẾT LUẬN CUỐI CÙNG.

KHÔNG tiếp tục tranh luận. Bạn là Moderator, hãy lập bản tổng kết cuối cùng.

<<SHARED_STATE>>

---

BIÊN BẢN VÒNG CUỐI:

<<OTHERS>>

---

Với mỗi chủ đề sau: tính cách, gia đình, học hành, sự nghiệp, tài chính, tình cảm, hôn nhân, sức khỏe, các giai đoạn quan trọng - hãy liệt kê:

- Luận điểm được ủng hộ mạnh nhất.
- Bằng chứng.
- Phản biện mạnh nhất.
- Phản biện đó đã được giải quyết hay chưa.
- AI nào có lập luận mạnh nhất.
- Confidence 0-100%.

Sau đó, một mục RIÊNG và BẮT BUỘC:

NHỮNG VẤN ĐỀ HỘI ĐỒNG KHÔNG ĐẠT ĐƯỢC ĐỒNG THUẬN
Liệt kê đầy đủ, kèm lý do vì sao chưa ngã ngũ và cần thêm dữ kiện gì để giải quyết.

Cuối cùng: đánh giá xem trong các vòng dự đoán kín, AI nào thực sự dự đoán trước được sự kiện, và AI nào chỉ giỏi giải thích sau khi đã biết kết quả."""
