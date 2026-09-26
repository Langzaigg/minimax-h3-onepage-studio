# 兔酱拍片工作室：ComfyUI 适配源码

这里分发本任务编写的三个小型适配模块，以及负责注册它们的 shim。没有复制第三方整仓、模型、DLL、VapourSynth、CUDA 工具、用户配置或素材。

| 文件 | 来源和用途 |
|---|---|
| `ComfyUI-StudioPost/__init__.py` | 本任务源码。按帧调用 ComfyUI 官方图像 SR、分组调用原生 RIFE，并及时释放不需要的中间结果；不包含新的神经网络或插帧算法。 |
| `ComfyUI-DLSS5/studio_filter.py` | 本任务源码。四项真实 NR 参数、自定义开关、社区风格映射、运行回执；调用已安装上游的深度、光流和桥接接口。 |
| `ComfyUI-DLSS5/studio_integration.py` | 本任务源码。串行阶段之间释放未交给 Comfy 模型管理器的深度/光流缓存，注册重复调用时不重复包裹。 |
| `ComfyUI-DLSS5/studio_register.py` | 本任务注册 shim。复制并扩展上游节点映射，保留其他节点、显示名、WEB_DIRECTORY 和入口代码。 |

`studio_filter.py` 和 StudioPost 源码对应本机已经验证的节点；只有 memory wrapper 增加了幂等注册保护。仓库测试直接读取这里的源文件，因此不需要先覆盖正在运行的生产节点才能验证参数与注册行为。

## 安装前提

- ComfyUI 需提供 `ImageScale.upscale`、`ImageUpscaleWithModel.execute` 和 `FrameInterpolate.execute`。已验证源码版本为 `79be670e2d9be63e238785af307369d2b9039ed1`。
- 在 ComfyUI 的 `custom_nodes/ComfyUI-DLSS5` 安装 [HECer/ComfyUI-DLSS5](https://github.com/HECer/ComfyUI-DLSS5/tree/f4f59cd1fe39e785a3b600ca20b5e09194a9e38d)，参考版本 `f4f59cd1fe39e785a3b600ca20b5e09194a9e38d`，并按上游要求安装依赖。
- 上游 `bridge_runner.py` 在本任务中未修改，不在本目录重复分发。安装器检查它支持四个真实 native 浮点参数和 `core.dlssnr.Enhance`；也检查 adapter 实际调用的上游 Python 接口。
- 上游 `nodes.py` 的两处本地改动仅用于让子进程在 Windows 隐藏窗口。安装器只对已识别的准确代码片段补入 `CREATE_NO_WINDOW`，不复制或整体替换第三方 `nodes.py`。如果上游代码结构改变，会停止并要求重新审查，而不是盲目套补丁。
- NR 运行时和 GPU 支持需单独准备与验证。本仓库不分发 NR/SR DLL、修改后的 Ada DLL、驱动或权重；RTX 40 NR 属于社区兼容方案，不能表述为 NVIDIA 官方支持。安装 Python adapter 并不等于 NR 已可运行。

## 检查和安装

在工作台仓库根目录执行，`python` 使用工作台环境；路径指向含 `nodes.py`、`custom_nodes` 的 ComfyUI 目录：

```powershell
python scripts/install_comfyui_adapters.py --comfy-root ..\ComfyUI --check
python scripts/install_comfyui_adapters.py --comfy-root ..\ComfyUI
python scripts/install_comfyui_adapters.py --comfy-root ..\ComfyUI --check
```

`--check` 严格只读。退出 0 表示上游接口兼容；JSON 的 `up_to_date` 表明文件是否已经一致，`changes_required` 列出将更新的精确文件。缺文件、未知接口或不安全路径返回 2。

安装前先校验全部目标、接口与原文件哈希，再将所有将被替换的原文件保存到 ComfyUI 下的 `.studio-adapter-backups/<时间和随机后缀>/`，其中 `manifest.json` 记录更新前后哈希。随后逐文件原子替换。重复执行没有差异时不写文件、不创建新备份。安装器拒绝越出 ComfyUI 根目录的路径和指向外部的符号链接/重解析点，不递归删除，不启动或重启任何服务。

注册代码以内部 `Frame Studio adapters (managed)` 标记追加到原入口，更新时只替换这一段；这个兼容标记不等于产品名称。已有手工注册仍可共存，幂等 wrapper 避免重复释放包装。

安装后需由操作者在合适时机重启 ComfyUI，并检查 `/object_info/StudioDLSS5Filter`。Python 文件检查与单元测试不能代替真实 GPU 推理验收。
