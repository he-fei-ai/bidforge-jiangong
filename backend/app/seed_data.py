"""专项方案清单预置数据：按分类导入 / 升级 outline_library 表的标准目录

数据来源：专项方案清单.md（通用目录库 + 项目历史方案）
分类体系：12 个一级分类（基坑与土方 / 模板与支撑 / 脚手架 / 起重吊装 /
          临时设施 / 安全文明 / 质量与创优 / 防水与渗漏 / 装配式与结构 /
          装饰装修 / 机电与智能化 / 应急与专项）

目录内容来源：app.services.outline_templates —— 按建筑工程行业标准生成
  - 危大工程类方案：对齐住建部令第 37 号要求的九章法定内容
  - 每条目录附带编制依据（真实规范编号）与适用条件（含危大工程界定标准）

升级策略（SEED_VERSION）：
  - 首次执行：全量插入；
  - 再次执行：仅刷新版本号低于 SEED_VERSION（或 force=True）的条目，
    **保留 id 与 ref_count**，避免破坏 schemes.config_json 中已有的 library_ids 引用。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid

from app.db import init_db, get_conn
from app.services.outline_templates import build_outline, get_meta

logger = logging.getLogger("seed")

#: 预置目录内容版本：内容升级时递增，触发库内旧版本条目刷新
SEED_VERSION = "v2.0"

# 12 个一级分类及其包含的方案
SCHEME_CATALOG: dict[str, list[str]] = {
    "基坑与土方": [
        "基坑工程专项施工方案", "基坑工程专家评审方案", "基坑监测专项施工方案",
        "基坑围护专项施工方案", "围护工程专项施工方案", "围护搅拌桩机安装与拆卸专项施工方案",
        "土方开挖专项施工方案", "降水工程专项施工方案", "临时排水专项施工方案",
        "超前支撑注浆钢管专项施工方案", "水平钢支撑专项施工方案", "钢支撑施工方案",
        "H型钢拔除专项施工方案", "SMW工法型钢拔除专项施工方案",
        "新增电梯基坑开挖专项方案", "新增电梯基坑支护专项方案",
        "人工挖孔桩专项施工方案", "地下连续墙专项施工方案",
    ],
    "模板与支撑": [
        "高大支模专项施工方案", "高大支模专家评审方案",
        "超限梁模板支撑专项施工方案", "超限梁模板支撑专家评审方案",
        "模板工程专项施工方案", "地下室结构模板工程专项施工方案",
        "地下结构支撑排架专项施工方案",
    ],
    "脚手架": [
        "落地脚手架专项施工方案", "悬挑脚手架专项施工方案",
        "悬挑脚手架专家评审施工方案", "移动脚手架专项方案", "移动脚手架平台专项方案",
        "电梯井道脚手架施工", "电梯井道防护专项施工方案",
        "人货梯脚手架专项施工方案", "吊篮工程专项施工方案",
        "地下结构临时脚手架专项方案", "室内脚手架专项施工方案",
        "移动操作平台专项方案", "屋面构架脚手架专项施工方案",
        "外墙防护脚手架工程专家评审方案", "附着式升降脚手架专项施工方案",
    ],
    "起重吊装": [
        "塔吊基础专项施工方案", "塔吊基础专家评审方案",
        "塔吊安装专项施工方案", "塔吊拆卸专项施工方案",
        "群吊群塔专项施工方案", "人货电梯基础专项施工方案",
        "人货电梯安装与拆卸专项施工方案", "大型机械安拆专项方案",
        "预制装配构件吊装专项施工方案", "预制装配构件吊装专家评审方案",
        "地下结构汽车吊吊装专项方案", "人防门吊装专项施工方案",
        "升降机安装施工方案", "人货梯拆除", "钢结构吊装专项施工方案",
    ],
    "临时设施": [
        "大临设施专项施工方案", "临时用水专项施工方案",
        "临时用电专项施工方案", "临时围墙加固施工方案",
        "现场消防专项方案", "地下室临时照明专项施工方案",
        "安全通道施工方案", "围墙临时防护专项方案",
    ],
    "安全文明": [
        "安全施工组织设计", "安全措施计划", "安保计划",
        "防高坠专项方案", "高处作业专项方案", "高空作业平台车专项方案",
        "有限空间作业专项施工方案",
        "重大危险源清单编制", "危险源辨识、风险评价及控制措施清单",
        "高风险危险源及控制措施汇总表",
        "环境因素调查、评价及控制措施清单", "重要环境因素汇总表",
        "大气污染防治专项方案", "扬尘控制方案",
        "文明工地创优方案", "市文明工地创建",
        "建工集团\"十条指令\"执行措施方案", "建工集团安全管控\"十项零容忍\"措施方案",
        "预防高处坠落事故专项施工方案", "重大隐患排查治理专项方案",
        "\"一带一帽\"使用管理方案",
        "现场动火作业专项方案", "现场消防安全专项方案",
    ],
    "质量与创优": [
        "质量计划", "检验批专项方案", "样板先行专项方案",
        "创优质结构专项施工方案", "质量通病控制方案",
        "质量影像留存专项施工方案", "混凝土缺陷修补专项施工方案",
        "PC构件修补方案", "见证取样送检方案", "施工现场质量保证计划",
    ],
    "防水与渗漏": [
        "防水工程专项施工方案", "防渗漏工程专项施工方案",
        "地下结构防水工程专项方案", "装配式混凝土建筑防水",
        "渗漏修补专项方案", "外墙淋水试验专项方案及附图",
    ],
    "装配式与结构": [
        "装配式结构专项施工方案", "预制构件堆放专项施工方案",
        "预制构件加工及进场计划专项方案", "预制构件检测专项方案",
        "套筒灌浆标准方案", "套筒连接灌浆专项施工方案",
        "灌浆套筒检测方案", "二次结构专项施工方案", "砌体工程专项施工方案",
        "预制结构混凝土施工", "钢结构安装专项施工方案",
    ],
    "装饰装修": [
        "粗装饰工程专项施工方案", "精装修工程专项施工方案",
        "精装修施工组织设计方案", "地下室粗装饰专项施工方案",
        "保温工程专项施工方案", "外墙保温工程专项施工方案",
        "门窗工程专项施工方案", "铝合金门窗工程专项施工方案",
        "外墙装饰工程专项施工方案", "外墙涂料专项施工方案",
        "外立面线条专项方案", "屋面工程专项施工方案",
        "室外总体工程专项施工方案", "烟道安装专项施工方案",
        "建筑幕墙安装专项施工方案",
    ],
    "机电与智能化": [
        "机电安装施工组织设计", "地下结构机电安装工程施工组织设计",
        "地上结构机电安装工程施工组织设计",
        "智能化系统施工专项方案", "智能化改造更新工程专项方案",
        "通风空调工程更新专项方案", "消防工程专项施工方案",
        "消防改造更新专项方案", "建筑防雷接地更新专项方案",
        "节能工程施工方案", "节能保温工程专项方案",
    ],
    "应急与专项": [
        "应急预案", "安全生产应急预案专项施工方案",
        "防台防汛措施方案", "防台防汛应急预案", "防汛防台应急预案补充方案",
        "防暑降温专项方案", "冬雨季施工专项方案", "雨季施工方案",
        "新型冠状病毒感染的肺炎疫情防治方案",
        "治本攻坚三年行动方案", "安全生产治本攻坚三年行动治理实施方案",
        "重大事故隐患专项排查整治行动工作方案",
        "各类突发情况应急处置措施", "建筑拆除工程专项施工方案",
    ],
}

#: 分类 → 工程类型 / 专业（用于列表筛选与 AI 生成参考）
CATEGORY_PROFILE: dict[str, tuple[str, str]] = {
    "基坑与土方": ("房建", "土建"),
    "模板与支撑": ("房建", "土建"),
    "脚手架": ("房建", "土建"),
    "起重吊装": ("房建", "土建"),
    "临时设施": ("房建", "土建"),
    "安全文明": ("房建", "安全"),
    "质量与创优": ("房建", "质量"),
    "防水与渗漏": ("房建", "土建"),
    "装配式与结构": ("房建", "土建"),
    "装饰装修": ("房建", "装饰"),
    "机电与智能化": ("房建", "机电"),
    "应急与专项": ("房建", "安全"),
}


def _tags(scheme_name: str, category: str, meta: dict) -> str:
    """标签：分类 + 方案名 + 模板 + 危大分级，用于关键词搜索与筛选"""
    parts = [category, scheme_name, meta.get("risk", ""), meta.get("template", "")]
    return ",".join(p for p in parts if p)


def _build_record(scheme_name: str, category: str) -> dict:
    """生成一条预置目录的完整字段"""
    meta = get_meta(scheme_name)
    outline = build_outline(scheme_name)
    engineering_type, profession = CATEGORY_PROFILE.get(category, ("房建", "土建"))
    return {
        "name": f"{scheme_name}标准目录",
        "type": category,
        "engineering_type": engineering_type,
        "profession": profession,
        "applicable_conditions": meta.get("applicable", ""),
        "basis": meta.get("basis", ""),
        "outline_json": json.dumps(outline, ensure_ascii=False),
        "tags": _tags(scheme_name, category, meta),
    }


async def seed_catalog(force: bool = False) -> dict:
    """导入 / 升级预置方案目录到 outline_library 表。

    Args:
        force: True 时忽略版本号，强制刷新全部预置条目。

    Returns:
        {"inserted": int, "updated": int, "skipped": int, "total": int}
    """
    await init_db()
    conn = await get_conn()

    # 已存在的预置条目（按名称索引，保留 id 与 ref_count，避免破坏已有引用）
    cur = await conn.execute(
        "SELECT id, name, version, ref_count FROM outline_library WHERE source='预置清单'")
    existing = {r["name"]: dict(r) for r in await cur.fetchall()}

    inserted = updated = skipped = 0
    for category, schemes in SCHEME_CATALOG.items():
        for scheme_name in schemes:
            rec = _build_record(scheme_name, category)
            name = rec["name"]
            old = existing.get(name)

            if old and not force and old.get("version") == SEED_VERSION:
                skipped += 1
                continue

            if old:
                await conn.execute(
                    "UPDATE outline_library SET type=?, engineering_type=?, profession=?,"
                    " applicable_conditions=?, basis=?, outline_json=?, tags=?, version=?,"
                    " updated_at=datetime('now','localtime') WHERE id=?",
                    (rec["type"], rec["engineering_type"], rec["profession"],
                     rec["applicable_conditions"], rec["basis"], rec["outline_json"],
                     rec["tags"], SEED_VERSION, old["id"]))
                updated += 1
            else:
                await conn.execute(
                    "INSERT INTO outline_library (id, name, type, engineering_type, profession,"
                    " applicable_conditions, basis, outline_json, tags, version, source,"
                    " review_status, ref_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), name, rec["type"], rec["engineering_type"],
                     rec["profession"], rec["applicable_conditions"], rec["basis"],
                     rec["outline_json"], rec["tags"], SEED_VERSION,
                     "预置清单", "已通过", 0))
                inserted += 1

    await conn.commit()

    # 清理历史遗留的预置脏数据（名称已不在当前清单中的旧版本条目，
    # 如早期误录入的「大气污染防治D专项方案标准目录」），避免清单出现重复/错误项。
    valid_names = [f"{s}标准目录" for schemes in SCHEME_CATALOG.values() for s in schemes]
    placeholders = ",".join("?" * len(valid_names))
    cur = await conn.execute(
        f"DELETE FROM outline_library WHERE source='预置清单'"
        f" AND version<>? AND name NOT IN ({placeholders})",
        (SEED_VERSION, *valid_names))
    removed = cur.rowcount or 0
    if removed:
        # 同时清理其版本归档，避免孤儿记录
        await conn.execute(
            "DELETE FROM outline_library_versions WHERE library_id NOT IN"
            " (SELECT id FROM outline_library)")
        await conn.commit()
        logger.info("清理历史遗留预置条目 %d 条", removed)

    total = inserted + updated + skipped
    logger.info(
        "预置清单同步完成（%s）：新增 %d，刷新 %d，跳过 %d，清理 %d，合计 %d",
        SEED_VERSION, inserted, updated, skipped, removed, total)
    return {
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "removed": removed,
        "total": total,
        "version": SEED_VERSION,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="同步专项方案预置标准目录")
    parser.add_argument("--force", action="store_true", help="忽略版本号，强制刷新全部预置条目")
    args = parser.parse_args()
    result = asyncio.run(seed_catalog(force=args.force))
    print(f"同步完成：{result}")


if __name__ == "__main__":
    main()
