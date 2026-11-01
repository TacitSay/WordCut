"""视频剪辑模块：按 EDL 删除区间剪视频，并与剪辑后音频合流。

两种精度模式：
- fast：全程 -c copy 直通，剪辑点对齐到最近关键帧。最快、零画质损失，边界可能偏 0.5-2 秒。
- precise：剪辑点附近的 GOP 重编码，其余 -c copy。偏差 < 1 帧。

不变量：
- 不变量 1：源视频只读，结果写到新的 dst。
- 不变量 2：所有时间单位为秒（float）。
- 不变量 7：删除区间满足 0 <= start < end，越界部分裁剪到视频时长内。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

try:
    from model import DeleteSpan
except ImportError:
    from ..model import DeleteSpan  # type: ignore[no-redef]

from .ffmpeg_bin import MediaToolError, resolve_ffmpeg, resolve_ffprobe

__all__ = [
    "cut_video",
    "concat_av",
    "mux_av",
    "encode_audio_aac",
    "extract_keyframes",
    "keep_segments",
    "CUT_MODE_FAST",
    "CUT_MODE_PRECISE",
    "MAX_GOP_SECONDS",
]

CUT_MODE_FAST = "fast"
CUT_MODE_PRECISE = "precise"
MAX_GOP_SECONDS = 4.0
_TIMEOUT = 600

#: 视频编码器探测结果缓存（首次切视频时确定，进程内复用）。
_encoder_cache: Optional[Tuple[List[str], str]] = None

#: 每次 ffmpeg 调用里合并多少个保留段。批越大进程越少、但单批解码跨度越长；
#: 20 段在"启动开销"和"解码浪费"之间比较均衡。
SEGMENTS_PER_CALL = 20


@dataclass(frozen=True)
class _VideoInfo:
    duration: float
    fps: float
    has_video: bool
    has_audio: bool


def _probe_video(path: Path) -> _VideoInfo:
    """用 ffprobe 读视频时长、帧率与流类型。"""
    ffprobe = resolve_ffprobe()
    command = [
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    result = subprocess.run(
        command, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=_TIMEOUT,
    )
    if result.returncode != 0:
        raise MediaToolError(
            f"ffprobe 读取视频失败（{path}）：{(result.stderr or '').strip()[-600:]}"
        )
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaToolError(f"ffprobe 输出不是合法 JSON：{exc}") from exc

    streams = payload.get("streams") or []
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    duration = 0.0
    raw_duration = (payload.get("format") or {}).get("duration")
    if raw_duration:
        duration = float(raw_duration)
    elif video_streams and video_streams[0].get("duration"):
        duration = float(video_streams[0]["duration"])

    fps = 25.0
    if video_streams:
        rate = video_streams[0].get("avg_frame_rate") or video_streams[0].get("r_frame_rate")
        if rate and "/" in str(rate):
            num, _, den = str(rate).partition("/")
            try:
                value = float(num) / float(den) if float(den) else 0.0
                if value > 0:
                    fps = value
            except (ValueError, ZeroDivisionError):
                pass

    if duration <= 0:
        raise MediaToolError(f"无法确定视频时长：{path}")

    return _VideoInfo(
        duration=duration, fps=fps,
        has_video=bool(video_streams), has_audio=bool(audio_streams),
    )


def extract_keyframes(src: Path, limit: Optional[int] = None) -> List[float]:
    """提取视频所有关键帧的时间戳（秒，升序）。"""
    ffprobe = resolve_ffprobe()
    command = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-skip_frame", "nokey",
        "-show_entries", "frame=best_effort_timestamp_time,pkt_pts_time",
        "-of", "csv=p=0", str(src),
    ]
    result = subprocess.run(
        command, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=_TIMEOUT,
    )
    if result.returncode != 0:
        raise MediaToolError(
            f"ffprobe 读取关键帧失败（{src}）：{(result.stderr or '').strip()[-600:]}"
        )
    times: List[float] = []
    for line in (result.stdout or "").splitlines():
        raw = line.strip().split(",")[0].strip()
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if value >= 0:
            times.append(value)
        if limit is not None and len(times) >= limit:
            break
    times.sort()
    return times


def _merge_deletes(deletes: Sequence[DeleteSpan], duration: float) -> List[Tuple[float, float]]:
    """把删除区间裁剪到 [0, duration] 内并合并重叠部分。"""
    spans: List[Tuple[float, float]] = []
    for span in sorted(deletes, key=lambda item: (item.start, item.end)):
        start = max(0.0, float(span.start))
        end = min(float(duration), float(span.end))
        if end <= start:
            continue
        spans.append((start, end))

    merged: List[Tuple[float, float]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def keep_segments(deletes: Sequence[DeleteSpan], duration: float) -> List[Tuple[float, float]]:
    """删除区间的补集（保留区间），单位秒。全部删除时返回空列表。"""
    merged = _merge_deletes(deletes, duration)
    segments: List[Tuple[float, float]] = []
    cursor = 0.0
    for start, end in merged:
        if start > cursor:
            segments.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        segments.append((cursor, duration))
    return segments


def _snap_to_keyframe(time_value: float, keyframes: Sequence[float], mode: str) -> float:
    """把时间点吸附到关键帧。fast 向下吸附（不越过删除边界）。"""
    if not keyframes:
        return time_value
    if mode == CUT_MODE_FAST:
        candidates = [value for value in keyframes if value <= time_value]
        return candidates[-1] if candidates else 0.0
    return min(keyframes, key=lambda value: abs(value - time_value))


def cut_video(
    src: Path,
    deletes: List[DeleteSpan],
    dst: Path,
    mode: str = CUT_MODE_PRECISE,
    duration: Optional[float] = None,
    on_progress: Optional[Any] = None,
) -> dict:
    """按 EDL 删除区间剪视频，产物写到 dst。

    参数:
        on_progress: 可选回调 ``(done_batches, total_batches, kept_seconds)``，
            每批切完调用一次，供异步导出任务上报进度。

    返回 {"mode", "segments", "duration", "elapsed_ms", "reencoded_fraction"}。
    """
    import time

    if mode == CUT_MODE_FAST:
        # 实测结论：MP4/H.264 下 -c copy 的 -t 按包边界截断，时间精度不可控
        # （删 10.5s / 120s 源，实测输出偏差 +24.1s）。改为走精确模式。
        # 详见 docs/视频剪辑精度实测.md
        mode = CUT_MODE_PRECISE
    if mode != CUT_MODE_PRECISE:
        raise ValueError(f"mode 必须是 {CUT_MODE_PRECISE!r}，实际 {mode!r}")

    started = time.perf_counter()
    src = Path(src)
    dst = Path(dst)
    if not src.is_file():
        raise MediaToolError(f"源视频不存在：{src}")
    dst.parent.mkdir(parents=True, exist_ok=True)

    if duration is None:
        info = _probe_video(src)
        total = info.duration
    else:
        total = float(duration)

    segments = keep_segments(deletes, total)
    if not segments:
        raise ValueError("删除后没有任何保留区间，输出视频为空")

    ffmpeg = resolve_ffmpeg()
    keyframes = extract_keyframes(src) if mode == CUT_MODE_FAST else []

    workdir = Path(tempfile.mkdtemp(prefix="wordcut_videocut_"))
    parts: List[Path] = []
    reencoded = 0.0
    try:
        encoder_args, _encoder_name = _pick_video_encoder(ffmpeg)

        # 按批切：每批一次 ffmpeg（filter_complex 里 trim + concat），而不是
        # 每个保留段起一次进程。2 小时素材常见上千个剪辑点，单次进程的固定
        # 开销（启动 + 打开 250MB 容器 ≈0.15s）累计就能到几分钟。
        batches = [
            segments[i : i + SEGMENTS_PER_CALL]
            for i in range(0, len(segments), SEGMENTS_PER_CALL)
        ]
        for batch_index, batch in enumerate(batches):
            part = workdir / f"part{batch_index:04d}.mp4"
            _cut_batch(ffmpeg, src, part, batch, encoder_args)
            parts.append(part)
            for start, end in batch:
                reencoded += min(MAX_GOP_SECONDS, end - start)
            if on_progress is not None:
                try:
                    on_progress(batch_index + 1, len(batches), sum(e - s for s, e in batch))
                except Exception:  # noqa: BLE001 - 进度回调失败不影响导出
                    pass

        _concat_parts(ffmpeg, parts, workdir, dst)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    kept = sum(end - start for start, end in segments)
    return {
        "mode": mode,
        "segments": len(segments),
        "duration": kept,
        "elapsed_ms": round(elapsed_ms, 1),
        "reencoded_fraction": round(reencoded / kept, 4) if kept > 0 else 0.0,
    }


def _pick_video_encoder(ffmpeg: str) -> Tuple[List[str], str]:
    """挑视频编码器：优先 NVIDIA 硬编码（NVENC），不可用退回 libx264。

    为什么值得探测：视频导出要"每个剪辑点重编码一个 GOP"，2 小时素材上千个
    剪辑点时，libx264 veryfast 实测约 28 倍实时（100 段就要 176 秒），
    NVENC 通常快 3–6 倍，直接决定导出是"几分钟"还是"十几分钟"。

    探测方式：真跑一次 1 帧编码（比解析 `-encoders` 输出更可靠——有编码器
    但驱动/显卡不支持时会直接失败）。

    返回:
        ``(编码参数列表, 编码器名)``。
    """
    global _encoder_cache
    if _encoder_cache is not None:
        return _encoder_cache

    nvenc_args = [
        "-c:v", "h264_nvenc",
        "-preset", "p4",       # p1(最快) – p7(最慢最好)，p4 是均衡档
        "-rc", "vbr",
        "-cq", "21",           # 恒定质量，比固定码率更适合"只重编码小段"
        "-b:v", "0",
        "-pix_fmt", "yuv420p",
    ]
    try:
        probe = subprocess.run(
            [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                # 尺寸必须 >= NVENC 下限（实测 128x128 会报
                # "Frame Dimension less than the minimum supported value"），
                # 用 320x240 探测最稳
                "-f", "lavfi", "-i", "color=c=black:s=320x240:r=1:d=1",
                *nvenc_args, "-f", "null", "-",
            ],
            capture_output=True,
            timeout=60,
        )
        if probe.returncode == 0:
            _encoder_cache = (nvenc_args, "h264_nvenc")
            return _encoder_cache
    except (OSError, subprocess.TimeoutExpired):
        pass

    _encoder_cache = (
        ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"],
        "libx264",
    )
    return _encoder_cache


def _cut_batch(
    ffmpeg: str,
    src: Path,
    dst: Path,
    segments: Sequence[Tuple[float, float]],
    encoder_args: Sequence[str],
) -> None:
    """用一次 ffmpeg 调用切出多个保留段并拼接成 ``dst``（只含视频轨）。

    为什么批量：精确模式必须重编码，而每段单独起进程的固定开销很大
    （启动 + 打开几百 MB 的容器 ≈0.15s）。2 小时素材上千个剪辑点时，光启动
    就要几分钟。这里用 ``trim + concat`` 滤镜把一批段合成一次调用。

    时间轴处理：``-ss`` 先快速定位到批起点，滤镜里的 ``trim`` 用**相对时间**，
    因此每批只解码自己覆盖的范围，不会从文件开头重复解码。
    """
    if not segments:
        return

    batch_start = float(segments[0][0])
    batch_end = float(segments[-1][1])
    span = max(0.0, batch_end - batch_start)
    count = len(segments)

    filters: List[str] = []
    for index, (start, end) in enumerate(segments):
        rel_start = max(0.0, float(start) - batch_start)
        rel_end = max(0.0, float(end) - batch_start)
        filters.append(
            f"[0:v]trim=start={rel_start:.6f}:end={rel_end:.6f},setpts=PTS-STARTPTS[v{index}]"
        )

    if count == 1:
        map_label = "[v0]"
    else:
        labels = "".join(f"[v{index}]" for index in range(count))
        filters.append(f"{labels}concat=n={count}:v=1:a=0[out]")
        map_label = "[out]"

    command: List[str] = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{batch_start:.6f}", "-t", f"{span:.6f}", "-i", str(src),
        "-filter_complex", ";".join(filters),
        "-map", map_label,
        "-an",  # 音频最后统一由 concat_av 用剪辑后的音频轨写入
    ]
    command += list(encoder_args)
    command += ["-force_key_frames", "expr:eq(t,0)", "-movflags", "+faststart", str(dst)]

    _run(command, f"批切 {count} 段（{batch_start:.2f}–{batch_end:.2f}s）")


def _cut_one(
    ffmpeg: str,
    src: Path,
    dst: Path,
    start: float,
    end: float,
    mode: str,
    keyframes: Sequence[float],
) -> None:
    """切出单个保留区间。"""
    span = max(0.0, end - start)

    if mode == CUT_MODE_FAST and keyframes:
        snapped = _snap_to_keyframe(start, keyframes, CUT_MODE_FAST)
        if snapped < end:
            start = snapped
            span = end - start

    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    command += ["-ss", f"{start:.6f}", "-t", f"{span:.6f}", "-i", str(src)]

    if mode == CUT_MODE_FAST:
        command += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    else:
        encoder_args, _name = _pick_video_encoder(ffmpeg)
        command += list(encoder_args)
        command += [
            "-force_key_frames", "expr:eq(t,0)",
            "-c:a", "aac", "-b:a", "192k",
        ]

    command += ["-movflags", "+faststart", str(dst)]
    _run(command, f"切视频 {start:.3f}-{end:.3f}s")


def _concat_parts(ffmpeg: str, parts: Sequence[Path], workdir: Path, dst: Path) -> None:
    """用 concat demuxer 拼接多个片段（流复制）。"""
    if len(parts) == 1:
        shutil.copyfile(parts[0], dst)
        return

    list_file = workdir / "concat.txt"
    lines = []
    for part in parts:
        safe = str(part).replace("'", "''")
        lines.append(f"file '{safe}'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", "-movflags", "+faststart", str(dst),
    ]
    _run(command, "拼接片段")


def encode_audio_aac(src: Path, dst: Path, bitrate: str = "192k") -> None:
    """把音频轨单独编码成 AAC（.m4a），供后续 ``mux_av(copy)`` 合流。

    为什么单独做：合流时如果现编码 AAC，2 小时音频要 ~100 秒，而这段完全可以
    和"视频裁剪"并行（裁剪用 GPU/编码器，AAC 用 CPU），总耗时从
    ``视频 + 音频`` 降到 ``max(视频, 音频)``。
    """
    ffmpeg = resolve_ffmpeg()
    source = Path(src)
    target = Path(dst)
    if not source.is_file():
        raise MediaToolError(f"待编码的音频不存在：{source}")
    target.parent.mkdir(parents=True, exist_ok=True)

    argv = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-vn",
        "-c:a", "aac", "-b:a", bitrate,
        str(target),
    ]
    _run(argv, "编码音频轨（AAC）")


def mux_av(video: Path, audio: Path, dst: Path) -> None:
    """把视频轨（copy）与已编码好的音频轨（copy）合流成 mp4。"""
    ffmpeg = resolve_ffmpeg()
    argv = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "copy",
        "-shortest", "-movflags", "+faststart",
        str(dst),
    ]
    _run(argv, "合流音视频（流复制）")


def concat_av(video: Path, audio: Path, dst: Path) -> None:
    """把视频轨与剪辑后的音频轨合流。视频轨 -c copy，音频重编码 AAC。"""
    video = Path(video)
    audio = Path(audio)
    dst = Path(dst)
    if not video.is_file():
        raise MediaToolError(f"视频不存在：{video}")
    if not audio.is_file():
        raise MediaToolError(f"音频不存在：{audio}")
    dst.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg = resolve_ffmpeg()
    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart", str(dst),
    ]
    _run(command, "音视频合流")


def _run(command: Sequence[str], action: str) -> None:
    """执行 ffmpeg 命令，失败时抛出带 stderr 末尾的 MediaToolError。"""
    try:
        result = subprocess.run(
            list(command), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise MediaToolError(f"{action}失败：找不到 ffmpeg（{exc}）") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaToolError(f"{action}超时（{_TIMEOUT}s）") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-800:]
        raise MediaToolError(
            f"{action}失败（exit {result.returncode}）：{detail or '<无输出>'}"
        )
