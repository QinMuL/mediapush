"""ffprobe 媒体探测：真实流参数 → 命名质量标签。

设计约定（与用户确认的命名规范对应）：
- 文件名只可信：片名/年份/季集/来源(WEB-DL 等)/发布组
- 分辨率/HDR 效果/视频编码/音频编码必须实测（文件名自称不可信）
- 只读容器头部（流信息在 mkv header / mp4 moov），秒级完成，与全量读的
  MD4 计算完全不同量级

标签映射（命名圈惯例）：
- 分辨率按最大边归一：2160p/1080p/720p/480p（2.35:1 宽幅电影高度不达标，
  按宽度判）
- DV：side_data DOVI configuration record 的 dv_profile → "DoVi P5/P7/P8"
  （不叠 HDR10 后缀）
- color_transfer：arib-std-b67=HDR Vivid（唯一可实测的 Vivid 传输函数）；
  smpte2084=HDR10（国内 Vivid 编码也用 PQ 且 CUVA 元数据不可暴露，
  容器层面与 HDR10 不可辨，统一归 HDR10）；其余=SDR
- 视频 codec：h264→H.264、hevc→H.265、av1→AV1
- 音频：codec+声道（eac3→DDP、truehd→TrueHD、dts profile→DTS-HD MA…）
- 帧率：avg_frame_rate 归一（23.976fps / 60fps）
- 色深：pix_fmt 实测（yuv420p10le→10-bit）；10-bit 及以上才标注，
  8-bit 命名圈惯例折叠不标
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ProbeTags:
    """ffprobe 实测出的命名标签。"""

    resolution: str = ""       # 2160p / 1080p / 720p / 480p
    effect: str = ""           # DoVi P8 / HDR10 / HDR Vivid / SDR
    video_encode: str = ""     # H.264 / H.265 / AV1
    audio_encode: str = ""     # DDP 5.1 / TrueHD 7.1 / AAC 2.0
    audio_tracks: int = 0      # 音轨数（多语种提示用）
    frame_rate: str = ""       # 29.97fps / 60fps（avg_frame_rate 归一）
    bit_depth: str = ""        # 10-bit（pix_fmt 实测；8-bit 惯例不标注）


# ---------------------------------------------------------------------- #
# 纯映射函数（单测友好，不依赖 ffprobe）
# ---------------------------------------------------------------------- #
def normalize_resolution(width: int, height: int) -> str:
    """按最大边归一分辨率（宽幅电影高度不足，用宽度判）。"""
    dim = max(width, height) if width and height else 0
    if dim >= 3200:
        return "2160p"
    if dim >= 1700:
        return "1080p"
    if dim >= 1100:
        return "720p"
    return "480p" if dim else ""


def normalize_effect(dv_profile: int | None, color_transfer: str) -> str:
    """DV profile + 传输函数 → 效果标签。

    DV 只标 profile（DoVi P5/P7/P8），不叠 HDR10 后缀；
    arib-std-b67 是唯一可实测的 HDR Vivid 传输函数，其余 PQ 统一 HDR10
    （国内 Vivid 编码也用 PQ 且 CUVA 元数据 ffprobe 不暴露，容器层面不可辨，
    统一归 HDR10）。
    """
    if dv_profile:
        return f"DoVi P{dv_profile}"
    if color_transfer == "smpte2084":
        return "HDR10"
    if color_transfer == "arib-std-b67":
        return "HDR Vivid"
    return "SDR"


def normalize_frame_rate(avg_frame_rate: str) -> str:
    """avg_frame_rate（"24000/1001" / "60/1"）→ "23.976fps" / "60fps"。"""
    s = (avg_frame_rate or "").strip()
    if not s or s in ("0/0", "N/A"):
        return ""
    try:
        num, _, den = s.partition("/")
        d = int(den) if den else 1
        fps = int(num) / d if d else 0.0
    except (ValueError, ZeroDivisionError):
        return ""
    if fps <= 0:
        return ""
    out = f"{fps:.3f}".rstrip("0").rstrip(".")
    return f"{out}fps"


def normalize_bitrate(bps: int | str | None) -> str:
    """比特率 → "17.3Mbps"。非正数/非法输入返回空。（备用：命名模板暂不使用）"""
    try:
        bps = int(bps or 0)
    except (TypeError, ValueError):
        return ""
    if bps <= 0:
        return ""
    return f"{round(bps / 1_000_000, 1):.1f}Mbps"


_BIT_DEPTH_RE = re.compile(r"^yuv[a-z0-9]*?(\d{2})[bl]e?$")


def normalize_bit_depth(pix_fmt: str, bits_per_raw_sample: int | str | None = None) -> str:
    """色深 → "10-bit"。pix_fmt 优先（yuv420p10le→10），回退 bits_per_raw_sample。

    命名圈惯例：10-bit 及以上才标注，8-bit 折叠不标。
    """
    m = _BIT_DEPTH_RE.match((pix_fmt or "").strip())
    if m:
        bits = int(m.group(1))
    else:
        try:
            bits = int(bits_per_raw_sample or 0)
        except (TypeError, ValueError):
            bits = 0
    return f"{bits}-bit" if bits >= 10 else ""


_VIDEO_CODEC = {
    "h264": "H.264",
    "avc": "H.264",
    "hevc": "H.265",
    "av1": "AV1",
    "vp9": "VP9",
    "mpeg2video": "MPEG2",
    "vc1": "VC1",
}

_AUDIO_CODEC = {
    "eac3": "DDP",
    "ac3": "DD",
    "truehd": "TrueHD",
    "dts": "DTS",
    "flac": "FLAC",
    "aac": "AAC",
    "opus": "Opus",
}
_PCM_RE = re.compile(r"^pcm_")
_CHANNEL_LABEL = {1: "1.0", 2: "2.0", 3: "2.1", 6: "5.1", 7: "6.1", 8: "7.1"}


def normalize_audio(codec: str, profile: str, channels: int) -> str:
    """音频 codec+profile+声道数 → 标签。"""
    codec = (codec or "").strip()
    if not codec:
        return ""
    name = _AUDIO_CODEC.get(codec)
    # DTS profile 形如 "DTS-HD MA" / "DTS-HD HRA" / "DTS Express" / "DTS"
    if (
        codec == "dts"
        and profile in ("DTS-HD MA", "DTS-HD HRA", "DTS Express", "DTS 96/24")
    ):
        name = profile
    if _PCM_RE.match(codec):
        name = "LPCM"
    if not name:
        return codec
    ch = _CHANNEL_LABEL.get(channels, str(channels) if channels else "")
    return f"{name} {ch}" if ch else name


# ---------------------------------------------------------------------- #
# ffprobe 输出 → ProbeTags
# ---------------------------------------------------------------------- #
def _dv_profile(video: dict) -> int | None:
    for sd in video.get("side_data_list") or []:
        # ffprobe 8 实测字段名为 "DOVI configuration record"（带空格小写），
        # 用前缀匹配兼容不同版本写法
        if (sd.get("side_data_type") or "").lower().startswith("dovi"):
            p = sd.get("dv_profile")
            if p is not None:
                return int(p)
    return None


def tags_from_ffprobe(data: dict) -> ProbeTags:
    """解析 ffprobe JSON（-show_streams -show_format）为命名标签。"""
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    tags = ProbeTags(audio_tracks=len(audios))
    if video:
        tags.resolution = normalize_resolution(
            video.get("width") or 0, video.get("height") or 0
        )
        tags.video_encode = _VIDEO_CODEC.get(video.get("codec_name", ""), "")
        tags.effect = normalize_effect(_dv_profile(video), video.get("color_transfer", ""))
        tags.frame_rate = normalize_frame_rate(video.get("avg_frame_rate", ""))
        # 色深：pix_fmt 实测（yuv420p10le→10-bit；8-bit 惯例不标注）
        tags.bit_depth = normalize_bit_depth(
            video.get("pix_fmt", ""), video.get("bits_per_raw_sample")
        )
    if audios:
        a = audios[0]
        tags.audio_encode = normalize_audio(
            a.get("codec_name", ""), a.get("profile") or "", a.get("channels") or 0
        )
    return tags


async def probe_file(path: str) -> ProbeTags | None:
    """探测单个文件。失败返回 None（调用方降级：质量标签留空）。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_format", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
    except Exception as exc:  # noqa: BLE001 - ffprobe 缺失/路径问题
        logger.warning("ffprobe 执行失败 %s：%s", path, exc)
        return None
    if proc.returncode != 0:
        logger.warning("ffprobe 返回码 %s：%s", proc.returncode, path)
        return None
    try:
        return tags_from_ffprobe(json.loads(out.decode("utf-8", "replace")))
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("ffprobe 输出解析失败 %s：%s", path, exc)
        return None
