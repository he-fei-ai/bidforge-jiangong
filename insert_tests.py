# -*- coding: utf-8 -*-
import os

TESTS_FILE = os.path.join(os.getcwd(), "backend", "tests", "test_docx_math.py")
SECTION_MARKER = b"# ---"
TARGET_LABEL = "# \u4e71\u7801\u6e05\u7406"  # # 乱码清理

with open(TESTS_FILE, "rb") as f:
    data = f.read()

# Find all occurrences of the section marker
pos = 0
positions = []
while True:
    idx = data.find(SECTION_MARKER, pos)
    if idx == -1:
        break
    positions.append(idx)
    pos = idx + 1

print(f"Found {len(positions)} section markers at byte offsets: {positions}")

# Find the one followed by the target label
target_idx = None
for idx in positions:
    # Look ahead from the marker to find the label on the next line
    # The marker is "# ---" followed by \r\n then "# 乱码清理"
    after = data[idx:idx+100]
    # Decode the relevant portion
    text_after = after.decode("utf-8", errors="replace")
    lines = text_after.split("\r\n")
    for line in lines:
        if TARGET_LABEL in line:
            target_idx = idx
            break
    if target_idx is not None:
        break

if target_idx is None:
    print("ERROR: Could not find section marker followed by target label")
    for idx in positions:
        snippet = data[idx:idx+50].decode("utf-8", errors="replace")
        print(f"  Marker at {idx}: {snippet[:40]!r}")
    raise SystemExit(1)

print(f"Target section marker at byte offset {target_idx}")

# Build the test functions with CRLF line endings
NEW_TESTS = (
    "def test_nested_brace_scripts_sub_and_sup():\r\n"
    "    \"\"\"Branch 2: sSub + ^ -> sSubSup (merged into <m:sSubSup>).\"\"\"\r\n"
    "    from app.services import docx_math\r\n"
    "\r\n"
    "    xml = docx_math.latex_to_omml(r\"x_{i+1}^{n+1}\")\r\n"
    "    assert \"<m:sSubSup>\" in xml\r\n"
    "\r\n"
    "\r\n"
    "def test_empty_script_params_dropped():\r\n"
    "    \"\"\"FIX-C2: Empty script params are dropped (no empty <m:sub>/<m:sup>).\"\"\"\r\n"
    "    from app.services import docx_math\r\n"
    "\r\n"
    "    xml = docx_math.latex_to_omml(r\"x_{}\")\r\n"
    "    assert \"<m:sub>\" not in xml\r\n"
    "    assert \"<m:sup>\" not in xml\r\n"
    "\r\n"
    "\r\n"
    "def test_trailing_orphan_script_discarded():\r\n"
    "    \"\"\"L714-715: Trailing orphan script markers are discarded.\"\"\"\r\n"
    "    from app.services import docx_math\r\n"
    "\r\n"
    "    xml = docx_math.latex_to_omml(r\"x_\")\r\n"
    "    assert \"<m:sub>\" not in xml\r\n"
    "    assert \"<m:sup>\" not in xml\r\n"
    "\r\n"
    "\r\n"
    "def test_sup_then_sub_merge_order():\r\n"
    "    \"\"\"Branch 3: sSup + _ -> sSubSup (reverse order, <m:sSubSup>).\"\"\"\r\n"
    "    from app.services import docx_math\r\n"
    "\r\n"
    "    xml = docx_math.latex_to_omml(r\"x^i_j\")\r\n"
    "    assert \"<m:sSubSup>\" in xml\r\n"
    "\r\n"
    "\r\n"
    "def test_script_on_nested_base():\r\n"
    "    \"\"\"Branch 6: Script on a nested base node creates nested <m:sSub>.\"\"\"\r\n"
    "    from app.services import docx_math\r\n"
    "\r\n"
    "    xml = docx_math.latex_to_omml(r\"x_{i}_{j}\")\r\n"
    "    assert \"<m:sSub>\" in xml\r\n"
    "    assert xml.count(\"<m:sSub>\") == 2\r\n"
    "\r\n"
    "\r\n"
    "def test_script_on_operator():\r\n"
    "    \"\"\"Branch 1: Script on an operator node creates <m:sSup>.\"\"\"\r\n"
    "    from app.services import docx_math\r\n"
    "\r\n"
    "    xml = docx_math.latex_to_omml(r\"e^{2x}\")\r\n"
    "    assert \"<m:sSup>\" in xml\r\n"
)

# Convert to bytes
new_tests_bytes = NEW_TESTS.encode("utf-8")

# Insert before the target marker
# The marker is at target_idx, which starts with "# ---\r\n"
# We want to insert our tests BEFORE this marker
# But we need to ensure there are two blank lines before the marker
# Currently the file has: ...L75 content\r\n\r\n\r\n# ---\r\n# 乱码清理\r\n
# So we insert our tests at target_idx, and they already have two trailing blank lines

inserted_data = data[:target_idx] + new_tests_bytes + data[target_idx:]

# Write back
with open(TESTS_FILE, "wb") as f:
    f.write(inserted_data)

# Verification
print(f"Insertion complete!")
print(f"Old byte count: {len(data)}")
print(f"New byte count: {len(inserted_data)}")
print(f"Added: {len(new_tests_bytes)} bytes")
print(f"BOM: {inserted_data[:3] == b'\xef\xbb\xbf'}")
print(f"CRLF count in new file: {inserted_data.count(b'\r\n')}")
print(f"Test functions added: {NEW_TESTS.count('def test_')}")
