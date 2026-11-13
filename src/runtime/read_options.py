"""MCP与CLI共用的文档/评论请求窗口；合法参数由schema生成说明。"""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class DocumentReadOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    page_numbers: list[int] | None = Field(
        default=None, max_length=500, description="原件物理页号，从1开始；不填读全部预算内页面。"
    )
    include_ocr: bool | None = Field(
        default=None, description="false禁止云OCR；true也增强疑似缺正文页；未填只补空文字层。"
    )
    storage_allowed: bool = Field(default=True, description="false禁止持久处理快照；本地读取仍需显式根目录授权。")

    @field_validator("page_numbers")
    @classmethod
    def valid_pages(cls, value):
        if value is not None and (not value or len(value) != len(set(value)) or any(n < 1 for n in value)):
            raise ValueError("page_numbers_must_be_unique_positive_integers")
        return value


class TaskLimitOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    queue_timeout_s: float = Field(default=3600, gt=0, le=604800, description='排队期限，不消耗执行预算。')
    user_wait_timeout_s: float = Field(default=86400, gt=0, le=604800, description='用户交互等待期限；不自动处理验证码。')
    overall_timeout_s: float = Field(default=86400, gt=0, le=604800, description='本次任务绝对授权期限，包含全部阶段。')
    progress_timeout_s: float | None = Field(default=None, ge=1, le=3600, description='显式允许真实完成块进展续期；心跳不续期，始终受overall约束。')


class MediaReadOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    max_frames: int = Field(default=8, ge=1, le=32)
    frame_times_s: list[float] | None = Field(default=None, max_length=32, description='明确疑点的秒位置；未填按全片增量采样。')
    image_max_side: int = Field(default=640, ge=640, le=1920)
    video_height: int = Field(default=480, ge=480, le=1080, description='明确需要细节时提高下载分辨率；下载大小保护不变。')

    @field_validator('frame_times_s')
    @classmethod
    def valid_times(cls, value):
        import math
        if value is not None and (not value or any(not math.isfinite(x) or x < 0 for x in value) or len(set(value)) != len(value)):
            raise ValueError('invalid_frame_times')
        return value


class CommentReadOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    root_limit: int = Field(default=20, ge=1, le=100)
    reply_limit: int = Field(default=10, ge=0, le=100)
    total_limit: int = Field(default=100, ge=1, le=500)
    max_pages: int = Field(default=5, ge=1, le=20)
    order: Literal["relevance", "time"] = "relevance"
    query: str = Field(default="", max_length=500, description="本次研究问题/线索，用于可解释筛选；不是事实可信度。")
    view_limit: int = Field(default=12, ge=1, le=50)
    include_images: bool = True
    include_ocr: bool = False
    include_transcript: bool = False
    media_limit: int = Field(default=20, ge=1, le=100)


def normalized_options(model, value):
    return model.model_validate(value.model_dump() if hasattr(value, "model_dump") else value or {}).model_dump()
