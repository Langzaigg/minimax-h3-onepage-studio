from PIL import Image
import av
import pytest
from services import title_overlay
from services.post_processing import _run_ffmpeg, probe_video


@pytest.mark.parametrize("dimensions,bounds,safe", [
    ((1280,720), [147,413,547,553], [128,72,1152,648]),
    ((1920,1080), [220,620,820,832], [192,108,1728,972]),
    ((2560,1440), [293,827,1093,1109], [256,144,2304,1296]),
])
def test_title_stays_safe_preserves_frames_and_audio(tmp_path, dimensions, bounds, safe):
    source = tmp_path / "source.mp4"
    _run_ffmpeg(["-f", "lavfi", "-i", f"color=c=blue:size={dimensions[0]}x{dimensions[1]}:rate=30", "-f", "lavfi", "-i",
                 "anullsrc=r=48000:cl=stereo", "-t", .5, "-c:v", "libx264", "-c:a", "aac", source])
    title = tmp_path / "title.png"
    def audio_packets(path):
        with av.open(str(path)) as container:
            return [bytes(packet) for packet in container.demux(audio=0) if packet.size]
    def timestamps(path):
        with av.open(str(path)) as container:
            return [frame.pts*frame.time_base for frame in container.decode(video=0)]
    old_audio, old_timestamps = audio_packets(source), timestamps(source)
    Image.new("RGBA", (560, 198), (255, 255, 255, 200)).save(title)
    result = title_overlay.apply_title(source, title, fade=False)
    assert result["title_bounds"] == bounds
    assert result["title_safe_bounds"] == safe
    info = probe_video(source)
    assert info["frames"] == 15
    assert info["has_audio"]
    assert (info["width"], info["height"]) == dimensions
    assert audio_packets(source) == old_audio
    assert timestamps(source) == old_timestamps


def test_720p_fading_title_does_not_add_audio_to_silent_video(tmp_path):
    source, title = tmp_path/"silent.mp4", tmp_path/"title.png"
    _run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:size=1280x720:rate=24", "-t", "0.5", "-c:v", "libx264", source])
    Image.new("RGBA", (560,198), (255,255,255,200)).save(title)
    result = title_overlay.apply_title(source, title, fade=True)
    info = probe_video(source)
    assert info["frames"] == 12 and info["fps"] == 24 and not info["has_audio"]
    assert result["title_fade"]
