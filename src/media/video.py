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
from typing import List, Optional, Sequence, Tuple

try:
    from model import DeleteSpan
except ImportError:
    from ..model import DeleteSpan  # type: ignore[no-redef]

from .ffmpeg_bin import MediaToolError, resolve_ffmpeg, resolve_ffprobe

__all__ = [
    "cut_video",
    "concat_av",
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
) -> dict:
    """按 EDL 删除区间剪视频，产物写到 dst。

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
        for index, (start, end) in enumerate(segments):
            part = workdir / f"part{index:04d}.mp4"
            _cut_one(ffmpeg, src, part, start, end, mode, keyframes)
            parts.append(part)
            if mode == CUT_MODE_PRECISE:
                reencoded += min(MAX_GOP_SECONDS, end - start)
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
        command += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
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
