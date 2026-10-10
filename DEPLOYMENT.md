# 兔酱拍片工作室 · 部署说明

本文对应当前仓库代码，命令使用 Windows PowerShell。网页操作见 [操作指南](PIPELINE_GUIDE.md)，默认配置见 [config.example.yml](config.example.yml)。服务是否已经启动，应以实际进程、能力接口和推理结果为准。

## 1. 环境与目录

完整流程需要 Windows 64 位、可运行 ComfyUI 的 NVIDIA GPU、充足的内存和 SSD 空间。使用三个相互独立的 Python 环境：

| 环境 | 用途 |
|---|---|
| 工作台虚拟环境 | FastAPI、任务管理、FFmpeg/PyAV、图片处理与 LLM 请求 |
| ComfyUI 环境 | PyTorch/CUDA、Qwen、H3、LTX 和自定义节点 |
| DLSS 独立运行时 | VapourSynth、D3D12 NR 桥接；不要安装进前两个环境 |

本次实测为 Windows 11、Python 3.13、RTX 4080 SUPER（设备实际报告约 **32GB 显存**）、约96GB内存；ComfyUI 环境为 PyTorch `2.14.0+cu130`。这是本机配置记录，不能当作普通 16GB 4080 SUPER 的性能或容量保证。高分辨率源视频会产生更大的 latent 2× 画布，显存、内存与临时磁盘消耗均会增加。

当前后处理代码通过工作台父目录定位 `ComfyUI/models` 与 DLSS runtime，因此请保留以下布局。仅修改 `comfyui.base_url` 不会改变这些本地检查路径，也不构成完整的远端 ComfyUI 部署。

```text
任意工作目录/
├─ ComfyUI/
│  ├─ main.py
│  ├─ models/
│  └─ custom_nodes/
├─ python_embeded/                    # ComfyUI portable 自带；普通安装可使用自己的环境
└─ minimax-h3-onepage-studio/          # 本仓库，名称可改，仍需与 ComfyUI 同级
   ├─ .venv/
   ├─ config.yml                     # 私有配置
   ├─ video_data/                    # 生成任务、素材和工作区
   └─ access_data/                   # 账户、API Key、用量记录
```

## 2. 工作台环境与私有配置

在仓库目录创建独立环境。以下使用本机已验证的 Python 3.13；ComfyUI 的 Python 和 CUDA 不由这些命令安装或更新。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path -LiteralPath config.yml)) {
    Copy-Item -LiteralPath config.example.yml -Destination config.yml
}
notepad config.yml
```

先设置以下项目：

| 配置 | 建议与实际行为 |
|---|---|
| `server.host` / `server.port` | 本机使用 `127.0.0.1` / `7788`。模板的 `0.0.0.0` 会监听全部网卡。 |
| `access.mode` | `admin` 为免登录管理员模式，仅适合可信本机；`key` 为管理员／API Key 验证；`register` 为无密码用户名注册，不是密码账户系统。 |
| `server.admin_key` | 替换模板值。分享给其他用户时使用 `key` 模式，并为不同用户创建独立 API Key。 |
| `comfyui.base_url` | 默认 `http://127.0.0.1:8188`。 |
| `comfyui.input_dir` | 可选，填写实际 ComfyUI `input` 目录，用于清理 H3 上传的临时副本。 |
| `comfyui.models.*` | H3 模型文件名需与安装文件完全一致；不填写完整磁盘路径。 |
| `llm.base_url` / `llm.model` | 模板为 `https://api.commandcode.ai/provider/v1` / `moonshotai/Kimi-K2.7-Code`。 |
| `llm.api_keys` | 在未提交的 `config.yml` 填写自己的密钥；程序使用首个非空项。不使用 LLM 时可留空。 |
| `ffmpeg.path` | H3 参考视频预处理和 GIF 下载使用的 `ffmpeg.exe` 路径；后处理另外支持 PATH 中的 FFmpeg 或 `imageio-ffmpeg` 自带程序。 |

启用 LLM 辅助时，会向所配置供应商发送提示词和相关参考素材；Qwen、H3、LTX 的生成计算仍在 ComfyUI 中运行。请勿把供应商密钥填入提示词、工作区或示例文件。

可用环境变量覆盖配置，例如 `VIDEO_SERVICE_CONFIG`、`COMFYUI_BASE_URL`、`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`、`SERVICE_ADMIN_KEY`、`SERVICE_RESTART_HOST`、`SERVICE_RESTART_PORT`。`VIDEO_SERVICE_CONFIG` 指定整个配置文件；环境变量优先于文件。

数据目录可在配置中单独设置，相对路径以仓库目录为基准：

```yaml
storage:
  task_data_dir: "video_data"
  access_data_dir: "access_data"
  workflow_dir: "video_workflows"
```

对应覆盖变量为 `VIDEO_DATA_FOLDER`、`ACCESS_DATA_FOLDER`、`VIDEO_WORKFLOW_FOLDER`。保留仓库提供的 `video_workflows` 模板。`config.yml`、数据、环境与运行时均不应提交 Git；不要将整个私有配置复制到公开排错信息。

## 3. ComfyUI 与节点

安装支持 Qwen Image 2.1、MiniMax H3、LTX 2.5 与 INT8 ConvRot 的 ComfyUI 版本。本机代码基线为 `79be670e2d9be63e238785af307369d2b9039ed1`，这是复查版本，不要求所有新安装盲目切换到同一提交。更新已有 ComfyUI 前先保存依赖版本及自定义节点，使用该环境自身的依赖安装方式。

当前流程需要以下组件：

| 组件 | 作用与来源 |
|---|---|
| ComfyUI 原生节点 | Qwen、H3、LTX 编码／采样／视频读写及 RIFE；模型已下载不代表节点版本兼容。 |
| ComfyUI-LTX2.5-MSR | [作者仓库](https://github.com/liconstudio/ComfyUI-LTX2.5-MSR)，本机固定 `03061c18f8a79d26f3c63931dd8e28f5e5d872a6`；提供 MSR LoRA 加载和人物参考引导。 |
| comfyui-kjnodes | [作者仓库](https://github.com/kijai/ComfyUI-KJNodes)，提供 `ImagePadKJ` 的边缘复制补边。 |
| ComfyUI-StudioPost | 本仓库适配源码，提供 `StudioFrameInterpolateSampled`，保留所需 RIFE 帧并减少中间张量占用。 |
| ComfyUI-DLSS5 | [上游仓库](https://github.com/HECer/ComfyUI-DLSS5)，Windows NR 桥接与深度／运动引导；另需本仓库的 `StudioDLSS5Filter` 适配。 |
| ComfyUI-Spectrum-MiniMax-H3 | [上游仓库](https://github.com/xmarre/ComfyUI-Spectrum-MiniMax-H3)，提供 `SpectrumApplyMiniMaxH3` 跳步加速；本机基线 `806fe49`（v0.2.29）。仅在 H3 普通基模启用加速时需要。 |

H3 综合工作流使用原生节点，不要求额外安装 Dasiwa 的界面节点包。**首尾帧与多模态参考的普通基模默认挂载 Spectrum 跳步加速**（需安装上表插件；关闭 `comfyui.spectrum_apply.enabled` 时工作流会移除该节点并直接回连模型加载器，此时无需安装）；综合模式的两个基模自带 Turbo，不使用该节点。不要为了归档的后处理预设下载全部旧模型或安装全部实验节点。

在 ComfyUI 的 `custom_nodes` 目录安装上游包，再用 **ComfyUI 的 Python** 安装这些包各自的依赖。DLSS 上游手动克隆还需要 Git LFS；按上游说明取得实际二进制，不能把 LFS 指针文件当 DLL。DLSS 本机上游基线为 `f4f59cd1fe39e785a3b600ca20b5e09194a9e38d`。

工作台适配源与安装脚本位于 [integrations/comfyui](integrations/comfyui) 和 [scripts/install_comfyui_adapters.py](scripts/install_comfyui_adapters.py)。在工作台目录运行：

```powershell
.\.venv\Scripts\python.exe scripts/install_comfyui_adapters.py --comfy-root ..\ComfyUI --check
.\.venv\Scripts\python.exe scripts/install_comfyui_adapters.py --comfy-root ..\ComfyUI
.\.venv\Scripts\python.exe scripts/install_comfyui_adapters.py --comfy-root ..\ComfyUI --check
```

`--check` 只读，退出码0表示兼容；仍需看 `up_to_date` 和 `changes_required` 判断是否已经安装，退出码2表示不兼容或读取失败。安装会在 ComfyUI 的 `.studio-adapter-backups` 中保留备份，可重复执行，向已有 DLSS 包注册适配，而不是覆盖整份上游实现；不下载模型、DLL 或私有 runtime。安装后重启 ComfyUI，网页刷新无法加载 Python 节点。

## 4. 模型清单

下载至 `ComfyUI/models/<类别>/`。H3 文件名可通过配置修改；Qwen 和当前后处理共享组件使用下表名称，由工作流构造器读取。仓库不打包权重，也不自动取得受限模型访问权限。

| 用途 | 类别 | 文件名 |
|---|---|---|
| Qwen | `diffusion_models` | `qwen_image_2.1_bf16.safetensors` |
| Qwen | `text_encoders` | `qwen3vl_8b_int8_convrot.safetensors` |
| Qwen | `vae` | `qwen_image_2.1_vae_bf16.safetensors` |
| H3 动画特调 | `diffusion_models` | `DasiwaMinimaxH3_dasiwaHybridV4Turbo_int8.safetensors` |
| H3 Eros 特调 | `diffusion_models` | `ErosMax_H3_beta5_Turbo_INT8.safetensors` |
| H3 首尾帧 | `diffusion_models` | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` |
| H3 多模态参考 | `diffusion_models` | `minimax_h3_ref2va_pruned_int8_convrot.safetensors` |
| H3 共用 | `text_encoders` | `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` |
| H3 共用 | `vae` | `minimax_h3_video_vae_fp16.safetensors`、`minimax_h3_audio_vae_fp32.safetensors` |
| LTX 当前基模 | `diffusion_models` | `REDGraft_LTX25_Fast2K_INT8.safetensors` |
| LTX 编码器 | `text_encoders` | `gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors` |
| LTX VAE | `vae` | `ltx-2.5-video-vae-conv-bf16.safetensors`、`ltx-2.5-audio-vae-bf16.safetensors` |
| MSR V1 | `loras` | `LTX-2.5-Licon-MSR-V1.safetensors` |
| LTX latent 2× | `latent_upscale_models` | `ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors` |
| 可选 2× 插帧所用组件 | `frame_interpolation` | `rife_v4.26.safetensors` |

当前 LTX 能力检查也检查 RIFE 文件，请一并安装，即使日常使用“不插帧”。普通 FL2V／Ref2V 的 Turbo LoRA 是独立可选项，文件名留空则不启用；Dasiwa V4 Turbo 与 Eros 均自带 Turbo，默认 8 步（V4 Turbo 也可改用 4 步），不要再叠加一份 Turbo LoRA。

来源：[Qwen Image 2.1](https://huggingface.co/Comfy-Org/Qwen-Image-2.1)、[MiniMax H3](https://huggingface.co/Comfy-Org/MiniMax-H3)、[Dasiwa](https://civitai.com/models/2877206?modelVersionId=3392061)、[Eros](https://huggingface.co/TenStrip/10Eros-Max)、[REDGraft](https://civitai.com/models/1295569?modelVersionId=3250230)、[LTX 2.5](https://huggingface.co/Lightricks/LTX-2.5)、[MSR](https://huggingface.co/LiconStudio/LTX-2.5-Multiple-Subject-Reference)、[RIFE](https://huggingface.co/Comfy-Org/frame_interpolation)。下载镜像应核对发布者、版本及 SHA256，不能仅依据文件名相似替换。

当前动画特调为 Dasiwa Hybrid **V4 Turbo** int8（19.53 GB），官方校验值 SHA256 `36471afb817747b711b9abad3d8e6b7c88a7c2b91b4887d040a8dcb7ee82b2d6`；作者同时提供 int4（11.68 GB）与另一 int8 量化（15.60 GB），换用其它量化时请同步更新 `hybrid_diffusion` 文件名。

当前 MSR 使用 **V1 LoRA、conv VAE 和本机低噪三步配置**。不要换成 V2／AVref、非 conv VAE 或旧 19B LoRA 后仍声称使用同一流程。采样按实际源尺寸补齐 32 网格，再做 latent 2×，不会先缩至交付目标的一半；目标 720p／1080p／1440p 在去补边后处理。

## 5. DLSS5 社区 Ada NR

本项为原尺寸滤镜，不安装或启用工作台的 DLSS 超分／DLSS 插帧。名称沿用社区项目，不代表 NVIDIA 官方 RTX 40 系支持。

除了 Python 节点，还需要以下本机文件：

```text
ComfyUI/custom_nodes/ComfyUI-DLSS5/
├─ studio_filter.py                  # 工作台四参数适配
├─ studio_integration.py             # 原引导网络的显存清理适配
├─ studio_register.py                # 工作台节点注册适配
├─ bridge_runner.py                  # 上游 NR 调用器
└─ runtime/
   ├─ config.json                    # 本机绝对路径，私有、不入库
   ├─ vsdlssnr.dll                   # VapourSynth NR 插件
   ├─ nvngx_dlssnr.dll               # 与 GPU 匹配的 NR runtime
   └─ ...                           # 独立 Python / VapourSynth / NumPy
```

先按 [上游运行时说明](https://github.com/HECer/ComfyUI-DLSS5/blob/main/runtime/README.md)配置和检查。上游“文件存在／插件加载成功”与“本机实际推理成功”是不同检查。4080S 使用本机单独构建的 [Ada 补丁](https://github.com/dev-camo/dlssnr-patcher/tree/98dac112990e5cea30ab03d74625fc848bce5c92)，不能直接假设上游默认 NR DLL 支持该设备；补丁和 NVIDIA DLL 的来源、授权与哈希需要另外保存。

本机已测 NR 为 `310.8.0` 的社区 `sm_89` 构建，SHA256 为 `cb1a1a6e47e38b9229db4f22081c0f9b1271783599675e0ab13d41d4d039755a`；独立运行时为 VapourSynth R80 / Python 3.13.7。迁移时重建或按授权复制运行时，并改写 `runtime/config.json` 的 `python`、`nr_plugin`、`nr_runtime`、`temp_dir` 路径。不要覆盖原始 NVIDIA DLL，也不要把本机 `verification.json` 当作新机器已经完成推理的证明。

深度引导使用 Depth Anything V2 Small；视频运动引导使用 RAFT Large，首次调用可能下载缓存权重。离线机器应提前准备这些缓存，并确认 ComfyUI 账户可读。工作台新节点会提交四个真实参数并读取原生回执；`controls_contract_version=2` 才表示新节点已加载。实验范围 0–5 只是允许扩大传参数值，不表示效果准确增强五倍。

## 6. 启动与验收

先启动 ComfyUI。本机 portable 布局的基础命令如下；普通安装用其自己的 Python 执行同一个 `main.py`：

```powershell
# 在 ComfyUI portable 根目录
.\python_embeded\python.exe -s .\ComfyUI\main.py --windows-standalone-build --listen 127.0.0.1 --port 8188
```

`--use-sage-attention` 等优化参数仅在对应组件已安装并验证后添加。本机曾使用 `--reserve-vram 2 --vram-headroom 1`；不要为了照搬启动参数安装另一套 PyTorch，或在不支持该选项的 ComfyUI 版本上强行使用。

再在工作台目录启动单个服务进程：

```powershell
.\.venv\Scripts\python.exe -B run.py
```

不要使用多 worker 共同读写同一任务目录。任务锁和后台调度位于进程内，多实例共用数据可能重复恢复任务。本机 `start_service.bat` 是未入库的便捷脚本，含特定 Python 路径且会结束端口占用进程；新部署应使用上述命令或自行配置的服务管理器。

确认网页 `http://127.0.0.1:7788/video-generate`，再做只读检查：

```powershell
Invoke-RestMethod http://127.0.0.1:7788/api/health
Invoke-RestMethod http://127.0.0.1:8188/object_info/StudioDLSS5Filter
.\.venv\Scripts\python.exe scripts/check_comfyui.py
```

`check_comfyui.py` 检查 H3（含 Eros）、Qwen、两项活动后处理、本地 runtime 与队列，不执行推理。登录工作台后查看 `/api/studio/capabilities`：Qwen `image.enabled`、当前两项后处理的 `available` 应为真；DLSS 应有 `controls_contract_version: 2`。若采用密钥访问，该接口需要已验证 Cookie 或 API Key。缺失项按返回的节点／模型／runtime 清单处理，健康接口返回 `ok` 本身不验证 GPU。

完成真实短样验收：

1. Qwen 生成一张小档参考图，放入工作区，再拖入视频输入。
2. H3 生成短视频，检查模式、原片比例与帧率；开启自动二采后确认只创建一个子任务。
3. LTX 输出检查目标尺寸、帧数、音轨和连续运动；不插帧保持源帧率，2× 插帧为源帧率两倍。
4. DLSS 处理一张图片、一个视频指定时间预览和一段短视频；检查结果保持原尺寸、帧数和时间轴，新参数存在实际回执。静态 alpha 保留；MP4/H.264 不保留视频 alpha，仅保留首条原音轨。
5. 输入小数、长种子和上传素材后刷新，确认恢复当前步骤／参数；保存工作区、导出 ZIP，并测试导入恢复。

界面可用、依赖齐全与画面质量是三个不同验收项。LTX 二采会改变细节，快速动作与循环接缝仍需查看成片。

开发验证不调用真实 GPU，使用独立临时数据：

```powershell
.\.venv\Scripts\python.exe -m pip install pytest httpx
.\.venv\Scripts\python.exe -m pytest -q
node --test tests/frontend_postprocess.test.cjs
```

Node 仅用于前端测试，页面运行不需要 Node 服务。Python 测试的 `conftest.py` 隔离数据、网络和重启边界；测试通过不能替代上述真实推理。

## 7. 备份、升级和数据保留

升级前先等待 GPU 队列结束，停止工作台写入，然后备份：

- `config.yml`、`access_data/`：访问身份、密钥与配置，作为私有备份保存。
- `video_data/`：H3、图片、后处理任务元数据、原素材和成片，以及 `studio/`、`workspaces/` 下的完整数据。
- 当前 ComfyUI 提交、依赖版本、模型清单与哈希、自定义节点及本机 DLSS runtime 配置。

服务器工作区版本不是独立磁盘备份；删除整个工作区会一并删除该工作区的历史版本。浏览器刷新恢复存在该站点的 IndexedDB/localStorage，切换端口、域名、浏览器或登录身份不会自动迁移；跨设备请使用工作区 ZIP。

配置默认每周清理超过90天的 H3 历史，并在磁盘使用率达到90%时删除最旧的已完成／失败 H3 任务。当前这项清理并不等同于回收所有 Studio 和工作区文件。重要素材应保存到工作区并另行备份，模型删除需核对当前所有工作流引用，不依赖文件名猜测。

更新代码后重新安装工作台依赖，检查并更新必要的适配节点，再依次启动 ComfyUI 与工作台，重复能力检查和短样。保留原 `config.yml`，按模板差异补充配置；不要直接用模板覆盖私有配置。任务恢复依赖元数据及 ComfyUI 历史，服务重启并不能承诺继续已经丢失的 GPU 执行状态。

本机项目路径为 `D:\ComfyUI_windows_portable\minimax-h3-onepage-studio`，正式端口约定为 ComfyUI `8188`、工作台 `7788`。这些是现有机器的位置与端口，不是其他机器的安装要求；历史研究使用的 `7789`／`8189` 不应作为生产入口。
