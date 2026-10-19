"""应用配置 - 通过环境变量 / .env 文件加载。"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 应用
    app_name: str = "sleep-monitor-backend"
    app_env: str = "dev"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_debug: bool = True

    # 安全
    secret_key: str = "dev-secret-key-please-change-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440

    # 微信小程序一键登录
    wechat_appid: str = "wx7b36e5cc5c54ef2a"
    wechat_secret: str = "25983a5aff085d4da75de63245722344"

    # 数据库
    database_url: str = "sqlite+aiosqlite:///./data/sleep_monitor.db"

    # LLM
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_model: str = "gpt-4o-mini"

    # ===== 数据接入（INGEST_MODE: simulator | http | amqp | mqtt）=====
    ingest_mode: str = "simulator"

    # HTTP webhook（华为云规则引擎 HTTPS 转发到本后端）
    http_webhook_token: str | None = None  # 校验华为云推送的共享令牌；留空则不校验

    # AMQP 推送（华为云 IoTDA AMQP 队列）
    amqp_host: str | None = None
    amqp_port: int = 5671
    amqp_username: str | None = None
    amqp_password: str | None = None
    amqp_queue: str = "sleep-monitor-queue"

    # 直接 MQTT 订阅
    mqtt_broker: str | None = None
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_topic: str = "$oc/devices/+/sys/messages/up"

    # 模拟器（INGEST_MODE=simulator 时生效）：自动生成测试数据
    simulator_interval_seconds: int = 30
    simulator_sleep_hours_min: int = 6
    simulator_sleep_hours_max: int = 8
    simulator_sample_interval_minutes: int = 5
    simulator_heart_rate_mean: float = 65.0
    simulator_heart_rate_std: float = 6.0
    simulator_resp_rate_mean: float = 15.0
    simulator_resp_rate_std: float = 2.0
    simulator_snore_segments_max: int = 4

    @property
    def use_real_llm(self) -> bool:
        return bool(self.openai_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
