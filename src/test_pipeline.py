"""pipeline.py 测试: 端到端编排"""
import sys, json, tempfile, shutil
from pathlib import Path
sys.path.insert(0, r'E:\WordCut\src')
import numpy as np
from pipeline import load_tokens, build_deletes, run_pipeline

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

# 构造模拟 faster-whisper 输出
words = [
    {"seg": 0, "start": 0.0, "end": 0.4, "dur": 0.4, "word": "今天", "prob": 0.95},
    {"seg": 0, "start": 0.4, "end": 0.8, "dur": 0.4, "word": "我们", "prob": 0.92},
    {"seg": 0, "start": 0.8, "end": 1.2, "dur": 0.4, "word": "讨论", "prob": 0.98},
    {"seg": 0, "start": 1.2, "end": 1.8, "dur": 0.6, "word": "问题。", "prob": 0.99},
    {"seg": 1, "start": 2.5, "end": 3.0, "dur": 0.5, "word": "谢谢。", "prob": 0.97},
]

sr = 16000
samples = (0.3 * np.sin(2*np.pi*440*np.arange(int(3.5*sr))/sr)).astype(np.float32)
samples_copy = samples.copy()

# 1. load_tokens
tokens = load_tokens(words)
check(f"load_tokens 数量({len(tokens)})", len(tokens) == 5)
check("token id 格式 t00000", tokens[0].id == 't00000')
check("token text strip", tokens[0].text == '今天')
check("token confidence", abs(tokens[0].confidence - 0.95) < 1e-6)
check("token is_deleted 默认 False", all(not t.is_deleted for t in tokens))

# 2. build_deletes 单个
spans = build_deletes(tokens, {'t00001'})
check(f"单个删除区间({len(spans)})", len(spans) == 1 and spans[0].start == 0.4)

# 3. build_deletes 合并(间隔<0.3)
spans2 = build_deletes(tokens, {'t00000','t00001'}, pause_merge=0.5)
check(f"相邻合并({len(spans2)})", len(spans2) == 1 and spans2[0].start == 0.0 and spans2[0].end == 0.8)

# 4. 连片删除：词序相邻的被删词默认合并成一段（不再留词间空隙）
spans3 = build_deletes(tokens, {'t00000','t00001'})
check(f"连片默认合并({len(spans3)})", len(spans3) == 1)
check(
    f"连片区间覆盖首尾({spans3[0].start}-{spans3[0].end})",
    spans3[0].start == 0.0 and abs(spans3[0].end - 0.8) < 1e-9,
)

# 4b. merge_gaps=0 可退回"逐词各删一段"
spans3b = build_deletes(tokens, {'t00000','t00001'}, merge_gaps=0.0)
check(f"merge_gaps=0 不合并({len(spans3b)})", len(spans3b) == 2)

# 4c. 中间夹着保留词时**不能**合并（否则会连没删的词一起剪掉）
spans4 = build_deletes(tokens, {'t00000','t00002'})
check(f"跨保留词不合并({len(spans4)})", len(spans4) == 2)

# 4d. 词序相邻但间隔超过阈值（长静默）不合并
spans5 = build_deletes(tokens, {'t00003','t00004'})
check(f"超阈值不合并({len(spans5)})", len(spans5) == 2)

tmp = Path(tempfile.mkdtemp(prefix='wordcut_pipe_'))
try:
    # 5. 完整流水线
    result = run_pipeline(words, samples, sr, {'t00001'}, tmp, 'ep01')
    check("pipeline 返回 token_count", result['token_count'] == 5)
    check("pipeline 返回 deleted_count", result['deleted_count'] == 1)
    check(f"输出时长 {result['output_duration']:.3f}s < 3.5s", result['output_duration'] < 3.5)

    paths = result['paths']
    check("三件套落盘", all(Path(v).exists() for v in paths.values()))
    print(f"  音频: {paths['audio_path']}")
    print(f"  字幕: {paths['srt_path']}")
    print(f"  EDL : {paths['edl_path']}")

    srt_text = Path(paths['srt_path']).read_text(encoding='utf-8')
    check("字幕不含被删词", '我们' not in srt_text)
    check("字幕含保留词", '今天' in srt_text)
    print("--- SRT 内容 ---")
    print(srt_text)

    # 6. 源样本未被修改(不变量1)
    check("源样本未被修改", np.array_equal(samples, samples_copy))

    # 7. 缺失 id 报错
    try:
        run_pipeline(words, samples, sr, {'t99999'}, tmp, 'bad')
        check("缺失id报错", False)
    except ValueError as e:
        check("缺失id报错", 't99999' in str(e))

    # 8. 全删语义: 按 token 范围删除只覆盖 [0, 末token.end]，音频尾部空白段仍保留
    #    真正的"输出为空"需删除区间覆盖整个音频，用 compute_keep_segments 直接验证预检逻辑
    import edl_render
    from model import DeleteSpan
    # tokens 之间有 1.8-2.5 的停顿，因此保留区为 2 段: (1.8,2.5) 和尾部 (3.0,3.5)
    keep = edl_render.compute_keep_segments(
        [DeleteSpan(start=t.start, end=t.end, cause='word') for t in tokens], 3.5)
    check(f"全删token后仍有保留区(尾部空白), 实际{len(keep)}段", len(keep) == 2 and keep[0][0] == 1.8 and keep[-1][1] == 3.5)

    # 8b. 删除区间覆盖全程 -> 无保留区
    full_cover = edl_render.compute_keep_segments([DeleteSpan(start=0.0, end=3.5, cause='word')], 3.5)
    check("覆盖全程时保留区为空", full_cover == [])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n结果: {passed} 通过, {failed} 失败")
exit(1 if failed else 0)
