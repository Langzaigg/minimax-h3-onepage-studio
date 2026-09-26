import hashlib

import av
from PIL import Image, ImageChops, ImageStat
import pytest

from services.media_overlay import apply_overlay, probe_media, resolve_geometry, validate_options
from services.post_processing import _run_ffmpeg, probe_video


def video(path, *, color="blue", duration=1, fps="10", audio=False):
    args = ["-f", "lavfi", "-i", f"color=c={color}:size=64x48:rate={fps}"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    args += ["-t", duration, "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac"]
    _run_ffmpeg([*args, path])
    return path


def pixels(path):
    with av.open(str(path)) as container:
        return [frame.to_image() for frame in container.decode(video=0)]


def timestamps(path):
    with av.open(str(path)) as container:
        return [frame.pts * frame.time_base for frame in container.decode(video=0)]


def audio_packets(path):
    with av.open(str(path)) as container:
        return [bytes(packet) for packet in container.demux(audio=0) if packet.size]


def png(path, *, size=(8, 8), color=(255, 0, 0, 255)):
    Image.new("RGBA", size, color).save(path)
    return path


def test_default_no_overlay_copies_base_byte_for_byte(tmp_path):
    source = png(tmp_path / "base.png", size=(20, 16))
    result = apply_overlay(source, None, tmp_path / "copy.png")
    assert result["overlay"] == {"enabled": False}
    assert source.read_bytes() == (tmp_path / "copy.png").read_bytes()


def test_image_alpha_opacity_anchor_and_partial_clipping(tmp_path):
    base = png(tmp_path / "base.png", size=(20, 16), color=(0, 0, 255, 255))
    layer = png(tmp_path / "layer.png", color=(255, 0, 0, 128))
    out = tmp_path / "out.png"
    result = apply_overlay(base, layer, out, anchor="bottom-right", offset_x=2,
                           width_percent=40, opacity=.5)
    assert result["overlay"]["bounds"] == [14, 8, 22, 16]
    assert result["overlay"]["visible_bounds"] == [14, 8, 20, 16]
    with Image.open(out) as im:
        assert im.size == (20, 16)
        assert im.getpixel((0, 0)) == (0, 0, 255, 255)
        assert im.getpixel((19, 15)) == (64, 0, 191, 255)
    result = apply_overlay(base, layer, out, x=-4, y=-4)
    with Image.open(out) as im:
        assert im.getpixel((0, 0)) == (128, 0, 127, 255)
        assert im.getpixel((4, 4)) == (0, 0, 255, 255)


def test_transparent_base_retains_alpha_and_webp_jpeg_inputs(tmp_path):
    base = png(tmp_path / "base.png", size=(20, 16), color=(0, 0, 0, 0))
    layer = png(tmp_path / "layer.png", color=(255, 0, 0, 128))
    out = tmp_path / "out.webp"
    apply_overlay(base, layer, out, opacity=.5)
    with Image.open(out) as im:
        assert im.convert("RGBA").getpixel((2, 2)) == (255, 0, 0, 64)
    jpeg = tmp_path / "layer.jpg"
    Image.new("RGB", (3, 6), "green").save(jpeg)
    result = apply_overlay(base, jpeg, tmp_path / "jpeg-base.png", height=8)
    assert (result["overlay"]["width"], result["overlay"]["height"]) == (4, 8)


@pytest.mark.parametrize("options", [
    {"opacity": float("nan")}, {"opacity": 1.1}, {"width": -1},
    {"x": 1}, {"x": 1.5, "y": 0}, {"anchor": "unknown"},
    {"start": 1, "end": 1}, {"width_percent": 0},
    {"width_percent": 50, "width": 10}, {"preview_time": -1},
    {"overlay_frame_time": -1}, {"loop": "true"},
])
def test_invalid_options_rejected_before_io(options):
    with pytest.raises(ValueError):
        validate_options(**options)


def test_pure_geometry_retains_aspect_and_all_anchor_contract():
    assert resolve_geometry((100, 80), (10, 20), width=20)["bounds"] == [0, 0, 20, 40]
    assert resolve_geometry((100, 80), (10, 20), anchor="center")["bounds"] == [45, 30, 55, 50]
    assert resolve_geometry((100, 80), (10, 20), anchor="bottom-center", offset_y=-3)["bounds"] == [45, 57, 55, 77]
    assert resolve_geometry((100, 80), (10, 20), x=-2, y=5, anchor="bottom-right", offset_x=100)["bounds"] == [-2, 5, 8, 25]


def test_video_png_preserves_fractional_pts_framecount_and_original_audio(tmp_path):
    base = video(tmp_path / "base.mp4", fps="30000/1001", duration=.7, audio=True)
    layer = png(tmp_path / "layer.png", size=(16, 16), color=(255, 0, 0, 128))
    out = tmp_path / "out.mp4"
    old_bytes = hashlib.sha256(base.read_bytes()).hexdigest()
    result = apply_overlay(base, layer, out, x=8, y=8, opacity=.5, start=.1, end=.4)
    assert timestamps(base) == timestamps(out)
    assert audio_packets(base) == audio_packets(out)
    assert hashlib.sha256(base.read_bytes()).hexdigest() == old_bytes
    assert result["timing_verified"] and result["audio_packets_preserved"]
    before, after = pixels(base), pixels(out)
    assert len(before) == len(after)
    assert sum(ImageStat.Stat(ImageChops.difference(before[0], after[0])).mean) / 3 < 4
    middle = after[6].getpixel((12, 12))
    assert 45 < middle[0] < 85 and 165 < middle[2] < 215
    assert after[-1].getpixel((12, 12))[2] > 230


@pytest.mark.parametrize("loop", [False, True])
def test_video_overlay_timing_loop_and_never_adds_material_audio(tmp_path, loop):
    base = video(tmp_path / "base.mp4", duration=1.2)
    layer = video(tmp_path / "layer.mp4", color="red", duration=.3, audio=True)
    out = tmp_path / "out.mp4"
    result = apply_overlay(base, layer, out, width=16, x=8, y=8, start=.2, end=.9, loop=loop)
    p = pixels(out)
    assert len(p) == 12 and timestamps(base) == timestamps(out)
    assert not result["has_audio"]
    assert p[1].getpixel((10, 10))[2] > 230
    assert p[3].getpixel((10, 10))[0] > 230
    assert (p[6].getpixel((10, 10))[0] > 230) == loop
    assert p[9].getpixel((10, 10))[2] > 230


def test_video_on_still_frame_metadata_and_preview_interval(tmp_path):
    base = png(tmp_path / "base.png", size=(64, 48), color=(0, 0, 255, 255))
    layer = video(tmp_path / "layer.mp4", color="red", duration=.3)
    out = tmp_path / "out.png"
    result = apply_overlay(base, layer, out, width=16, overlay_frame_time=.1)
    assert result["overlay"]["video_frame_extracted"]
    assert result["overlay"]["frame_time"] == .1
    with Image.open(out) as im:
        assert im.getpixel((3, 3))[0] > 230
    before = apply_overlay(base, layer, out, start=2, end=3, preview_time=1)
    assert not before["overlay"]["applied"]
    with Image.open(out) as im:
        assert im.getpixel((3, 3)) == (0, 0, 255, 255)
    exhausted = apply_overlay(base, layer, out, start=2, end=3, preview_time=2.7)
    assert not exhausted["overlay"]["applied"]
    looping = apply_overlay(base, layer, out, start=2, end=3, preview_time=2.7, loop=True)
    assert looping["overlay"]["applied"]
    assert looping["overlay"]["frame_time"] == pytest.approx(.1)


def test_video_webm_alpha_is_composited_not_black_background(tmp_path):
    base = video(tmp_path / "base.mp4", duration=.5)
    transparent = png(tmp_path / "transparent.png", size=(16, 16), color=(255, 0, 0, 128))
    layer = tmp_path / "alpha.webm"
    _run_ffmpeg(["-loop", 1, "-framerate", 10, "-i", transparent, "-t", .5,
                 "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", 0, layer])
    assert probe_media(layer)["alpha_decoder"] == "libvpx-vp9"
    out = tmp_path / "out.mp4"
    apply_overlay(base, layer, out, opacity=.5, x=8, y=8)
    center = pixels(out)[1].getpixel((12, 12))
    assert 45 < center[0] < 85 and 165 < center[2] < 215


@pytest.mark.parametrize("timestamp_offset", [0, 2])
def test_video_variable_rate_and_nonzero_start_pts_are_preserved(tmp_path, timestamp_offset):
    base = tmp_path / "base.mp4"
    _run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=64x48:rate=10:duration=1",
                 "-vf", "select='not(eq(n,3)+eq(n,4)+eq(n,7))'", "-fps_mode", "vfr",
                 "-output_ts_offset", timestamp_offset, "-c:v", "libx264", base])
    layer = png(tmp_path / "layer.png")
    out = tmp_path / "out.mp4"
    result = apply_overlay(base, layer, out, start=.1, end=.5)
    assert timestamps(base) == timestamps(out)
    assert result["frames"] == 7


def test_verification_failure_keeps_source_and_existing_destination(tmp_path, monkeypatch):
    from services import media_overlay
    base = video(tmp_path / "base.mp4", duration=.2)
    layer = png(tmp_path / "layer.png")
    out = tmp_path / "out.mp4"
    out.write_bytes(b"previous finished output")
    before = base.read_bytes()
    def fail(*args):
        raise RuntimeError("bad timing")
    monkeypatch.setattr(media_overlay, "_verify_video", fail)
    with pytest.raises(RuntimeError, match="bad timing"):
        apply_overlay(base, layer, out)
    assert base.read_bytes() == before
    assert out.read_bytes() == b"previous finished output"
    assert not list(tmp_path.glob(".media-overlay-*"))
