# 书影 Bookreel · 小说转视频 (Novel to Video Converter)

本地化的小说转视频流水线，面向单张 RTX 4090 24GB。详细部署步骤见 [`部署说明.txt`](部署说明.txt)。

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `setup.bat` | 主菜单 |
| `scripts/env.bat` | 公共路径与参数（模型仓库、上下文长度在这里改） |
| `scripts/install.bat` | 建 venv，安装 vLLM / ComfyUI / 音频依赖 |
| `scripts/hf-login.bat` | 登录 Hugging Face（gated 仓库必需） |
| `scripts/download.bat` | 下载权重 |
| `scripts/run-llm.bat` | 档位 A · Qwen3.8-27B AWQ — `:8000` |
| `scripts/run-llm-small.bat` | 档位 A 备选 · Qwen3-8B AWQ — `:8000` |
| `scripts/run-comfy.bat` | 档位 B · ComfyUI Flux.2 — `:7860` |
| `scripts/run-hailuo.bat` | 档位 C · Hailuo 视频 — `:9000` |
| `scripts/run-light.bat` | 常驻 CPU 服务（检索 / 旁白 / 对齐） |
| `scripts/stop.bat` | 释放显存 |
| `scripts/health.bat` | 端口与显存体检 |
| `Pipeline Runner.dc.html` | 网页控制台（直连本地接口） |
| `Novel to Video Studio.dc.html` | 设计稿 / Studio 界面 |
| `comfy-flux2-9x16.json` | Flux.2 工作流模板（节点名需与本机 ComfyUI 一致） |

## 快速开始

1. 双击 `setup.bat`（或在 PowerShell 中执行 `.\setup.bat`）
2. 选 `1` 安装环境
3. 选 `L` 登录 Hugging Face，并在 <https://huggingface.co/black-forest-labs/FLUX.2-dev> 点击 Accept
4. 选 `2` 下载权重

## 环境要求

- Python 3.11 或 3.12（3.13/3.14 下 torch、ComfyUI 的轮子常常还没跟上）
- NVIDIA GPU，24GB 显存（RTX 4090 基准）

## 说明

所有 `.bat` 一律 ASCII + CRLF —— Windows 的 cmd 在 `chcp` 生效前按当前代码页逐字节读取批处理文件，中文注释会让解析器错位。中文说明统一放在 `部署说明.txt`。

模型权重、虚拟环境、日志和 ComfyUI/Hailuo 运行目录均由脚本在本地生成，不纳入版本管理（见 `.gitignore`）。
