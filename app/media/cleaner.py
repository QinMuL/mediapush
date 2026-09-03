"""视频元数据清洗（L1 保守档）：检测 + ffmpeg remux 清洗，零重编码。

处理的三类垃圾（title/名称命中广告关键词才动，其余一律不碰）：
1. 容器全局 tags：如 title 塞"更多资源访问 xxx.com"→ -map_metadata -1
2. 垃圾章节：章节 title 含广告/网址 → ffmetadata 过滤回灌（正常章节保留）
3. 广告音轨/字幕轨：流 title 含广告/网址 → remux 时不映射该轨

不做（明确边界）：
- 画面水印/角标：烧在像素里，remux 洗不掉（重编码伤画质，违背秒级 remux 原则）
- 按语言删轨：L1 不做（误删风险高），仅按 title 关键词
- 附件（字体/图片）：全部保留（-map 0:t?，字体是字幕渲染依赖）

清洗后校验：视频轨数一致 + 时长差 < 1.5s，不过则删除半成品并报错
（调用方走退避重试），原件永远不动。
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 广告关键词（大小写不敏感子串匹配；保守集：只收"明确推广信号"，
# 不收"压制/字幕组"等可能出现在正常轨名的词）
DEFAULT_JUNK_KEYWORDS: tuple[str, ...] = (
    "http://", "https://", "www.", ".com", ".net", ".org", ".cc",
    ".xyz", ".top", ".icu", "promo", "trailer",
    "广告", "官网", "更多资源", "资源站", "ed2k://", "magnet:?",
    "点击", "订阅", "频道推广", "群号", "qq群",
)


@dataclass
class CleanReport:
    """单文件的垃圾元数据检测结果。"""

    junk_tags: list[str] = field(default_factory=list)      # 容器 tags 命中项
    junk_chapters: list[str] = field(default_factory=list)  # 垃圾章节 title
    junk_tracks: list[dict] = field(default_factory=list)   # 广告轨（含 index）
    # 清洗期协作字段（report_from_ffprobe 不填；clean() 构造 -map 用）
    kept_track_indexes: list[int] = field(default_factory=list)

    @property
    def has_junk(self) -> bool:
        return bool(self.junk_tags or self.junk_chapters or self.junk_tracks)

    def summary(self) -> str:
        """一行摘要（轮汇总明细用）。"""
        parts: list[str] = []
        if self.junk_tags:
            parts.append(f"容器标签×{len(self.junk_tags)}")
        if self.junk_chapters:
            parts.append(f"垃圾章节×{len(self.junk_chapters)}")
        if self.junk_tracks:
            kinds = "、".join(
                f"{t['kind']}#{t['index']}" for t in self.junk_tracks
            )
            parts.append(f"广告轨 {kinds}")
        return "；".join(parts)


def _hit(text: str, keywords: tuple[str, ...]) -> str | None:
    """text 命中任一关键词返回该关键词，否则 None。"""
    low = (text or "").lower()
    for kw in keywords:
        if kw in low:
            return kw
    return None


def report_from_ffprobe(data: dict,
                        keywords: tuple[str, ...] = DEFAULT_JUNK_KEYWORDS) -> CleanReport:
    """解析 ffprobe JSON（-show_streams -show_chapters -show_format）为垃圾报告。

    纯函数（单测友好，不依赖 ffprobe 进程）。
    """
    rpt = CleanReport()
    # 1) 容器全局 tags（title/comment 等）
    for key, val in (data.get("format", {}).get("tags") or {}).items():
        if _hit(str(val), keywords):
            rpt.junk_tags.append(f"{key}={str(val)[:60]}")
    # 2) 章节 title
    for ch in data.get("chapters") or []:
        title = (ch.get("tags") or {}).get("title", "")
        if _hit(title, keywords):
            rpt.junk_chapters.append(title[:60])
    # 3) 音轨/字幕轨 title（不按语言判，只按关键词）
    for st in data.get("streams") or []:
        if st.get("codec_type") not in ("audio", "subtitle"):
            continue
        title = (st.get("tags") or {}).get("title", "")
        if _hit(title, keywords):
            rpt.junk_tracks.append({
                "index": st.get("index", -1),
                "kind": "音轨" if st.get("codec_type") == "audio" else "字幕",
                "title": title[:60],
            })
    return rpt


async def _ffprobe_json(path: str) -> dict | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_chapters", "-show_format", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ffprobe 执行失败 %s：%s", path, exc)
        return None
    if proc.returncode != 0:
        return None
    import json
    try:
        return json.loads(out.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        return None


async def inspect(path: str,
                  keywords: tuple[str, ...] = DEFAULT_JUNK_KEYWORDS) -> CleanReport | None:
    """检测单文件。返回 None 表示探测失败（调用方按"不清洗"降级）。"""
    data = await _ffprobe_json(path)
    if data is None:
        return None
    return report_from_ffprobe(data, keywords)


# ---------------------------------------------------------------------- #
# 清洗（ffmpeg remux）
# ---------------------------------------------------------------------- #
async def _run(cmd: list[str]) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        return proc.returncode == 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("ffmpeg 执行失败：%s", exc)
        return False


def _build_args(src: str, dst: str, rpt: CleanReport,
                chapter_meta: str | None) -> list[str]:
    """按检测报告构造 remux 参数（-c copy 零重编码）。

    顺序约定：全部 -i 在前 → 输出选项（-map/-c/-map_metadata/-map_chapters）
    → 输出文件（ffmpeg 不允许输出选项后穿插 -i）。
    """
    args = ["ffmpeg", "-y", "-i", src]
    if chapter_meta is not None:
        args += ["-i", chapter_meta]
    if rpt.junk_tracks:
        # 显式逐轨映射：视频/音频/字幕保留非垃圾轨，附件全保留
        args += ["-map", "0:v"]
        for idx in rpt.kept_track_indexes:
            args += ["-map", f"0:{idx}"]
        args += ["-map", "0:t?"]
    else:
        args += ["-map", "0"]  # 无广告轨：全部流原样
    args += ["-c", "copy"]
    # 需要 meta 时：全局 tags 与章节统一由过滤文件回灌（输入1）
    # ——不用 -map_metadata -1：实测该选项会连流级 title 一并清除
    if chapter_meta is not None:
        args += ["-map_metadata", "1", "-map_chapters", "1"]
    args.append(dst)
    return args


async def _export_chapters_filtered(src: str, rpt: CleanReport,
                                    tmp_meta: str) -> str | None:
    """导出 ffmetadata 并过滤，返回过滤文件路径；失败返回 None（兜底不动）。

    输出内容 = 全局段（有垃圾 tags → 清空；否则原样回灌）+ 干净章节段。
    作为第二个输入喂给 -map_metadata 1 -map_chapters 1：
    - 全局垃圾标签被替换为空/干净值
    - 垃圾章节被剔除，正常章节保留
    - 流级 title 不受影响（默认随各流复制）
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", src, "-f", "ffmetadata", tmp_meta,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    if proc.returncode != 0:
        return None
    try:
        raw = open(tmp_meta, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    lines = raw.splitlines()
    out_lines = [";FFMETADATA1"]
    # 全局段：头之后、第一个 [CHAPTER]/[STREAM] 之前的 key=value
    if not rpt.junk_tags:
        i = 1
        while i < len(lines) and lines[i].strip() not in ("[CHAPTER]", "[STREAM]"):
            if lines[i].strip():
                out_lines.append(lines[i])
            i += 1
    # 章节段：垃圾段丢弃，干净段原样保留
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line == "[CHAPTER]":
            j = i + 1
            block: list[str] = []
            while j < len(lines) and lines[j].strip() not in ("[CHAPTER]", "[STREAM]"):
                block.append(lines[j])
                j += 1
            title = next(
                (ln.split("=", 1)[1] for ln in block
                 if ln.strip().lower().startswith("title=")), ""
            )
            if not _hit(title, DEFAULT_JUNK_KEYWORDS):
                out_lines.append("")
                out_lines.append("[CHAPTER]")
                out_lines.extend(block)
            i = j
        else:
            i += 1
    try:
        with open(tmp_meta, "w", encoding="utf-8") as f:
            f.write("\n".join(out_lines) + "\n")
    except OSError:
        return None
    return tmp_meta


class CleanError(Exception):
    """清洗失败（半成品已清理，原件未动）。"""


async def clean(src: str, dst: str, rpt: CleanReport) -> None:
    """remux 清洗 src → dst。成功后调用方负责删 src。

    失败（ffmpeg/校验/探测）→ 删除 dst 半成品并抛 CleanError
    （调用方退避重试），src 原件始终不动。
    """
    tmp_meta = dst + ".chapters.meta"
    try:
        chapter_meta: str | None = None
        # 全局 tags 或章节有垃圾 → 统一走 ffmetadata 过滤回灌
        # （-map_metadata -1 实测会连流级 title 一并清除，弃用）
        if rpt.junk_tags or rpt.junk_chapters:
            chapter_meta = await _export_chapters_filtered(src, rpt, tmp_meta)
            if chapter_meta is None:
                logger.warning(
                    "ffmetadata 过滤失败（%s）→ 全局 tags/章节原样保留兜底", src
                )
        # 广告轨存在时：预计算保留轨 index（构造 -map 用）
        if rpt.junk_tracks:
            data = await _ffprobe_json(src)
            junk_idx = {t["index"] for t in rpt.junk_tracks}
            rpt.kept_track_indexes = [
                st["index"] for st in (data or {}).get("streams", [])
                if st.get("codec_type") in ("audio", "subtitle")
                and st.get("index") not in junk_idx
            ]
        if not await _run(_build_args(src, dst, rpt, chapter_meta)):
            raise CleanError("ffmpeg remux 失败")
        # 校验：视频轨数一致 + 时长差 < 1.5s
        src_data, dst_data = await _ffprobe_json(src), await _ffprobe_json(dst)
        try:
            src_v = sum(1 for s in src_data["streams"] if s["codec_type"] == "video")
            dst_v = sum(1 for s in dst_data["streams"] if s["codec_type"] == "video")
            src_d = float(src_data["format"]["duration"])
            dst_d = float(dst_data["format"]["duration"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CleanError(f"校验探测失败：{exc}") from exc
        if src_v != dst_v or abs(src_d - dst_d) > 1.5:
            raise CleanError(
                f"校验不过（视频轨 {src_v}->{dst_v}，时长 {src_d:.1f}->{dst_d:.1f}s）"
            )
        logger.info("元数据清洗完成：%s → %s（%s）", src, dst, rpt.summary())
    except Exception:
        _remove(dst)
        raise
    finally:
        _remove(tmp_meta)


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
