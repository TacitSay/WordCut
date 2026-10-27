"""exporter.py 测试"""
import sys, json, tempfile, shutil
from pathlib import Path
sys.path.insert(0, r'E:\WordCut\src')
import numpy as np
from model import EDL, DeleteSpan
from exporter import export_audio, export_srt, export_edl, export_all

passed = 0
failed = 0

def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS: {name}")
    else:
        failed += 1
        print(f"FAIL: {name}")

tmp = Path(tempfile.mkdtemp(prefix='wordcut_test_'))
sr = 16000
samples = (0.3 * np.sin(2*np.pi*440*np.arange(sr)/sr)).astype(np.float32)

try:
    # 1. wav 导出
    wav = tmp / 'a.wav'
    export_audio(samples, sr, wav)
    check("wav 导出存在", wav.exists() and wav.stat().st_size > 1000)

    # 2. 源数组未被修改(不变量1)
    check("导出后源数组未变", np.array_equal(samples, (0.3 * np.sin(2*np.pi*440*np.arange(sr)/sr)).astype(np.float32)))

    # 3. mp3 导出(需 ffmpeg)
    mp3 = tmp / 'a.mp3'
    try:
        export_audio(samples, sr, mp3)
        check("mp3 导出存在", mp3.exists() and mp3.stat().st_size > 1000)
    except RuntimeError as e:
        check(f"mp3 导出(ffmpeg不可用): {e}"[:80], False)

    # 4. srt 导出(UTF-8 无 BOM)
    srt = tmp / 'a.srt'
    export_srt("1\n00:00:00,000 --> 00:00:01,000\n测试字幕\n", srt)
    raw = srt.read_bytes()
    check("srt 无 BOM", not raw.startswith(b'\xef\xbb\xbf'))
    check("srt 中文正确", '测试字幕' in raw.decode('utf-8'))

    # 5. EDL 导出
    edl = EDL(source='a.wav', sample_rate=sr)
    edl.add_delete(1.0, 2.0, cause='word')
    ej = tmp / 'a.edl.json'
    export_edl(edl, ej)
    data = json.loads(ej.read_text(encoding='utf-8'))
    check("EDL JSON 正确", data['source'] == 'a.wav' and len(data['deletes']) == 1)

    # 6. EDL 往返
    edl2 = EDL.from_dict(data)
    check("EDL 往返一致", edl2.deletes[0].start == 1.0)

    # 7. export_all
    out = tmp / 'out'
    paths = export_all(samples, sr, out, 'test01', edl, "1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    check("export_all 三文件", all(Path(v).exists() for v in paths.values()))
    check("export_all 键名", set(paths.keys()) == {'audio_path','srt_path','edl_path'})

    # 8. 采样率不一致报错
    try:
        export_all(samples, 8000, out, 'x', edl, "")
        check("采样率不一致报错", False)
    except ValueError:
        check("采样率不一致报错", True)

    # 9. 空数组报错
    try:
        export_audio(np.array([], dtype=np.float32), sr, tmp/'e.wav')
        check("空数组报错", False)
    except ValueError:
        check("空数组报错", True)

finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n结果: {passed} 通过, {failed} 失败")
exit(1 if failed else 0)
