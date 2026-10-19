"""SQLAlchemy ORM 模型。

设计要点：
- User / Device / SleepRecord 三张核心表
- SleepRecord 内部存储原始数据（JSON 字符串）+ 分析结果（JSON 字符串）
- 使用 SQLite 时 JSON 字段以 TEXT 存储，跨数据库时迁移到 JSON 列类型
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(128))
    # 昵称（前端显示用，登录时初始化为 username / 微信昵称，可用户改）
    nickname: Mapped[str | None] = mapped_column(String(64))
    # 头像 URL（默认头像或微信头像 URL；存的是 URL 字符串，不存二进制）
    avatar_url: Mapped[str | None] = mapped_column(String(512))
    # 微信 openid（一键登录用，仅当用户通过微信登录时填充）
    wechat_openid: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)
    # 微信 unionid（同一开放平台下多公众号/小程序统一 ID，备用）
    wx_unionid: Mapped[str | None] = mapped_column(String(128), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    devices: Mapped[list["Device"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    sleep_records: Mapped[list["SleepRecord"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    device_type: Mapped[str] = mapped_column(String(32), default="sleep_monitor", nullable=False)
    # owner_id 为空表示「待归属」：设备已上云/有上报，但尚未被某个用户通过配网认领
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    is_online: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 注：三开关（snore/stage/bcg）的**权威**状态在设备本地 NVS（BLE 读写），
    #     不存本表；云端可查镜像见 DeviceCfg（device_cfg 快照表）。
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    owner: Mapped[User] = relationship(back_populates="devices")


class DeviceCfg(Base):
    """设备三开关 cfg 的云端快照（可查镜像，非权威）。

    权威源是设备本地 NVS（BLE 0x04/0x05 读写）；本表只存 IoTDA 上行的
    evt:"cfg" 快照（BLE 切开关时设备补发一次）+ 每帧 cfg 字段兜底刷新的
    最近值，供后端判断"某设备当前是否在监测"——换手机/多设备管理不依赖 BLE。
    断网期间快照可能缺失/过期，设备不补发（过期 cfg 不应覆盖新状态）。
    """

    __tablename__ = "device_cfg"

    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    # {"snore":0|1|2,"stage":0|1|2,"bcg":0|1|2}（OFF=0 / SIM=1 / REAL=2）
    cfg_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    device: Mapped[Device] = relationship()


class DeviceCommand(Base):
    """下发给设备的一条控制指令（pending -> done/failed）。

    云端先把指令落库，华为云侧（设备/规则引擎）通过
    GET /devices/{id}/commands/pending 轮询取走并回写状态。
    """

    __tablename__ = "device_commands"
    __table_args__ = (
        Index("ix_cmd_device_status", "device_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    command: Mapped[str] = mapped_column(String(64), nullable=False)
    # 任意 JSON 参数（如灵敏度数值、固件版本、开关值）
    value: Mapped[dict | None] = mapped_column(JSON)
    # pending | done | failed
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    device: Mapped[Device] = relationship()


class SleepRecord(Base):
    """一条完整的睡眠记录（从设备上报 → 分析 → 报告）。"""

    __tablename__ = "sleep_records"
    __table_args__ = (
        Index("ix_sleep_user_start", "user_id", "start_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # user_id 为空表示该记录来自「待归属」设备（设备已上云但尚未被用户认领），先落库不丢
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)

    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # 设备原始数据（毫米波雷达 + 音频片段摘要）
    raw_data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # DataAgent 分析结果（睡眠分期、呼吸事件、鼾声事件等）
    analysis: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # ReportAgent 生成的报告
    report: Mapped[str | None] = mapped_column(Text)

    # 关键指标列（方便列表查询与排序）
    sleep_quality_score: Mapped[float | None] = mapped_column(Float)
    avg_heart_rate: Mapped[float | None] = mapped_column(Float)
    avg_respiratory_rate: Mapped[float | None] = mapped_column(Float)
    apnea_risk: Mapped[str | None] = mapped_column(String(16))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    user: Mapped[User | None] = relationship(back_populates="sleep_records")
    device: Mapped[Device] = relationship()


__all__ = ["User", "Device", "DeviceCfg", "DeviceCommand", "SleepRecord", "SleepFrame", "UserMessage", "DailyReportSummary"]


class SleepFrame(Base):
    """端侧 30s 一帧的结构化数据（由设备端 EdgeAggregator 发布到 MQTT，
    IoTDA 规则引擎转 HTTP 推到 /ingest/huawei/frame 后写入）。

    字段缺/为 -1 表示该端侧模型未启用（OFF/不可用），前端降级展示。
    """

    __tablename__ = "sleep_frames"
    __table_args__ = (
        Index("ix_frame_device_start", "device_id", "window_start_ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    # 窗口时间戳（秒，自 epoch）
    window_start_ts: Mapped[int] = mapped_column(Integer, nullable=False)
    window_end_ts: Mapped[int] = mapped_column(Integer, nullable=False)

    stage: Mapped[int] = mapped_column(Integer, default=-1, nullable=False)        # -1 / 0=Wake 1=Light 2=Deep
    heart_rate: Mapped[int] = mapped_column(Integer, default=-1, nullable=False)  # bpm
    resp_rate: Mapped[int] = mapped_column(Integer, default=-1, nullable=False)   # 次/分
    motion: Mapped[int] = mapped_column(Integer, default=-1, nullable=False)       # -1 / 0/1
    snore_seconds: Mapped[int] = mapped_column(Integer, default=-1, nullable=False)  # 0-30
    flags: Mapped[int] = mapped_column(Integer, default=0, nullable=False)         # bit0=snore_off, bit1=stage_off, bit2=bcg_off

    # 原始 JSON（IoTDA 上行 payload 的字符串，方便回放/再训练）
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    device: Mapped[Device] = relationship()


class UserMessage(Base):
    """用户通知消息：设备离线 / 重度鼾声 / 呼吸暂停高风险 / 报告完成 / 设备绑定。

    写入点：
      1) ingest 路径上：每帧入库时由 IngestService 钩子按规则判断（重度鼾声 / 设备离线 / 呼吸暂停）
      2) 报告生成完成时：ReportService 写一条 type=report_ready
      3) 设备绑定时：DeviceService.create_command 写一条 type=device_bound
    """

    __tablename__ = "user_messages"
    __table_args__ = (
        Index("ix_msg_user_created", "user_id", "created_at"),
        Index("ix_msg_user_unread", "user_id", "read_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # 类型见模块顶部注释
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    # info | warning | error
    severity: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # 关联资源（任意可选）
    ref_device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    ref_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("sleep_records.id", ondelete="SET NULL"), nullable=True
    )
    ref_frame_id: Mapped[int | None] = mapped_column(
        ForeignKey("sleep_frames.id", ondelete="SET NULL"), nullable=True
    )

    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    user: Mapped[User] = relationship()


class DailyReportSummary(Base):
    """每日报告摘要（长期趋势智能体的输入）。

    一条记录 = 某用户在某 device 上某 [day_start_ts, day_start_ts+86400) 区间的简化报告。
    时间锚点：每天 12:00 切分（即 day_start_ts 是某天 12:00 的 epoch 秒）。
    """

    __tablename__ = "daily_report_summaries"
    __table_args__ = (
        Index("ix_daily_user_device_window", "user_id", "device_id", "day_start_ts", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    day_start_ts: Mapped[int] = mapped_column(Integer, nullable=False)
    day_end_ts: Mapped[int] = mapped_column(Integer, nullable=False)
    frame_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 缺失比例 0-1，1 = 全程无数据
    data_completeness: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # summary_json 是 daily_report_graph 的输出（dict 序列化为 JSON 字符串）
    summary_json: Mapped[str | None] = mapped_column(Text)
    # summary_text 是一句话中文摘要
    summary_text: Mapped[str | None] = mapped_column(String(512))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
