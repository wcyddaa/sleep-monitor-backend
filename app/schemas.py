"""Pydantic Schema：API 入参/出参模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field

T = TypeVar("T")


# ============== 通用 ==============
class ApiResponse(BaseModel, Generic[T]):
    """统一响应外壳。"""
    code: int = 0
    message: str = "ok"
    data: T | None = None


# ============== 用户 / 认证 ==============
class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    email: EmailStr | None = None
    full_name: str | None = None
    # 注册时可同时传昵称；缺省时后端回填为 username
    nickname: str | None = Field(default=None, min_length=1, max_length=64)


class UserUpdate(BaseModel):
    """前端 PATCH /auth/me 的入参：所有字段都可选，至少传一个。"""
    nickname: str | None = Field(default=None, min_length=1, max_length=64)
    avatar_url: str | None = Field(default=None, max_length=512)
    full_name: str | None = Field(default=None, min_length=1, max_length=128)
    email: EmailStr | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    email: str | None = None
    full_name: str | None = None
    nickname: str | None = None
    avatar_url: str | None = None
    is_active: bool
    created_at: datetime


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class LoginRequest(BaseModel):
    username: str
    password: str


class WxLoginRequest(BaseModel):
    """微信小程序一键登录：wx.login() 得到的临时 code 换 token。

    携带昵称/头像由前端 chooseAvatar + wx.getUserProfile 拿到（小程序 2022 后已
    不再弹原生授权弹窗，需要用户主动点"使用微信头像/昵称"按钮选）。后端直接
    写入 users.nickname / users.avatar_url。
    """
    code: str
    nickname: str | None = None
    avatar_url: str | None = None


# ============== 用户消息 ==============
class UserMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    type: str
    severity: str
    title: str
    content: str
    ref_device_id: int | None = None
    ref_record_id: int | None = None
    ref_frame_id: int | None = None
    read_at: datetime | None = None
    created_at: datetime


class UserMessageListOut(BaseModel):
    """消息列表 + 未读数 + 全部已读 entry 动作。"""
    items: list[UserMessageOut]
    unread_count: int
    total: int


# ============== 设备 ==============
class DeviceCreate(BaseModel):
    device_id: str = Field(min_length=3, max_length=64)
    name: str
    device_type: str = "sleep_monitor"


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    device_id: str
    name: str
    device_type: str
    is_online: bool
    last_seen: datetime | None = None
    created_at: datetime


class DeviceRuntime(BaseModel):
    """设备实时运行状态（由最近一条记录推断）。"""
    is_online: bool
    last_seen: datetime | None = None
    latest_heart_rate: float | None = None
    latest_respiratory_rate: float | None = None
    latest_bed_status: str | None = None  # in_bed | out_of_bed
    latest_turnover_count: int | None = None  # 最近一次记录的累计翻身次数


class DeviceDetailOut(BaseModel):
    """设备详情 = 基础信息 + 实时状态。"""
    id: int
    device_id: str
    name: str
    device_type: str
    is_online: bool
    last_seen: datetime | None = None
    created_at: datetime
    runtime: DeviceRuntime


class DeviceCommandCreate(BaseModel):
    command: str = Field(min_length=1, max_length=64)
    value: Any | None = None


class UpgradeRequest(BaseModel):
    version: str | None = None


class DeviceCommandOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    device_id: int
    command: str
    value: Any | None = None
    status: str
    created_at: datetime


# ============== 睡眠数据 ==============
class VitalSignSample(BaseModel):
    """单条毫米波雷达样本。"""
    timestamp: datetime
    heart_rate: float = Field(ge=20, le=200)
    respiratory_rate: float = Field(ge=5, le=60)
    bed_status: str = Field(pattern=r"^(in_bed|out_of_bed)$")
    turnover_count: int = Field(ge=0, le=100, default=0)


class AudioSegment(BaseModel):
    """单段音频事件。"""
    start: datetime
    end: datetime
    snore_detected: bool = False
    intensity: float = Field(ge=0.0, le=1.0, default=0.0)


class SleepDataIngest(BaseModel):
    """设备上报的睡眠数据。"""
    device_id: str
    start_time: datetime
    end_time: datetime
    vital_signs: list[VitalSignSample]
    audio_segments: list[AudioSegment] = []


class SleepRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    device_id: int
    start_time: datetime
    end_time: datetime
    sleep_quality_score: float | None = None
    avg_heart_rate: float | None = None
    avg_respiratory_rate: float | None = None
    apnea_risk: str | None = None
    analysis: dict = {}
    report: str | None = None
    created_at: datetime


# ============== 统计聚合 ==============
class StatsOverviewOut(BaseModel):
    total_records: int
    total_sleep_seconds: float
    avg_sleep_seconds: float | None = None
    avg_score: float | None = None
    avg_heart_rate: float | None = None
    avg_respiratory_rate: float | None = None


class TrendPoint(BaseModel):
    date: str
    score: float | None = None
    duration_hours: float | None = None
    avg_heart_rate: float | None = None
    avg_respiratory_rate: float | None = None


class StatsTrendsOut(BaseModel):
    days: int
    points: list[TrendPoint] = []


class StatsStagePoint(BaseModel):
    stage: str
    minutes: float
    ratio: float


class StatsStagesOut(BaseModel):
    records: int
    stages: list[StatsStagePoint] = []


# ============== 报告 ==============
class ReportRequest(BaseModel):
    sleep_record_id: int
    style: str = Field(default="detailed", pattern=r"^(detailed|summary|pediatric)$")


class ReportRangeRequest(BaseModel):
    """按区间生成睡眠报告的入参。

    前端场景：用户选 [start_ts, end_ts)（epoch 秒），调一次。
    区间大小不限；超 7 天时建议拆分多次。
    """
    device_id: int
    start_ts: int = Field(ge=0, description="区间起（epoch 秒，UTC）")
    end_ts: int = Field(ge=0, description="区间止（epoch 秒，UTC）")


class ReportRangeOut(BaseModel):
    """按区间生成报告的出参。

    coverage / summary / degradation 一起返回，前端可画
    「数据完整度环」「缺失段列表」「降级徽章」。
    """
    device_id: int
    start_ts: int
    end_ts: int
    report: str
    frame_count: int
    coverage: dict = {}
    summary: dict = {}
    degradation: dict = {}


# ============== 长期趋势（每日摘要 + 看板）==============
class TrendDailyItem(BaseModel):
    """最近 N 天（按 12:00 切分）的每日摘要。"""
    day_start_ts: int
    day_label: str | None = None
    score: float | None = None
    duration_min: float | None = None
    deep_pct: float | None = None
    light_pct: float | None = None
    rem_pct: float | None = None
    wake_pct: float | None = None
    avg_heart_rate: float | None = None
    avg_respiratory_rate: float | None = None
    snore_minutes: float | None = None
    apnea_risk: str | None = None
    data_completeness: float | None = None
    frame_count: int | None = None
    summary_text: str | None = None


class TrendDashboardOut(BaseModel):
    """睡眠看板：用于 home 顶部昨晚概览 + dashboard 页。"""
    days: int
    sample_count: int
    last_night: dict | None = None
    avg_score: float | None = None
    avg_duration_min: float | None = None
    avg_deep_pct: float | None = None
    avg_snore_minutes: float | None = None
    avg_heart_rate: float | None = None
    avg_respiratory_rate: float | None = None
    score_trend: str = "stable"   # up | down | stable | no_data
    insight: str = ""
    missing_days: int = 0
    # ★ 顺带把 N 天明细一起返回：前端 dashboard 页 4 张图表要用，
    #   不再单独发 /sleep/trend/weekly 请求（避免同一批摘要被算两遍）。
    items: list[TrendDailyItem] = []


class TrendWeeklyOut(BaseModel):
    """最近 N 天每日一行，给 history 页。"""
    days: int
    items: list[TrendDailyItem] = []


# ============== 对话 ==============
class ChatMessage(BaseModel):
    role: str = Field(pattern=r"^(user|assistant|system)$")
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []
    sleep_record_id: int | None = None  # 可选：绑定某条记录作为上下文


class ChatResponse(BaseModel):
    reply: str
    citations: list[str] = []


# ============== 30s 结构化数据帧 ==============
# 注：三开关（snore/stage/bcg）是设备本地状态，走 BLE 读写、不经云端，
#     后端不再存/转发 cfg；当前配置随每一帧的 raw_json 里的 cfg 字段可见。
class SleepFrameOut(BaseModel):
    """30s 一帧结构化数据。字段 -1 = 该端侧模型未启用。"""
    model_config = ConfigDict(from_attributes=True)
    id: int
    device_id: int
    window_start_ts: int
    window_end_ts: int
    stage: int
    heart_rate: int
    resp_rate: int
    motion: int
    snore_seconds: int
    flags: int
    raw_json: str | None = None
    created_at: datetime


class FrameIngestRequest(BaseModel):
    """IoTDA → /ingest/huawei/frame 推送（免登录）。"""
    device_id: str
    start: int
    end: int
    stage: int = -1
    hr: int = -1
    rr: int = -1
    motion: int = -1
    snore_sec: int = -1
    flags: int = 0
    cfg: dict | None = None  # 固件随帧上行的当前三开关状态（只读，不落库）
    raw: dict | None = None  # 可选：原始 30s JSON（IoTDA 转发原 payload）


class FrameAnalysisOut(BaseModel):
    """对最新一帧 30s 结构化数据的 AI 分析结果（不落库）。"""
    frame: SleepFrameOut
    analysis: str
    generated_at: datetime
