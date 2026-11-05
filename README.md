# WordCut 词剪

**基于词级对齐的播客访谈文本驱动剪辑工具。** 

<p align="center">
  <img src="assets/brand/wordcut-icon-1024.png" alt="WordCut" width="140" />
</p>

---

## 快速开始

### 方式一：双击

在项目根目录双击 **`start.bat`**。脚本会自动完成：

1. 检查 Python / Node 与前端依赖（首次缺依赖会自行 `npm install`）
2. 在**同一个窗口**里启动后端（:8000）与前端（:5173）
3. 轮询 `/api/health` 与前端口
4. 打开浏览器到 <http://localhost:5173>。

### 方式二：发行版（免 Node）

从 [Releases](https://github.com/TacitSay/WordCut/releases) 下载 `WordCut-<版本>-win64.zip`，
解压后双击 **`start-prod.bat`**：后端在 `http://127.0.0.1:8000` 同时托管界面与 API，
**不需要 Node、不需要 npm install**，只要求 Python 3.10 + ffmpeg。

包内不含模型（首次转写自动下载）与 CUDA 运行库（`scripts\fetch_cuda_libs.py` 按需补齐）。

### 方式三：命令行

```powershell
# 1) 后端
cd src
python -m uvicorn api:app --host 127.0.0.1 --port 8000

# 2) 前端
cd frontend
npm install          # 首次
npm run dev          # 打开 http://localhost:5173
```

---

## 依赖与自动下载

**仓库里不含模型，也不需要你手动下载。** 第一次点「转写」时，如果
`models/faster-whisper-small/` 不存在，后端会自动从 HuggingFace 拉取
（约 460MB），官方源不通时自动换国内镜像 `hf-mirror.com` 重试，落到
`models/faster-whisper-<名字>/`。

| 依赖 | 需要什么 | 怎么来 |
|------|---------|--------|
| Python 包 | 见 `requirements.txt` | `python -m pip install -r requirements.txt` |
| 模型 | `small`（默认） | **自动下载**，或 `python scripts\ensure_models.py` 预热 |
| ffmpeg / ffprobe | 转码、视频导出 | 装到 PATH；项目会自动找到 |
| CUDA 运行库 | GPU 加速（cuDNN 8 + cuBLAS 12） | `python scripts\fetch_cuda_libs.py`（约 1.7GB，缺失则自动回退 CPU） |
| 前端依赖 | Node 20+ | `start.bat` 自动 `npm install` |

想换模型（`tiny` / `base` / `medium` / `large-v3` …）：

```powershell
python scripts\ensure_models.py --model medium
# 或让服务用别的模型
set WORDCUT_WHISPER_MODEL=medium
```

离线环境（禁止联网下载）：

```powershell
set WORDCUT_NO_MODEL_DOWNLOAD=1
set WORDCUT_WHISPER_MODEL_PATH=D:\models\faster-whisper-small
```

### 环境变量一览

| 变量 | 默认 | 作用 |
|------|------|------|
| `WORDCUT_WHISPER_MODEL` | `small` | 模型尺寸 |
| `WORDCUT_WHISPER_MODEL_PATH` | 空 | 直接指定已有模型目录（优先于下载） |
| `WORDCUT_MODELS_DIR` | `<项目>/models` | 模型根目录 |
| `WORDCUT_NO_MODEL_DOWNLOAD` | 空 | `1` 时禁止自动下载 |
| `WORDCUT_WHISPER_DEVICE` | `auto` | `auto` / `cuda` / `cpu`（论文做对照实验用） |
| `WORDCUT_WHISPER_BATCHED` | `1` | `0` 关闭批处理转写（退回顺序模式） |
| `WORDCUT_WHISPER_BATCH_SIZE` | `8` | 批处理并行窗口数 |
| `WORDCUT_WHISPER_BEAM` | `5` | 顺序模式的 beam size |
| `WORDCUT_SEGMENT_STRATEGY` | `rule` | 默认分段策略（rule / semantic / semantic_local / semantic_api） |
| `WORDCUT_SEMANTIC_MODEL` | `Xenova/bge-small-zh-v1.5` | 本地语义模型（ONNX，约 25MB，首次自动下载） |
| `WORDCUT_SEMANTIC_ON_GPU` | 空 | `1` 时语义模型也用 GPU（默认纯 CPU，省显存） |
| `WORDCUT_SEMANTIC_API_BASE` | 空 | 语义分段 API 的 base（OpenAI 兼容，如 `https://api.openai.com/v1`） |
| `WORDCUT_SEMANTIC_API_KEY` | 空 | 语义分段 API 的 key |
| `WORDCUT_SEMANTIC_API_MODEL` | 空 | 语义分段 API 的模型名 |
| `WORDCUT_SEMANTIC_IDLE_SECONDS` | `600` | 语义模型空闲多久自动卸载 |
| `WORDCUT_WHISPER_IDLE_SECONDS` | `900` | 转写模型空闲多久自动卸载 |

---

## 环境要求

- **Python 3.10**
- **Node.js 20+**
- **ffmpeg 6+**（带 `libx264`、`aac`；NVENC 可选）
- Windows 10/11
- GPU 可选：有 NVIDIA 显卡时自动用 CUDA/fp16，实测 2 小时素材 **124 秒**转完；
  没有或驱动不匹配会自动回退 CPU/int8（约 4.6 倍慢）

---

## 常用命令

```powershell
python scripts\run_tests.py
python scripts\run_tests.py edl_render pipeline   # 只跑指定模块

# 环境自检 / 预热模型
python scripts\ensure_models.py

# CUDA 运行库（约 1.7GB）
python scripts\fetch_cuda_libs.py


# 桌面快捷方式（带图标）
powershell -ExecutionPolicy Bypass -File scripts\make_shortcut.ps1

cd frontend
npm run build
```

---

## 使用流程

1. **选择文件**（音频或视频）→ 自动转 16kHz 单声道 WAV 并创建项目
2. **转写** → 词级时间戳；长素材按 10 分钟分块，边转边出，可断点续转
3. **分段** → 右侧面板可选「规则 / 语义（本地模型）/ 语义（API）」重新切段，不改变任何词与时间戳
4. **删改** → 选中文本按 `Delete` 删除；连片删除自动合并成一段（词间停顿一并剪掉）
5. **试听** → 空格播放；波形上红色块 = 已删除区间
6. **导出** → 音频（wav/mp3/m4a）+ 字幕（SRT）+ EDL（JSON）；可选导出成品 MP4

---

## 常见问题

**Q：转写很慢 / 显示用的是 CPU？**
看启动窗口里的 `[后端]` 日志与 `GET /api/health` 的 `whisper_device`。
若是 `cpu`，通常是缺 CUDA 运行库：跑 `python scripts\fetch_cuda_libs.py`，
或用 `python scripts\check_cuda.py` 看具体原因（驱动、cuDNN 版本）。

**Q：模型下载卡住 / 失败？**
可以手动下载 `Systran/faster-whisper-small` 的四个文件
（`model.bin`、`config.json`、`tokenizer.json`、`vocabulary.txt`）放到
`models/faster-whisper-small/`，或用 `WORDCUT_WHISPER_MODEL_PATH` 指向别处。

**Q：端口被占用？**
`start.bat` 检测到 8000/5173 已被占用会直接复用；要强制重来就 `start.bat -Restart`。

**Q：pip 装不上（代理报 FileNotFoundError）？**
换国内源：`python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`。
本机还准备了 `scripts/fetch_cuda_libs.py` 这种"下 wheel 直接解包"的方式可参考。

**Q：2 小时素材要多久？**
转写（GPU 批处理）约 2 分钟；纯音频导出约 1 秒；带视频导出约 3 分钟（要重编码裁剪点）。

---