r"""
Canh bao khi trong chuoi PAGE co escape kieu \n / \t chua duoc nhan doi.

PAGE trong talkshow.py la chuoi Python THUONG, khong phai raw string. Viet
\n trong doan JS o do se bi Python doi thanh xuong dong that, lam vo chuoi
JS va hong CA khoi <script> — trang van tai duoc, nhung khong nut nao chay
va cung khong bao gi ra man hinh.

Loi nay da xay ra that mot lan (o nhap "Prompt bo sung"), rat kho doan neu
chi nhin trang web, nen de san file kiem tra o day.

Chay:  venv\Scripts\python.exe check_page_escapes.py
"""

import re
import sys
from pathlib import Path

SRC = Path(__file__).parent / "talkshow.py"

# Mot dau backslash le loi (khong phai backslash doi) di kem n / t / r
LONE_ESCAPE = re.compile(r"(?<!\\)\\[ntr]")


def main() -> int:
    lines = SRC.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith('PAGE = """'))
        end = next(i for i, l in enumerate(lines)
                   if i > start and l.startswith('""".replace'))
    except StopIteration:
        print("Khong tim thay vung PAGE trong talkshow.py — bo qua.")
        return 0

    bad = [(i + 1, lines[i].strip())
           for i in range(start + 1, end)
           if LONE_ESCAPE.search(lines[i])]

    if not bad:
        print(f"OK — vung PAGE (dong {start + 1}..{end + 1}) "
              f"khong con escape le loi.")
        return 0

    print(f"LOI — {len(bad)} dong trong PAGE co escape chua nhan doi:")
    for num, text in bad:
        print(f"  dong {num}: {text[:100]}")
    print(r"  Sua bang cach viet backslash doi: \\n thay cho \n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
