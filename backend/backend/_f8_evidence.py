import re

from docx import Document

path = r"c:\Users\HEFEI\.trae-cn\attachments\6ac5f18c3e706c34f9ec0610\3597e0cd-ad7f-4049-8fc5-b5679b0ff62e_装饰装修专项施工方案_20261007_第4轮.docx"
doc = Document(path)

patterns = {
    "工期": re.compile(r"[^。\n]*(?:总工期|工期|建设工期|施工工期)[^。\n]*\d+[^。\n]*(?:日历天|天|个月|月)[^。\n]*"),
    "甲醛": re.compile(r"[^。\n]*甲醛[^。\n]*(?:0\.\d+|\d+\.\d+)[^。\n]*"),
}

seen = set()
for i, p in enumerate(doc.paragraphs):
    t = p.text.strip()
    if not t:
        continue
    for label, pat in patterns.items():
        for m in pat.finditer(t):
            s = m.group(0).strip()
            key = (label, s)
            if key in seen:
                continue
            seen.add(key)
            print(f"[段{i}][{label}] {s[:120]}")
