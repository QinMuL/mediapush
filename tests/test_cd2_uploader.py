"""Cd2UploaderService 单元测试（不连真实 CD2，gRPC 层 mock）。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from app.media.cd2_uploader import Cd2UploaderService, Cd2UploadReport


# ---------------------------------------------------------------------- #
# 测试工具
# ---------------------------------------------------------------------- #
@dataclass
class FakeFile:
    """模拟 CD2 CloudDriveFile。"""

    name: str
    fullPathName: str
    size: int
    isDirectory: bool = False
    writeTime: SimpleNamespace = field(
        default_factory=lambda: SimpleNamespace(seconds=1700000000)
    )


class FakeUploader(Cd2UploaderService):
    """重写 gRPC 层的假实现。"""

    def __init__(self, settings, src_files, dst_files, container=None):
        super().__init__(container, settings)
        self._src_files = src_files
        self._dst_files = dst_files
        self.submitted: list[str] = []
        self.deleted: list[str] = []
        self.tasks_result: list = []

    def _ensure_conn(self):
        return True

    def _login(self):
        return True

    def _list_dir(self, path):
        if path == self.src_dir:
            return self._src_files
        if path == self.dst_dir:
            return self._dst_files
        return []

    def _submit_copy(self, src_path):
        self.submitted.append(src_path)
        return True

    def _query_tasks(self):
        return self.tasks_result

    def _delete_file(self, src_path):
        self.deleted.append(src_path)
        return True


def make_settings(**overrides) -> SimpleNamespace:
    defaults = dict(  # noqa: C408
        cd2_upload_interval_seconds=60.0,
        cd2_address="127.0.0.1:19798",
        cd2_token="test-token",
        cd2_username="",
        cd2_password="",
        cd2_upload_src="/media/media/C",
        cd2_upload_dst="/115open/tmp",
        cd2_upload_dry_run=True,
        cd2_stuck_days=7.0,
        cd2_report_admin=True,
        tg_admin_ids=[42],
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_fake(tmp_path, src_files, dst_files, **overrides) -> FakeUploader:
    overrides.setdefault("state_db_path", str(tmp_path / "cd2_state.db"))
    u = FakeUploader(make_settings(**overrides), src_files, dst_files)
    return u


def _task(src="/media/media/C/a.mkv", status=3, uploaded=1024, total=1024):
    return SimpleNamespace(
        sourcePath=src,
        destPath="/115open/tmp",
        status=status,
        uploadedBytes=uploaded,
        totalBytes=total,
        errors=[SimpleNamespace(path="/x", error="boom")],
    )


# ---------------------------------------------------------------------- #
# 用例
# ---------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_dry_run_submits_nothing(tmp_path):
    """dry-run：只出日志，不提交 CopyFile；去重只记内存（不污染 completed）。"""
    u = make_fake(
        tmp_path,
        src_files=[FakeFile("a.mkv", "/media/media/C/a.mkv", 1024)],
        dst_files=[],
    )
    r1 = await u.run_once()  # 第 1 轮：快照（不稳定）
    assert r1.dry_submitted == 0
    r2 = await u.run_once()  # 第 2 轮：稳定 → 模拟提交
    assert r2.dry_submitted == 1
    assert u.submitted == []
    # dry-run 模拟结果只进内存集合：切实际模式后该文件正常上传
    assert "/media/media/C/a.mkv" in u._dry_done
    assert "/media/media/C/a.mkv" not in u._completed
    r3 = await u.run_once()  # 第 3 轮：dry_done 内存去重，不重复出日志
    assert r3.dry_submitted == 0


@pytest.mark.asyncio
async def test_dedup_skips_existing(tmp_path):
    """115 目标已有同名 → 跳过记完成。"""
    u = make_fake(
        tmp_path,
        src_files=[FakeFile("a.mkv", "/media/media/C/a.mkv", 1024)],
        dst_files=[FakeFile("a.mkv", "/115open/tmp/a.mkv", 1024)],
    )
    await u.run_once()
    r = await u.run_once()
    assert r.skipped == 1
    assert u.submitted == []
    assert "/media/media/C/a.mkv" in u._completed


@pytest.mark.asyncio
async def test_real_submit_and_task_complete(tmp_path):
    """实际模式：提交 → 任务完成 → 删源 + 记完成。"""
    u = make_fake(
        tmp_path,
        src_files=[FakeFile("a.mkv", "/media/media/C/a.mkv", 2048)],
        dst_files=[],
        cd2_upload_dry_run=False,
    )
    await u.run_once()
    r = await u.run_once()
    assert r.submitted == 1
    assert u.submitted == ["/media/media/C/a.mkv"]
    assert "/media/media/C/a.mkv" in u._tasks

    u.tasks_result = [_task(status=3)]
    r2 = await u.run_once()
    await asyncio.sleep(0.05)  # 等 executor 删源
    assert r2.completed == 1
    assert u.deleted == ["/media/media/C/a.mkv"]
    assert "/media/media/C/a.mkv" in u._completed
    assert "/media/media/C/a.mkv" not in u._tasks


@pytest.mark.asyncio
async def test_serial_one_task_at_a_time(tmp_path):
    """串行：有活跃任务时不再提交新的。"""
    files = [
        FakeFile("a.mkv", "/media/media/C/a.mkv", 1024),
        FakeFile("b.mkv", "/media/media/C/b.mkv", 1024),
    ]
    u = make_fake(tmp_path, src_files=files, dst_files=[], cd2_upload_dry_run=False)
    await u.run_once()
    r = await u.run_once()
    assert r.submitted == 1
    assert len(u._tasks) == 1

    u.tasks_result = [_task(status=0)]  # Pending 中
    r2 = await u.run_once()
    assert r2.submitted == 0
    assert len(u._tasks) == 1


@pytest.mark.asyncio
async def test_task_failed_records_retry(tmp_path):
    """任务失败（status=4）→ 记退避，不删源。"""
    u = make_fake(
        tmp_path,
        src_files=[FakeFile("a.mkv", "/media/media/C/a.mkv", 1024)],
        dst_files=[],
        cd2_upload_dry_run=False,
    )
    await u.run_once()
    await u.run_once()
    u.tasks_result = [_task(status=4)]
    r = await u.run_once()
    assert r.failed == 1
    assert u.deleted == []
    assert u._retry_state["/media/media/C/a.mkv"]["failures"] == 1


@pytest.mark.asyncio
async def test_state_persistence(tmp_path):
    """completed/退避状态入库（state.db），重启恢复。"""
    from pathlib import Path

    u = make_fake(
        tmp_path,
        src_files=[FakeFile("a.mkv", "/media/media/C/a.mkv", 1024)],
        dst_files=[],
        cd2_upload_dry_run=False,
    )
    await u.run_once()
    await u.run_once()  # 稳定 → 提交
    u.tasks_result = [_task(status=3)]
    await u.run_once()
    await asyncio.sleep(0.05)  # 等 executor 删源
    u._retry_state["/media/media/C/x.mkv"] = {
        "failures": 2, "first_seen": time.time(), "next_retry": time.time() + 3600
    }
    u._save_state()
    assert Path(u._store.db_path).exists()

    u2 = make_fake(tmp_path, src_files=[], dst_files=[])
    u2._load_state()
    assert "/media/media/C/a.mkv" in u2._completed
    assert u2._retry_state["/media/media/C/x.mkv"]["failures"] == 2


def test_report_summary():
    r = Cd2UploadReport(scanned=5, completed=2, skipped=1, failed=1, stuck=1, active=1)
    s = r.summary()
    assert "扫描 5" in s
    assert "上传完成 2" in s
    assert "已存在跳过 1" in s
    assert "失败退避 1" in s
    assert "卡死" in s


def test_status_lines(tmp_path):
    u = make_fake(tmp_path, src_files=[], dst_files=[])
    lines = u.status_lines()
    assert any("CD2 上传" in x for x in lines)
    assert any("DRY_RUN" in x for x in lines)


# ---------------------------------------------------------------------- #
# 建议 2：列目录返回 None 时冷却 warning（5min 内同路径只 1 warning）
# ---------------------------------------------------------------------- #
class FakeUploaderListDirNone(FakeUploader):
    """_list_dir 永远返回 None（模拟 CD2 命名空间路径错 / gRPC 断）。"""

    def _list_dir(self, path):
        return None


@pytest.mark.asyncio
async def test_list_dir_none_emits_warning_with_cooldown(tmp_path, caplog):
    """首 2 轮连续 None：只 1 条 warning（冷却 5min）；冷却外再 1 轮再打 1 条。"""
    import logging as _lg
    from unittest.mock import patch as _patch

    u = FakeUploaderListDirNone(make_settings(), [], [])
    u.state_file = tmp_path / "cd2_state.json"
    caplog.set_level(_lg.WARNING, logger="app.media.cd2_uploader")

    # 冷却 300s = 5min；用「时钟固定再手动推进」模式，确保 cd2_uploader 内任何多次
    # time.time() 调用在「同一轮」内看到同一时间戳（避免 next(iter) 消耗）。
    clock = {"t": 1_700_000_000.0}

    def _tick(delta: float = 0.0) -> None:
        clock["t"] += delta

    with _patch("app.media.cd2_uploader.time.time", lambda: clock["t"]):
        # 轮 1：首次 None → 应 warning
        await u.run_once()
        warns = [r for r in caplog.records if r.levelno >= _lg.WARNING
                 and "CD2 源目录" in r.getMessage()]
        assert len(warns) == 1, f"首轮: expect=1, actual={[r.getMessage() for r in warns]}"
        caplog.clear()

        # 轮 2：推进 36s（仍在 300s 冷却窗内）→ 不 warning
        _tick(36.0)
        await u.run_once()
        warns = [r for r in caplog.records if r.levelno >= _lg.WARNING
                 and "CD2 源目录" in r.getMessage()]
        assert len(warns) == 0, f"冷却内(36s): expect=0, actual={[r.getMessage() for r in warns]}"
        caplog.clear()

        # 轮 3：推进到 300s 正点（刚好 300s，>= 冷却）→ 仍按 >= 判断，会打
        _tick(264.0)  # 36+264 = 300s
        await u.run_once()
        warns = [r for r in caplog.records if r.levelno >= _lg.WARNING
                 and "CD2 源目录" in r.getMessage()]
        assert len(warns) == 1, f"冷却到期: expect=1, actual={[r.getMessage() for r in warns]}"
        caplog.clear()

        # 轮 4：再推 10s → 310s 但还没到 300+300=600s → 不打
        _tick(10.0)
        await u.run_once()
        warns = [r for r in caplog.records if r.levelno >= _lg.WARNING
                 and "CD2 源目录" in r.getMessage()]
        assert len(warns) == 0, f"第2冷却窗内(10s): expect=0, actual={[r.getMessage() for r in warns]}"


@pytest.mark.asyncio
async def test_list_dir_none_returns_empty_report(tmp_path):
    """列目录返回 None 时 report 全 0 不抛错（保持现有静默+空 report 行为）。"""
    u = FakeUploaderListDirNone(make_settings(), [], [])
    u.state_file = tmp_path / "cd2_state.json"
    r = await u.run_once()
    assert r.scanned == 0
    assert r.submitted == 0
    assert r.completed == 0


# ---------------------------------------------------------------------- #
# admin 通知：有动作才发 + 明细行 + 开关 + 空轮静默
# ---------------------------------------------------------------------- #
class _FakeTg:
    """同时模拟 TelegramService（send_message）与其 raw bot（可编辑）。

    send_message 返回带 message_id 的对象；bot 属性指向自身，
    使 container.telegram.bot.edit_message_text 可用（进度条消息路径）。
    """

    def __init__(self) -> None:
        self.sent: list[tuple[object, str]] = []
        self.edits: list[tuple[object, int, str]] = []
        self._next_id = 500

    async def send_message(self, chat_id, text):
        self._next_id += 1
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))

    @property
    def bot(self):
        return self


class _FakeContainer:
    def __init__(self, tg) -> None:
        self.telegram = tg


@pytest.mark.asyncio
async def test_send_report_completed_with_details(tmp_path):
    """完成轮：发 admin 且带文件名明细。"""
    tg = _FakeTg()
    u = make_fake(tmp_path, src_files=[], dst_files=[], cd2_upload_dry_run=False)
    u.container = _FakeContainer(tg)

    report = Cd2UploadReport(scanned=3, completed=1)
    report.details.append("✅ a.mkv（1.00GB · 5.0 分钟 · 3.3 MB/s）")
    await u._send_report(report)

    assert len(tg.sent) == 1
    cid, text = tg.sent[0]
    assert cid == 42
    assert "📤 CD2 上传汇总" in text
    assert "上传完成 1" in text
    assert "a.mkv" in text


@pytest.mark.asyncio
async def test_send_report_empty_round_silent(tmp_path):
    """空轮（只有扫描/传输中，无动作）不发通知。"""
    tg = _FakeTg()
    u = make_fake(tmp_path, src_files=[], dst_files=[])
    u.container = _FakeContainer(tg)

    await u._send_report(Cd2UploadReport(scanned=5, active=2))
    assert tg.sent == []


@pytest.mark.asyncio
async def test_send_report_disabled_by_config(tmp_path):
    """CD2_REPORT_ADMIN=false：有动作也不发。"""
    tg = _FakeTg()
    u = make_fake(tmp_path, src_files=[], dst_files=[], cd2_report_admin=False)
    u.container = _FakeContainer(tg)

    await u._send_report(Cd2UploadReport(completed=1))
    assert tg.sent == []


@pytest.mark.asyncio
async def test_send_report_dry_run_header(tmp_path):
    """dry-run 模式带 [DRY-RUN] 头，明细含模拟上传行。"""
    tg = _FakeTg()
    u = make_fake(tmp_path, src_files=[], dst_files=[])  # 默认 dry_run=True
    u.container = _FakeContainer(tg)

    report = Cd2UploadReport(dry_submitted=1)
    report.details.append("🔍 [DRY-RUN] 将上传 a.mkv（1.00GB）")
    await u._send_report(report)

    assert len(tg.sent) == 1
    _, text = tg.sent[0]
    assert "[DRY-RUN] CD2 上传汇总" in text
    assert "将上传 a.mkv" in text


@pytest.mark.asyncio
async def test_send_report_no_container_silent(tmp_path):
    """container=None（未接线）：不抛错不发。"""
    u = make_fake(tmp_path, src_files=[], dst_files=[])
    await u._send_report(Cd2UploadReport(completed=1))  # 不应抛异常


@pytest.mark.asyncio
async def test_run_once_populates_details(tmp_path):
    """run_once 的 dry-run 提交轮会写明细行（通知内容来源）。"""
    u = make_fake(
        tmp_path,
        src_files=[FakeFile("a.mkv", "/media/media/C/a.mkv", 1024)],
        dst_files=[],
    )
    await u.run_once()  # 快照轮
    r = await u.run_once()  # 稳定 → dry 提交
    assert r.dry_submitted == 1
    assert any("a.mkv" in d for d in r.details)


# ---------------------------------------------------------------------- #
# 进度条消息：提交发条 → 传输中编辑 → 完成/失败收尾；汇总不重复发
# ---------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_progress_lifecycle_submit_transfer_complete(tmp_path):
    """实际模式全流程：提交发进度条 → 传输中编辑（字节+速度+ETA）→ 完成收尾；
    该任务的提交/完成均由进度消息单独通知，不再进汇总明细。"""
    tg = _FakeTg()
    u = make_fake(
        tmp_path,
        src_files=[FakeFile("a.mkv", "/media/media/C/a.mkv", 2048)],
        dst_files=[],
        cd2_upload_dry_run=False,
    )
    u.container = _FakeContainer(tg)
    await u.run_once()  # 快照轮

    r = await u.run_once()  # 稳定 → 提交 + 发进度条
    assert r.submitted == 1
    assert r.progress_notified == 1  # 提交已由进度消息通知
    assert not any("新任务" in d for d in r.details)
    assert len(tg.sent) == 1  # 每个 admin 一条进度消息
    assert "📤 CD2 上传开始 · a.mkv" in tg.sent[0][1]
    assert f"[{'░' * 20}] 0%" in tg.sent[0][1]

    u.tasks_result = [_task(status=2, uploaded=1024, total=2048)]
    await u.run_once()  # 传输中 → 编辑进度条
    assert len(tg.edits) == 1
    etext = tg.edits[0][2]
    assert "📤 CD2 上传中 · a.mkv" in etext
    assert "50%" in etext
    assert tg.edits[0][1] == 501  # 编辑的是开始时发的那条消息
    assert tg.edits[0][0] == 42  # 发给了配置的 admin

    u.tasks_result = [_task(status=3, uploaded=2048, total=2048)]
    r2 = await u.run_once()  # 完成 → 编辑为完成态
    assert r2.completed == 1
    assert r2.progress_notified == 1
    assert "✅ CD2 上传完成 · a.mkv" in tg.edits[-1][2]
    assert "[████████████████████] 100%" in tg.edits[-1][2]
    # 已单独通知 → 汇总不再需要发（动作全被进度消息覆盖）
    await u._send_report(r2)
    assert tg.sent[-1][1] == tg.sent[0][1]  # 没有新发消息


@pytest.mark.asyncio
async def test_progress_lifecycle_failure(tmp_path):
    """失败路径：进度消息编辑为失败态（原因+退避），汇总不重复。"""
    tg = _FakeTg()
    u = make_fake(
        tmp_path,
        src_files=[FakeFile("a.mkv", "/media/media/C/a.mkv", 2048)],
        dst_files=[],
        cd2_upload_dry_run=False,
    )
    u.container = _FakeContainer(tg)
    await u.run_once()
    await u.run_once()  # 提交 + 进度消息

    u.tasks_result = [_task(status=4)]
    r = await u.run_once()  # 失败
    assert r.failed == 1
    assert r.progress_notified == 1
    assert "⚠️ CD2 上传失败 · a.mkv" in tg.edits[-1][2]
    assert "1.0h 后重试" in tg.edits[-1][2]


@pytest.mark.asyncio
async def test_progress_transferring_without_bytes_shows_elapsed(tmp_path):
    """CD2 不报字节（uploadedBytes=0）：降级显示已传时长而非 0% 假进度。"""
    tg = _FakeTg()
    u = make_fake(
        tmp_path,
        src_files=[FakeFile("a.mkv", "/media/media/C/a.mkv", 2048)],
        dst_files=[],
        cd2_upload_dry_run=False,
    )
    u.container = _FakeContainer(tg)
    await u.run_once()
    await u.run_once()  # 提交

    u.tasks_result = [_task(status=2, uploaded=0, total=2048)]
    await u.run_once()
    assert "传输中（CD2 单文件不报字节进度）" in tg.edits[-1][2]
    assert "已 " in tg.edits[-1][2]


@pytest.mark.asyncio
async def test_progress_suppressed_when_report_admin_off(tmp_path):
    """CD2_REPORT_ADMIN=false：不发进度条消息，走原汇总明细路径。"""
    tg = _FakeTg()
    u = make_fake(
        tmp_path,
        src_files=[FakeFile("a.mkv", "/media/media/C/a.mkv", 2048)],
        dst_files=[],
        cd2_upload_dry_run=False,
        cd2_report_admin=False,
    )
    u.container = _FakeContainer(tg)
    await u.run_once()
    r = await u.run_once()  # 提交
    assert r.submitted == 1
    assert r.progress_notified == 0
    assert tg.sent == []
    assert any("新任务 a.mkv" in d for d in r.details)  # 回退到汇总明细
