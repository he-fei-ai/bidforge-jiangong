import re
from collections import Counter
from docx import Document

path = r"c:\Users\HEFEI\.trae-cn\attachments\6ac5f18c3e706c34f9ec0610\3597e0cd-ad7f-4049-8fc5-b5679b0ff62e_装饰装修专项施工方案_20261007_第4轮.docx"
doc = Document(path)

EN_RE = re.compile(r"[A-Za-z]{2,}(?:\s+[A-Za-z]{2,})+")
MANUAL_NUM_RE = re.compile(r"^\s*(?:第?[0-9]+[章、.]|[0-9]+(?:\.[0-9]+)+\s*[\u4e00-\u9fff]|[（(]?[0-9]+[）)]\s*)")

en_hits = Counter()
manual = []
for i, p in enumerate(doc.paragraphs):
    t = p.text.strip()
    if not t:
        continue
    for m in EN_RE.finditer(t):
        s = m.group(0).strip()
        if len(s) >= 4:
            en_hits[s] += 1
    if MANUAL_NUM_RE.match(t) and not t.startswith(("表", "图")):
        manual.append((i, t[:40]))

print("===== 英文短语（>=2 词，出现≥2 次）=====")
for s, n in en_hits.most_common():
    if n >= 2:
        print(f"{n}x  {s}")

print("\n===== 手工编号样例（前20）=====")
for i, t in manual[:20]:
    print(f"[段{i}] {t}")
print("手工编号段落总数:", len(manual))
