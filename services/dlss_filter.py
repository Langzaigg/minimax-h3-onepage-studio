"""Same-size community Ada NR filtering; never SR or frame interpolation."""
from __future__ import annotations

import json
import math
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageOps

from services import post_processing as pp

LOOKS = {"neutral": "Neutral / faithful", "realistic": "Realistic detail", "detail": "Strong detail"}
STYLES = {"default": 0, "natural": 1, "cinematic": 2}
CONTROLS = {"output_blend": 1.0, "local_tone": .4,
            "local_structure": .5, "skin_mask": .3}
NATIVE_CONTROLS = {"output_blend": ("intensity", 1.0), "local_tone": ("style_strength", 1.0),
                   "local_structure": ("local_structure", 1.0), "skin_mask": ("skin_structure", -1.0)}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def filter_options(look="neutral", strength=.25, **advanced):
    version = advanced.pop("controls_version", 1)
    if version == 2:
        return advanced_options(**advanced)
    if version != 1 or advanced:
        raise ValueError("无效滤镜参数版本")
    strength = float(strength)
    if look not in LOOKS or not math.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError("滤镜风格或强度无效；强度须在0–1之间")
    return {"look": look, "strength": strength}


def advanced_options(*, style="default", allow_experimental=False, **controls):
    if style not in STYLES or not isinstance(allow_experimental, bool):
        raise ValueError("无效滤镜风格或实验范围设置")
    allowed = set(CONTROLS) | {name + "_enabled" for name in CONTROLS}
    if controls.keys() - allowed:
        raise ValueError("未知滤镜参数")
    result = {"controls_version": 2, "style": style, "allow_experimental": allow_experimental}
    maximum = 5 if allow_experimental else 1
    for name, default in CONTROLS.items():
        value = float(controls.get(name, default))
        enabled = controls.get(name + "_enabled", True)
        if not math.isfinite(value) or not 0 <= value <= maximum or not isinstance(enabled, bool):
            raise ValueError(f"滤镜参数 {name} 须在0–{maximum}之间，启用状态须为布尔值")
        result[name], result[name + "_enabled"] = value, enabled
    return result


def native_parameters(settings):
    if settings.get("controls_version") != 2:
        return None
    return {"style": STYLES[settings["style"]], "auto_mask": True, "feature_id": 18, "preset": 0,
            "depth_inverted": True, **{native: settings[name] if settings[name + "_enabled"] else fallback
                for name, (native, fallback) in NATIVE_CONTROLS.items()}}


def probe_media(path):
    path = Path(path)
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        with Image.open(path) as raw:
            if getattr(raw, "n_frames", 1) != 1:
                raise ValueError("滤镜图片输入仅支持静态图片；动画请转为视频")
            image = ImageOps.exif_transpose(raw)
            width, height = image.size
            image.load()
        return {"media_kind": "image", "width": width, "height": height,
                "frames": 1, "fps": None, "duration": 0, "has_audio": False}
    return {"media_kind": "video", **pp.probe_video(path)}


def build_filter_workflow(input_filename, *, media_kind="video", options=None,
                          filename_prefix="studio/dlss_filter", has_audio=True,
                          source_width=864, source_height=480):
    settings = filter_options(**(options or {}))
    if media_kind not in {"image", "video"}:
        raise ValueError("无效滤镜素材类型")
    graph = {}
    if media_kind == "image":
        graph["1"] = {"class_type": "LoadImage", "inputs": {"image": str(input_filename)}}
        images = ["1", 0]
    else:
        graph["1"] = {"class_type": "LoadVideo", "inputs": {"file": str(input_filename)}}
        graph["2"] = {"class_type": "GetVideoComponents", "inputs": {"video": ["1", 0]}}
        images = ["2", 0]
    graph["3"] = {"class_type": "ImagePadKJ", "inputs": {"image": images,
        "left": 0, "top": 0, "right": (-source_width) % 32, "bottom": (-source_height) % 32,
        "extra_padding": 0, "pad_mode": "edge_pixel", "color": "0, 0, 0"}}
    if settings.get("controls_version") == 2:
        graph["4"] = {"class_type": "StudioDLSS5Filter", "inputs": {
            "image": ["3", 0], **{key: value for key, value in settings.items() if key != "controls_version"}}}
    else:
        graph["4"] = {"class_type": "DLSS5EasyPipeline", "inputs": {
            "image": ["3", 0], "scenario": "Still image" if media_kind == "image" else "Short video / best quality",
            "operation": "Neural rendering only", "scale": "2x", "quality": "Quality",
            "look": LOOKS[settings["look"]], "effect_strength": settings["strength"]}}
    # scale/quality are mandatory widgets but ignored by the node's NR-only
    # branch. No SR, resizer, RIFE, or frame-generation node is connected.
    # Comfy's MP4 writer requires even dimensions. Keep at most one padding
    # pixel for intermediate video encoding; CPU packaging crops it off again.
    graph["5"] = {"class_type": "ImageCrop", "inputs": {"image": ["4", 0], "x": 0, "y": 0,
        "width": source_width + (source_width % 2 if media_kind == "video" else 0),
        "height": source_height + (source_height % 2 if media_kind == "video" else 0)}}
    if media_kind == "image":
        graph["90"] = {"class_type": "SaveImage", "inputs": {"images": ["5", 0], "filename_prefix": filename_prefix}}
    else:
        graph["90"] = {"class_type": "CreateVideo", "inputs": {"images": ["5", 0], "fps": ["2", 2], "bit_depth": 8}}
        if has_audio:
            graph["90"]["inputs"]["audio"] = ["2", 1]
        graph["91"] = {"class_type": "SaveVideo", "inputs": {"video": ["90", 0],
            "filename_prefix": filename_prefix, "format": "mp4", "format.codec": "h264",
            "format.codec.encoding": "re-encode", "format.codec.encoding.crf": 14.0}}
    return graph


def extract_preview(source, destination, media_kind, frame_index):
    if media_kind == "image":
        with Image.open(source) as raw:
            normalized = ImageOps.exif_transpose(raw)
            normalized.convert("RGBA" if "A" in normalized.getbands() or "transparency" in normalized.info else "RGB").save(destination)
        return
    import av
    with av.open(str(source)) as container:
        for index, frame in enumerate(container.decode(video=0)):
            if index == frame_index:
                frame.to_image().save(destination)
                return
    raise ValueError("预览帧超出原视频范围")


def preview_position(source, *, frame_index=0, time_seconds=None):
    """Find the display frame using real presentation timestamps, including VFR."""
    import av
    if time_seconds is not None and (not math.isfinite(time_seconds) or time_seconds < 0):
        raise ValueError("预览时间须为有效非负秒数")
    previous = None
    with av.open(str(source)) as container:
        stream = container.streams.video[0]
        origin = float(stream.start_time * stream.time_base) if stream.start_time is not None else 0.0
        for index, frame in enumerate(container.decode(video=0)):
            if frame.pts is None:
                raise ValueError("视频缺少显示时间戳，无法准确选择预览帧")
            timestamp = max(0.0, float(frame.pts * frame.time_base) - origin)
            if time_seconds is None:
                if index == frame_index:
                    return index, timestamp
            else:
                if timestamp > time_seconds + 1e-9:
                    return previous or (index, timestamp)
                previous = (index, timestamp)
    if time_seconds is not None and previous is not None:
        return previous
    raise ValueError("预览帧超出原视频范围")


def video_timing(path):
    """Read presentation timing without retaining decoded video pixels."""
    import av
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        timestamps, durations = [], []
        for frame in container.decode(stream):
            if frame.pts is None:
                raise ValueError("视频缺少显示时间戳")
            timestamps.append(frame.pts * frame.time_base)
            durations.append(frame.duration * frame.time_base)
        if not timestamps or any(b <= a for a, b in zip(timestamps, timestamps[1:])):
            raise ValueError("视频显示时间戳无效")
        for index in range(len(durations) - 1):
            durations[index] = timestamps[index + 1] - timestamps[index]
        if not durations[-1]:
            durations[-1] = Fraction(1, 1) / stream.average_rate
        return {"timestamps": timestamps, "durations": durations,
                "time_base": stream.time_base, "rate": stream.average_rate}


def restore_video_timing(rendered, source, output, *, has_audio):
    """Restore source PTS when Comfy's fixed-rate encoder flattens VFR timing.

    CFR output normally needs only packet copying. The exceptional VFR/offset
    path re-encodes NR frames with source PTS and packet durations, retaining
    every original frame and copying the original audio packets unchanged.
    """
    import av
    expected, rendered_timing = video_timing(source), video_timing(rendered)
    if len(expected["timestamps"]) != len(rendered_timing["timestamps"]):
        raise RuntimeError("DLSS滤镜改变了原视频帧数")
    dimensions = pp.probe_video(source)
    canvas = pp.probe_video(rendered)
    cropped = (canvas["width"], canvas["height"]) != (dimensions["width"], dimensions["height"])
    if cropped and (canvas["width"], canvas["height"]) != (dimensions["width"] + dimensions["width"] % 2,
                                                            dimensions["height"] + dimensions["height"] % 2):
        raise RuntimeError("滤镜视频的中间尺寸不符合原图补边尺寸")
    retimed = any(expected[key] != rendered_timing[key] for key in ("timestamps", "durations"))
    video = Path(rendered)
    if retimed or cropped:
        video = Path(output).parent / "filter_source_timing.mp4"
        time_base = expected["time_base"]
        duration_by_pts = dict(zip(expected["timestamps"], expected["durations"]))
        with av.open(str(rendered)) as reader, av.open(str(video), "w", options={
                "video_track_timescale": str(time_base.denominator)}) as writer:
            stream = writer.add_stream("libx264", rate=expected["rate"])
            stream.width, stream.height = dimensions["width"], dimensions["height"]
            stream.pix_fmt = "yuv420p" if stream.width % 2 == stream.height % 2 == 0 else "yuv444p"
            stream.time_base = stream.codec_context.time_base = time_base
            stream.codec_context.max_b_frames = 0
            stream.options = {"crf": "14", "preset": "fast"}
            def mux(packets):
                for packet in packets:
                    duration = duration_by_pts[packet.pts * packet.time_base]
                    packet.duration = int(duration / packet.time_base)
                    writer.mux(packet)
            for index, frame in enumerate(reader.decode(video=0)):
                if cropped:
                    frame = av.VideoFrame.from_image(frame.to_image().crop((0, 0, stream.width, stream.height)))
                frame.pts = int(expected["timestamps"][index] / time_base)
                frame.time_base = time_base
                frame.duration = int(expected["durations"][index] / time_base)
                mux(stream.encode(frame))
            mux(stream.encode(None))
    with av.open(str(video)) as filtered, av.open(str(source)) as original, av.open(str(output), "w", options={
            "avoid_negative_ts": "disabled", "movflags": "+faststart",
            "video_track_timescale": str(expected["time_base"].denominator)}) as writer:
        video_stream = writer.add_stream_from_template(filtered.streams.video[0])
        audio_stream = writer.add_stream_from_template(original.streams.audio[0]) if has_audio else None
        for packet in filtered.demux(video=0):
            if packet.dts is not None:
                packet.stream = video_stream
                writer.mux(packet)
        if audio_stream is not None:
            for packet in original.demux(audio=0):
                if packet.dts is not None:
                    packet.stream = audio_stream
                    writer.mux(packet)
    actual = video_timing(output)
    if any(expected[key] != actual[key] for key in ("timestamps", "durations")):
        raise RuntimeError("滤镜封装改变了原视频显示时间戳")
    return {"timestamps_preserved": True, "source_timing_restored": retimed,
            "variable_frame_rate": len(set(expected["durations"])) > 1,
            "encoding_padding_removed": cropped}


def run_filter(source, output, *, media_kind="video", preview=False, preview_frame=0, preview_time=None,
               options=None, comfy_url="http://127.0.0.1:8188", progress_callback=None, on_submitted=None):
    source, output = Path(source), Path(output)
    info = probe_media(source)
    if media_kind != info["media_kind"]:
        raise ValueError("滤镜素材类型与实际文件不符")
    if preview_time is None and (preview_frame < 0 or preview_frame >= info["frames"]):
        raise ValueError("预览帧超出原素材范围")
    preview_timestamp = None
    if preview and media_kind == "video":
        if preview_time is not None and preview_time >= info["duration"]:
            raise ValueError("预览时间超出原视频范围")
        preview_frame, preview_timestamp = preview_position(source, frame_index=preview_frame, time_seconds=preview_time)
    if media_kind == "video" and info["duration"] > 16.25:
        raise ValueError("视频滤镜目前支持16秒以内的短片")
    settings = filter_options(**(options or {}))
    output_kind = "image" if preview or media_kind == "image" else "video"
    if output.suffix.lower() != (".png" if output_kind == "image" else ".mp4"):
        raise ValueError("滤镜输出文件类型与任务不符")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = progress_callback or (lambda percent, message: None)
    client = pp.PostprocessComfyClient(comfy_url)
    client.session.trust_env = False
    response = client.session.get(client.base_url + "/object_info", timeout=30)
    response.raise_for_status()
    nodes = response.json()
    missing, _ = pp._dlss_runtime_status("dlss_nr")
    if missing:
        raise RuntimeError("DLSS NR运行时未就绪：" + ", ".join(missing))
    input_path = source
    preview_source = None
    if output_kind == "image":
        preview_source = output.parent / "preview_source.png"
        extract_preview(source, preview_source, media_kind, preview_frame)
        input_path = preview_source
    report(10, "提交原尺寸DLSS神经滤镜")
    graph = build_filter_workflow(client.upload(input_path), media_kind=output_kind, options=settings,
                                  filename_prefix=f"studio/dlss_filter/{output.parent.name}", has_audio=False,
                                  source_width=info["width"], source_height=info["height"])
    absent = sorted({node["class_type"] for node in graph.values()} - set(nodes))
    if absent:
        raise RuntimeError("滤镜节点缺失：" + ", ".join(absent))
    (output.parent / "workflow_dlss_filter.json").write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf8")
    rendered = output if output_kind == "image" else output.parent / "filter_render.mp4"
    receipt = client.render(graph, rendered, callback=lambda message: report(50, message), on_submitted=on_submitted)
    expected_native = native_parameters(settings)
    if expected_native is not None and receipt.get("native_parameters") != expected_native:
        raise RuntimeError("滤镜节点未返回匹配的实际原生参数")
    actual = probe_media(rendered)
    render_size = (info["width"] + (info["width"] % 2 if output_kind == "video" else 0),
                   info["height"] + (info["height"] % 2 if output_kind == "video" else 0))
    if (actual["width"], actual["height"]) != render_size:
        raise RuntimeError("DLSS滤镜改变了原图尺寸")
    alpha_preserved = False
    if output_kind == "image" and media_kind == "image":
        with Image.open(preview_source) as original:
            if "A" in original.getbands():
                alpha = original.getchannel("A").copy()
                with Image.open(output) as filtered:
                    preserved = filtered.convert("RGBA")
                preserved.putalpha(alpha)
                preserved.save(output)
                alpha_preserved = True
    timing = {}
    if output_kind == "video":
        if actual["frames"] != info["frames"]:
            raise RuntimeError("DLSS滤镜改变了原视频帧数")
        timing = restore_video_timing(rendered, source, output, has_audio=info["has_audio"])
        actual = probe_media(output)
        if ((actual["width"], actual["height"], actual["frames"]) != (info["width"], info["height"], info["frames"])
                or not timing["variable_frame_rate"] and abs(actual["fps"]-info["fps"]) > .001):
            raise RuntimeError("滤镜封装改变了原尺寸或时间轴")
        if actual["has_audio"] != info["has_audio"]:
            raise RuntimeError("滤镜封装未保留原音轨状态")
    effect_enabled = (not settings["output_blend_enabled"] or settings["output_blend"] > 0
                      if settings.get("controls_version") == 2 else settings["strength"] > 0)
    result = {**actual, "path": str(output), "output_kind": output_kind, "source_media_kind": media_kind,
        "resampling": False, "segments": 1, "preset": "dlss_nr", "filter_options": settings,
        "native_parameters": expected_native,
        "preview": bool(preview), "preview_frame": preview_frame if preview else None,
        "preview_timestamp": preview_timestamp,
        "preview_source": str(preview_source) if preview_source else None, "receipt": receipt,
        "effect_enabled": effect_enabled, "pipeline_version": "same-size-nr-v2" if settings.get("controls_version") == 2 else "same-size-nr-v1",
        "alpha_preserved": alpha_preserved, **timing,
        "processing_padding": {"right": (-info["width"]) % 32, "bottom": (-info["height"]) % 32, "mode": "edge_pixel"},
        "source_info": info, "source": pp.SOURCES["dlss_nr"],
        "implementation": "Community Ada NR bridge; not NVIDIA official RTX40 support; no upscaling or interpolation"}
    (output.parent / "filter_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
    report(100, "已完成")
    return result
