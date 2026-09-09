# 书影 Bookreel · 小说转视频 (Novel to Video Converter)

本地化的小说转视频流水线。两套部署：单张 RTX 4090 24GB（Windows）与 Apple Silicon 统一内存（macOS，按 M3 Max 64GB 调参）。
详细部署步骤见 [`部署说明.txt`](部署说明.txt) 与 [`部署说明-macOS.txt`](部署说明-macOS.txt)。

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `setup.bat` | Windows：全部逻辑都在这一个文件里：菜单、安装、下载、各档位、体检、释放显存 |
| `setup.command` | macOS 同上一份，按统一内存重调（档位可并存、Metal、日志后台） |
| `scripts/get-llama.ps1` | 拉取 llama.cpp 的 Windows CUDA 预编译包 |
| `scripts/get-llama-mac.sh` | 拉取 llama.cpp 的 macos-arm64 Metal 包，失败退到 brew / 源码编译 |
| `scripts/get-torch.ps1` | 装 CUDA 版 torch，并验证 `torch.cuda.is_available()` |
| `scripts/cosyvoice-req.txt` | CosyVoice2 推理依赖（不用上游 requirements.txt，见文件内说明） |
| `scripts/pip-constraints.txt` | `setuptools<81`，供 sdist 构建时找得到 pkg_resources |
| `services/embed_server.py` | 检索向量 · OpenAI 兼容 `/v1/embeddings` — `:8002`（控制台只体检，不调用；留给外部检索用） |
| `services/tts_server.py` | 旁白 · CosyVoice2 零样本 `/tts` — `:9100` |
| `services/asr_server.py` | 对齐 · FunASR `/align`，返回字幕时间轴与 SRT — `:9101`（第 05 步拼整章字幕用） |
| `services/h3_server.py` | 出片 · MiniMax-H3 经 h3.c 本地渲染，接口同 MiniMax 云端 — `:9000`（仅 macOS） |
| `services/web_server.py` | 网页控制台的静态服务 — `:8080`（只用标准库，不需要 venv） |
| `voices/` | 旁白参考音（`storyteller.wav` + 同名 `.txt`） |
| `Pipeline Runner.dc.html` | 网页控制台（直连本地接口） |
| `comfy-flux2-9x16.json` | Flux.2 工作流模板（节点名需与本机 ComfyUI 一致） |
| `support.js` | 两个 `.dc.html` 的运行时，由设计工具生成 —— 别手改 |
| `_ds/<uuid>/` | 设计系统的样式与 token 包，两个 `.dc.html` 都 `<link>` 它 |

`support.js` 与 `_ds/` 是设计工具的产物，不是手写代码，但所有 `.dc.html` 都直接引用，删了页面就打不开。
`_ds/` 的 uuid 目录名由工具决定，重新生成设计系统时会换名字，届时两个 HTML 的引用要一起改。

## 档位

| 档位 | 内容 | 端口 |
| --- | --- | --- |
| A | Qwen3.8-27B Q4_K_M（llama.cpp） | `:8000` |
| S | Qwen3-8B（更快的备选） | `:8000`（macOS 上是 `:8001`，可与档位 A 并存） |
| B | Flux.2（ComfyUI） | `:7860` |
| C | Hailuo 视频（需自备 `hailuo/server.py`）；macOS 上默认走 MiniMax-H3 + h3.c | `:9000` |
| 常驻 | 检索 / 旁白 / 对齐（纯 CPU） | `:8002` `:9100` `:9101` |
| 控制台 | 网页界面（`setup` 菜单 `W`） | `:8080` |

## 打开控制台 / 远程访问

菜单 `W`。它会先起 `services/web_server.py`，再用浏览器打开
`http://127.0.0.1:8080/Pipeline%20Runner.dc.html`。

**不能直接双击 `.dc.html`。** `support.js` 里的 dc runtime 启动时会
`fetch(location.href)` 把自己的源码再读一遍，浏览器在 `file://` 下拒绝这个请求，
页面会白屏。所以必须走 HTTP —— 这就是 `web_server.py` 存在的全部理由。
它只用标准库，因此在跑菜单 1 之前就能起来；也只交出页面真正引用的那几个文件，
不会把 `models/`、`logs/`、HF token 一起端出去。

想从手机或另一台机器打开：把 `setup.command` / `setup.bat` 顶部的 `BIND_LAN`
改成 `1`，重起服务，再选 `W`，服务端会打印本机的局域网地址。
控制台里的端点默认跟着页面地址走 —— 从 `http://192.168.x.x:8080` 打开时，
它们自动指向 `192.168.x.x`，不用手改（`127.0.0.1` 指的是你手上那台设备，
不是跑模型的那台，这是远程访问最容易踩的坑）。

`BIND_LAN=1` 会把模型服务一起绑到 `0.0.0.0`。这些服务都没有口令，CORS 也是
`*`，所以只在信得过的网络里开；要放到更远的地方，前面加隧道或反代，不要直接
把端口暴露出去。

`setup.bat` / `setup.command` 都可以直接带命令字调用，例如 `setup.bat health`、`./setup.command llm`。

## 快速开始（Windows）

1. 双击 `setup.bat`（或在 PowerShell 中执行 `.\setup.bat`）
2. 选 `1` 安装环境
3. 选 `L` 登录 Hugging Face，并在 <https://huggingface.co/black-forest-labs/FLUX.2-dev> 点击 Accept
4. 选 `2` 下载权重

## 快速开始（macOS · Apple Silicon）

1. `chmod +x setup.command scripts/get-llama-mac.sh`，之后可在 Finder 里双击
2. 选 `1` 安装环境（venv + llama.cpp Metal 包 + ComfyUI）
3. 选 `L` 登录 Hugging Face，并在 <https://huggingface.co/black-forest-labs/FLUX.2-dev> 点击 Accept
4. 选 `2` 下载权重 —— 与 4090 相反，Mac 上要 bf16 版 unet，MPS 没有 fp8 kernel

与 4090 版的主要差别：显存不再是独立一块，档位 A/B 可以同时开着；
上下文从 16K 提到 32K；小模型独占 `:8001`；档位 C 走 MiniMax-H3 + h3.c
本地出片（菜单 `V` 下权重约 144GB，`C` 起服务），能跑但很慢——一段 10 秒几十分钟，
整章仍建议走 MiniMax 云端 API 或留给 4090。

## 环境要求

- Python 3.11 或 3.12 最稳。3.13 可用，但 `setup.bat` 需要额外绕开几个坑：
  vLLM 在 Windows 上根本没有轮子（已换成 llama.cpp），PyPI 的 `torch` 在 Windows 上是 CPU 版
  （已改为从 PyTorch 的 CUDA 源安装并验证），CosyVoice 的 `requirements.txt` 也装不上。
- NVIDIA GPU，24GB 显存（RTX 4090 基准）

macOS：
- Apple Silicon（按 M3 Max 64GB 统一内存调参），macOS 14+
- Xcode 命令行工具（`xcode-select --install`）、`ffmpeg`
- 终端不要跑在 Rosetta 下，`uname -m` 必须是 `arm64`，否则 torch 装成 CPU 版

## 说明

所有 `.bat` 一律 ASCII + CRLF —— Windows 的 cmd 在 `chcp` 生效前按当前代码页逐字节读取批处理文件，中文注释会让解析器错位。中文说明统一放在 `部署说明.txt`；macOS 的放在 `部署说明-macOS.txt`。
`setup.command` 同样只用 ASCII（LF 换行），理由不是解析器，是保持两边一致好对读。

模型权重、虚拟环境、日志、llama.cpp 二进制、`third_party/` 以及 ComfyUI/Hailuo 运行目录
均由脚本在本地生成，不纳入版本管理（见 `.gitignore`）。
