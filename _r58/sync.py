import shutil, pathlib
src = pathlib.Path("backend"); dst = pathlib.Path("_r58/backend")
n_app = n_test = skipped = 0
for p in src.rglob("*.py"):
    try:
        data = p.read_bytes()
    except Exception:
        skipped += 1; continue
    rel = p.relative_to(src)
    t = dst / rel
    t.parent.mkdir(parents=True, exist_ok=True)
    if t.exists() and t.read_bytes() == data:
        continue
    t.write_bytes(data)
    if rel.parts[0] == "app": n_app += 1
    else: n_test += 1
print("overlay app:", n_app, "overlay tests:", n_test, "unreadable skipped:", skipped)
