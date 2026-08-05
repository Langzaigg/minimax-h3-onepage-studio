from datetime import date

from pydantic import BaseModel, Field


class AccessVerifyRequest(BaseModel):
    secret: str = Field(min_length=1, max_length=512)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    valid_for_hours: int = Field(ge=1, le=87600, description="生效时长，单位小时，最大十年")


class ApiKeyRecord(BaseModel):
    id: str
    name: str
    api_key: str
    created_at: str
    expires_at: str
    active: bool
    last_used_at: str | None = None
    revoked_at: str | None = None


class UsageStatsResponse(BaseModel):
    start_date: date
    end_date: date
    weeks: list[str]
    endpoints: list[str]
    keys: list[dict]
    rows: list[dict]
