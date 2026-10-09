"""外科式补齐 insert_after 定位精度回归（2026-09-23 · 歧义定位修复）。

背景：_merge_patch_chapters 旧实现取第一个互含命中就插入，目录存在
同名/近似标题时（如「安全保证措施」与「安全文明施工保证措施」）新章会
被插到错误位置。现改为：精确命中优先且唯一才采用；模糊命中要求唯一；
歧义/零命中一律追加末尾（宁缺勿错位）。
"""
from app.routers import sse_handlers as sh


def _titles(outline: list) -> list:
    return [n["title"] for n in outline]


class TestMergePatchPrecision:
    def test_exact_match_inserts_after(self):
        outline = [{"title": "工程概况", "children": []},
                   {"title": "安全保证措施", "children": []}]
        merged = sh._merge_patch_chapters(outline, [
            {"title": "应急预案", "insert_after": "安全保证措施", "children": []}])
        assert _titles(merged) == ["工程概况", "安全保证措施", "应急预案"]
        assert merged[2]["id"] == "3", "插入后必须统一重排编号"

    def test_unique_fuzzy_match_still_inserts(self):
        """回归保护：唯一互含命中的旧能力不得丢失。"""
        outline = [{"title": "工程概况", "children": []},
                   {"title": "安全文明施工保证措施", "children": []}]
        merged = sh._merge_patch_chapters(outline, [
            {"title": "监测方案", "insert_after": "安全文明施工保证措施",
             "children": []}])
        assert _titles(merged) == ["工程概况", "安全文明施工保证措施", "监测方案"]

    def test_exact_preferred_over_fuzzy(self):
        """「安全保证措施」既有精确命中又是近似标题的互含命中 → 必须取精确位。"""
        outline = [{"title": "安全保证措施", "children": []},
                   {"title": "安全文明施工保证措施", "children": []}]
        merged = sh._merge_patch_chapters(outline, [
            {"title": "应急预案", "insert_after": "安全保证措施", "children": []}])
        assert _titles(merged) == ["安全保证措施", "应急预案", "安全文明施工保证措施"]

    def test_fuzzy_ambiguous_appends_end(self):
        """互含命中多处（近似标题歧义）→ 追加末尾，不插错位置。"""
        outline = [{"title": "安全文明施工保证措施", "children": []},
                   {"title": "安全文明施工管理办法", "children": []}]
        merged = sh._merge_patch_chapters(outline, [
            {"title": "绿色施工", "insert_after": "安全文明施工", "children": []}])
        assert _titles(merged) == ["安全文明施工保证措施", "安全文明施工管理办法",
                                   "绿色施工"], "歧义命中不得取第一处插入"

    def test_multiple_exact_hits_append_end(self):
        """同名标题多处精确命中同样视为歧义 → 追加末尾。"""
        outline = [{"title": "保障措施", "children": []},
                   {"title": "保障措施", "children": []}]
        merged = sh._merge_patch_chapters(outline, [
            {"title": "新章", "insert_after": "保障措施", "children": []}])
        assert _titles(merged) == ["保障措施", "保障措施", "新章"]

    def test_no_match_appends_end_regression(self):
        """回归（test_outline_call_optimization 既有用例）：匹配不到追加末尾。"""
        outline = [{"title": "工程概况", "children": []}]
        merged = sh._merge_patch_chapters(outline, [
            {"title": "监测方案", "insert_after": "不存在的章", "children": []}])
        assert _titles(merged) == ["工程概况", "监测方案"]
        assert merged[0]["id"] == "1" and merged[1]["id"] == "2"
