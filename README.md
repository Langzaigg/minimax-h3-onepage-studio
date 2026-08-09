<div align="center">

# MiniMax H3 One-Page Studio

### MiniMax H3 一页式创作台

内置 FL2VA 与 Ref2VA 工作流的独立视频生成服务，集成提示词润色、异步任务、实时进度、私有资源代理和 API Key 管理。

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.133%2B-009688?logo=fastapi&logoColor=white)
![ComfyUI](https://img.shields.io/badge/ComfyUI-MiniMax_H3-222222)
![Workflow](https://img.shields.io/badge/Workflow-FL2VA_%2B_Ref2VA-5B5BD6)

[快速开始](#快速开始) · [ComfyUI 部署](#comfyui-部署) · [配置](#配置) · [API](#api-使用) · [真实示例](#真实生成示例)

</div>

<p align="center">
  <img src="examples/reference_to_video_c3749536/preview.gif" width="720" alt="MiniMax H3 参考图生成视频预览">
</p>

<p align="center">
  <sub>真实 Ref2VA 结果的 8 秒压缩预览 · <a href="examples/reference_to_video_c3749536/outputs/AI_Video_reference_to_video_20260805_c3749536.mp4">查看带声音的完整 15 秒 MP4</a></sub>
</p>

## 项目亮点

| 能力 | 说明 |
|---|---|
| 一页式创作 | 文本、首帧、末帧、首尾帧和多模态参考生成集中在同一页面 |
| 内置工作流 | 仓库直接提供 MiniMax H3 FL2VA 与 Ref2VA API 工作流 |
| 动态节点图 | 根据输入自动增删图片、视频和音频读取分支 |
| 多模态参考 | 最多 9 张图片、3 个视频、3 个独立音频，可使用视频自带音轨 |
| AI 提示词润色 | 参考图、视频代表帧和音频频谱均可作为视觉模型输入 |
| 持久化异步任务 | 支持排队、采样进度、重启恢复、历史回填和结果下载 |
| 后端资源代理 | 前端不接触 ComfyUI 地址、prompt ID 或真实输出路径 |
| 本地访问控制 | 管理员密钥、API Key、浏览器会话、Key 撤销和调用统计 |

## 工作方式

```mermaid
flowchart LR
    Client["Playground / API Client"] -->|"API Key / Session"| App["FastAPI Studio"]
    App -->|"Prompt polishing"| LLM["Vision LLM"]
    App -->|"Dynamic workflow"| ComfyUI["Private ComfyUI"]
    ComfyUI -->|"WebSocket progress"| App
    ComfyUI -->|"Generated video"| App
    App --> Store["Local task & access data"]
    App -->|"Protected media proxy"| Client
```

浏览器只保存自己创建的不可预测任务 ID。任务列表接口必须显式提交这些 ID，不会枚举服务端全局历史；唯一例外是管理员（`X-Admin-Key`）可以查看全部任务历史。

访问模式由 `access.mode` 控制：

- `key`（默认）：使用管理员密钥或 API Key 验证。
- `register`：任意用户名即可注册使用（无密码），会话保存在浏览器；输入管理员密钥则自动以管理员身份登录。每个用户/密钥只能看到自己创建的任务历史，管理员仍可查看全部。

## 快速开始

### 环境要求

- Python 3.11+
- 可从 API 服务访问的 ComfyUI 实例
- OpenAI/vLLM 兼容的多模态模型服务，用于提示词润色
- FFmpeg 与 FFprobe，用于分析参考视频和音频

### 安装

```powershell
git clone <repository-url>
cd minimax-h3-onepage-studio

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### 启动

修改 [`config.yml`](config.yml) 后运行：

```powershell
python -B scripts\check_comfyui.py
python -B run.py
```

本机持续运行（自动检测代码/配置更新并重启，不执行任何 git 操作）可直接双击仓库根目录的 [`start_service.bat`](start_service.bat)；关闭该窗口即停止服务。

默认入口：

| 页面 | 地址 |
|---|---|
| 视频创作 | `http://127.0.0.1:7788/video-generate` |
| 管理控制台 | `http://127.0.0.1:7788/admin-console` |
| API 文档 | `http://127.0.0.1:7788/docs` |
| 健康检查 | `http://127.0.0.1:7788/api/health` |

首次进入时使用管理员密钥完成验证，再通过管理控制台创建有名称和有效期的 API Key。

> [!IMPORTANT]
> `config.yml` 中的管理员密钥仅适合内网首次启动。生产部署必须通过 `SERVICE_ADMIN_KEY` 环境变量覆盖，并使用 HTTPS。

## ComfyUI 部署

项目要求包含 MiniMax H3 原生节点的最新稳定版 ComfyUI。参考：[ComfyUI](https://github.com/comfyanonymous/ComfyUI)、[官方 MiniMax H3 工作流模板](https://github.com/Comfy-Org/workflow_templates/tree/main/templates)、[ComfyUI 服务端路由](https://docs.comfy.org/development/comfyui-server/comms_routes)。

### 官方示例模型

默认配置与当前官方 I2V、T2V、R2V 示例模板一致：扩散模型使用剪枝版 INT8 ConvRot，文本编码器使用 INT8 ConvRot 版本（`qwen3vl_32b_minimax_h3_int8_convrot.safetensors`）。

| 配置项 | 官方示例文件名 | ComfyUI 目录 |
|---|---|---|
| `fl2va_diffusion` | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | `models/diffusion_models/` |
| `ref2va_diffusion` | `minimax_h3_ref2va_pruned_int8_convrot.safetensors` | `models/diffusion_models/` |
| `text_encoder` | `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` | `models/text_encoders/` |
| `video_vae` | `minimax_h3_video_vae_fp16.safetensors` | `models/vae/` |
| `audio_vae` | `minimax_h3_audio_vae_fp32.safetensors` | `models/vae/` |

模型可从 [Comfy-Org/MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3) 获取。预期目录：

```text
ComfyUI/models/
├─ diffusion_models/
│  ├─ minimax_h3_fl2va_pruned_int8_convrot.safetensors
│  └─ minimax_h3_ref2va_pruned_int8_convrot.safetensors
├─ text_encoders/
│  └─ qwen3vl_32b_minimax_h3_int8_convrot.safetensors
└─ vae/
   ├─ minimax_h3_video_vae_fp16.safetensors
   └─ minimax_h3_audio_vae_fp32.safetensors
```

### 必要节点

```text
MiniMaxH3ImageToVideo       MiniMaxH3ReferenceToVideo
LoadImage / ImageScale     LoadVideo / GetVideoComponents
LoadAudio                  CreateVideo / SaveVideo
```

> [!NOTE]
> 采样加速节点 **Spectrum Apply MiniMax H3**（`SpectrumApplyMiniMaxH3`，来自 [ComfyUI-Spectrum-MiniMax-H3](https://github.com/xmarre/ComfyUI-Spectrum-MiniMax-H3)）默认**不启用**。仅在 `comfyui.spectrum_apply.enabled: true` 时 FL2VA 与 Ref2VA 两条工作流才会串联该节点，此时 ComfyUI 必须安装上述自定义节点；关闭时工作流会完全移除该节点并直接回连模型加载器，**无需安装该依赖**。详见[采样加速](#采样加速spectrum)。

启动 ComfyUI 后执行检查：

```powershell
python -B scripts\check_comfyui.py
```

脚本会按照 YAML 验证连接、节点、模型文件和队列接口。ComfyUI 应位于受控内网，仅允许本 API 服务访问。

## 配置

所有必要参数集中在 [`config.yml`](config.yml)：

```yaml
server:
  host: 0.0.0.0
  port: 7788
  admin_key: "admin"

access:
  # key = 密钥模式（默认，管理员/API Key）；register = 注册模式（用户名即注册，无密码）
  mode: "key"

comfyui:
  base_url: "http://127.0.0.1:8188"
  timeout_seconds: 60
  models:
    fl2va_diffusion: "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
    ref2va_diffusion: "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
    text_encoder: "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"
    video_vae: "minimax_h3_video_vae_fp16.safetensors"
    audio_vae: "minimax_h3_audio_vae_fp32.safetensors"
  # 采样加速（默认不启用）
  spectrum_apply:
    enabled: false

llm:
  base_url: "http://127.0.0.1:8000/v1"
  api_key: ""
  model: "vision-model"

ffmpeg:
  path: ""  # ffmpeg.exe 绝对路径；留空则前端不提供 GIF 下载

video:
  duration: {default: 5, min: 5, max: 15}
  resolution_megapixels: {default: 0.4, min: 0.2, max: 1.0}
```

相对数据目录以仓库根目录解析。也可以通过 `VIDEO_SERVICE_CONFIG` 指向外部 YAML：

```powershell
$env:VIDEO_SERVICE_CONFIG = 'D:\config\minimax-h3-studio.yml'
```

> [!IMPORTANT]
> **`config.yml` 已被 `.gitignore` 忽略，不会提交到版本库**——所有配置（包括 API Key、管理员密钥、本机 ffmpeg 路径、部署开关等私有项）统一写在 `config.yml`。仓库内仅提供 [`config.example.yml`](config.example.yml) 作为默认值范例：新环境复制它为 `config.yml` 后按需修改即可。提交前请用 `git grep` 检查敏感信息（例如 `git grep -n "sk-\|api_key"`）。

生产环境常用覆盖项：

| 环境变量 | 用途 |
|---|---|
| `SERVICE_ADMIN_KEY` | 管理员密钥 |
| `COMFYUI_BASE_URL` | ComfyUI 地址 |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 润色模型配置 |
| `COMFYUI_FL2VA_MODEL` / `COMFYUI_REF2VA_MODEL` | 两个扩散模型文件名 |
| `COMFYUI_TEXT_ENCODER` | 文本编码器文件名 |
| `COMFYUI_VIDEO_VAE` / `COMFYUI_AUDIO_VAE` | VAE 文件名 |
| `COMFYUI_SPECTRUM_ENABLED` | 采样加速开关（true/false） |
| `FFMPEG_PATH` | ffmpeg 可执行文件路径（GIF 转换） |
| `VIDEO_DATA_FOLDER` / `ACCESS_DATA_FOLDER` | 运行时数据目录 |

`/api/config` 只返回前端需要的视频参数与 `ffmpeg_enabled`，不包含 ComfyUI、LLM 或密钥信息。

## 内置工作流

### FL2VA：文本与参考帧生成

[`video_workflows/video_api.json`](video_workflows/video_api.json) 统一处理四种输入：

- 无图片：文本生成视频
- 仅首帧或仅末帧：保留对应图像分支
- 首帧 + 末帧：同时约束开头与结尾
- 自动比例：首帧 → 末帧 → 16:9

### Ref2VA：多模态参考生成

[`video_workflows/reference_video_api.json`](video_workflows/reference_video_api.json) 根据上传内容动态创建图片、视频与音频读取分支。自动比例优先使用第一张参考图，否则使用 16:9。

`ref_image_size` 支持：

- `match`：匹配生成分辨率，默认且速度更快
- `max`：最高身份保真，参考 token 参与每一步采样，耗时显著增加

## 采样加速（Spectrum）

两条工作流（FL2VA 与 Ref2VA）都内置了可选的采样加速节点 **Spectrum Apply MiniMax H3**（`SpectrumApplyMiniMaxH3`），接线方式为：模型加载器 → Spectrum 节点 → 调度器/引导器。

| 配置 | 行为 |
|---|---|
| `comfyui.spectrum_apply.enabled: false`（默认） | 构建工作流时**移除**该节点，模型直接连回加载器；ComfyUI 无需安装任何额外依赖 |
| `comfyui.spectrum_apply.enabled: true` | 工作流串联该节点；ComfyUI 必须安装 [ComfyUI-Spectrum-MiniMax-H3](https://github.com/xmarre/ComfyUI-Spectrum-MiniMax-H3)（节点位于 `sampling/spectrum`） |

依赖安装（仅启用时需要）：

```powershell
cd ComfyUI/custom_nodes
git clone https://github.com/xmarre/ComfyUI-Spectrum-MiniMax-H3.git
# 重启 ComfyUI
```

Spectrum 通过对隐藏特征做 Chebyshev 岭回归预测来跳过部分 transformer 评估，从而加速采样，属于近似加速而非无损执行；`enabled` 关闭时原生逐帧采样。注意 README 顶部的示例 Gif 与安全建议：加速会改变去噪轨迹，质量敏感场景请同种子对比。

## NSFW 风格 Lora

`comfyui.lora.nsfw_lora_name`（默认 `SexyGod-NaughtyTimes-v2-rank512.safetensors`）指定的 Lora 通过任务参数 `nsfw_strength` 控制强度，仅支持配置的五档（默认 `0 / 0.3 / 0.5 / 0.7 / 1.0`）：

| `nsfw_strength` | 行为 |
|---|---|
| `0`（默认） | 工作流完全不含 Lora 节点，不加载 Lora 文件 |
| `0.3` / `0.5` / `0.7` / `1.0` | fl2v 工作流在 UNETLoader 与 Spectrum/调度器之间插入 `LoraLoader` 节点，model 与 clip 均应用该强度 |

约束：

- **仅应用于 fl2v 工作流**（文本/首帧/尾帧/首尾帧生成）；多模态参考生成接口同样接收该参数并记录到任务元数据，但**不应用于** Ref2VA 工作流（前端选项不禁用，选择后不生效）。
- Lora 文件需位于 ComfyUI `models/loras/` 目录；强度为 `0` 时完全不加载，避免大文件占显存。
- 强度档位可在 `comfyui.lora.nsfw_lora_levels` 调整（逗号分隔，必须包含 `0`）。

## 视频转 GIF 下载

配置 `ffmpeg.path`（ffmpeg.exe 绝对路径，或安装 `pip install imageio-ffmpeg` 后使用其自带静态二进制）后，前端详情页会对已完成视频显示「下载 GIF」按钮，服务端使用 palette 方式将视频转为 ≤480px 的循环 GIF 并缓存：

```http
GET /api/video/tasks/{task_id}/gif
```

`ffmpeg.path` 留空时该接口返回 404，前端也不显示 GIF 按钮。

## API 使用

所有视频接口均需要 API Key、管理员密钥或已验证的浏览器会话：

```http
X-API-Key: dc_xxx
Authorization: Bearer dc_xxx
X-Admin-Key: your-admin-secret
```

### 创建文本/参考帧任务

```bash
curl -X POST http://127.0.0.1:7788/api/video/tasks \
  -H "X-API-Key: dc_xxx" \
  -F "prompt=A cinematic rainy railway platform" \
  -F "duration=5" \
  -F "resolution_megapixels=0.4" \
  -F "aspect_ratio=auto" \
  -F "polish_prompt_first=true" \
  -F "nsfw_strength=0.3" \
  -F "first_frame=@first.png"
```

`nsfw_strength` 可选，仅支持 `0 / 0.3 / 0.5 / 0.7 / 1.0` 五档，默认 `0`；仅在 fl2v 工作流生效（详见上文「NSFW 风格 Lora」）。

### 创建多模态参考任务

```bash
curl -X POST http://127.0.0.1:7788/api/video/reference-tasks \
  -H "X-API-Key: dc_xxx" \
  -F "prompt=Create a coherent cinematic sequence" \
  -F "duration=10" \
  -F "resolution_megapixels=0.4" \
  -F "aspect_ratio=auto" \
  -F "polish_prompt_first=true" \
  -F "ref_image_size=match" \
  -F "reference_images=@character.png" \
  -F "reference_videos=@motion.mp4" \
  -F "reference_audios=@music.wav"
```

注意：参考生成接口同样接受 `nsfw_strength`（仅记录到任务元数据），但 NSFW Lora 不应用于 Ref2VA 工作流。

### 查询进度与结果

```bash
curl -H "X-API-Key: dc_xxx" \
  http://127.0.0.1:7788/api/video/tasks/{task_id}
```

同一个接口返回状态、进度、阶段、排队位置和结果。完成后 `assets.video` 是受保护的本服务代理地址。全部接口和字段约束请查看 `/docs`。

## 真实生成示例

该示例使用四张参考图生成一段 15 秒、1280 × 736、带声音的视频。

<table>
  <tr>
    <td align="center"><img src="examples/reference_to_video_c3749536/references/reference_image_1.png" width="190" alt="Picture 1"><br><code>&lt;Picture 1&gt;</code></td>
    <td align="center"><img src="examples/reference_to_video_c3749536/references/reference_image_2.png" width="190" alt="Picture 2"><br><code>&lt;Picture 2&gt;</code></td>
    <td align="center"><img src="examples/reference_to_video_c3749536/references/reference_image_3.png" width="190" alt="Picture 3"><br><code>&lt;Picture 3&gt;</code></td>
    <td align="center"><img src="examples/reference_to_video_c3749536/references/reference_image_4.png" width="190" alt="Picture 4"><br><code>&lt;Picture 4&gt;</code></td>
  </tr>
</table>

<p align="center">
  <strong>生成结果</strong><br><br>
  <img src="examples/reference_to_video_c3749536/preview.gif" width="720" alt="生成结果 GIF 预览">
</p>

| 参数 | 值 |
|---|---|
| 工作流 | `reference_to_video` |
| 时长 | 15 秒 |
| 输出 | 1280 × 736 / 0.9 MP / 16:9 |
| 参考保真度 | `max` |
| 任务 ID | `c37495363d3b448ea9187e320e3c28b8` |

- [完整 MP4（含声音）](examples/reference_to_video_c3749536/outputs/AI_Video_reference_to_video_20260805_c3749536.mp4)
- [请求参数](examples/reference_to_video_c3749536/request.json)
- [最终提示词](examples/reference_to_video_c3749536/prompt.txt)
- [结果摘要](examples/reference_to_video_c3749536/result.json)

`prompt.txt` 已是润色后的最终提示词，因此示例请求使用 `polish_prompt_first=false`，避免二次润色造成漂移。

## 安全与数据维护

- 输入、输出和下载全部通过已认证的 API 代理。
- 客户端无法获得 ComfyUI 地址、prompt ID 或真实文件路径。
- 历史接口按身份隔离：每个 API Key/注册用户只能看到自己创建的任务，管理员可查看全部。
- API Key、调用统计和任务文件均维护在部署机器本地。
- 默认保留终态任务 90 天，每周日 03:00 清理。
- 每天 00:05 检查磁盘；占用达到 90% 时从最旧终态任务开始清理。
- 润色中、准备中、排队中和运行中的任务不会被自动清理。

## 验证

```powershell
# 自动化测试
python -B -m pytest -q

# 应用导入
python -B -c "import main; print(main.app.title)"

# ComfyUI 节点、模型与队列
python -B scripts\check_comfyui.py
```
