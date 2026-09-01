"""Ed2kPusherService 单测。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.core.processor import ProcessResult
from app.media.ed2k_pusher import Ed2kPusherService, PushReport


class _FakeSettings:
    ed2k_push_interval_seconds = 60.0
    ed2k_push_stuck_days = 7.0
    ed2k_push_dry_run = True
    ed2k_push_report_admin = False
    ed2k_push_report_channel = False
    tg_admin_ids: list = []


class _FakeTg:
    """模拟 TelegramService + raw bot（进度条消息需要 edit_message_text）。"""

    def __init__(self) -> None:
        self.sent: list = []
        self.edits: list = []
        self._next_id = 700

    async def send_message(self, chat_id, text):
        from types import SimpleNamespace
        self._next_id += 1
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))

    @property
    def bot(self):
        return self


class _FakeProcessor:
    def __init__(self, *, fail_once=0, ok=True, dup=False, message=""):
        self.calls = []
        self._fail_once = fail_once
        self._ok = ok
        self._dup = dup
        self._message = message

    async def process(self, parsed):
        self.calls.append(parsed.code)
        if self._fail_once > 0:
            self._fail_once -= 1
            return ProcessResult(False, "临时失败", dup=False)
        return ProcessResult(ok=self._ok, message=self._message, dup=self._dup, title="t")


@pytest.fixture()
def sandbox(tmp_path: Path):
    class _S:
        pass
    box = _S()
    box.jsonl = tmp_path / "ed2k_results.jsonl"
    box.state_db = tmp_path / "state.db"
    return box


def _mk_service(sandbox, *, processor=None, dry_run=True):
    st = _FakeSettings()
    st.ed2k_push_dry_run = dry_run
    st.state_db_path = str(sandbox.state_db)

    class _C:
        pass
    container = _C()
    container.processor = processor
    svc = Ed2kPusherService(container, st)
    svc.results_file = sandbox.jsonl
    return svc


URL1 = "ed2k://|file|f1.mkv|5000|aabbccdd|/"
URL2 = "ed2k://|file|f2.mkv|8000|eeff0011|/"


def test_dry_run_does_not_call_processor(sandbox):
    recs = [
        {"ed2k": URL1, "name": "f1.mkv", "size_bytes": 5000, "at": 1},
        {"ed2k": URL2, "name": "f2.mkv", "size_bytes": 8000, "at": 2},
    ]
    sandbox.jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
        encoding="utf-8",
    )
    proc = _FakeProcessor()
    svc = _mk_service(sandbox, processor=proc, dry_run=True)
    r = asyncio.run(svc.run_once())
    assert r.read == 2 and r.dry_pushed == 2
    assert proc.calls == []


def test_incremental_then_push(sandbox):
    sandbox.jsonl.write_text(
        json.dumps({"ed2k": URL1, "name": "f1", "size_bytes": 1, "at": 1}) + "\n",
        encoding="utf-8",
    )
    proc = _FakeProcessor()
    svc = _mk_service(sandbox, processor=proc, dry_run=False)
    r1 = asyncio.run(svc.run_once())
    assert r1.pushed == 1 and proc.calls == [URL1]
    with sandbox.jsonl.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ed2k": URL2, "name": "f2", "size_bytes": 2, "at": 2}) + "\n")
    r2 = asyncio.run(svc.run_once())
    assert r2.read == 1 and r2.pushed == 1
    assert proc.calls == [URL1, URL2]


def test_retry_state_is_recorded(sandbox):
    sandbox.jsonl.write_text(
        json.dumps({"ed2k": URL1, "name": "f1", "size_bytes": 1, "at": 1}) + "\n",
        encoding="utf-8",
    )
    proc = _FakeProcessor(fail_once=1)
    svc = _mk_service(sandbox, processor=proc, dry_run=False)
    r1 = asyncio.run(svc.run_once())
    assert r1.failed == 1
    assert URL1 in svc._state


def test_dup_counted(sandbox):
    sandbox.jsonl.write_text(
        json.dumps({"ed2k": URL1, "name": "f1", "size_bytes": 1, "at": 1}) + "\n",
        encoding="utf-8",
    )
    proc = _FakeProcessor(dup=True)
    svc = _mk_service(sandbox, processor=proc, dry_run=False)
    r = asyncio.run(svc.run_once())
    assert r.skipped_dup == 1


def test_offset_persisted(sandbox):
    sandbox.jsonl.write_text(
        json.dumps({"ed2k": URL1, "name": "f1", "size_bytes": 1, "at": 1}) + "\n",
        encoding="utf-8",
    )
    svc = _mk_service(sandbox, processor=_FakeProcessor(), dry_run=False)
    asyncio.run(svc.run_once())
    proc2 = _FakeProcessor()
    svc2 = _mk_service(sandbox, processor=proc2, dry_run=False)
    asyncio.run(svc2.run_once())
    assert proc2.calls == []


def test_report_summary():
    r = PushReport(read=5, pushed=2, dry_pushed=1, skipped_dup=1, failed=1, stuck=1)
    s = r.summary()
    assert "推送 2" in s and "模拟推送 1" in s
    assert "已推过去重 1" in s and "失败退避 1" in s and "卡死 1" in s


# ---------------------------------------------------------------------- #
# 批量进度条消息（≥2 条实际推送轮：进度条 → 收尾编辑为汇总）
# ---------------------------------------------------------------------- #
URL3 = "ed2k://|file|f3.mkv|9000|11223344|/"


def _write_records(sandbox, urls):
    lines = [
        json.dumps({"ed2k": u, "name": f"f{i+1}.mkv", "size_bytes": 100, "at": i + 1})
        for i, u in enumerate(urls)
    ]
    sandbox.jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mk_progress_service(sandbox, tg, processor):
    st = _FakeSettings()
    st.ed2k_push_dry_run = False
    st.ed2k_push_report_admin = True
    st.tg_admin_ids = [7]
    st.state_db_path = str(sandbox.state_db)

    class _C:
        pass
    container = _C()
    container.processor = processor
    container.telegram = tg
    svc = Ed2kPusherService(container, st)
    svc.results_file = sandbox.jsonl
    return svc


def test_batch_round_sends_progress_and_final_edited_to_summary(sandbox):
    """≥2 条实际推送轮：先发进度条消息，收尾直接编辑为汇总（不另发新消息）。"""
    tg = _FakeTg()
    proc = _FakeProcessor()
    svc = _mk_progress_service(sandbox, tg, proc)
    _write_records(sandbox, [URL1, URL2, URL3])

    r = asyncio.run(svc.run_once())
    assert r.pushed == 3
    # 进度条消息：每个 admin 一条，初始 0/3
    assert len(tg.sent) == 1
    cid, text = tg.sent[0]
    assert cid == 7
    assert "📤 ed2k 推送进度 · 0/3" in text
    assert "[░░░░░░░░░░░░░░░░░░░░] 0%" in text

    asyncio.run(svc._send_report(r))
    # 汇总通过编辑进度消息送达（不是新发）
    assert len(tg.sent) == 1
    assert len(tg.edits) == 1
    etext = tg.edits[0][2]
    assert "ed2k 推送汇总" in etext
    assert "读取 3 条新记录：推送 3" in etext


def test_single_record_round_skips_progress(sandbox):
    """单条记录轮：不发进度条，汇总正常新发。"""
    tg = _FakeTg()
    proc = _FakeProcessor()
    svc = _mk_progress_service(sandbox, tg, proc)
    _write_records(sandbox, [URL1])

    r = asyncio.run(svc.run_once())
    assert r.pushed == 1
    assert tg.sent == []  # 没有进度消息

    asyncio.run(svc._send_report(r))
    assert len(tg.sent) == 1  # 汇总走新发
    assert tg.edits == []
    assert "推送 1" in tg.sent[0][1]


def test_dry_run_round_skips_progress(sandbox):
    """dry-run 轮处理再快也不发进度条（瞬时完成无意义）。"""
    tg = _FakeTg()
    st = _FakeSettings()
    st.ed2k_push_dry_run = True
    st.ed2k_push_report_admin = True
    st.tg_admin_ids = [7]
    st.state_db_path = str(sandbox.state_db)

    class _C:
        pass
    container = _C()
    container.processor = _FakeProcessor()
    container.telegram = tg
    svc = Ed2kPusherService(container, st)
    svc.results_file = sandbox.jsonl
    _write_records(sandbox, [URL1, URL2])

    r = asyncio.run(svc.run_once())
    assert r.dry_pushed == 2
    assert tg.sent == [] and tg.edits == []


def test_progress_not_sent_when_report_admin_off(sandbox):
    """REPORT_ADMIN=false：多条实际推送轮也不发进度条。"""
    tg = _FakeTg()
    proc = _FakeProcessor()
    svc = _mk_progress_service(sandbox, tg, proc)
    svc.settings.ed2k_push_report_admin = False
    _write_records(sandbox, [URL1, URL2])

    r = asyncio.run(svc.run_once())
    assert r.pushed == 2
    assert tg.sent == [] and tg.edits == []
