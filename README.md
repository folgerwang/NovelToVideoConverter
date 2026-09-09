# 书影 Bookreel · 小说转视频 (Novel to Video Converter)

本地化的小说转视频流水线：一句设定 → 制作圣经 → 逐章正文 → 分镜 → 参考图 → 成片，全程跑在自己的机器上。

三套部署，同一份流水线：

| 平台 | 入口 | 硬件基准 |
| --- | --- | --- |
| Windows | [`setup.bat`](setup.bat) | 单张 RTX 4090 24GB 独立显存 |
| macOS | [`setup.command`](setup.command) | Apple Silicon 统一内存（按 M3 Max 64GB 调参） |
| Linux | [`launch.sh`](launch.sh) | Strix Halo 128GB（BIOS 划 96GiB 显存 / 31GiB 主存） |

详细部署步骤见 [`部署说明.txt`](部署说明.txt) 与 [`部署说明-macOS.txt`](部署说明-macOS.txt)；
Linux 这台机器上踩过的坑与不能回退的规则写在 [`CLAUDE.md`](CLAUDE.md)。

## 三步流水线

三步各自只用一个档位，**同一时间只有一个档位占着 GPU** —— 统一内存的机器上这是关键，
显存要靠停掉上一个档位的进程才还得回来（ComfyUI 的 `/free` 并不还）。

| 步 | 页面 | 做什么 | 用什么 |
| --- | --- | --- | --- |
| 1 · 写小说 | `Story Studio.dc.html` | 故事基础与结构 → 制作圣经 → 逐章正文 → 导演分镜（机位/光比/轴线/台词念法） | 档位 A · 大模型 `:8010` |
| 2 · 出参考图 | `Reference Images.dc.html` | 按第一步的提示词出人物白板图、场景空景、道具图，入库并打标签 | 档位 B · ComfyUI Flux.2 `:7860` |
| 3 · 出片 | `Pipeline Runner.dc.html` | 按分镜逐镜出 10 秒片段，按章拼成成片 | 档位 C · MiniMax-H3 `:9000` |

`Library.dc.html` 是横向的资产库视图（`/Library.dc.html?slug=<项目>`），看一个项目已经攒下的
圣经、章节、分镜与参考图。

产物全部以纯 JSON 落在 `projects/<slug>/` 下，所以第 2、3 步是读文件，不需要第 1 步的服务还开着。
`projects/` 不纳入版本管理。

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `setup.bat` | Windows：全部逻辑都在这一个文件里：菜单、安装、下载、各档位、体检、释放显存 |
| `setup.command` | macOS 同上一份，按统一内存重调（档位可并存、Metal、日志后台） |
| `launch.sh` | Linux：一条命令把整套栈拉起来（`start` / `stop` / `restart` / `status` / `logs <名字>`），不下载任何权重 |
| `CLAUDE.md` | 提示词与渲染这两块「不能回退」的规则，每一条都是被一张错图换来的 |
| `scripts/get-llama.ps1` | 拉取 llama.cpp 的 Windows CUDA 预编译包 |
| `scripts/get-llama-mac.sh` | 拉取 llama.cpp 的 macos-arm64 Metal 包，失败退到 brew / 源码编译 |
| `scripts/get-torch.ps1` | 装 CUDA 版 torch，并验证 `torch.cuda.is_available()` |
| `scripts/cosyvoice-req.txt` | CosyVoice2 推理依赖（不用上游 requirements.txt，见文件内说明） |
| `scripts/pip-constraints.txt` | `setuptools<81`，供 sdist 构建时找得到 pkg_resources |
| `services/story_server.py` | 第 1 步 · 圣经/正文/分镜/资产提示词 — `:8010`（只用档位 A，不碰 ComfyUI） |
| `services/web_server.py` | 网页控制台的静态服务与渲染队列 — `:8080`（只用标准库，不需要 venv） |
| `services/embed_server.py` | 检索向量 · OpenAI 兼容 `/v1/embeddings` — `:8002`（控制台只体检，不调用；留给外部检索用） |
| `services/tts_server.py` | 旁白 · CosyVoice2 零样本 `/tts` — `:9100` |
| `services/asr_server.py` | 对齐 · FunASR `/align`，返回字幕时间轴与 SRT — `:9101`（第 3 步拼整章字幕用） |
| `services/h3_server.py` | 出片 · MiniMax-H3 经 h3.c 本地渲染，接口同 MiniMax 云端 — `:9000`（macOS / Linux） |
| `voices/` | 旁白参考音（`storyteller.wav` + 同名 `.txt`）。自带的那对是 CosyVoice 的示例音，占位用，换成自己的 |
| `Story Studio.dc.html` `Reference Images.dc.html` `Pipeline Runner.dc.html` `Library.dc.html` | 四个页面，见上面的三步流水线 |
| `comfy-flux2-9x16.json` | Flux.2 工作流模板（节点名需与本机 ComfyUI 一致） |
| `support.js` | 四个 `.dc.html` 的运行时，由设计工具生成 —— 别手改 |
| `_ds/<uuid>/` | 设计系统的样式与 token 包，四个 `.dc.html` 都 `<link>` 它 |

`support.js` 与 `_ds/` 是设计工具的产物，不是手写代码，但所有 `.dc.html` 都直接引用，删了页面就打不开。
`_ds/` 的 uuid 目录名由工具决定，重新生成设计系统时会换名字，届时四个 HTML 的引用要一起改。

## 档位

| 档位 | 内容 | 端口 |
| --- | --- | --- |
| A | Qwen3.8-27B Q4_K_M —— Windows/macOS 走 llama.cpp，Linux 走 ollama | `:8000`（Linux 上是 `:11434`） |
| S | Qwen3-8B（更快的备选） | `:8000`（macOS 上是 `:8001`，可与档位 A 并存） |
| B | Flux.2（ComfyUI） | `:7860` |
| C | Hailuo 视频（需自备 `hailuo/server.py`）；macOS / Linux 上默认走 MiniMax-H3 + h3.c | `:9000` |
| 故事 | 第 1 步的服务（转调档位 A） | `:8010` |
| 常驻 | 检索 / 旁白 / 对齐（纯 CPU） | `:8002` `:9100` `:9101` |
| 控制台 | 网页界面（`setup` 菜单 `W`；Linux 上由 `launch.sh` 起） | `:8080` |

`story_server` 说的是 OpenAI 协议，所以底下是 llama.cpp 还是 ollama 它不关心，
`--llm` 指到哪个就用哪个。

## 打开控制台 / 远程访问

Windows / macOS 选菜单 `W`；Linux 上 `./launch.sh` 起完就已经在跑了。
打开 <http://127.0.0.1:8080/> —— 首页列出三步、各步的服务是否在听，以及已有的项目。

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

## 快速开始（Linux）

`launch.sh` 只负责起服务，不装环境也不下权重 —— venv、ComfyUI、ollama 与各档位的权重
要先按 `部署说明.txt` 备好。

```sh
./launch.sh            # 起所有还没在跑的；已经在听的端口原样放着，重复跑无害
./launch.sh status     # 谁在听，谁没起来
./launch.sh logs web   # 跟一个日志（web | story | comfy | ollama | embed | tts | asr | video）
./launch.sh stop       # 停掉这个脚本起的那些
./launch.sh restart    # 等于 stop 再 start
```

缺权重的档位会被跳过而不是报错，所以装了多少就能跑多少。
默认大模型是 `qwen3.8-27b`（`LLM_MODEL` 可改）。

这台机器上的两件事写在 `CLAUDE.md` 里，改之前先读：主存（31GiB）才是瓶颈而不是显存，
所以 ComfyUI 用 `--cache-none` 反而最快；Flux.2 因此走 GGUF（Q4_K_S 18GB，
fp8mixed 的 33.8GB 装不下），需要 ComfyUI-GGUF 节点。

## 环境要求

- Python 3.11 或 3.12 最稳。3.13 可用，但 `setup.bat` 需要额外绕开几个坑：
  vLLM 在 Windows 上根本没有轮子（已换成 llama.cpp），PyPI 的 `torch` 在 Windows 上是 CPU 版
  （已改为从 PyTorch 的 CUDA 源安装并验证），CosyVoice 的 `requirements.txt` 也装不上。
- NVIDIA GPU，24GB 显存（RTX 4090 基准）

macOS：
- Apple Silicon（按 M3 Max 64GB 统一内存调参），macOS 14+
- Xcode 命令行工具（`xcode-select --install`）、`ffmpeg`
- 终端不要跑在 Rosetta 下，`uname -m` 必须是 `arm64`，否则 torch 装成 CPU 版

Linux：
- ROCm 与 ollama 各自装好，`launch.sh` 只负责起它们
- 没有系统 `ffmpeg` 也行：`venvs/tools` 里装 `imageio-ffmpeg`，再把 `bin/ffmpeg`
  软链过去，`launch.sh` 会把 `bin/` 加进 `PATH`（旁白混流和拼片都要它）

## 说明

所有 `.bat` 一律 ASCII + CRLF —— Windows 的 cmd 在 `chcp` 生效前按当前代码页逐字节读取批处理文件，中文注释会让解析器错位。中文说明统一放在 `部署说明.txt`；macOS 的放在 `部署说明-macOS.txt`。
`setup.command` 同样只用 ASCII（LF 换行），理由不是解析器，是保持两边一致好对读。

模型权重、虚拟环境、日志、llama.cpp 二进制、`third_party/`、ComfyUI/Hailuo 运行目录、
`bin/` 下的本地软链，以及第 1-3 步的产物 `projects/`，均由脚本在本地生成，
不纳入版本管理（见 `.gitignore`）。
