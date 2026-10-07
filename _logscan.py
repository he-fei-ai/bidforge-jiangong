# -*- coding: utf-8 -*-
import re, collections, os

LOGDIR = r"j:\编程\专项方案工具箱\logs"
FILES = ["backend.log", "backend.log.1", "backend.log.3"]

pat = re.compile(r'^\S+ \S+ \[(ERROR|WARNING|INFO)\] ([^:]+): (.*)$')
uuid_re = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
hex_re = re.compile(r'[0-9a-f]{16,}')
num_re = re.compile(r'\d+(\.\d+)?')
sq_re = re.compile(r"'[^']*'")

def norm(m):
    m = uuid_re.sub('<uuid>', m)
    m = hex_re.sub('<hex>', m)
    m = num_re.sub('<n>', m)
    m = sq_re.sub("'<x>'", m)
    return m

rows = []
per_file = {}
for fn in FILES:
    p = os.path.join(LOGDIR, fn)
    cnt = collections.Counter()
    with open(p, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.rstrip('\n')
            mm = pat.match(line)
            if not mm:
                continue
            lvl, mod, msg = mm.group(1), mm.group(2).strip(), mm.group(3)
            rows.append((lvl, mod, msg))
            cnt[lvl] += 1
    per_file[fn] = cnt

print("matched rows:", len(rows))
for fn, c in per_file.items():
    print(fn, dict(c))

for lvl in ("ERROR", "WARNING"):
    print("\n================ %s patterns ================" % lvl)
    c = collections.Counter((mod, norm(msg)) for l, mod, msg in rows if l == lvl)
    for (mod, p), n in c.most_common(90):
        print("%6d  [%s] %s" % (n, mod, p[:170]))
