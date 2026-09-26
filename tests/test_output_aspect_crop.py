import pytest

from services import video_generation as video


@pytest.mark.parametrize("mode", ["first_frame", "first_last_frame", "reference_to_video", "hybrid_to_video"])
def test_pixel_grid_is_cropped_before_encoding_without_changing_audio_or_sampling(mode):
    graph = video._workflow(mode, "A quiet scene", 1280, 736, 8, 42)
    video_id = next(key for key, node in graph.items() if node["class_type"] == "CreateVideo")
    previous_images = graph[video_id]["inputs"]["images"]
    previous_audio = graph[video_id]["inputs"].get("audio")
    video._crop_workflow_output(graph, 1280, 736, 1280, 720)
    crop_id = "studio_output_crop_" + video_id
    crop = graph[crop_id]
    assert crop["class_type"] == "ImageCrop"
    assert crop["inputs"] == {"image": previous_images, "width": 1280, "height": 720, "x": 0, "y": 8}
    assert graph[video_id]["inputs"]["images"] == [crop_id, 0]
    assert graph[video_id]["inputs"].get("audio") == previous_audio
    condition = graph["136" if mode == "reference_to_video" else "104"]["inputs"]
    assert (condition["width"], condition["height"]) == (1280, 736)


def test_crop_rejects_enlargement_and_odd_delivery_dimensions():
    for width, height in ((1296, 720), (1280, 737), (1279, 720)):
        with pytest.raises(ValueError):
            video._crop_workflow_output({}, 1280, 736, width, height)


def test_legacy_exact_canvas_needs_no_crop():
    graph = {"91": {"class_type": "CreateVideo", "inputs": {"images": ["10", 0]}}}
    video._crop_workflow_output(graph, 864, 480, 864, 480)
    assert list(graph) == ["91"]


def test_completion_checks_the_actual_file_dimensions(tmp_path):
    import av
    from PIL import Image
    path = tmp_path / "exact.mp4"
    with av.open(str(path), "w") as container:
        stream = container.add_stream("libx264", rate=24)
        stream.width, stream.height, stream.pix_fmt = 128, 72, "yuv420p"
        for packet in stream.encode(av.VideoFrame.from_image(Image.new("RGB", (128, 72)))):
            container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    video._verify_output_dimensions(path, {"output_width": 128, "output_height": 72})
    with pytest.raises(ValueError, match="成品尺寸"):
        video._verify_output_dimensions(path, {"output_width": 128, "output_height": 96})
