# 书影 Bookreel · 小说转视频 (Novel to Video Converter)

本地化的小说转视频流水线，面向单张 RTX 4090 24GB。详细部署步骤见 [`部署说明.txt`](部署说明.txt)。

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `setup.bat` | 全部逻辑都在这一个文件里：菜单、安装、下载、各档位、体检、释放显存 |
| `scripts/get-llama.ps1` | 拉取 llama.cpp 的 Windows CUDA 预编译包 |
| `scripts/get-torch.ps1` | 装 CUDA 版 torch，并验证 `torch.cuda.is_available()` |
| `scripts/cosyvoice-req.txt` | CosyVoice2 推理依赖（不用上游 requirements.txt，见文件内说明） |
| `scripts/pip-constraints.txt` | `setuptools<81`，供 sdist 构建时找得到 pkg_resources |
| `services/embed_server.py` | 检索向量 · OpenAI 兼容 `/v1/embeddings` — `:8002` |
| `services/tts_server.py` | 旁白 · CosyVoice2 零样本 `/tts` — `:9100` |
| `services/asr_server.py` | 对齐 · FunASR `/align`，返回字幕时间轴与 SRT — `:9101` |
| `voices/` | 旁白参考音（`storyteller.wav` + 同名 `.txt`） |
| `Pipeline Runner.dc.html` | 网页控制台（直连本地接口） |
| `Novel to Video Studio.dc.html` | 设计稿 / Studio 界面 |
| `comfy-flux2-9x16.json` | Flux.2 工作流模板（节点名需与本机 ComfyUI 一致） |

## 档位

| 档位 | 内容 | 端口 |
| --- | --- | --- |
| A | Qwen3.8-27B Q4_K_M（llama.cpp） | `:8000` |
| S | Qwen3-8B（更快的备选） | `:8000` |
| B | Flux.2（ComfyUI） | `:7860` |
| C | Hailuo 视频（需自备 `hailuo/server.py`） | `:9000` |
| 常驻 | 检索 / 旁白 / 对齐（纯 CPU） | `:8002` `:9100` `:9101` |

`setup.bat` 也可以直接带命令字调用，例如 `setup.bat health`、`setup.bat llm`、`setup.bat comfy`。

## 快速开始

1. 双击 `setup.bat`（或在 PowerShell 中执行 `.\setup.bat`）
2. 选 `1` 安装环境
3. 选 `L` 登录 Hugging Face，并在 <https://huggingface.co/black-forest-labs/FLUX.2-dev> 点击 Accept
4. 选 `2` 下载权重

## 环境要求

- Python 3.11 或 3.12 最稳。3.13 可用，但 `setup.bat` 需要额外绕开几个坑：
  vLLM 在 Windows 上根本没有轮子（已换成 llama.cpp），PyPI 的 `torch` 在 Windows 上是 CPU 版
  （已改为从 PyTorch 的 CUDA 源安装并验证），CosyVoice 的 `requirements.txt` 也装不上。
- NVIDIA GPU，24GB 显存（RTX 4090 基准）

## 说明

所有 `.bat` 一律 ASCII + CRLF —— Windows 的 cmd 在 `chcp` 生效前按当前代码页逐字节读取批处理文件，中文注释会让解析器错位。中文说明统一放在 `部署说明.txt`。

模型权重、虚拟环境、日志、llama.cpp 二进制、`third_party/` 以及 ComfyUI/Hailuo 运行目录
均由脚本在本地生成，不纳入版本管理（见 `.gitignore`）。
