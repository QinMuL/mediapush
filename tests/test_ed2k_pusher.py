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
    box.state = tmp_path / "state.json"
    return box


def _mk_service(sandbox, *, processor=None, dry_run=True):
    st = _FakeSettings()
    st.ed2k_push_dry_run = dry_run

    class _C:
        pass
    container = _C()
    container.processor = processor
    svc = Ed2kPusherService(container, st)
    svc.results_file = sandbox.jsonl
    svc.state_file = sandbox.state
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
