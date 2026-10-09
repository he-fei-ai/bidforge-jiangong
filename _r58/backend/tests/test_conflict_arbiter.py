"""冲突仲裁器 单元测试：AI 分批仲裁与失败隔离、批量回写。"""
import app.services.conflict_arbiter as ca
import pytest


def _conflict(cid: str, topic: str = "项目总工期") -> dict:
    return {
        "id": cid,
        "conflict_type": "numeric",
        "topic": topic,
        "severity": "medium",
        "occurrences": [
            {"section_id": "S1", "section_title": "S1", "value": "100天", "text": "t1"},
            {"section_id": "S2", "section_title": "S2", "value": "100天", "text": "t2"},
        ],
    }


class _FakeDB:
    def __init__(self):
        self.executed: list[tuple] = []
        self.executemany_calls = 0
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, sql, params=()):
        self.executed.append(params)

    async def executemany(self, sql, rows):
        self.executemany_calls += 1
        self.executed.extend(rows)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class TestAiArbitrateBatching:
    async def test_大批量按批次拆分请求(self, monkeypatch):
        monkeypatch.setattr(ca, "ARBITRATE_BATCH_SIZE", 20)
        seen_sizes: list[int] = []

        async def fake_batch(batch, **kw):
            seen_sizes.append(len(batch))
            return {c["id"]: {"conflict_id": c["id"], "authoritative_value": "100天"}
                    for c in batch}

        monkeypatch.setattr(ca, "_arbitrate_batch", fake_batch)
        conflicts = [_conflict(f"C{i}") for i in range(45)]
        results = await ca.ai_arbitrate(conflicts, facts="", design_docs="",
                                        standards="", project_requirements="")
        assert seen_sizes == [20, 20, 5]
        assert len(results) == 45

    async def test_单批失败只降级该批不影响其他批(self, monkeypatch):
        monkeypatch.setattr(ca, "ARBITRATE_BATCH_SIZE", 20)
        calls = {"n": 0}

        async def fake_batch(batch, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("timeout")
            return {c["id"]: {"conflict_id": c["id"], "authoritative_value": "100天"}
                    for c in batch}

        monkeypatch.setattr(ca, "_arbitrate_batch", fake_batch)
        conflicts = [_conflict(f"C{i}") for i in range(45)]
        results = await ca.ai_arbitrate(conflicts, facts="", design_docs="",
                                        standards="", project_requirements="")
        assert len(results) == 25  # 45=20+20+5，第 2 批 20 条降级为空
        assert "C20" not in results and "C39" not in results
        assert "C0" in results and "C44" in results

    async def test_空输入不请求(self, monkeypatch):
        called = {"n": 0}

        async def fake_batch(batch, **kw):
            called["n"] += 1
            return {}

        monkeypatch.setattr(ca, "_arbitrate_batch", fake_batch)
        assert await ca.ai_arbitrate([], facts="", design_docs="",
                                     standards="", project_requirements="") == {}
        assert called["n"] == 0


class TestArbitrateConflictsPersist:
    async def test_单事务executemany批量回写(self, monkeypatch):
        async def fake_ai(conflicts, **kw):
            return {c["id"]: {"authoritative_value": "120天",
                              "authoritative_source": "AI", "severity": "high"}
                    for c in conflicts}

        monkeypatch.setattr(ca, "ai_arbitrate", fake_ai)
        db = _FakeDB()
        conflicts = [_conflict("C1"), _conflict("C2")]
        out = await ca.arbitrate_conflicts(db, conflicts, facts="")
        assert len(out) == 2
        assert db.executemany_calls == 1
        assert db.commits == 1
        assert len(db.executed) == 2
        assert out[0]["authoritative_value"] == "120天"
        assert out[0]["status"] == "pending"

    async def test_无权威值标记skipped(self, monkeypatch):
        monkeypatch.setattr(ca, "ai_arbitrate",
                            lambda *a, **k: _async_return({}))
        db = _FakeDB()
        # occurrences 取值互异、无多数 → skipped
        c = {
            "id": "C9", "conflict_type": "text", "topic": "描述",
            "occurrences": [
                {"section_id": "S1", "value": "甲"},
                {"section_id": "S2", "value": "乙"},
            ],
        }
        out = await ca.arbitrate_conflicts(db, [c], facts="")
        assert out[0]["status"] == "skipped"
        assert out[0]["authoritative_value"] == ""
        assert db.executed[0][5] == "skipped"


async def _async_return(value):
    return value