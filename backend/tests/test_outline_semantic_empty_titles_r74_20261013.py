"""目录生成 · 纯语义空话标题检出率收口测试（R74b · 2026-10-13）

背景（AGENTS.md 遗留项①）：「纯语义空话标题（『有关要求』）仍会漏检」。
此前 GENERIC_TITLE_PATTERNS 覆盖了「专项N」「其他」「补充」「通用」等约 30 种模式，
但漏掉了「有关要求」「等等」「若干」等纯语义空话标题，以及英文等价（etc/misc/other）。

本轮修复：
  - outline_quality.GENERIC_TITLE_PATTERNS 新增 15+ 模式，覆盖中文/英文纯语义空话标题；
  - 新增长方案合成语料测试（§8.4 数据缺口），60+ 节点、4 级深度，验证大规模目录
    下质量检查不报错、不误报。

所有结论均有代码依据；测试可独立运行（pytest tests/test_outline_semantic_empty_titles_r74_20261013.py）。
"""
from __future__ import annotations

import json

import pytest

from app.services.outline_quality import (
    check_outline_continuity,
    check_structure_standards,
    detect_generic_titles,
    is_generic_title,
)


# ---------------------------------------------------------------------------
# 测试用标题样本
# ---------------------------------------------------------------------------

# 新增模式应命中的纯语义空话标题
NEW_GENERIC_TITLES = [
    "有关要求",              # 直接命中 ^有关要求
    "有关要求（2）",          # 带冲突后缀，_norm 剥除后仍命中
    "要求",                  # 仅「要求」无修饰
    "等等",                  # 等等
    "若干",                  # 若干
    "若干要求",              # 若干X
    "若干说明",              # 若干X
    "其他要求",              # 其他X
    "其他措施",              # 其他X
    "其他说明",              # 其他X
    "其他补充",              # 其他X
    "相关措施",              # 相关X
    "相关说明",              # 相关X
    "相关补充",              # 相关X
    "相关内容",              # 相关X
    "补充说明",              # 补充X
    "补充要求",              # 补充X
    "补充措施",              # 补充X
]

# 英文等价标题（case-insensitive）
NEW_ENGLISH_TITLES = [
    "etc", "Etc", "ETC",
    "misc", "Misc", "MISC",
    "other", "Other", "OTHER",
]

# 合法标题（不得误报）—— 与 LEGIT_OUTLINE 互补
LEGIT_NO_FALSE_POSITIVE = [
    "技术要求",              # ^要求$ 不匹配（有修饰词）
    "安全要求",              # ^要求$ 不匹配
    "质量要求",              # ^要求$ 不匹配
    "有关要求及措施",         # 此标题虽空泛，但含具体措施指向，保留
    "其他安全措施",          # ^其他措施$ 不匹配（有「安全」修饰）
    "其他注意事项",          # 不含在模式中
    "注意事项",              # 不含在模式中
    "专项施工方案",          # 合法
    "深基坑支护施工",        # 合法
    "技术要求与措施",        # 含具体要求指向
]


# ---------------------------------------------------------------------------
# 1. 新模式检测：纯语义空话标题全部命中
# ---------------------------------------------------------------------------
class TestNewGenericPatterns:
    """验证新增的纯语义空话标题模式全部可检出。"""

    @pytest.mark.parametrize("title", NEW_GENERIC_TITLES)
    def test_catches_chinese_semantic_empty(self, title):
        """中文纯语义空话标题必须被检出。"""
        assert is_generic_title(title), f"应检出：{title}"

    @pytest.mark.parametrize("title", NEW_ENGLISH_TITLES)
    def test_catches_english_semantic_empty(self, title):
        """英文纯语义空话标题必须被检出（case-insensitive）。"""
        assert is_generic_title(title), f"应检出：{title}"

    def test_detect_generic_titles_in_outline(self):
        """在目录树中检出新增模式。"""
        outline = [{"title": t, "children": []} for t in NEW_GENERIC_TITLES[:5]]
        hits = detect_generic_titles(outline)
        hit_titles = {h["title"] for h in hits}
        for t in NEW_GENERIC_TITLES[:5]:
            assert t in hit_titles, f"{t} 未在检出结果中"
            kind = next(h["kind"] for h in hits if h["title"] == t)
            assert kind == "generic", f"{t} 应为 generic 类型"


# ---------------------------------------------------------------------------
# 2. 误报防护：合法标题不得被误报
# ---------------------------------------------------------------------------
class TestNoFalsePositive:
    """验证合法标题（含相似子串）不被误报。"""

    @pytest.mark.parametrize("title", LEGIT_NO_FALSE_POSITIVE)
    def test_legitimate_titles_not_flagged(self, title):
        """合法标题不得被检出为泛化/占位。"""
        assert not is_generic_title(title), f"不应误报：{title}"

    def test_no_false_positive_on_legit_outline(self):
        """整个合法目录树零误报。"""
        outline = [
            {"title": "专项施工方案", "children": []},
            {"title": "技术要求", "children": [{"title": "材料要求", "children": []}]},
            {"title": "安全要求", "children": []},
            {"title": "注意事项", "children": []},
            {"title": "其他安全措施", "children": []},
        ]
        hits = detect_generic_titles(outline)
        assert hits == [], f"合法目录出现误报：{[h['title'] for h in hits]}"


# ---------------------------------------------------------------------------
# 3. 与连续性校验的集成：generic_titles issue 正确产出
# ---------------------------------------------------------------------------
class TestContinuityIntegration:
    """验证新增模式通过 check_outline_continuity 正确进入 issues。"""

    def test_generic_titles_in_issues(self):
        """纯语义空话标题必须进入 generic_titles issue。"""
        outline = [
            {"title": "工程概况", "children": [{"title": "有关要求", "children": []}]},
            {"title": "技术要求", "children": [{"title": "等等", "children": []}]},
            {"title": "质量要求", "children": [{"title": "若干", "children": []}]},
        ]
        report = check_outline_continuity(outline)
        assert not report["ok"], "应检出问题"
        assert report["issue_counts"]["generic_titles"] == 3
        flagged = {item["title"] for item in report["generic_titles"]}
        assert flagged == {"有关要求", "等等", "若干"}

    def test_clean_outline_still_ok(self):
        """无空话标题的合法目录仍然通过。"""
        outline = [
            {"title": "工程概况", "children": [{"title": "项目地理位置", "children": []}]},
            {"title": "施工部署", "children": [{"title": "施工准备", "children": []}]},
            {"title": "技术措施", "children": [{"title": "质量保证", "children": []}]},
        ]
        report = check_outline_continuity(outline)
        assert report["ok"], f"合法目录不应有问题：{report['issue_counts']}"
        assert report["issue_counts"]["generic_titles"] == 0


# ---------------------------------------------------------------------------
# 4. 长方案合成语料（§8.4 数据缺口收口）
# ---------------------------------------------------------------------------
def _build_long_scheme() -> list:
    """构建 60+ 节点、4 级深度的真实施工专项方案目录。"""
    return [
    {
        "title": "工程概况",
        "children": [
            {
                "title": "项目基本情况",
                "children": [
                    {
                        "title": "工程名称",
                        "children": []
                    },
                    {
                        "title": "建设地点",
                        "children": []
                    },
                    {
                        "title": "建设规模",
                        "children": []
                    },
                    {
                        "title": "参建单位",
                        "children": []
                    }
                ]
            },
            {
                "title": "工程地质条件",
                "children": [
                    {
                        "title": "地形地貌",
                        "children": []
                    },
                    {
                        "title": "地层分布",
                        "children": []
                    },
                    {
                        "title": "地下水情况",
                        "children": []
                    }
                ]
            },
            {
                "title": "周边环境",
                "children": []
            }
        ],
        "description": "项目基本情况"
    },
    {
        "title": "编制依据",
        "children": [
            {
                "title": "法律法规",
                "children": []
            },
            {
                "title": "技术标准与规范",
                "children": [
                    {
                        "title": "国家标准",
                        "children": []
                    },
                    {
                        "title": "行业标准",
                        "children": []
                    },
                    {
                        "title": "地方标准",
                        "children": []
                    }
                ]
            },
            {
                "title": "设计文件",
                "children": []
            }
        ],
        "description": "法律法规与技术标准"
    },
    {
        "title": "施工计划",
        "children": [
            {
                "title": "施工进度计划",
                "children": [
                    {
                        "title": "总进度安排",
                        "children": []
                    },
                    {
                        "title": "关键线路",
                        "children": []
                    }
                ]
            },
            {
                "title": "劳动力配置计划",
                "children": []
            },
            {
                "title": "机械设备配置计划",
                "children": [
                    {
                        "title": "大型机械",
                        "children": []
                    },
                    {
                        "title": "小型机具",
                        "children": []
                    }
                ]
            },
            {
                "title": "材料供应计划",
                "children": []
            }
        ],
        "description": "进度与资源配置"
    },
    {
        "title": "施工部署",
        "children": [
            {
                "title": "施工总体部署",
                "children": []
            },
            {
                "title": "施工分区",
                "children": []
            },
            {
                "title": "施工顺序",
                "children": []
            },
            {
                "title": "施工协调",
                "children": []
            }
        ],
        "description": "总体安排"
    },
    {
        "title": "施工工艺技术",
        "children": [
            {
                "title": "施工工艺流程",
                "children": [
                    {
                        "title": "总体流程",
                        "children": []
                    },
                    {
                        "title": "各工序衔接",
                        "children": []
                    }
                ]
            },
            {
                "title": "支护结构施工",
                "children": [
                    {
                        "title": "桩基施工",
                        "children": [{"title": "钻孔灌注桩", "children": []}]
                    },
                    {
                        "title": "冠梁施工",
                        "children": []
                    },
                    {
                        "title": "支撑架设",
                        "children": []
                    }
                ]
            },
            {
                "title": "降水井施工",
                "children": [
                    {
                        "title": "井点布置",
                        "children": []
                    },
                    {
                        "title": "井管制作",
                        "children": []
                    }
                ]
            },
            {
                "title": "土方开挖",
                "children": [
                    {
                        "title": "分层开挖",
                        "children": []
                    },
                    {
                        "title": "出土路线",
                        "children": []
                    }
                ]
            },
            {
                "title": "监测方案",
                "children": [
                    {
                        "title": "监测项目",
                        "children": []
                    },
                    {
                        "title": "监测频率",
                        "children": []
                    },
                    {
                        "title": "预警值",
                        "children": []
                    }
                ]
            }
        ],
        "description": "核心工艺"
    },
    {
        "title": "施工安全保证措施",
        "children": [
            {
                "title": "安全管理体系",
                "children": [
                    {
                        "title": "组织机构",
                        "children": []
                    },
                    {
                        "title": "职责分工",
                        "children": []
                    }
                ]
            },
            {
                "title": "安全风险分析",
                "children": []
            },
            {
                "title": "安全技术措施",
                "children": [
                    {
                        "title": "临时用电安全",
                        "children": []
                    },
                    {
                        "title": "高处作业安全",
                        "children": []
                    },
                    {
                        "title": "起重吊装安全",
                        "children": []
                    },
                    {
                        "title": "有限空间作业安全",
                        "children": []
                    }
                ]
            },
            {
                "title": "应急预案",
                "children": [
                    {
                        "title": "应急组织",
                        "children": []
                    },
                    {
                        "title": "应急物资",
                        "children": []
                    },
                    {
                        "title": "应急响应程序",
                        "children": []
                    }
                ]
            }
        ],
        "description": "安全管控"
    },
    {
        "title": "质量保证措施",
        "children": [
            {
                "title": "质量管理体系",
                "children": []
            },
            {
                "title": "质量控制点",
                "children": []
            },
            {
                "title": "检验批划分",
                "children": []
            },
            {
                "title": "验收标准",
                "children": []
            }
        ],
        "description": "质量管控"
    },
    {
        "title": "环境保护措施",
        "children": [
            {
                "title": "扬尘控制",
                "children": []
            },
            {
                "title": "噪声控制",
                "children": []
            },
            {
                "title": "废水处理",
                "children": []
            },
            {
                "title": "文明施工",
                "children": []
            }
        ],
        "description": "文明施工"
    },
    {
        "title": "计算书及相关图纸",
        "children": [
            {
                "title": "支护结构设计计算",
                "children": []
            },
            {
                "title": "稳定性验算",
                "children": [
                    {
                        "title": "整体稳定性",
                        "children": []
                    },
                    {
                        "title": "坑底抗隆起",
                        "children": []
                    },
                    {
                        "title": "抗倾覆验算",
                        "children": []
                    }
                ]
            },
            {
                "title": "降水计算",
                "children": []
            },
            {
                "title": "相关图纸",
                "children": []
            }
        ],
        "description": "计算依据"
    }
]


class TestLongSchemeCorpus:
    """长方案合成语料测试（§8.4 数据缺口收口）。"""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.outline = _build_long_scheme()

    def test_long_scheme_all_legitimate(self):
        """60+ 节点长方案所有标题均为合法施工术语，零误报。"""
        hits = detect_generic_titles(self.outline)
        flagged = [h["title"] for h in hits]
        assert hits == [], f"合法长方案出现误报：{flagged}"

    def test_long_scheme_continuity_ok(self):
        """长方案连续性校验通过。"""
        report = check_outline_continuity(self.outline)
        assert report["ok"], f"合法长方案连续性不应有问题：{report['issue_counts']}"
        assert report["nodes"] >= 60, f"节点数不足：{report['nodes']}"
        assert report["max_level"] >= 4, f"深度不足：{report['max_level']}"

    def test_long_scheme_structure_standards(self):
        """长方案结构规范检查正常产出结果。"""
        report = check_structure_standards(self.outline)
        assert report["evaluated"] is True
        assert 5 <= report["chapters"] <= 10
        assert report["max_depth"] <= 4

    def test_long_scheme_node_count(self):
        """验证合成语料的节点数符合预期规模。"""
        from app.services.outline_quality import collect_titles
        entries = collect_titles(self.outline)
        assert len(entries) >= 60, f"预期 >= 60 节点，实际 {len(entries)}"
        max_level = max(e["level"] for e in entries)
        assert max_level >= 4, f"预期 >= 4 级深度，实际 {max_level}"

    def test_long_scheme_with_planted_generic(self):
        """在长方案中植入一个空话标题，验证被检出。"""
        outline = json.loads(json.dumps(self.outline))
        outline[5]["children"][3]["children"].append(
            {"title": "有关要求", "children": []})
        hits = detect_generic_titles(outline)
        assert len(hits) == 1
        assert hits[0]["title"] == "有关要求"
        assert hits[0]["kind"] == "generic"
