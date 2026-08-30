from typing import Literal

from pydantic import BaseModel, Field


VideoMode = Literal[
    "text_to_video", "first_frame", "last_frame", "first_last_frame", "reference_to_video"
]


class VideoTaskSummary(BaseModel):
    id: str
    mode: VideoMode
    status: Literal["polishing", "preparing", "queued", "running", "completed", "failed"]
    prompt: str
    original_prompt: str | None = None
    polished_prompt: str | None = None
    duration: float
    width: int
    height: int
    resolution_megapixels: float = 0.4
    resolution_preset: str | None = Field(
        default=None, description="turbo 分辨率档位 id（如 turbo-0.5-6）；普通档位为空"
    )
    turbo_steps: int | None = Field(default=None, description="turbo 蒸馏采样步数（4/6/8）")
    aspect_ratio: str = "16:9"
    seed: int
    progress: float = Field(ge=0, le=100)
    queue_position: int | None = None
    current_stage: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None
    assets: dict[str, str] = Field(default_factory=dict)
    video_filename: str | None = None
    warnings: list[str] = Field(default_factory=list)
    references: list[dict] = Field(default_factory=list)
    reference_counts: dict[str, int] = Field(default_factory=dict)
    ref_image_size: str | None = None
    use_video_audio: bool | None = None
    nsfw: bool | None = None
    nsfw_preset: str | None = Field(
        default=None, description="NSFW 档位 id（如 aio / aio-vagina）；未开启为空"
    )
    # 历史字段：NSFW 曾用强度/原生开关，现统一为 nsfw 布尔（读取时历史值回落为 true）。
    nsfw_strength: float | None = None
    nsfw_native: bool | None = None


class VideoTaskList(BaseModel):
    tasks: list[VideoTaskSummary]


class VideoQueueStatus(BaseModel):
    running: int
    pending: int
    tasks: list[dict]


class PromptPolishResponse(BaseModel):
    prompt: str
    model: str
    mode: VideoMode
