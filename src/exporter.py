"""导出模块：把剪辑结果落盘成音频、SRT 字幕与 EDL JSON。

纯 Python + numpy + soundfile（mp3 场景额外调用 ffmpeg 子进程）。对外四个函数：

- :func:`export_audio`：写 wav（或转码 mp3）——目标音频。
- :func:`export_srt`：写 UTF-8（无 BOM）的 SRT 字幕。
- :func:`export_edl`：把 :class:`model.EDL` 序列化成 JSON。
- :func:`export_all`：一次性导出 ``{basename}.wav`` / ``{basename}.srt`` /
  ``{basename}.edl.json``。

遵守的不变量（见 docs/不变量.md）：

* 不变量 1：源音频只读。本模块对传入的 ``samples`` 只读不写，任何格式转换
  （整型 -> 浮点、非连续 -> 连续）都发生在**新数组**上，调用方持有的数组
  内容与内存布局均不会被改动。
* 不变量 2：所有时间戳单位为秒（float）；本模块只写字节，不做时间换算。
* 不变量 5：字幕文本由调用方（:func:`subtitle.rebuild_srt`）生成，导出层
  不判断插入 / 删除语义，只负责编码与换行规范化。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Union

import numpy as np
import soundfile as sf

from model import EDL

__all__ = [
    "FFMPEG_BIN",
    "MP3_BITRATE_ARGS",
    "export_audio",
    "export_srt",
    "export_edl",
    "export_all",
]

#: ffmpeg 可执行文件；可用环境变量 ``WORDCUT_FFMPEG`` 覆盖（便于自定义路径）。
FFMPEG_BIN: str = os.environ.get("WORDCUT_FFMPEG", "ffmpeg")

#: mp3 转码参数：VBR 质量 2（约 190 kbps），足够配音/口播场景。
MP3_BITRATE_ARGS = ("-codec:a", "libmp3lame", "-q:a", "2")

#: 整型样本的位宽 -> soundfile subtype。
_INT_SUBTYPES: Dict[int, str] = {16: "PCM_16", 24: "PCM_24", 32: "PCM_32"}

#: 时间目录里临时 wav 的文件名前缀（便于失败后人工排查残留）。
_TEMP_WAV_PREFIX = ".wordcut_export_"


# ----------------------------------------------------------------------
# 1. 音频导出
# ----------------------------------------------------------------------
def export_audio(
    samples: np.ndarray,
    sample_rate: int,
    output_path: Union[str, Path],
) -> None:
    """把音频样本写成文件；后缀为 ``.mp3`` 时经 ffmpeg 转码。

    流程：

    * ``.wav``：直接用 :func:`soundfile.write` 写出；浮点样本写成 32-bit
      float WAV（无损、不过载），整型样本按原位宽写成对应 PCM。
    * ``.wav``：直接用 :func:`soundfile.write` 写出（无损）。
    * ``.flac``：经 ffmpeg 转码为无损 FLAC（压缩率约 50%，音质无损失）。
    * ``.mp3`` / ``.m4a``：经 ffmpeg 转码为有损格式（MP3 VBR q2 / AAC 192k）。
    * 转码类格式统一先写临时 float WAV 再调用 ffmpeg，无论成功与否都会删除临时文件。

    **不变量 1**：传入的 ``samples`` 只读。函数内不做任何就地运算，整型样本
    转浮点、非连续数组转连续数组都产生新数组。

    参数:
        samples: 音频样本，一维（单声道）数组，或二维 ``(帧数, 声道数)``
            数组；dtype 为浮点（建议 float32，取值约在 [-1, 1]）或
            int16/int32 等整数 PCM。
        sample_rate: 采样率（Hz），正整数。
        output_path: 目标文件路径；父目录不存在时会自动创建。

    返回:
        ``None``。成功即文件已落盘。

    异常:
        TypeError: ``samples`` 不是数值数组，或 ``sample_rate`` 不是整数。
        ValueError: ``samples`` 维度/声道数不合法、为空数组，``sample_rate``
            非正，或 ``output_path`` 没有扩展名。
        OSError: 目录创建失败、文件写入失败（如磁盘满 / 无权限）。
        RuntimeError: mp3 场景下找不到 ffmpeg，或 ffmpeg 转码返回非零。
    """
    data = _validate_samples(samples)
    rate = _validate_sample_rate(sample_rate)
    path = _prepare_output_path(output_path)

    if path.suffix in _FFMPEG_AUDIO_SUFFIXES:
        _export_via_ffmpeg(data, rate, path)
        return

    _write_soundfile(data, rate, path, subtype=_subtype_for(data, path))


#: 需要经 ffmpeg 转码的后缀 -> 转码参数。
_FFMPEG_AUDIO_SUFFIXES = {
    ".mp3": ("-codec:a", "libmp3lame", "-q:a", "2"),
    ".m4a": ("-codec:a", "aac", "-b:a", "192k"),
    ".flac": ("-codec:a", "flac", "-compression_level", "5"),
}


def _export_via_ffmpeg(data: np.ndarray, sample_rate: int, path: Path) -> None:
    """经临时 WAV 中转，把样本转码为目标格式（mp3 / m4a / flac）。"""
    ffmpeg = _resolve_ffmpeg()

    # 临时文件放在输出目录，避免跨盘拷贝；dir 参数保证同一文件系统内 rename。
    fd, temp_name = tempfile.mkstemp(
        prefix=_TEMP_WAV_PREFIX, suffix=".wav", dir=str(path.parent)
    )
    os.close(fd)
    temp_path = Path(temp_name)

    try:
        # 中转 WAV 统一用 float32，避免整型样本先转 PCM 再转 mp3 的二次量化。
        _write_soundfile(
            _as_float32(data), sample_rate, temp_path, subtype="FLOAT"
        )
        command = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(temp_path),
            *_FFMPEG_AUDIO_SUFFIXES[path.suffix],
            str(path),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(
                "ffmpeg 转码 {suffix} 失败（exit code {code}）：{detail}；"
                "命令：{command}".format(
                    suffix=path.suffix,
                    code=result.returncode,
                    detail=detail[-800:] or "<无输出>",
                    command=" ".join(command),
                )
            )
    except FileNotFoundError as exc:  # 竞态：解析到路径但已不可执行
        raise RuntimeError(
            f"无法执行 ffmpeg（{ffmpeg}）：{exc}"
        ) from exc
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # 清理失败不影响主流程，但不能静默
            raise RuntimeError(
                f"{path.suffix} 转码后删除临时文件失败：{temp_path}（{exc}）"
            ) from exc


def _write_soundfile(
    data: np.ndarray,
    sample_rate: int,
    path: Path,
    subtype: Union[str, None],
) -> None:
    """调用 :func:`soundfile.write`，把底层异常包装成带路径的清晰信息。"""
    try:
        sf.write(str(path), data, sample_rate, subtype=subtype)
    except (RuntimeError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "写出音频失败：{path}（subtype={subtype!r}, dtype={dtype}, "
            "shape={shape}）：{exc}".format(
                path=path,
                subtype=subtype,
                dtype=data.dtype,
                shape=data.shape,
                exc=exc,
            )
        ) from exc
    except OSError as exc:
        raise OSError(f"写出音频失败：{path}（{exc}）") from exc


def _subtype_for(data: np.ndarray, path: Path) -> Union[str, None]:
    """按 dtype 与后缀选择 soundfile subtype；非 wav 交给 soundfile 推断。"""
    if path.suffix != ".wav":
        return None
    if data.dtype.kind == "f":
        return "FLOAT"
    bits = data.dtype.itemsize * 8
    return _INT_SUBTYPES.get(bits, "PCM_16")


def _as_float32(data: np.ndarray) -> np.ndarray:
    """返回 float32 副本；整型按位宽归一化到 [-1, 1)。

    永远是**新数组**，不触碰调用方的 ``samples``（不变量 1）。
    """
    if data.dtype.kind == "f":
        return np.ascontiguousarray(data, dtype=np.float32)
    scale = 1.0 / float(2 ** (data.dtype.itemsize * 8 - 1))
    return np.ascontiguousarray(
        data.astype(np.float64) * scale, dtype=np.float32
    )


def _resolve_ffmpeg() -> str:
    """定位 ffmpeg 可执行文件；找不到时抛 RuntimeError。"""
    candidate = shutil.which(FFMPEG_BIN)
    if candidate:
        return candidate
    explicit = Path(FFMPEG_BIN)
    if explicit.is_file():
        return str(explicit)
    raise RuntimeError(
        "导出 mp3 需要 ffmpeg，但未在 PATH 中找到可执行文件 "
        f"{FFMPEG_BIN!r}；请安装 ffmpeg 或设置环境变量 WORDCUT_FFMPEG "
        "指向其绝对路径，也可改为导出 .wav。"
    )


# ----------------------------------------------------------------------
# 2. 字幕导出
# ----------------------------------------------------------------------
def export_srt(srt_text: str, output_path: Union[str, Path]) -> None:
    """把 SRT 文本写为 UTF-8（无 BOM）文件。

    换行统一成 ``\\n``（Windows 上不会出现 ``\\r\\n``，避免部分播放器把
    ``\\r`` 当成正文）；非空文本末尾补一个换行——这是 SRT 的惯例收尾。
    空字符串照常写出空文件（合法，表示没有字幕）。

    参数:
        srt_text: SRT 文本，通常来自 :func:`subtitle.rebuild_srt`。
        output_path: 目标文件路径；父目录不存在时会自动创建。

    返回:
        ``None``。

    异常:
        TypeError: ``srt_text`` 不是字符串。
        ValueError: ``output_path`` 没有扩展名。
        OSError: 目录创建失败或写入失败。
    """
    if not isinstance(srt_text, str):
        raise TypeError(f"srt_text 必须是 str，实际为 {type(srt_text).__name__}")

    path = _prepare_output_path(output_path)

    text = srt_text.replace("\r\n", "\n").replace("\r", "\n")
    if text and not text.endswith("\n"):
        text += "\n"

    # utf-8 编码不会产生 BOM；显式剥离只是防止上游文本自带 U+FEFF。
    raw = text.encode("utf-8")
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]

    try:
        path.write_bytes(raw)
    except OSError as exc:
        raise OSError(f"写出字幕失败：{path}（{exc}）") from exc


# ----------------------------------------------------------------------
# 3. EDL 导出
# ----------------------------------------------------------------------
def export_edl(edl: EDL, output_path: Union[str, Path]) -> None:
    """把 :class:`model.EDL` 序列化成 JSON 文件（UTF-8，无 BOM）。

    序列化内容就是 ``edl.to_dict()`` 的结果（``source`` / ``sample_rate`` /
    ``deletes``），不额外包壳，保证 ``EDL.from_dict(json.load(...))`` 可直接
    往返；中文按原样写出（``ensure_ascii=False``），缩进 2 空格便于 diff。

    参数:
        edl: 编辑决策列表。
        output_path: 目标文件路径；父目录不存在时会自动创建。

    返回:
        ``None``。

    异常:
        TypeError: ``edl`` 不是 :class:`model.EDL`。
        ValueError: ``output_path`` 没有扩展名，或 ``to_dict()`` 结果无法
            被 JSON 编码（理论上不会发生）。
        OSError: 目录创建失败或写入失败。
    """
    if not isinstance(edl, EDL):
        raise TypeError(f"edl 必须是 model.EDL，实际为 {type(edl).__name__}")

    path = _prepare_output_path(output_path)

    try:
        payload = json.dumps(edl.to_dict(), ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"EDL 无法序列化为 JSON：{exc}") from exc

    try:
        path.write_bytes((payload + "\n").encode("utf-8"))
    except OSError as exc:
        raise OSError(f"写出 EDL 失败：{path}（{exc}）") from exc


# ----------------------------------------------------------------------
# 4. 一次性导出
# ----------------------------------------------------------------------
def export_all(
    samples: np.ndarray,
    sample_rate: int,
    output_dir: Union[str, Path],
    basename: str,
    edl: EDL,
    srt_text: str,
) -> Dict[str, Path]:
    """一次性导出 ``{basename}.wav``、``{basename}.srt``、``{basename}.edl.json``。

    先把全部参数校验完再动磁盘：校验失败时不会留下半成品文件。

    参数:
        samples: 剪辑后的音频样本（只读，见不变量 1）。
        sample_rate: 采样率（Hz），必须与 ``edl.sample_rate`` 一致。
        output_dir: 输出目录；不存在时自动创建。
        basename: 文件主名（不含扩展名、不含路径分隔符），如 ``"interview_01"``。
        edl: 编辑决策列表。
        srt_text: SRT 字幕文本。

    返回:
        形如 ``{"audio_path": Path, "srt_path": Path, "edl_path": Path}`` 的
        字典，值为实际写出的绝对路径（已解析符号链接与相对段）。

    异常:
        TypeError: ``edl`` 不是 :class:`model.EDL`，或 ``srt_text`` 非 str。
        ValueError: ``basename`` 为空 / 含路径分隔符，``sample_rate`` 非正，
            或 ``sample_rate`` 与 ``edl.sample_rate`` 不一致。
        OSError / RuntimeError: 同 :func:`export_audio` / :func:`export_srt` /
            :func:`export_edl`。
    """
    # ---- 先校验，后落盘 ----
    data = _validate_samples(samples)
    rate = _validate_sample_rate(sample_rate)
    if not isinstance(edl, EDL):
        raise TypeError(f"edl 必须是 model.EDL，实际为 {type(edl).__name__}")
    if int(edl.sample_rate) != rate:
        raise ValueError(
            f"edl.sample_rate={edl.sample_rate!r} 与 sample_rate={rate!r} 不一致"
            "（说明样本与 EDL 不是同一份素材）"
        )
    if not isinstance(srt_text, str):
        raise TypeError(f"srt_text 必须是 str，实际为 {type(srt_text).__name__}")
    stem = _validate_basename(basename)

    directory = _ensure_directory(Path(output_dir))

    paths: Dict[str, Path] = {
        "audio_path": directory / f"{stem}.wav",
        "srt_path": directory / f"{stem}.srt",
        "edl_path": directory / f"{stem}.edl.json",
    }

    export_audio(data, rate, paths["audio_path"])
    export_srt(srt_text, paths["srt_path"])
    export_edl(edl, paths["edl_path"])

    return {name: path.resolve() for name, path in paths.items()}


# ----------------------------------------------------------------------
# 内部校验工具
# ----------------------------------------------------------------------
def _validate_samples(samples: np.ndarray) -> np.ndarray:
    """校验样本数组并返回原数组（只读语义，不复制、不改写）。"""
    if isinstance(samples, np.ndarray) and samples.dtype.kind not in "fiu":
        raise TypeError(
            f"samples 必须是数值数组，实际 dtype={samples.dtype}"
        )
    array = np.asarray(samples)
    if array.dtype.kind not in "fiu":
        raise TypeError(
            "samples 必须是数值数组（浮点或整型 PCM），"
            f"实际 dtype={array.dtype}"
        )
    if array.ndim not in (1, 2):
        raise ValueError(
            f"samples 必须是一维或二维数组，实际为 {array.ndim} 维"
        )
    if array.ndim == 2 and array.shape[1] not in (1, 2):
        raise ValueError(
            "二维 samples 必须形如 (帧数, 声道数)，声道数只能为 1 或 2，"
            f"实际 shape={array.shape}"
        )
    if array.size == 0:
        raise ValueError("samples 不能为空数组（至少要有一个样本）")
    return array


def _validate_sample_rate(sample_rate: int) -> int:
    """校验采样率：必须是正整数，拒绝 bool。"""
    if isinstance(sample_rate, bool) or not isinstance(
        sample_rate, (int, np.integer)
    ):
        raise TypeError(
            f"sample_rate 必须是 int，实际为 {type(sample_rate).__name__}"
        )
    rate = int(sample_rate)
    if rate <= 0:
        raise ValueError(f"sample_rate 必须为正整数，实际为 {rate!r}")
    return rate


def _validate_basename(basename: str) -> str:
    """校验文件主名：非空、不含路径分隔符与通配保留字符。"""
    if not isinstance(basename, str):
        raise TypeError(f"basename 必须是 str，实际为 {type(basename).__name__}")
    stem = basename.strip()
    if not stem:
        raise ValueError("basename 不能为空")
    if stem in (".", "..") or Path(stem).name != stem:
        raise ValueError(
            f"basename 只能是文件主名，不能包含路径分隔符，实际为 {basename!r}"
        )
    invalid = set('<>:"/\\|?*')
    bad = sorted(set(stem) & invalid)
    if bad:
        raise ValueError(
            f"basename 含有非法字符 {''.join(bad)!r}，实际为 {basename!r}"
        )
    return stem


def _prepare_output_path(output_path: Union[str, Path]) -> Path:
    """把输出路径转成 :class:`pathlib.Path`，校验扩展名并建好父目录。"""
    if not isinstance(output_path, (str, Path)):
        raise TypeError(
            f"output_path 必须是 str 或 pathlib.Path，实际为 "
            f"{type(output_path).__name__}"
        )
    path = Path(output_path)
    if not path.name:
        raise ValueError(f"output_path 不能是目录，实际为 {output_path!r}")
    if not path.suffix:
        raise ValueError(
            f"output_path 必须带扩展名（如 .wav / .srt / .json），"
            f"实际为 {output_path!r}"
        )
    _ensure_directory(path.parent)
    return path


def _ensure_directory(directory: Path) -> Path:
    """确保目录存在（含父级），失败时抛出带路径的 OSError。"""
    if directory == Path(""):
        directory = Path(".")
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(f"无法创建输出目录：{directory}（{exc}）") from exc
    return directory
