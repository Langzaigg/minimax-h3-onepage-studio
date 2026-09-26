# 参考图 → 视频 → 1080p 成片

适用于这台本地工作台，更新于 2026-09-26。新版页面按三个步骤组织；旧版 `README.md` 中关于混合模型 V1、Spectrum 与模型供应商的说明以本文及 `config.example.yml` 为准。

**当前画质验收未通过。** 用户进一步指出8秒片头和循环的超分仍有跳动、细节放大效果不足。下面保留的真实运行成功记录不等于最终画质合格；RIFE/RTX候选也未作为可靠最终版接受。正在按现成工作流重新核查VAE时域分块、原片分辨率、帧率转换和实际超分质量，旧文件保留作对照。

已完成[公开作者工作流与 VAE 核实报告](../maintenance/studio-20260926/workflow-research-2/WORKFLOW_AND_VAE_FINDINGS.md)，其中按真正相连的节点记录 PeterDuncan、REDGraft、mdkberry、官方 IC-LoRA 与 Sulphur2 的参数。下文旧的“推荐默认”属于当时短片测试结论，已被用户完整视频反馈否决；新候选在本机完整片段验证前不作为画质验收通过的方案。

## 使用顺序

1. **参考图生成 / 修改**：输入画面要求，可上传最多 4 张参考图；需要时点击 LLM 辅助提示词。Qwen Image 2.1 完成后，可送入视频步骤作为首帧或参考图。
2. **视频生成**：综合模式选择“动画特调”或“NSFW 特调”；也可独立使用 FL2V 首尾帧模式或 Ref2V 多模态参考模式。先用约 0.2–0.4 MP 确认构图与动作，再制作高清视频。LLM 辅助保留输入的角色、文字与动作约束。
3. **视频后处理**：选择原视频，默认使用AnimeVideo-v3动漫超分，选择720p、1080p或2K交付。需要改变帧率时用真实RIFE；二拍动画修复是显式高级选项。扩散二采保留为实验重绘，可能改脸、手和衣装，不作为本次默认。

完成的视频可在**当前结果栏、视频详情或历史记录操作区**点击“一键二采”，立即创建后处理任务。它优先使用本机可用的 `redgraft_msr`，其次 `ltx25_msr`；继承该视频的交付目标、实际生成提示词和 `seed_text` 精确种子，旧任务缺少交付目标时使用当前页面目标。默认输出 **30 fps、`cadence_stride=1`、`loop=false`**，保留原始时长；不会根据首尾帧模式猜测循环意图。需要循环时先点“调整二采设置”，启用循环并设置时长后提交。所有入口在提交期间禁用重复操作；当前页面已知的未完成 MSR 任务会显示“查看二采任务”，错误在按钮旁和提示条显示。“一键”描述的是提交方式，不代表画质已通过验收。

“狼仔鸽鸽”直播素材应保持完整角色和准确标题，并将全部标题笔画与装饰放入 16:9 画面四周至少 10% 的内边距内。待机动作宜轻微、周期稳定；首尾姿态与机位保持接近。循环封装可以柔化接缝，不能自动修复明显的姿势跳变或角色漂移。

## 主题与历史复用

页面右上角可选择 **浅色、深色、跟随系统**，默认跟随系统。选择会保存在当前浏览器；只有选择跟随系统时，才会随操作系统外观实时切换。

历史面板将参考图、生成视频和后处理任务放在同一个列表，支持类型筛选、提示词 / 模型 / 任务 ID 搜索与分页。选择记录后可查看结果、参数并继续操作。普通账户只显示自己的记录，管理员可查看全部。

- **参考图复用参数**：恢复原始提示词、画幅、完整种子、LLM 选项和原输入图；继续修改则把生成结果作为新参考图。结果可送入视频步骤作为首帧、首尾帧或参考图。
- **视频复用参数**：恢复生成模式、模型、提示词、时长、画幅、种子及输入素材，再微调生成；也可直接送入后处理。
- **后处理复用参数**：沿用原视频、人物参考图、提示词、高清方案、帧率、循环时长、接缝设置、标题及淡入设置；也可选已有成片作为新的处理素材。历史视频在服务器内复制，无需浏览器重复下载上传整个视频。

“复用参数”和“调整二采设置”只回填表单，点击生成才创建新任务；“一键二采”会直接提交。新任务保留原任务。完整种子以 `seed_text` 字符串传递，避免浏览器对大整数取整导致重跑条件变化。历史原素材缺失时需要重新补齐，不能把缺图当作原样复用。

## 两个 H3 综合模型

两者都具有 FL2VA 与 Ref2VA 混合能力，因此共用综合标签页。旧 FL2V／Ref2V 的 NSFW LoRA 档位和三件专用 LoRA 已按用户要求清理；新的 NSFW 特调使用 Eros Hybrid。普通 FL2V／Ref2V 与其 Turbo 档位继续保留。`hybrid_preset` 选择模型，历史兼容字段 `nsfw` 不会自动替换模型。

| 档位 | 本机文件 | 默认采样 |
|---|---|---|
| `anime` 动画特调 | `DasiwaMinimaxH3_dasiwaHybridV2_int8.safetensors` | 非蒸馏，25 步，`res_multistep / simple`，video/audio shift `12 / 3` |
| `nsfw` NSFW 特调 | `ErosMax_H3_beta5_Turbo_INT8.safetensors` | 内置 Turbo，8 步，`res_multistep / simple`，video/audio shift 起点 `12 / 3` |

Dasiwa 作者推荐非蒸馏版本 20–25 步，video shift 10–12、audio shift 3–5。Eros 作者给出的 beta5 运动质量方案是 `res_multistep / simple` 6–9 步；音频优先可试 `LCM / simple` 6–8 步。Eros 的 `12 / 3` 是工作台沿用的 H3 起点，**不是作者声明的 beta5 最优 shift**。两档均使用 `BasicGuider`（单路条件，等效 CFG 1），关闭 Spectrum 与额外采样缓存；Eros 不再叠加 Turbo LoRA。可选旧版 Dasiwa Turbo 仅对 `anime` 生效，不能用它覆盖 Eros。

本地文件与原作者 Civitai 下载条目 SHA-256 相符：

- Dasiwa V2：`4cb8e1eaa9c3e5c664822760890bcbe6078455401cfa43205baf322e856d25f8`。
- Eros beta5：`4dd965496e5b1b83cd13c65cbe7a535b8a4d94ae768a7646b4e336d52c4781cf`。原下载名带 `%20`，部署名已规范化为上表名称。

依据：[Dasiwa 作者模型与设置](https://civitai.com/models/2877206?modelVersionId=3314675)、[Dasiwa 作者工作流](https://github.com/darksidewalker/dasiwa-comfyui-workflows/tree/main/C-MMH3)、[Eros 作者模型页](https://civitai.com/models/2851079?modelVersionId=3294059)、[Eros 作者采样说明](https://huggingface.co/TenStrip/10Eros-Max?not-for-all-audiences=true)。

## 模型清理记录（2026-09-26）

端到端验收后已删除 10 个无当前流程引用的模型，释放 **114,291,150,857 字节（106.442 GiB）**。包含旧 Dasiwa V1 / V1 Turbo、LTX2.3 三件套、官方 LTX2.5 对照主模型、两版旧 FL2V Turbo LoRA，以及未使用的 PinkFluffyBunny / SexyGod LoRA。磁盘空闲空间实际增加 106.442 GiB。

Qwen2.1、Dasiwa V2、Eros、原 FL2V / Ref2V 及其三种普通 Turbo LoRA、REDGraft 与共享 LTX2.5 组件、RTX / DLSS 依赖全部保留；两个交付视频 SHA256 校验保持一致。Downloads 中的 DasiwaV2 / Eros 是模型文件的硬链接，没有重复占用数据空间。旧 Hybrid Turbo 的本机配置和配置示例均关闭。

随后按用户要求另行移除了旧 NSFW 专用的 `HMNSFW-AIO-V2.5`、`Vagina_minimax-h3_epoch20` 和 `AfterMidnight_ref2va_h3_sexytime_rank64-v1.2` 三件 LoRA，合计 **1,356,448,560 字节（约 1.263 GiB）**；`comfyui.nsfw.fl2v_presets` 与 `ref2v_presets` 已清空，`aio`／`aio-vagina`／`aftermidnight` 档位退役。共享的普通 FL2V／Ref2V 基模没有删除，新 Eros 模型也未受影响。私有配置已备份至工作区 `backups`，逐文件删除和共享文件留存检查见 `../maintenance/studio-20260926/cleanup-old-nsfw.json`。旧任务、输出和既往测试记录保留；旧 NSFW LoRA 配方已不再具备原样复现条件，不能把历史测试通过视为当前仍安装这些权重。

四个已清理的 LTX 对照预设在依赖缺失时从页面隐藏，后端构图保留用于记录与 REDGraft 内部复用，重新安装对应权重后可恢复显示。历史 A/B 记录代表清理前的实际测试，不能用来判断当前权重是否仍在磁盘。完整逐文件记录见 `../maintenance/studio-20260926/model-cleanup/cleanup-result.json`。

## Qwen Image 2.1

| 画幅 ID | 输出尺寸 |
|---|---|
| `landscape-hd` | 2048×1152，16:9 |
| `landscape-draft` | 1536×864，16:9 |
| `portrait` | 1152×2048，9:16 |
| `square` | 1536×1536 |

使用已安装的 `qwen_image_2.1_bf16.safetensors`、`qwen3vl_8b_int8_convrot.safetensors` 与 `qwen_image_2.1_vae_bf16.safetensors`。默认 25 步 `euler / simple`、CFG 1；参考图 KV 缓存为 `auto / default`，交付不透明 RGB PNG。无参考图时走文生图；编辑时先等比容纳完整参考图，画幅不符则补边并明确要求模型自然扩展背景，不通过裁脸、裁身体或拉伸来凑画幅。

每次任务保存 `workflow_qwen21.json` 与 `image_generation_state.json`。恢复已有 ComfyUI prompt ID 时，沿用原提示词、种子及客户端 ID，不重复提交生成。原生节点与模型来源：[Comfy-Org Qwen Image 2.1](https://huggingface.co/Comfy-Org/Qwen-Image-2.1)、[官方编辑模板](https://github.com/Comfy-Org/workflow_templates/blob/main/templates/image_qwen_image_2_1_image_edit.json)。

## 高清后处理方案与实际边界

当前默认 `anime_sr`：采用 [Real-ESRGAN 官方 AnimeVideo-v3](https://github.com/xinntao/Real-ESRGAN) 权重，读取原生输入像素，RIFE 在原分辨率上按目标时间戳采样，然后逐帧运行原版4×超分并立即缩至交付尺寸。没有扩散二采，也没有先把输入降到目标的一半。49帧同素材对照的动漫线条、脸型与手部比本次LTX/RTX候选更忠实；完整成片仍需实际播放验收，不能把默认选择当作已获用户认可。

交付参数统一为 `target_resolution=720p|1080p|1440p`，分别为1280×720、1920×1080、2560×1440。界面“2K”明确指2560×1440。默认1080p兼容旧任务。模型在相应目标画布工作，最终封装只复制视频包，不重新改帧率或固定缩到1080p。目标、原尺寸、原fps/帧数、种子、图与Comfy任务收据均记录，改目标会失效旧缓存。

| 预设 ID | 当前实际处理 | 边界 |
|---|---|---|
| `anime_sr` | 原尺寸 → 可选真实RIFE → 官方AnimeVideo-v3逐帧4× → 目标尺寸 | 当前动漫默认；无扩散重绘 |
| `ltx25_msr` / `redgraft_msr` | 原像素右下补边 → 整段VAE编码 → 固定latent2× → MSR三步二采 → 去补边 → 交付尺寸 | 官方distilled／REDGraft只替换基模，同图同参数；不会按目标先缩小源，长片待验证 |
| `redgraft_refine` | 整段plain VAE编码 → 可选learned latent2× → 4步/.15低噪 → 长时间上下文解码 | PeterDuncan流程的REDGraft/conv VAE适配，非精确复现；会改脸、手和服饰，实验可选 |
| `redgraft_pixel_2stage` | 整段Pixel IC-LoRA8步 → latent2× → 基础模型3步 | 生成式重绘；输出画布按目标与64网格推导，角色一致性不能保证 |
| `ltx25_refine` / `ltx25_pixel_2x` / `ltx25_pixel_2stage` | 相应官方LTX2.5权重路线 | 按当前依赖显示；本次已重新安装官方distilled作MSR对照，依赖齐全不等于画质通过 |
| `ltx23_refine` | 整段低噪V2V | 对照模型已清理，未安装时隐藏 |
| `rtx_vsr` | 原生尺寸输入RTX VFX SDK → 目标 → 真实RIFE | 无扩散；本次动画仍可能出现油画块，保留对照 |
| `h3_refine` | 整段H3低噪细化，可带角色参考 | 真实二采，资源较重；不能因一秒样例通过推断长片 |
| `rtx_loop` | 原尺寸RIFE闭环 → RTX目标尺寸 | 旧版对照；不再声称稳定合格；仅24fps输入 |
| `redgraft_loop` | 整段960×544实验二采 → RTX目标尺寸 → RIFE闭环 | 原候选被用户否决；保留实验比较，不推荐 |
| `dlss_sr` / `dlss_nr` | 社区独立运行时SR，NR可选 → 目标 → RIFE | 已有一秒真实GPU推理记录；新整段执行方式仍需逐片验收 |

REDGraft 文件名带INT8，实际为混合W4A8+INT8量化，SHA256 `ab59bb5e74e76937b55a6876fb23c4b58261e798227eb544f7d8a2934728c882`。[作者发布](https://civitai.com/models/1295569?modelVersionId=3250230) 随模型的8+3 JSON是原生I2V/T2V，不是H3视频精修成功示例；本工作台不能据此声称完整复现作者的H3管线。

MSR路线参考 [LiconStudio作者源码](https://github.com/liconstudio/ComfyUI-LTX2.5-MSR/tree/03061c18f8a79d26f3c63931dd8e28f5e5d872a6)、[作者V1工作流及权重](https://huggingface.co/LiconStudio/LTX-2.5-Multiple-Subject-Reference)和用户提供的H3→MSR图。本机32节点图先在原像素右下复制边缘补齐32网格，再编码整段源视频，用官方learned latent upscaler固定放大2×；MSR V1强度1增加静态角色参考，采样后移除参考guide。初始latent来自真实源视频，保留动作信息，不是空latent上删掉高噪声步骤。默认使用角色参考图，未提供时取原视频首帧；仅此一张角色参考，不自动混入其他人物或背景。

`ltx25_msr`用官方distilled INT8，`redgraft_msr`标为“REDGraft + MSR（NSFW 特调）”；两分支共用MSR V1、conv VAE、Euler、video/audio CFG 1和所有图参数，只有基模不同，不额外叠distill LoRA。本机短样中，作者图的`0.85, 0.7250, 0.4219, 0`起始噪声会改掉说话嘴形，因此当前两分支统一改为实测适配的`0.45, 0.25, 0.10, 0`三步。它不是作者原样配方，也不构成普遍保真保证；原0.85图仍留在research JSON用于复现，不增加页面档位。两分支工作流版本升至3，避免复用旧高噪声或按目标预缩源的缓存。

MSR严格按视频实际原分辨率进行latent 2×，交付目标不参与采样尺寸计算。`ImagePadKJ / edge_pixel`只将最右列、最下行复制到右侧和底部，补齐32网格；左上位置和全部源像素保持不变，不先缩放、裁切或居中补边。解码后从左上裁到原宽高的2倍，准确去除2倍补边，最后等比中心裁切/缩放为所选精确16:9交付尺寸。即使源已大于交付目标，也仍执行原尺寸latent 2×，不会自动改成direct V2V或限制采样面积；高分辨率源会显著增加显存、内存和时间需求，资源不足明确失败。显式传入的采样尺寸只可等于原图补齐后的2倍，否则拒绝。

| 实际源 | 补边后编码 | latent2×画布 | 去补边内容 | 示例交付 |
|---|---|---|---|---|
| 736×414 | 736×416 | 1472×832 | 1472×828 | 1280×720 |
| 960×540 | 960×544 | 1920×1088 | 1920×1080 | 1920×1080 |
| 1280×720 | 1280×736 | 2560×1472 | 2560×1440 | 1920×1080 |
| 1344×756 | 1344×768 | 2688×1536 | 2688×1512 | 2560×1440 |
| 864×480（旧待机源） | 864×480 | 1728×960 | 1728×960 | 1920×1080 |

同一源选择720p、1080p或2K，二采画布完全相同，仅最后交付节点改变。低分辨率源不会被强行预放到1280宽；例如864×480的二采内容仍只有1728×960，交付1080p还包含末端尺寸适配，不能称为原生1080p重绘。1280源短样保留更多脸部细节和嘴形，但快速手部仍有重影，不能据此宣称全面更好。每次任务将`mode/source/encode/padding/latent2x_canvas/content2x/final`写入`msr_geometry.json`和结果metadata，便于准确追溯。

MSR保持整段采样，解码时间窗口4096，不做独立时间分块扩散或拼接；空间tile随长度使用768/512/384，未加入ChunkFeedForward以保持短样A/B条件一致。保持原24fps源时间轴与原音轨，交付换帧率由后续真实RIFE完成。当前证据限短样，8秒和15秒的资源占用、嘴形、动作及循环仍需完整实跑检查；默认仍为`anime_sr`。来源、源码审查和候选JSON见 `../maintenance/ltx-reliable-20260926/msr_source_review.md`。

新版LTX低噪参考PeterDuncan实际连线的`simple / 4 steps / denoise .15`与全时间解码；作者原图是1376×768→learned latent2×→2752×1536。本机属于明确适配：只有目标半画布不会降低源像素时才使用latent2×与首帧guide；例如864×480到1080p先轻微升至960×544、latent放到1920×1088；1280×736输入直接编码目标画布，不先降至960×544。720p与2K档的较高源尺寸也可能走direct V2V，不能把作者原图尺寸或profile选择误当本机实际LTX采样计划；实际canvas以导出的图为准。最终小幅裁网格边缘。本机REDGraft与conv VAE也和原图权重/非conv VAE不同。49帧实跑中，修正后的LTX仍改变了脸型与手轮廓，因此没有强加为动漫默认。

受控VAE诊断已经证明：在同一49帧、同一latent且**没有任何采样器**的条件下，32帧/重叠8的解码在第24帧制造明显重影；同时分块编码会再增加误差。记录见 `../maintenance/studio-20260926/loop-motion-audit/vae-boundary-audit/DIAGNOSIS.md` 与 `boundary_contact.png`。所有LTX/REDGraft路线（含Pixel与旧循环）已去掉32/8编码/解码：普通编码、解码 `temporal_size=4096, temporal_overlap=32`，空间tile按帧数使用768/512/384、重叠64。长时间上下文不等于32GB一定能容纳任何片段；资源不足明确失败，不回退成独立4秒重绘拼接。实验通用处理限16秒，扩散路线只接受原生24fps。

所有当前worker已移除独立4秒扩散分段和最终CFR重复帧转换。目标30fps由原生RIFE v4.26实际插值；输出24fps且输入24fps、步幅1时保留原帧。`cadence_stride=1`默认保留所有源帧；只有明确二拍动画才选2（仅24fps源），不能把12姿态/秒套到任意视频。本次旧片人物ROI相邻帧变化有约5倍奇偶差，故修二拍节奏有依据。非整数源fps不会先四舍五入；不整除的比例在最多16倍原生RIFE子步上选最近位置，时间戳仍均匀，元数据记录真实源率。

普通片保留N/fps播放时长，末帧覆盖最后一个源帧时段。循环在末尾追加首帧作为插值锚点，再去掉重复输出首帧；这不能替代生成阶段的首尾姿态与运动连续性检查。音频使用同一源跨度/目标时长映射，补足或裁齐；循环首尾各40ms短淡化避免音频点击，不平移画面、不反放、不增加视觉交叉淡化。没有源音轨就不凭空加声音。`crossfade`仍为旧API兼容字段，当前整段路线不使用旧wrapped dissolve。

两个小型适配节点位于 `ComfyUI/custom_nodes/ComfyUI-StudioPost`：`StudioFrameInterpolateSampled`最多同时保留8个相邻插值区间，只保存实际输出所需帧；`StudioImageUpscaleToSize`逐帧调用原版SR后立即缩至目标，避免存整片4×巨型张量。目标数组预分配，不再list+cat双份占用。近16:9且比例差≤3%视作模型网格小裁；更大比例差在AnimeSR使用contain黑边，实验生成路线明确拒绝大比例裁切。节点不更换网络与推理数学，不新增Python依赖，导出的Comfy API图可复现。

本机早期一秒A/B、旧8秒/15秒运行记录均只是历史证据。旧REDGraft分段、整段低分二采以及RIFE+RTX1080候选均未通过用户完整播放验收；只看首末MAE或光流统计曾遗漏片中时域突变，旧推荐已撤回。保留旧片与收据，不覆盖为“修好”。真实新候选需要检查完整播放、两轮循环、脸/手/发丝局部、周期软帧、首尾运动速度及音频，不以单个指标批准交付。

参考图与准确标题最后叠加。RIFE模型来自 [Comfy-Org](https://huggingface.co/Comfy-Org/frame_interpolation)，本机SHA256 `151874592c877740e5db11522f4514df569eeafb0a0fcb2696f16e9e8d317c94`。成熟流程原图与参数追溯见 `../maintenance/studio-20260926/workflow-research-2/WORKFLOW_AND_VAE_FINDINGS.md`；[VAE时间边界研究](https://github.com/SlavaSexton/ComfyUI-OCIO/blob/main/docs/NODES_VAE.md) 与本机受控对照相符。Comfy capabilities 的available仅代表依赖齐全，不能表示画质验收通过。

NVIDIA RTX 视频超分与游戏 DLSS 是不同技术。社区 `ComfyUI-DLSS5` 名称不能作为“已部署 NVIDIA 官方 DLSS 5”的证明；当前集成明确标为实验，不能报告未经验证的官方兼容性或画质提升。

本机 1 秒带音频片段已完成 RTX 与 LTX 2.3 冒烟验证：均输出 1920×1080、30 fps、30 帧；RTX 处理约 4.2 秒，LTX 全流程约 24.2 秒。LTX 观测到的整卡显存峰值约 30,801 MiB；RTX 的后续记录包含驻留的 LTX 缓存，不能当成 RTX 独立显存需求。这只是短片测试，不代表 15 秒素材的速度或画质保证。记录在 `../maintenance/studio-20260926/post_smoke_results.json`。

DLSS SR 在 1 秒“狼仔鸽鸽”样本上完成真实 GPU 推理，输出 1920×1080、30 fps、30 帧，用时约 16.3 秒、整卡显存峰值约 6,319 MiB；同样本 RTX 约 5.0 秒、3,741 MiB。它仍是社区桥接，不是官方 DLSS 5 视频产品，保留为可选方案。记录与对照在 `../maintenance/studio-20260926/DLSS_SR_DEPLOYMENT.md`、`dlss_sr_vs_rtx.png`。

DLSS 5 NR 的社区 RTX 40 路径已实际部署。使用 [dev-camo/dlssnr-patcher 固定源码](https://github.com/dev-camo/dlssnr-patcher/tree/98dac112990e5cea30ab03d74625fc848bce5c92) 的 `--ada`，从已验签的 NVIDIA NR 310.8.0 原版独立构建；15 个 CUDA fatbin 重新编译到 `sm_89`，同时适配架构判断。仅下载 NVIDIA 官方 CUDA 13.3.73 的三个编译工具，没有更换 ComfyUI / PyTorch 环境。原版 DLL 保持 `Valid` 签名；修改版主动移除失效证书，状态为 `NotSigned`，SHA256 为 `cb1a1a6e47e38b9229db4f22081c0f9b1271783599675e0ab13d41d4d039755a`。这是公开补丁与开源桥接实现的社区方案，NVIDIA DLL 本身不因此变成开源或官方 RTX 40 支持版本。

本机同一秒“狼仔鸽鸽”动画重新测试：SR 用时 12.297 秒、整卡峰值 5,570 MiB；SR + Ada NR 用时 17.101 秒、峰值 5,875 MiB。两者均输出 1920×1080、30 fps、30 帧、1.000 秒且保留音轨。30 帧的平均 RGB 差异为 1.673/255，说明 NR 实际改变画面；接触图中角色和衣装基本保持，不能仅凭像素差宣称画质更优。当前使用 Neutral / faithful、effect_strength 0.25，适合作为可选神经修饰；当前动漫默认已改为AnimeVideo-v3。旧4秒分段入口已移除，新的整段/RIFE封装不扩大这次NR一秒样本的画质验证范围。

完整基准来源、补丁审计、编译命令、原/新 DLL 哈希、GPU 任务 ID 与逐帧比较见 `../maintenance/studio-20260926/nr-provenance/ADA_DEPLOYMENT.md`、`ada-build/SOURCE_AUDIT.md`、`ada-build/build_manifest.json`、`ada_smoke_results.json` 与 `ada_sr_nr_contact.png`。`runtime/verification.json` 将实际推理记录绑定到 DLL 和配置的文件签名，文件变化会使“已验证”状态失效。

依据：[官方 LTX 2.3 模板](https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_ltx2_3_flf2v.json)、[社区作者 LTX 2.5 V2V 配方](https://github.com/mdkberry/comfyui_workflows/tree/main/workflows_by_model/LTX25)、[官方 LTX 2.5 文档](https://docs.comfy.org/tutorials/video/ltx/ltx-2-5)、[LTX 2.5 Pixel IC-LoRA](https://huggingface.co/Lightricks/LTX-2.5-22b-IC-LoRA-Pixel-Spatial-Upscaler)、[NVIDIA VFX SDK](https://docs.nvidia.com/maxine/vfx-python/latest/index.html)、[Comfy-Org RTX 节点](https://github.com/Comfy-Org/Nvidia_RTX_Nodes_ComfyUI)、[社区 DLSS 桥接](https://github.com/HECer/ComfyUI-DLSS5)。

## 启动与提示词供应商

先启动本机 ComfyUI `http://127.0.0.1:8188`，再运行工作台的 `start_service.bat`，打开 `http://127.0.0.1:7788`。ComfyUI 由同一实例串行管理 GPU 任务；避免同时运行多个占满显存的实例。模型文件应放入 ComfyUI 对应的 `diffusion_models`、`text_encoders`、`vae`、`checkpoints` 目录。

实际凭据只保存在未提交的 `config.yml`，模板不含密钥。提示词模型配置为：

```yaml
llm:
  base_url: "https://api.commandcode.ai/provider/v1"
  api_keys: []  # 在本地填入供应商密钥，或设置 LLM_API_KEY 环境变量
  model: "moonshotai/Kimi-K2.7-Code"
  max_output_tokens: 4096
  reasoning_effort: "low"
```

`api_keys` 取第一个非空条目；旧 `api_key` 字段和 `LLM_API_KEY` 环境变量仍可使用，不要用空的 `api_key` 覆盖列表。图片与视频的 LLM 辅助都走同一供应商。关闭辅助时直接使用填写的提示词；生成权重仍在本机 ComfyUI 执行。

## 三步 API

请求遵守工作台现有访问控制。生成与后处理使用 multipart/form-data，返回任务 ID；轮询任务直到 `completed` 或 `failed`，完成后读取 `assets` 中的结果 URL。

| 接口 | 用途及关键字段 |
|---|---|
| `GET /api/config` | H3 档位、默认值、旧模式配置；含 `hybrid_presets` |
| `GET /api/studio/capabilities` | 图片画幅与后处理依赖、可用状态 |
| `POST /api/studio/image-prompt-polish` | `prompt`、`preset`、可选多值 `images` |
| `POST /api/studio/images` | 上述字段与 `seed`、`polish_prompt_first` |
| `POST /api/video/hybrid-prompt-polish` | 综合视频提示词辅助；`hybrid_preset=anime\|nsfw` |
| `POST /api/video/hybrid-tasks` | `prompt`、`hybrid_preset`、`duration`、`resolution_megapixels`、`aspect_ratio`，可选首尾帧与参考媒体 |
| `POST /api/video/tasks` | 原 FL2V 文本／首帧／尾帧／首尾帧任务 |
| `POST /api/video/reference-tasks` | 原 Ref2V 多模态参考任务 |
| `POST /api/studio/postprocess` | `video_task_id` 或上传 `video`；`preset`、`prompt`、`loop`、`duration`、`target_fps`、`crossfade`；可选 `reference_image` 人物参考、`title_image`（PNG/WebP）与 `title_fade` |
| `GET /api/video/tasks/{id}` | H3 视频进度与结果 |
| `GET /api/studio/tasks` | 图片与后处理历史 |
| `GET /api/studio/tasks/{id}` | 图片／后处理任务状态及 `assets` |
| `POST /api/studio/tasks/{id}/retry` | 重试失败任务；先确认 ComfyUI 状态，图片可恢复已有任务，后处理复用设置相同的已完成整段阶段 |

例如后处理使用 `preset=anime_sr`、`target_resolution=1080p`、`target_fps=30`；闭合待机片再设置 `loop=true,duration=15`。普通素材 `cadence_stride=1`，确认是24fps二拍动画后才使用2。源分辨率与交付目标独立记录，H3作者来源预设由界面联动；不能认为864×480足以产生原生1080p细节。所有当前worker保持整段时间轴，`crossfade`仅兼容旧请求，不再驱动分段交叉淡化。

透明标题可通过 `title_image` 上传，在视频细化完成后叠加，避免扩散采样改变准确字形；`title_fade=true` 用于登场片头淡入，循环待机片一般保持 `false`。素材上传失败的任务需重新上传创建；重试会先检查 ComfyUI 队列，避免给仍在执行的任务重复安排采样。

标题支持三档交付：位置、最大600×280范围均以1080p基准按比例缩放，并验证四周10%安全区。1080p基准位置220,620不变；720p和2K分别同比缩小/放大。实际CPU小片测试已逐档验证帧数、逐帧时间戳与音频包保持不变，无音轨素材不增加声音。

图片与后处理文件保存于 `VIDEO_DATA_FOLDER/studio`（默认 `video_data/studio`），无需新增存储配置。服务重启会检查未完成的持久化任务。接口完整参数与类型以运行中的 `http://127.0.0.1:7788/docs` 为准。实际验收应检查最终文件尺寸、时长、播放与循环接缝，并保留对应提示词、工作流、种子和 ComfyUI prompt ID。

## 工作区素材库与版本

打开页面的“工作区”，新建一个有名称的素材库。图片、视频、音频可直接上传；第一步图片结果、第二步视频结果和历史任务中的原输入／结果都能“加入工作区”。“保存当前创作”会收纳当前输入与已完成结果，并保存三个阶段的参数、提示词和精确种子字符串。之后“载入”恢复这些参数与素材，继续修改或创建新的生成任务。工作区不是生成任务队列；保存和载入不会自动运行模型。

MSR、REDGraft MSR 和 H3 精修的高清设置中有紧凑的“人物参考图”区域。可上传图片、从素材托盘拖入，或选中托盘图片点击“二采参考”；托盘的页内指针拖拽与原生拖放使用相同的素材权限检查。参考图优先级为**手动上传 → 原视频／后处理任务的参考图 → MSR 取源视频首帧**。切换视频来源会清空手动参考，避免沿用上一条视频的人物。历史复用会恢复原参考图；保存创作会同时收纳手动或原任务继承的参考，载入草稿时一起恢复，并随工作区 ZIP 导出导入。

素材保存在 `VIDEO_DATA_FOLDER/workspaces`。重复加入内容与名称相同的当前素材会复用素材项；跨工作区复制、从历史任务加入和选择工作区视频做后处理，均由服务器复制，避免大视频通过浏览器重复上传。复制后的素材独立存在，删除原任务或原工作区不影响副本，也不影响已创建任务的私有输入。

新工作区从版本 1 开始。显式保存、重命名、增加或删除素材、恢复版本都会创建一个检查点；草稿自动更新接口只更新当前草稿，不增加版本。删除素材只从当前素材库移除，旧版本仍能恢复它。若草稿引用了已删除的素材，页面会要求先恢复含该素材的版本；不会静默少图运行。恢复旧版本会再创建一个新版本，保留此前的记录。**删除整个工作区会永久删除它的草稿、全部版本与所有媒体副本**，生成历史和其他工作区不受影响。

“导出 ZIP”包含当前草稿、全部保存版本，以及当前和历史版本引用的实际媒体文件，不是仅导出本机路径。ZIP 可以在另一套工作台导入为新的工作区；素材 ID 保持不变以便草稿关联，新工作区 ID 和所有者由服务器重新分配。导入逐项校验允许目录、文件名、大小、SHA256 和媒体可读性，拒绝路径越界、链接、大小写重复文件、哈希错误和过高压缩比例；从不直接执行归档内的内容。媒体访问、复制、历史恢复、导出和删除都要求工作区所有者或管理员权限；知道 URL 不等于获得访问权限。

默认每个文件上限 1 GiB、每个工作区媒体总量 10 GiB（含历史保留文件）、当前素材上限 500 项、版本上限 2000 个。可在启动服务前设置 `WORKSPACE_MAX_FILE_BYTES`、`WORKSPACE_MAX_TOTAL_BYTES`、`WORKSPACE_MAX_ASSETS`、`WORKSPACE_MAX_REVISIONS` 环境变量调整。历史保留的唯一媒体文件上限为当前素材上限的 10 倍；草稿限 1 MiB、归档 JSON 元数据限 32 MiB，保存时也检查此预算，避免保存出无法重新导入的归档。删除当前素材不会释放历史需要的空间；需要完整释放时先导出备份，再删除整个工作区。

工作区 API 使用相同的现有身份认证，所有返回 URL 均由服务器生成：

| 接口 | 作用 |
|---|---|
| `GET /api/studio/workspaces`、`POST /api/studio/workspaces` | 列表；JSON `{name}` 新建 |
| `GET /api/studio/workspaces/{id}`、`PATCH .../{id}`、`DELETE .../{id}` | 详情；JSON `{name}` 重命名；删除整个工作区 |
| `POST .../{id}/assets`、`GET/DELETE .../{id}/assets/{asset_id}` | multipart 多值 `files` 上传；受保护媒体读取／移除 |
| `POST .../{id}/from-task` | JSON `{kind,task_id,asset_name}` 从授权历史任务复制原输入或结果 |
| `POST .../{id}/from-workspace` | JSON `{source_workspace_id,asset_id}` 从另一授权工作区复制 |
| `PUT .../{id}/draft`、`POST .../{id}/save` | JSON `{draft}` 更新当前草稿；JSON `{draft?,name?}` 创建保存版本 |
| `GET .../{id}/history`、`POST .../{id}/history/{revision_id}/restore` | 列出版本；恢复为新版本 |
| `GET .../{id}/export`、`POST /api/studio/workspaces/import` | 下载完整 ZIP；multipart `file` 导入为新工作区 |

详情直接返回 `id,name,assets,draft,revision,created_at,updated_at`。上传与复制增加 `added_asset_ids`，按输入顺序返回，去重时可包含相同 ID；删除素材返回更新详情，删除工作区返回 HTTP 204。后处理接口也可使用 `source_workspace_id` 与 `source_workspace_asset_id` 选择工作区视频，与上传视频、原视频任务、后处理历史来源互斥。完整请求定义以本机 `/docs` 为准。

## 原画保真基线（保留供对照，用户尚未最终验收）

当前目录：`D:/ComfyUI_windows_portable/output/langzaigege-live/`。工作区“狼仔鸽鸽直播间”与ZIP由真实历史任务收集。新版文件已完成规格与时间轴检查，但用户尚未最终验收画质，不把任务completed等同于画质批准。

| 文件 | 实际规格 | 真实流程与任务 |
|---|---|---|
| `reference_16x9.png` | 2048×1152 | Qwen Image2.1参考图，Kimi辅助提示词 |
| `intro_1080p.mp4` | 1920×1080，30fps，240帧，8.000秒 | 新Dasiwa V2初采1280×736裁至1280×720 → RIFE（stride1）→ AnimeVideo-v3 → 标题淡入；后处理`511524d56f1d4a63ba80a9b06b33933c` |
| `standby_loop_15s_1080p.mp4` | 1920×1080，30fps，450帧，15.000秒 | 保留原864×480同图首尾源 → RIFE闭环（显式stride2）→ AnimeVideo-v3 → 固定标题；后处理`31756f148b9543c8aa600fae68a410d1` |

两段没有额外扩散二采，均保留32kHz双声道音轨，音轨时长分别8秒与15秒。准确标题“狼仔鸽鸽”最后单独合成，范围`[220,620,820,832]`位于10%安全框`[192,108,1728,972]`。新片头初采任务为`4cc0e65b79644ee18683e9007f4076ab`；循环沿用`85d68f07bc444e82992b04bbdae25b8d`，没有重新生成用户认可的动作轨迹。

[新版检查记录](../maintenance/studio-20260926/upscale-reassessment/revised_candidate_review.md)包含真实任务、SHA256、逐帧ROI时间曲线、原分段位置、首尾速度与局部疑点；同目录`revised_*contact.png`、`revised_loop_seam.png`和JSON供复核。两轮30秒预览`revised_loop_two_cycles_preview.mp4`仅供验收，900帧的相邻PTS严格连续，正式待机片仍为15秒。末尾过渡与中段动作需实际播放判断，不能只用首末MAE证明循环合格。

旧REDGraft分段二采、旧RIFE+RTX交付、旧工作区ZIP和证据已归档至`D:/ComfyUI_windows_portable/output/langzaigege-live/previous/20260926-before-upscale-reassessment/`，保留原文件追溯，不作为当前通过验收版本。新初采1280×736原文件及裁边前metadata也已保留。

本次后端完整测试178项通过。真实浏览器记录包括[素材托盘与布局](../maintenance/studio-20260926/ui-tray-browser-verification.md)、[三档交付与源尺寸联动](../maintenance/studio-20260926/ui-delivery-browser-verification.md)和[工作区与ZIP](../maintenance/studio-20260926/ui-workspaces-browser-verification.md)。浏览器参数验收、GPU实际运行与用户画质验收分别记录。
