"""统一媒体流水线测试：dry 语义 / 全链流转 / 串行上传 / 恢复 / 退避。

纯 tmp_path + gRPC 层 Fake（不连真实 CD2/TMDB/网络）。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

import app.pipeline.service as pipeline_mod
from app.core.processor import ProcessResult
from app.media.cleaner import CleanError, CleanReport
from app.media.namer import NamingResult
from app.parser.media_parser import MediaData
from app.pipeline.service import PipelineReport, PipelineService


# ---------------------------------------------------------------------- #
# Fakes
# ---------------------------------------------------------------------- #
class _FakeSettings:
    pipeline_enabled = True
    pipeline_input_dir = ""
    pipeline_library_dir = ""
    pipeline_interval_seconds = 10.0
    pipeline_stable_rounds = 2
    pipeline_batch_max = 5
    pipeline_min_size_mb = 0.0
    pipeline_stuck_days = 7.0
    pipeline_rename_dry_run = True
    pipeline_push_dry_run = True
    pipeline_upload_dry_run = True
    pipeline_report_admin = False
    cd2_address = "127.0.0.1:19798"
    cd2_token = "test-token"
    cd2_username = ""
    cd2_password = ""
    cd2_upload_src = ""
    cd2_upload_dst = "/115open/tmp"
    tg_admin_ids: ClassVar[list] = []
    state_db_path = ""


class _FakeProcessor:
    def __init__(self, *, fail=False, dup=False):
        self.calls: list[str] = []
        self._fail = fail
        self._dup = dup

    async def process(self, parsed, chat_id=None):
        self.calls.append(parsed.code)
        if self._fail:
            return ProcessResult(False, "推送失败", dup=False)
        if self._dup:
            return ProcessResult(False, "已推送过", dup=True)
        return ProcessResult(True, "ok", title="t")


class _FakeContainer:
    def __init__(self, processor=None):
        self.tmdb = object()  # 非 None 即可（analyze_file 由 monkeypatch 接管）
        self.processor = processor
        self.telegram = None


@dataclass
class _FakeFile:
    name: str
    isDirectory: bool = False


@dataclass
class _FakeCd2Task:
    status: int
    sourcePath: str
    destPath: str
    uploadedBytes: int = 0
    totalBytes: int = 0
    uploadedFiles: int = 0
    totalFiles: int = 0
    errors: list = field(default_factory=list)
    startTime: SimpleNamespace = field(
        default_factory=lambda: SimpleNamespace(seconds=0)
    )


class FakePipeline(PipelineService):
    """重写 gRPC 层的假实现（继承全部阶段逻辑）。

    _cd2_path/_local_path 恒等映射：测试里 CD2 路径 = 本地路径
    （真实映射逻辑由独立单测覆盖，避免 Windows 分隔符干扰）。
    """

    def __init__(self, container, settings, a: Path, b: Path):
        super().__init__(container, settings)
        self.input_dir = a
        self.library_dir = b
        self.cd2_src = str(b)
        self.dst_files: list = []
        self.submitted: list = []
        self.deleted: list = []
        self.tasks_result: list = []

    def _cd2_path(self, local_path: str) -> str:
        return local_path

    def _local_path(self, cd2_path: str) -> str:
        return cd2_path

    def _ensure_conn(self):
        return True

    def _login(self):
        return True

    def _list_dir(self, path):
        if path == self.cd2_dst:
            return self.dst_files
        return []

    def _submit_copy(self, cd2_paths):
        self.submitted.append(cd2_paths)
        return True

    def _query_tasks(self):
        return self.tasks_result

    def _delete_file(self, cd2_path):
        # 模拟 CD2 删除挂载源文件（真实场景 B 是 CD2 挂载，删 CD2 路径即删本地）
        try:
            Path(cd2_path).unlink()
        except OSError:
            pass
        self.deleted.append(cd2_path)
        return True


@pytest.fixture()
def dirs(tmp_path: Path):
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    return a, b


@pytest.fixture(autouse=True)
def _fast_push(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "_PUSH_GAP", 0.0)


def _mk_service(a, b, processor=None, **overrides):
    st = _FakeSettings()
    st.state_db_path = str(a.parent / "state.db")
    for k, v in overrides.items():
        setattr(st, k, v)
    svc = FakePipeline(_FakeContainer(processor), st, a, b)
    svc.results_file = a.parent / "results.jsonl"
    return svc


def _high_conf_result(name="Furious.S01E04.2026.2160p.mkv"):
    return NamingResult(
        parsed=MediaData(title="Furious", year=2026, media_type="tv",
                         season=1, episode=4, raw=name),
        details={"title": "狂怒追缉", "year": 2026},
        proposed="狂怒追缉.2026.S01E04.第04集.2160p.WEB-DL.H.265.mkv",
    )


def _patch_high_conf(monkeypatch, result=None):
    result = result or _high_conf_result()
    monkeypatch.setattr(
        "app.pipeline.service.analyze_file",
        lambda path, tmdb: asyncio.sleep(0, result=result),
    )


# ---------------------------------------------------------------------- #
# 纯函数：临时/视频/伴行判定
# ---------------------------------------------------------------------- #
def test_is_temp_file():
    assert pipeline_mod.is_temp_file(Path("movie.mkv.!qb"))
    assert pipeline_mod.is_temp_file(Path("movie.part"))
    assert not pipeline_mod.is_temp_file(Path("movie.mkv"))


def test_is_video_file():
    assert pipeline_mod.is_video_file(Path("a.mkv"))
    assert pipeline_mod.is_video_file(Path("B.MP4"))
    assert not pipeline_mod.is_video_file(Path("a.nfo"))


def test_report_summary_composition():
    r = PipelineReport(scanned=5, renamed=1, hashed=1, pushed=1,
                       completed=1, submitted=1)
    s = r.summary()
    assert "A 扫描 5" in s and "重命名 1" in s
    assert "哈希 1" in s and "推送 1" in s and "上传完成 1" in s


# ---------------------------------------------------------------------- #
# ① 重命名阶段
# ---------------------------------------------------------------------- #
def test_rename_dry_flow(dirs, monkeypatch):
    """dry：只出拟移动日志，A 原文件不动、不哈希。"""
    a, b = dirs
    video = a / "Furious.S01E04.2026.2160p.mkv"
    video.write_bytes(b"0" * 64)
    svc = _mk_service(a, b)
    _patch_high_conf(monkeypatch)

    asyncio.run(svc.run_once())  # 快照
    r2 = asyncio.run(svc.run_once())  # 稳定 → dry 模拟
    assert r2.dry_renamed == 1 and r2.hashed == 0
    assert video.exists()
    assert not list(b.iterdir())
    assert str(video) in svc._dry_renamed


def test_rename_dry_flip_processes_immediately(dirs, monkeypatch):
    """dry→实际热切换：模拟过的文件立即正常处理（无需重启）。"""
    a, b = dirs
    video = a / "Furious.S01E04.2026.2160p.mkv"
    video.write_bytes(b"0" * 64)
    svc = _mk_service(a, b)
    _patch_high_conf(monkeypatch)

    asyncio.run(svc.run_once())
    r2 = asyncio.run(svc.run_once())
    assert r2.dry_renamed == 1

    svc.settings.pipeline_rename_dry_run = False
    # dry 处理不弹稳定快照 → 切实际后下一轮立即真实处理（无需重新等稳定）
    r3 = asyncio.run(svc.run_once())
    assert r3.renamed == 1 and r3.hashed == 1
    assert (b / "狂怒追缉.2026.S01E04.第04集.2160p.WEB-DL.H.265.mkv").is_file()
    assert not video.exists()


def test_rename_subtitle_companion_and_empty_dir_cleanup(dirs, monkeypatch):
    """实际移动：字幕伴行 + A 内空目录清理。"""
    a, b = dirs
    sub = a / "sub"
    sub.mkdir()
    video = sub / "Furious.S01E04.2026.2160p.mkv"
    video.write_bytes(b"0" * 64)
    video.with_suffix(".srt").write_text("srt")

    svc = _mk_service(a, b, pipeline_rename_dry_run=False)
    _patch_high_conf(monkeypatch)
    asyncio.run(svc.run_once())
    r2 = asyncio.run(svc.run_once())

    dest = b / "狂怒追缉.2026.S01E04.第04集.2160p.WEB-DL.H.265.mkv"
    assert r2.renamed == 1
    assert dest.is_file() and dest.with_suffix(".srt").is_file()
    assert not video.exists() and not sub.exists()  # 空目录已清


def test_rename_low_conf_hold_backoff(dirs, monkeypatch):
    """低置信：原地保留 + 退避状态记录（键 rename:path）。"""
    a, b = dirs
    video = a / "Unknown.Show.S01E01.mkv"
    video.write_bytes(b"0" * 64)
    svc = _mk_service(a, b, pipeline_rename_dry_run=False)
    low = NamingResult(
        parsed=MediaData(title="Unknown", year=None, media_type="tv",
                         season=1, episode=1, raw=video.name),
        reasons=["TMDB 无搜索结果"],
    )
    monkeypatch.setattr(
        "app.pipeline.service.analyze_file",
        lambda path, tmdb: asyncio.sleep(0, result=low),
    )
    asyncio.run(svc.run_once())
    r2 = asyncio.run(svc.run_once())
    assert r2.low_conf == 1 and video.exists()
    assert svc._failures.get(f"rename:{video}")["failures"] == 1


def test_rename_conflict_no_overwrite(dirs, monkeypatch):
    """B 已有同名：跳过不覆盖。"""
    a, b = dirs
    video = a / "Furious.S01E04.2026.2160p.mkv"
    video.write_bytes(b"0" * 64)
    dest = b / "狂怒追缉.2026.S01E04.第04集.2160p.WEB-DL.H.265.mkv"
    dest.write_bytes(b"EXISTING")
    svc = _mk_service(a, b, pipeline_rename_dry_run=False)
    _patch_high_conf(monkeypatch)

    asyncio.run(svc.run_once())
    r2 = asyncio.run(svc.run_once())
    assert r2.conflict == 1
    assert dest.read_bytes() == b"EXISTING"
    assert video.exists()


# ---------------------------------------------------------------------- #
# ① 重命名阶段 · 元数据清洗闸门（monkeypatch cleaner，不依赖 ffmpeg）
# ---------------------------------------------------------------------- #
def _patch_cleaner(monkeypatch, *, report=None, clean_impl=None):
    async def inspect_(path, keywords=()):
        return report
    monkeypatch.setattr("app.media.cleaner.inspect", inspect_)
    if clean_impl is not None:
        monkeypatch.setattr("app.media.cleaner.clean", clean_impl)


def test_clean_gate_disabled_moves_raw(dirs, monkeypatch):
    """开关关闭：原样 fast_move，cleaner 完全不被调用。"""
    a, b = dirs
    f = a / "dirty.mkv"
    f.write_bytes(b"raw-bytes")

    async def boom(path, keywords=()):  # 任何调用都视为失败
        raise AssertionError("cleaner 不应被调用")
    monkeypatch.setattr("app.media.cleaner.inspect", boom)

    svc = _mk_service(a, b, pipeline_rename_dry_run=False)
    report = PipelineReport()
    assert asyncio.run(svc._maybe_clean(f, b / "out.mkv", report))
    assert (b / "out.mkv").read_bytes() == b"raw-bytes"
    assert not f.exists()
    assert not report.cleaned_lines and not report.clean_dry_lines


def test_clean_gate_dry_run_reports_but_moves(dirs, monkeypatch):
    """CLEAN_DRY_RUN：报告垃圾但文件照常移动（不清洗）。"""
    a, b = dirs
    f = a / "dirty.mkv"
    f.write_bytes(b"raw-bytes")
    _patch_cleaner(monkeypatch, report=CleanReport(
        junk_tags=["title=ad www.x.com"], junk_chapters=["广告章节"],
    ))
    svc = _mk_service(a, b, pipeline_rename_dry_run=False,
                      pipeline_clean_enabled=True, pipeline_clean_dry_run=True)
    report = PipelineReport()
    assert asyncio.run(svc._maybe_clean(f, b / "out.mkv", report))
    assert (b / "out.mkv").read_bytes() == b"raw-bytes"  # 未清洗
    assert not f.exists()
    assert len(report.clean_dry_lines) == 1
    assert "CLEAN_DRY_RUN" in report.clean_dry_lines[0]
    assert "容器标签×1" in report.clean_dry_lines[0]
    assert report.cleaned_count == 0
    assert report.has_events()  # 纯检测轮也触发汇总
    assert "🧹 检测到垃圾 1" in report.summary()
    assert "🧹 检测到垃圾（1）" in report.grouped_details()


def test_clean_gate_clean_success_replaces_file(dirs, monkeypatch):
    """实清洗：clean() 产物落 B，A 原件删除，报告计入已清洗。"""
    a, b = dirs
    f = a / "dirty.mkv"
    f.write_bytes(b"raw-bytes")
    dest = b / "out.mkv"

    async def fake_clean(src, dst, rpt):
        Path(dst).write_bytes(Path(src).read_bytes() + b"-cleaned")
    _patch_cleaner(monkeypatch, report=CleanReport(junk_tracks=[
        {"index": 2, "kind": "音轨", "title": "promo"},
    ]), clean_impl=fake_clean)
    svc = _mk_service(a, b, pipeline_rename_dry_run=False,
                      pipeline_clean_enabled=True, pipeline_clean_dry_run=False)
    report = PipelineReport()
    assert asyncio.run(svc._maybe_clean(f, dest, report))
    assert dest.read_bytes() == b"raw-bytes-cleaned"  # B 是清洗产物
    assert not f.exists()                              # 原件已删
    assert report.cleaned_count == 1
    assert "🧹 清洗 1" in report.summary()
    assert "🧹 已清洗（1）" in report.grouped_details()


def test_clean_gate_clean_failure_backoff(dirs, monkeypatch):
    """清洗失败：原件保留 A、无半成品、计失败退避。"""
    a, b = dirs
    f = a / "dirty.mkv"
    f.write_bytes(b"raw-bytes")

    async def fail_clean(src, dst, rpt):
        raise CleanError("ffmpeg remux 失败")
    _patch_cleaner(monkeypatch, report=CleanReport(junk_tags=["title=ad"]),
                   clean_impl=fail_clean)
    svc = _mk_service(a, b, pipeline_rename_dry_run=False,
                      pipeline_clean_enabled=True, pipeline_clean_dry_run=False)
    report = PipelineReport()
    assert not asyncio.run(svc._maybe_clean(f, b / "out.mkv", report))
    assert f.exists()                       # 原件保留待重试
    assert not (b / "out.mkv").exists()     # 无半成品
    assert report.failed == 1


def test_clean_gate_inspect_fail_degrades(dirs, monkeypatch):
    """检测失败（ffprobe 不可用）：按干净降级原样移动，不阻塞流水线。"""
    a, b = dirs
    f = a / "dirty.mkv"
    f.write_bytes(b"raw-bytes")
    _patch_cleaner(monkeypatch, report=None)  # inspect 返回 None
    svc = _mk_service(a, b, pipeline_rename_dry_run=False,
                      pipeline_clean_enabled=True, pipeline_clean_dry_run=False)
    report = PipelineReport()
    assert asyncio.run(svc._maybe_clean(f, b / "out.mkv", report))
    assert (b / "out.mkv").read_bytes() == b"raw-bytes"
    assert not report.cleaned_lines and not report.clean_dry_lines


# ---------------------------------------------------------------------- #
# ② 哈希 + 推送
# ---------------------------------------------------------------------- #
def test_full_flow_hash_push_submit(dirs, monkeypatch):
    """全链真实流转：移入 B → 哈希入账 → 推卡片 → 提交上传（单轮串行）。"""
    a, b = dirs
    video = a / "Furious.S01E04.2026.2160p.mkv"
    video.write_bytes(b"0" * 1_000_000)
    proc = _FakeProcessor()
    svc = _mk_service(a, b, processor=proc,
                      pipeline_rename_dry_run=False,
                      pipeline_push_dry_run=False,
                      pipeline_upload_dry_run=False)
    _patch_high_conf(monkeypatch)

    asyncio.run(svc.run_once())  # 快照
    r2 = asyncio.run(svc.run_once())  # 稳定 → 全链一轮完成

    dest = b / "狂怒追缉.2026.S01E04.第04集.2160p.WEB-DL.H.265.mkv"
    assert r2.renamed == 1 and r2.hashed == 1
    assert r2.pushed == 1 and len(proc.calls) == 1
    assert proc.calls[0].startswith("ed2k://|file|")
    assert r2.submitted == 1 and len(svc.submitted) == 1
    assert svc.submitted[0][0] == str(dest)  # CD2 路径=本地（Fake 恒等映射）
    # 轮汇总分组明细：✅ 推送成功组 + 📤 新上传任务组（进度条无 admin 可达 → 走汇总）
    assert r2.pushed_titles == ["t"]
    lines = r2.grouped_details()
    assert "✅ 推送成功（1）" in lines and "  • t" in lines
    assert "📤 新上传任务（1）" in lines
    # 组间空行 + 不含 ed2k 原文链接（链接只进频道卡片）
    i, j = lines.index("✅ 推送成功（1）"), lines.index("📤 新上传任务（1）")
    assert j - i >= 2 and lines[j - 1] == ""
    assert not any(ln.startswith("ed2k://|file|") for ln in lines)
    # 账本 + JSONL
    assert str(dest) in svc._ledger
    rec = json.loads(svc.results_file.read_text(encoding="utf-8").splitlines()[0])
    assert rec["path"] == str(dest) and rec["ed2k"].startswith("ed2k://|file|")
    # 串行：任务进行中 → 下一轮不重复提交
    svc.tasks_result = [_FakeCd2Task(status=2, sourcePath=str(dest),
                                     destPath=svc.cd2_dst, totalBytes=1_000_000)]
    r3 = asyncio.run(svc.run_once())
    assert r3.submitted == 0 and r3.active == 1


def test_push_dry_then_flip(dirs, monkeypatch):
    """推送 dry：不调 processor；切实际后立即推送。"""
    a, b = dirs
    video = a / "Furious.S01E04.2026.2160p.mkv"
    video.write_bytes(b"0" * 640_000)
    proc = _FakeProcessor()
    svc = _mk_service(a, b, processor=proc,
                      pipeline_rename_dry_run=False,
                      pipeline_push_dry_run=True,
                      pipeline_upload_dry_run=True)
    _patch_high_conf(monkeypatch)
    asyncio.run(svc.run_once())
    r2 = asyncio.run(svc.run_once())
    assert r2.hashed == 1 and r2.dry_pushed == 1 and proc.calls == []
    # dry 明细：🔍 模拟推送组（资源名，不含 ed2k 原文链接）
    assert r2.dry_push_names and "狂怒追缉" in r2.dry_push_names[0]
    assert "🔍 模拟推送（1）" in r2.grouped_details()
    assert not any(ln.startswith("ed2k://|file|") for ln in r2.grouped_details())

    svc.settings.pipeline_push_dry_run = False
    r3 = asyncio.run(svc.run_once())
    assert r3.pushed == 1 and len(proc.calls) == 1


def test_push_dup_detail(dirs, monkeypatch):
    """命中去重：⏭️ 已推送过组（带文件名）。"""
    a, b = dirs
    video = a / "Furious.S01E04.2026.2160p.mkv"
    video.write_bytes(b"0" * 640_000)
    proc = _FakeProcessor(dup=True)
    svc = _mk_service(a, b, processor=proc,
                      pipeline_rename_dry_run=False,
                      pipeline_push_dry_run=False,
                      pipeline_upload_dry_run=True)
    _patch_high_conf(monkeypatch)
    asyncio.run(svc.run_once())
    r2 = asyncio.run(svc.run_once())
    assert r2.skipped_dup == 1
    lines = r2.grouped_details()
    assert "⏭️ 已推送过（1）" in lines
    assert any("狂怒追缉" in ln for ln in lines)


def test_upload_progress_fields_updated(dirs, monkeypatch):
    """传输中任务：CD2 文件级进度字段（status/files/bytes）回填 _TaskInfo。"""
    a, b = dirs
    video = a / "Furious.S01E04.2026.2160p.mkv"
    video.write_bytes(b"0" * 640_000)
    proc = _FakeProcessor()
    svc = _mk_service(a, b, processor=proc,
                      pipeline_rename_dry_run=False,
                      pipeline_push_dry_run=True,
                      pipeline_upload_dry_run=False)
    _patch_high_conf(monkeypatch)
    asyncio.run(svc.run_once())
    asyncio.run(svc.run_once())  # rename+hash+submit
    dest = b / "狂怒追缉.2026.S01E04.第04集.2160p.WEB-DL.H.265.mkv"
    # 传输中：status=2、文件 0/3、字节 0（视频完成前 CD2 不报字节）
    svc.tasks_result = [_FakeCd2Task(status=2, sourcePath=str(dest),
                                     destPath=svc.cd2_dst,
                                     uploadedFiles=0, totalFiles=3,
                                     uploadedBytes=0, totalBytes=640_000)]
    asyncio.run(svc.run_once())
    info = svc._tasks[str(dest)]
    assert info.status == 2
    assert info.uploaded_files == 0 and info.total_files == 3
    # 视频完成：文件 1/3、字节跳变到视频大小
    svc.tasks_result[0].uploadedFiles = 1
    svc.tasks_result[0].uploadedBytes = 640_000
    asyncio.run(svc.run_once())
    assert info.uploaded_files == 1 and info.uploaded_bytes == 640_000


def test_push_failure_backoff_and_retry(dirs, monkeypatch):
    """推送失败 → 退避（push:path）→ 到期重试成功。"""
    a, b = dirs
    video = a / "Furious.S01E04.2026.2160p.mkv"
    video.write_bytes(b"0" * 640_000)
    proc = _FakeProcessor(fail=True)
    svc = _mk_service(a, b, processor=proc,
                      pipeline_rename_dry_run=False,
                      pipeline_push_dry_run=False,
                      pipeline_upload_dry_run=True)
    _patch_high_conf(monkeypatch)
    asyncio.run(svc.run_once())
    asyncio.run(svc.run_once())  # rename+hash+push 失败
    dest = b / "狂怒追缉.2026.S01E04.第04集.2160p.WEB-DL.H.265.mkv"
    st = svc._failures.get(f"push:{dest}")
    assert st is not None and st["failures"] == 1

    asyncio.run(svc.run_once())  # 退避未到期
    assert len(proc.calls) == 1

    svc._failures._state[f"push:{dest}"]["next_retry"] = 0  # 强制到期
    proc2 = _FakeProcessor()
    svc.container.processor = proc2
    asyncio.run(svc.run_once())
    assert len(proc2.calls) == 1
    assert f"push:{dest}" not in svc._failures.dump()


def test_scan_b_reconciles_external_drops(dirs, monkeypatch):
    """B 对账：直接投放 B 的文件（迁移场景）2 轮稳定后哈希入账。"""
    a, b = dirs
    dest = b / "外部迁移.2026.S01E01.mkv"
    dest.write_bytes(b"0" * 1_000_000)
    proc = _FakeProcessor()
    svc = _mk_service(a, b, processor=proc,
                      pipeline_push_dry_run=False,
                      pipeline_upload_dry_run=True)
    r1 = asyncio.run(svc.run_once())  # 首见快照
    assert r1.hashed == 0
    r2 = asyncio.run(svc.run_once())  # 快照一致 → 哈希 + 推送
    assert r2.hashed == 1 and r2.pushed == 1
    assert str(dest) in svc._ledger


# ---------------------------------------------------------------------- #
# ③ 上传阶段
# ---------------------------------------------------------------------- #
def test_upload_dry_then_flip_submits(dirs, monkeypatch):
    """上传 dry：不提交任务；切实际后立即提交。"""
    a, b = dirs
    video = a / "Furious.S01E04.2026.2160p.mkv"
    video.write_bytes(b"0" * 640_000)
    proc = _FakeProcessor()
    svc = _mk_service(a, b, processor=proc,
                      pipeline_rename_dry_run=False,
                      pipeline_push_dry_run=True,
                      pipeline_upload_dry_run=True)
    _patch_high_conf(monkeypatch)
    asyncio.run(svc.run_once())
    r2 = asyncio.run(svc.run_once())
    assert r2.dry_submitted == 1 and svc.submitted == []

    svc.settings.pipeline_upload_dry_run = False
    r3 = asyncio.run(svc.run_once())
    assert r3.submitted == 1 and len(svc.submitted) == 1


def test_upload_dedup_skips_existing_in_115(dirs, monkeypatch):
    """115 目标已有同名：删本地源 + 记完成跳过（不提交任务）。"""
    a, b = dirs
    video = a / "Furious.S01E04.2026.2160p.mkv"
    video.write_bytes(b"0" * 640_000)
    dest = b / "狂怒追缉.2026.S01E04.第04集.2160p.WEB-DL.H.265.mkv"
    proc = _FakeProcessor()
    svc = _mk_service(a, b, processor=proc,
                      pipeline_rename_dry_run=False,
                      pipeline_push_dry_run=True,
                      pipeline_upload_dry_run=False)
    svc.dst_files = [_FakeFile(dest.name)]
    _patch_high_conf(monkeypatch)
    asyncio.run(svc.run_once())
    asyncio.run(svc.run_once())
    assert svc.submitted == []
    assert str(dest) in svc._completed
    assert not dest.exists()          # 本地源冗余已删
    assert svc.deleted == [str(dest)]  # 删除走 _cd2_path 映射后的路径


def test_upload_dedup_skip_dry_keeps_file(dirs, monkeypatch):
    """DRY 模式查重命中：仅记完成，不动文件（DRY 不删源）。"""
    a, b = dirs
    video = a / "Furious.S01E04.2026.2160p.mkv"
    video.write_bytes(b"0" * 640_000)
    dest = b / "狂怒追缉.2026.S01E04.第04集.2160p.WEB-DL.H.265.mkv"
    svc = _mk_service(a, b,
                      pipeline_rename_dry_run=False,
                      pipeline_push_dry_run=True,
                      pipeline_upload_dry_run=True)
    svc.dst_files = [_FakeFile(dest.name)]
    _patch_high_conf(monkeypatch)
    asyncio.run(svc.run_once())
    asyncio.run(svc.run_once())
    assert str(dest) in svc._completed
    assert dest.exists()               # 文件未动
    assert svc.deleted == []


def test_upload_complete_deletes_source_with_sidecars(dirs, monkeypatch):
    """任务完成：删源（视频+伴行）+ completed + 推送照常（不因上传完结跳过卡片）。"""
    a, b = dirs
    video = a / "Furious.S01E04.2026.2160p.mkv"
    video.write_bytes(b"0" * 640_000)
    proc = _FakeProcessor()
    svc = _mk_service(a, b, processor=proc,
                      pipeline_rename_dry_run=False,
                      pipeline_push_dry_run=True,  # 推送暂缓（验证上传完结不吞卡片）
                      pipeline_upload_dry_run=False)
    _patch_high_conf(monkeypatch)
    asyncio.run(svc.run_once())
    asyncio.run(svc.run_once())  # rename+hash+submit
    dest = b / "狂怒追缉.2026.S01E04.第04集.2160p.WEB-DL.H.265.mkv"
    dest.with_suffix(".srt").write_text("srt")
    svc.tasks_result = [_FakeCd2Task(status=3, sourcePath=str(dest),
                                     destPath=svc.cd2_dst)]

    r3 = asyncio.run(svc.run_once())
    assert r3.completed == 1
    assert not dest.exists() and not dest.with_suffix(".srt").exists()
    assert str(dest) in svc._completed
    assert str(dest) in svc.deleted
    # 上传完结不标记 pushed：卡片仍待推
    assert str(dest) not in svc._pushed

    svc.settings.pipeline_push_dry_run = False
    r4 = asyncio.run(svc.run_once())
    assert r4.pushed == 1  # 文件已删仍能按 URL 推卡片


def test_upload_delete_uses_cd2_namespace_path(dirs, monkeypatch):
    """回归（NAS 实发 bug）：删源必须经 _cd2_path 映射——本地路径直传 CD2 会
    NOT_FOUND（上传成功但删除必失败，文件永久残留 B）。"""
    a, b = dirs
    video = a / "Furious.S01E04.2026.2160p.mkv"
    video.write_bytes(b"0" * 640_000)
    svc = _mk_service(a, b,
                      pipeline_rename_dry_run=False,
                      pipeline_push_dry_run=True,
                      pipeline_upload_dry_run=False)
    # 非恒等映射：本地 B 路径 → CD2 命名空间 /cd2ns/ 前缀（真实部署即如此）
    svc._cd2_path = lambda local: "/cd2ns/" + Path(local).name
    _patch_high_conf(monkeypatch)
    asyncio.run(svc.run_once())
    asyncio.run(svc.run_once())  # rename+hash+submit（cd2_src 已是 /cd2ns/ 名字）
    dest = b / "狂怒追缉.2026.S01E04.第04集.2160p.WEB-DL.H.265.mkv"
    svc.tasks_result = [_FakeCd2Task(status=3, sourcePath="/cd2ns/" + dest.name,
                                     destPath=svc.cd2_dst)]

    r3 = asyncio.run(svc.run_once())
    assert r3.completed == 1
    # 删除收到的是 CD2 命名空间路径（bug 时这里是本地路径 str(dest)）
    assert svc.deleted == [f"/cd2ns/{dest.name}"]


def test_upload_complete_delete_failure_not_completed(dirs, monkeypatch):
    """上传完成但删源失败：不记 completed（防 B 静默残留），计失败退避。"""
    a, b = dirs
    video = a / "Furious.S01E04.2026.2160p.mkv"
    video.write_bytes(b"0" * 640_000)
    svc = _mk_service(a, b,
                      pipeline_rename_dry_run=False,
                      pipeline_push_dry_run=True,
                      pipeline_upload_dry_run=False)
    _patch_high_conf(monkeypatch)
    asyncio.run(svc.run_once())
    asyncio.run(svc.run_once())  # rename+hash+submit
    dest = b / "狂怒追缉.2026.S01E04.第04集.2160p.WEB-DL.H.265.mkv"
    svc.tasks_result = [_FakeCd2Task(status=3, sourcePath=str(dest),
                                     destPath=svc.cd2_dst)]
    svc._delete_file = lambda p: False  # 删除恒失败（不 unlink）

    r3 = asyncio.run(svc.run_once())
    assert r3.completed == 0 and r3.failed == 1
    assert dest.exists()                       # 源未删
    assert str(dest) not in svc._completed     # 未记完成 → 下轮查重路径兜底
    assert svc._failures.get(f"upload:{dest}")["failures"] == 1


def test_upload_skip_delete_failure_backoff(dirs, monkeypatch):
    """查重跳过路径删源失败：计失败退避，不记完成（下轮重试删源）。"""
    a, b = dirs
    video = a / "Furious.S01E04.2026.2160p.mkv"
    video.write_bytes(b"0" * 640_000)
    dest = b / "狂怒追缉.2026.S01E04.第04集.2160p.WEB-DL.H.265.mkv"
    svc = _mk_service(a, b,
                      pipeline_rename_dry_run=False,
                      pipeline_push_dry_run=True,
                      pipeline_upload_dry_run=False)
    svc.dst_files = [_FakeFile(dest.name)]  # 115 已存在同名
    svc._delete_file = lambda p: False
    _patch_high_conf(monkeypatch)
    asyncio.run(svc.run_once())
    r2 = asyncio.run(svc.run_once())
    assert r2.upload_skipped == 0 and r2.failed == 1
    assert dest.exists()
    assert str(dest) not in svc._completed


def test_upload_failure_backoff(dirs, monkeypatch):
    """CD2 任务失败 → upload:path 退避。"""
    a, b = dirs
    video = a / "Furious.S01E04.2026.2160p.mkv"
    video.write_bytes(b"0" * 640_000)
    proc = _FakeProcessor()
    svc = _mk_service(a, b, processor=proc,
                      pipeline_rename_dry_run=False,
                      pipeline_push_dry_run=True,
                      pipeline_upload_dry_run=False)
    _patch_high_conf(monkeypatch)
    asyncio.run(svc.run_once())
    asyncio.run(svc.run_once())
    dest = b / "狂怒追缉.2026.S01E04.第04集.2160p.WEB-DL.H.265.mkv"
    svc.tasks_result = [_FakeCd2Task(status=4, sourcePath=str(dest),
                                     destPath=svc.cd2_dst)]

    r3 = asyncio.run(svc.run_once())
    assert r3.failed == 1
    assert svc._failures.get(f"upload:{dest}")["failures"] == 1
    # 退避未到期 → 不重新提交
    asyncio.run(svc.run_once())
    assert len(svc.submitted) == 1


def test_upload_serial_one_at_a_time(dirs, monkeypatch):
    """串行约束：两个文件只提交一个，完成后再提交第二个。"""
    a, b = dirs
    for i in (1, 2):
        (a / f"Furious.S01E0{i}.2026.2160p.mkv").write_bytes(b"0" * (640_000 + i))

    def _analyze_by_episode(path, tmdb):
        import re as _re
        m = _re.search(r"S01E0(\d)", path)
        ep = m.group(1) if m else "9"
        result = NamingResult(
            parsed=MediaData(title="Furious", year=2026, media_type="tv",
                             season=1, episode=int(ep), raw=path),
            details={"title": "狂怒追缉", "year": 2026},
            proposed=f"狂怒追缉.2026.S01E0{ep}.第0{ep}集.2160p.WEB-DL.H.265.mkv",
        )
        return asyncio.sleep(0, result=result)

    monkeypatch.setattr("app.pipeline.service.analyze_file", _analyze_by_episode)
    proc = _FakeProcessor()
    svc = _mk_service(a, b, processor=proc,
                      pipeline_rename_dry_run=False,
                      pipeline_push_dry_run=True,
                      pipeline_upload_dry_run=False)
    asyncio.run(svc.run_once())
    r2 = asyncio.run(svc.run_once())  # 两个文件同轮 rename+hash
    assert r2.renamed == 2
    assert len(svc.submitted) == 1  # 只提交了第一个

    first = svc.submitted[0][0]
    svc.tasks_result = [_FakeCd2Task(status=3, sourcePath=first, destPath=svc.cd2_dst)]
    asyncio.run(svc.run_once())  # 完成 → 删源 → 提交第二个
    assert len(svc.submitted) == 2
    assert svc.submitted[1][0] != first


class _ProgressBot:
    def __init__(self) -> None:
        self.sent: list = []
        self.edits: list = []

    async def send_message(self, chat_id, text):
        from types import SimpleNamespace
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=1)

    async def edit_message_text(self, chat_id, message_id, text):
        self.edits.append(text)


class _ProgressTg:
    def __init__(self) -> None:
        self.bot = _ProgressBot()


def test_upload_progress_message_style(dirs, monkeypatch):
    """进度条消息与轮汇总同款格式：头「图标 标题 · 开始时间」+ 📁 文件行 + 进度行。

    生命周期四态（开始/传输中/完成）共用任务开始时刻作头部时间锚点。
    """
    a, b = dirs
    video = a / "Furious.S01E04.2026.2160p.mkv"
    video.write_bytes(b"0" * 640_000)
    tg = _ProgressTg()
    container = _FakeContainer(_FakeProcessor())
    container.telegram = tg
    st = _FakeSettings()
    st.state_db_path = str(a.parent / "state.db")
    st.pipeline_rename_dry_run = False
    st.pipeline_push_dry_run = True
    st.pipeline_upload_dry_run = False
    st.pipeline_report_admin = True
    st.tg_admin_ids = [7]
    svc = FakePipeline(container, st, a, b)
    svc.results_file = a.parent / "results.jsonl"
    _patch_high_conf(monkeypatch)

    asyncio.run(svc.run_once())
    asyncio.run(svc.run_once())  # rename+hash+submit → 发出"开始"消息
    assert len(tg.bot.sent) == 1
    start = tg.bot.sent[0][1]
    lines = start.splitlines()
    assert lines[0].startswith("📤 CD2 上传开始 · ")   # 头：图标 标题 · 时间
    assert lines[1].startswith("📁 狂怒追缉")          # 文件行（带体积）
    assert lines[2].startswith("[") and "0%" in lines[2]  # 进度行
    header_ts = lines[0].split(" · ", 1)[1]

    dest = b / "狂怒追缉.2026.S01E04.第04集.2160p.WEB-DL.H.265.mkv"
    svc.tasks_result = [_FakeCd2Task(status=2, sourcePath=str(dest),
                                     destPath=svc.cd2_dst,
                                     uploadedBytes=320_000, totalBytes=640_000)]
    asyncio.run(svc.run_once())  # 传输中编辑
    mid = [t for t in tg.bot.edits if t.startswith("📤 CD2 上传中")]
    assert mid, tg.bot.edits
    assert mid[0].splitlines()[0].endswith(header_ts)  # 同一时间锚点
    assert "50%" in mid[0] and "MB/s" in mid[0]

    svc.tasks_result[0].status = 3
    asyncio.run(svc.run_once())  # 完成收尾
    done = [t for t in tg.bot.edits if t.startswith("✅ CD2 上传完成")]
    assert done
    assert done[0].splitlines()[1].startswith("📁 ")
    assert done[0].splitlines()[2].startswith("[") and "100%" in done[0]


# ---------------------------------------------------------------------- #
# 状态持久化与恢复
# ---------------------------------------------------------------------- #
def test_state_persistence_roundtrip(dirs):
    """completed/pushed/failures 持久化 → 新实例恢复。"""
    a, b = dirs
    svc = _mk_service(a, b)
    svc._completed.add("/x/1.mkv")
    svc._pushed.add("/x/1.mkv")
    svc._failures.record("upload:/x/2.mkv", time.time())
    svc._save_state()

    svc2 = _mk_service(a, b)
    svc2._load_state()
    assert "/x/1.mkv" in svc2._completed
    assert "/x/1.mkv" in svc2._pushed
    assert svc2._failures.get("upload:/x/2.mkv") is not None


def test_ledger_rebuild_from_jsonl_filters_deleted(dirs):
    """账本重建：只保留仍存在于 B 的路径（已删源的历史条目丢弃）。"""
    a, b = dirs
    live = b / "live.mkv"
    live.write_bytes(b"0" * 64)
    svc = _mk_service(a, b)
    svc._append_result({"path": str(live), "name": "live.mkv", "ed2k": "ed2k://|file|live|1|a|/"})
    svc._append_result({"path": "/gone/dead.mkv", "name": "dead.mkv", "ed2k": "ed2k://|file|dead|1|b|/"})

    svc2 = _mk_service(a, b)
    svc2._load_state()
    assert str(live) in svc2._ledger
    assert "/gone/dead.mkv" not in svc2._ledger


def test_restart_recovery_rebuilds_tasks(dirs, monkeypatch):
    """重启恢复：CD2 侧进行中任务按本地路径重建 _tasks。"""
    a, b = dirs
    dest = b / "恢复.2026.S01E01.mkv"
    dest.write_bytes(b"0" * 640_000)
    svc = _mk_service(a, b)
    svc._ledger[str(dest)] = {"path": str(dest), "name": dest.name, "ed2k": "u"}
    svc.tasks_result = [
        _FakeCd2Task(status=2, sourcePath=str(dest), destPath=svc.cd2_dst,
                     uploadedBytes=1000, totalBytes=640_000)
    ]
    asyncio.run(svc._recover_tasks())
    assert str(dest) in svc._tasks
    info = svc._tasks[str(dest)]
    assert info.size == 640_000 and info.uploaded_bytes == 1000


# ---------------------------------------------------------------------- #
# 状态查询
# ---------------------------------------------------------------------- #
def test_status_texts_render(dirs, monkeypatch):
    a, b = dirs
    video = a / "Furious.S01E04.2026.2160p.mkv"
    video.write_bytes(b"0" * 640_000)
    proc = _FakeProcessor()
    svc = _mk_service(a, b, processor=proc,
                      pipeline_rename_dry_run=False,
                      pipeline_push_dry_run=False,
                      pipeline_upload_dry_run=False)
    _patch_high_conf(monkeypatch)
    asyncio.run(svc.run_once())
    asyncio.run(svc.run_once())

    overview = "\n".join(svc.overview_lines())
    assert "① A→B 重命名：实际" in overview
    assert "③ B→115：实际" in overview
    assert "A=" in overview and "CD2：" in overview

    push_text = svc.status_push_text()
    assert "流水线推送状态" in push_text and "已推 1" in push_text

    upload_text = svc.status_upload_text()
    assert "流水线上传状态" in upload_text and "已完成 0" in upload_text
