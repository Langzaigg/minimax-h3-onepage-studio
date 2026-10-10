<img src="static/img/tujang-studio.svg" width="64" height="64" alt="兔酱拍片工作室标志">

# 兔酱拍片工作室

在一个页面里完成参考图、视频生成和后处理。图像与视频由本地 ComfyUI 生成，可选用兼容 API 的多模态 LLM 辅助编写提示词。

[部署说明](DEPLOYMENT.md) · [操作指南](PIPELINE_GUIDE.md) · [配置模板](config.example.yml)

## 创作流程

1. **参考图**：Qwen Image 2.1 文生图及最多四张参考图编辑，结果可继续修改或放入工作区。
2. **视频**：MiniMax H3 支持综合、首尾帧和多模态参考。综合模式可切换 Dasiwa Hybrid V4 Turbo 动画特调与 Eros Max 特调（两者均自带 Turbo，默认 8 步直采），图像、视频步骤均可使用 LLM 辅助提示词。
3. **后处理**：LTX 二采固定按源视频原分辨率做 latent 2×；DLSS5 滤镜处理图片或视频，保留原尺寸与时间轴。两种方式均支持通用图片／视频叠加。

视频生成的“自动二采”默认关闭。开启后，服务器会在 H3 完成时接续 LTX 任务，浏览器关闭后仍可继续；原片和二采成片各自保留，二采失败不会覆盖已完成的原片。

## 分辨率与后处理

| 交付目标 | 自动二采的 H3 原片 | LTX 原尺寸 2× 内容 | 最终交付 |
|---|---|---|---|
| 720p | 736×414 | 1472×828 | 1280×720 |
| 1080p | 960×540 | 1920×1080 | 1920×1080 |
| 2K / 1440p | 1344×756 | 2688×1512 | 2560×1440 |

模型计算时所需补边会在输出前去除，交付尺寸保持精确 16:9。手动生成可选择像素预算和其他比例；独立后处理读取素材的实际尺寸，不强行缩成预设初采尺寸。LTX 默认保留源帧率，可选 2× 插帧，例如 24→48fps。

DLSS5 提供默认、自然、电影三种风格，以及输出混合、本地色调、本地结构、皮肤蒙版四个真实参数。可先预览图片或视频指定时间的画面，并左右比较。它使用社区 Ada NR 桥接，**不代表 NVIDIA 官方对 RTX 40 系的 DLSS 5 支持**；此路线不进行超分或插帧。

## 素材、历史与编辑恢复

- **工作区托盘**：拖入上传素材、历史输入与成品，再拖回对应步骤；支持命名、保存、服务器版本历史、ZIP 导入导出和删除。
- **历史复用**：统一检索图片、视频和后处理任务，查看输入、参数与成果，复用后继续微调。
- **刷新恢复**：同一浏览器、站点地址和登录身份下，恢复表单、步骤、子模式、菜单展开状态、预览位置及已保存的本地素材。刚选择但尚未写入浏览器存储的文件会提示重选，不会替换成旧文件。
- **外观**：浅色、深色与跟随系统；后处理选项横排，自定义参数默认折叠。

浏览器自动恢复用于续接当前编辑；需要跨设备迁移或长期保存时，请保存工作区并导出 ZIP。清理站点数据会清除浏览器本地恢复记录。

## 模型下载

仓库不含任何模型权重，需自行下载后放入 `ComfyUI/models/` 对应子目录。精确文件名、类别与校验值见[部署说明 §4 模型清单](DEPLOYMENT.md#4-模型清单)；`config.yml` 中的文件名必须与实际安装文件完全一致。

| 用途 | 下载来源 |
|---|---|
| Qwen Image 2.1（参考图） | [Comfy-Org/Qwen-Image-2.1](https://huggingface.co/Comfy-Org/Qwen-Image-2.1) |
| MiniMax H3 基模与共用组件（首尾帧 `fl2va`、多模态参考 `ref2va`、文本编码器、视频／音频 VAE） | [Comfy-Org/MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3) |
| H3 动画特调 Dasiwa Hybrid V4 Turbo | [CivitAI 2877206 · V4 Turbo](https://civitai.com/models/2877206?modelVersionId=3392061)（需登录；本机用 int8，作者另提供 int4 与另一 int8 量化） |
| H3 Eros 特调 | [TenStrip/10Eros-Max](https://huggingface.co/TenStrip/10Eros-Max) |
| LTX 2.5 基模与 VAE | [Lightricks/LTX-2.5](https://huggingface.co/Lightricks/LTX-2.5) |
| LTX MSR LoRA | [LiconStudio/LTX-2.5-Multiple-Subject-Reference](https://huggingface.co/LiconStudio/LTX-2.5-Multiple-Subject-Reference) |
| RIFE 插帧组件 | [Comfy-Org/frame_interpolation](https://huggingface.co/Comfy-Org/frame_interpolation) |
| LTX 后处理基模 REDGraft | [CivitAI 1295569 · Fast2K](https://civitai.com/models/1295569?modelVersionId=3250230) |

Hugging Face 资源可用 `huggingface-cli download <repo> <文件名> --local-dir <目标目录>` 拉取；CivitAI 需登录后在页面下载，或带 API Token 调用下载接口。部分仓库需先同意许可、登录或具备相应访问权限。从第三方镜像获取时，务必核对发布者、版本与 SHA256（例如动画特调 V4 Turbo int8 应为 `36471afb817747b711b9abad3d8e6b7c88a7c2b91b4887d040a8dcb7ee82b2d6`），不要仅凭文件名相似就替换。

## 启动

先按 [部署说明](DEPLOYMENT.md) 安装 ComfyUI、模型和工作台适配节点，再配置本地 `config.yml`。在工作台目录运行：

```powershell
.\.venv\Scripts\python.exe -B run.py
```

默认网页为 `http://127.0.0.1:7788/video-generate`，接口文档为 `/docs`，进程健康检查为 `/api/health`。能力检查 `/api/studio/capabilities` 会列出缺失的模型、节点或运行时；健康检查通过不等于模型已经可以推理。

仓库不包含模型权重、私有任务数据、私有配置或 DLSS 二进制运行时。LTX 属于生成式二采，仍可能改变人物细节；长片、快速运动与循环接缝应按成片检查。旧实验工作流保留历史读取，新建后处理仅提供 LTX 与 DLSS5 两条路线。
