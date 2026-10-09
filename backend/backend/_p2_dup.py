import re

from docx import Document

path = r"c:\Users\HEFEI\.trae-cn\attachments\6ac5f18c3e706c34f9ec0610\3597e0cd-ad7f-4049-8fc5-b5679b0ff62e_装饰装修专项施工方案_20261007_第4轮.docx"
doc = Document(path)

BODY_NUM_RE = re.compile(r"^[（(]([0-9]+)[）)]\s*(\S+)")

paras = [(p.style.name if p.style else "", p.text.strip()) for p in doc.paragraphs]

bare_restart = []
for i, (style, t) in enumerate(paras):
    m = BODY_NUM_RE.match(t)
    if not m or int(m.group(1)) != 1:
        continue
    # 向前找最近一个带编号段落，确认中间是否有分隔
    j = i - 1
    blanks = 0
    separator = None
    prev_num = None
    while j >= 0:
        sj, tj = paras[j]
        if not tj:
            blanks += 1
            j -= 1
            continue
        pm = BODY_NUM_RE.match(tj)
        if pm:
            prev_num = int(pm.group(1))
            break
        # 首个非空非编号段 = 引导句/小标题分隔
        separator = (sj, tj[:30])
        break
    if prev_num is not None and prev_num >= 2 and separator is None:
        bare_restart.append((i, prev_num, t[:40]))

print("无分隔裸回退（真·同级重复）条数:", len(bare_restart))
for i, prev, t in bare_restart:
    print(f"[段{i}] 紧接 {prev} 之后又出现: {t}")
