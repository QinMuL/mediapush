"""probe.py 标签映射单测（纯函数，不依赖 ffprobe）。"""

from app.media.probe import (
    ProbeTags,
    normalize_audio,
    normalize_bit_depth,
    normalize_effect,
    normalize_frame_rate,
    normalize_resolution,
    tags_from_ffprobe,
)


# ---------------- 分辨率归一 ---------------- #
def test_resolution_standard():
    assert normalize_resolution(3840, 2160) == "2160p"
    assert normalize_resolution(1920, 1080) == "1080p"
    assert normalize_resolution(1280, 720) == "720p"
    assert normalize_resolution(854, 480) == "480p"


def test_resolution_scope_movie():
    """2.35:1 宽幅电影：高度不足（800/1600），按宽度判。"""
    assert normalize_resolution(1920, 800) == "1080p"
    assert normalize_resolution(3840, 1600) == "2160p"


# ---------------- HDR 效果 ---------------- #
def test_effect_dv_profiles():
    """DV 只标 profile，不叠 HDR10 后缀。"""
    assert normalize_effect(5, "") == "DoVi P5"
    assert normalize_effect(8, "bt709") == "DoVi P8"
    assert normalize_effect(7, "smpte2084") == "DoVi P7"
    assert normalize_effect(8, "smpte2084") == "DoVi P8"


def test_effect_hdr_variants():
    assert normalize_effect(None, "smpte2084") == "HDR10"
    assert normalize_effect(None, "arib-std-b67") == "HDR Vivid"
    assert normalize_effect(None, "bt709") == "SDR"


# ---------------- 音频 ---------------- #
def test_audio_common():
    assert normalize_audio("eac3", "", 6) == "DDP 5.1"
    assert normalize_audio("truehd", "", 8) == "TrueHD 7.1"
    assert normalize_audio("aac", "", 2) == "AAC 2.0"
    assert normalize_audio("flac", "", 2) == "FLAC 2.0"
    assert normalize_audio("ac3", "", 6) == "DD 5.1"


def test_audio_dts_profiles():
    assert normalize_audio("dts", "DTS-HD MA", 6) == "DTS-HD MA 5.1"
    assert normalize_audio("dts", "DTS", 6) == "DTS 5.1"


def test_audio_lpcm():
    assert normalize_audio("pcm_s24le", "", 1) == "LPCM 1.0"


# ---------------- ffprobe JSON → 标签 ---------------- #
def _mk(video: dict, audios: list[dict]) -> dict:
    streams = [{"codec_type": "video", **video}]
    for a in audios:
        streams.append({"codec_type": "audio", **a})
    return {"streams": streams}


def test_tags_from_ffprobe_dv():
    data = _mk(
        {
            "codec_name": "hevc",
            "width": 3840,
            "height": 2160,
            "color_transfer": "smpte2084",
            "side_data_list": [
                {
                    "side_data_type": "DOVI configuration record",
                    "dv_profile": 8,
                }
            ],
        },
        [{"codec_name": "eac3", "channels": 6}, {"codec_name": "aac", "channels": 2}],
    )
    tags = tags_from_ffprobe(data)
    assert tags.resolution == "2160p"
    assert tags.effect == "DoVi P8"
    assert tags.video_encode == "H.265"
    assert tags.audio_encode == "DDP 5.1"
    assert tags.audio_tracks == 2


def test_tags_from_ffprobe_hdr_vivid():
    data = _mk(
        {"codec_name": "h264", "width": 3840, "height": 2160,
         "color_transfer": "arib-std-b67"},
        [{"codec_name": "aac", "channels": 2}],
    )
    tags = tags_from_ffprobe(data)
    assert tags.effect == "HDR Vivid"
    assert tags.video_encode == "H.264"


# ---------------- 帧率 / 码率 ---------------- #
def test_frame_rate_variants():
    assert normalize_frame_rate("24000/1001") == "23.976fps"
    assert normalize_frame_rate("60/1") == "60fps"
    assert normalize_frame_rate("25/1") == "25fps"
    assert normalize_frame_rate("30000/1001") == "29.97fps"
    assert normalize_frame_rate("0/0") == ""
    assert normalize_frame_rate("N/A") == ""
    assert normalize_frame_rate("") == ""
    assert normalize_frame_rate(None) == ""


def test_bit_depth_variants():
    """色深：pix_fmt 优先（yuv420p10le→10-bit），8-bit 惯例不标注。"""
    assert normalize_bit_depth("yuv420p10le") == "10-bit"
    assert normalize_bit_depth("yuv420p12le") == "12-bit"
    assert normalize_bit_depth("yuv420p16le") == "16-bit"
    assert normalize_bit_depth("yuv420p") == ""            # 8-bit 不标注
    assert normalize_bit_depth("yuv420p", 8) == ""
    assert normalize_bit_depth("", 10) == "10-bit"          # 回退 bits_per_raw_sample
    assert normalize_bit_depth("", 12) == "12-bit"
    assert normalize_bit_depth("", None) == ""
    assert normalize_bit_depth("", "abc") == ""


def test_tags_frame_rate_and_bit_depth():
    """帧率/色深提取（pix_fmt 实测）。"""
    data = _mk(
        {"codec_name": "hevc", "width": 3840, "height": 2160,
         "avg_frame_rate": "24000/1001", "pix_fmt": "yuv420p10le"},
        [{"codec_name": "eac3", "channels": 6}],
    )
    tags = tags_from_ffprobe(data)
    assert tags.frame_rate == "23.976fps"
    assert tags.bit_depth == "10-bit"

    # 8-bit：色深折叠不标
    data["streams"][0]["pix_fmt"] = "yuv420p"
    tags = tags_from_ffprobe(data)
    assert tags.bit_depth == ""


def test_tags_from_ffprobe_no_video():
    """无视频流（如纯音频）：不崩溃，音频标签仍提取。"""
    tags = tags_from_ffprobe({"streams": [
        {"codec_type": "audio", "codec_name": "flac", "channels": 2}
    ]})
    assert tags.audio_encode == "FLAC 2.0"
    assert tags.resolution == ""
    assert isinstance(tags, ProbeTags)
