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
    nsfw_strength: float | None = None


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
