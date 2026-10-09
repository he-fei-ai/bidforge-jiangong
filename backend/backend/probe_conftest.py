import pathlib

p = pathlib.Path("tests/conftest.py")
print("exists:", p.exists())
try:
    sz = p.stat().st_size
    print("size:", sz)
    data = p.read_bytes()
    print("read_ok:", len(data))
    print("head:", data[:200])
except Exception as e:
    print("ERR:", type(e).__name__, e)
    # Try absolute path
    pa = pathlib.Path(__file__).parent / "tests" / "conftest.py"
    print("abs_exists:", pa.exists())
    try:
        print("abs_read:", len(pa.read_bytes()))
    except Exception as e2:
        print("ABS_ERR:", type(e2).__name__, e2)
