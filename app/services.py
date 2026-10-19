"""业务服务层。

- AuthService：注册 / 登录
- DeviceService：设备 CRUD
- SleepService：数据接入、触发 DataAgent + ReportAgent，查询记录
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import chat_graph, data_graph, report_graph, report_range_graph
# _utcnow：models 里列默认值用的 UTC 取时函数，这里写入 last_seen 时复用，
#          避免两处各写一份导致语义漂移。
#          （此前 services.py 直接调 _utcnow() 却没导入 → NameError → webhook 500）
from app.models import Device, DeviceCommand, SleepRecord, User, _utcnow
from app.schemas import (
    ChatRequest,
    DeviceCreate,
    DeviceRuntime,
    SleepDataIngest,
    UserCreate,
    UserUpdate,
)
from app.security import create_access_token, hash_password, verify_password

logger = logging.getLogger(__name__)


# ============== Auth ==============
class AuthService:
    @staticmethod
    async def register(db: AsyncSession, payload: UserCreate) -> User:
        existing = await db.execute(select(User).where(User.username == payload.username))
        if existing.scalar_one_or_none():
            raise ValueError("用户名已存在")
        if payload.email:
            existing_email = await db.execute(select(User).where(User.email == payload.email))
            if existing_email.scalar_one_or_none():
                raise ValueError("邮箱已被注册")
        user = User(
            username=payload.username,
            email=payload.email,
            full_name=payload.full_name,
            nickname=payload.nickname or payload.username,
            hashed_password=hash_password(payload.password),
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user

    @staticmethod
    async def update_profile(db: AsyncSession, user: User, payload: UserUpdate) -> User:
        """PATCH /auth/me：部分字段更新（至少一个字段非空才生效）。

        - nickname / avatar_url / full_name / email 任意一个非空就更新
        - email 唯一性需要二次校验
        """
        has_change = False
        if payload.nickname is not None and payload.nickname != user.nickname:
            user.nickname = payload.nickname
            has_change = True
        if payload.avatar_url is not None and payload.avatar_url != user.avatar_url:
            user.avatar_url = payload.avatar_url
            has_change = True
        if payload.full_name is not None and payload.full_name != user.full_name:
            user.full_name = payload.full_name
            has_change = True
        if payload.email is not None and payload.email != user.email:
            # 邮箱唯一性校验
            if payload.email:
                exists = await db.execute(
                    select(User).where(User.email == payload.email, User.id != user.id)
                )
                if exists.scalar_one_or_none() is not None:
                    raise ValueError("邮箱已被其他账号使用")
            user.email = payload.email
            has_change = True

        if not has_change:
            # 即使没变化也返回刷新后的 user，避免前端缓存不一致
            await db.refresh(user)
            return user

        await db.commit()
        await db.refresh(user)
        return user

    @staticmethod
    async def authenticate(db: AsyncSession, username: str, password: str) -> User:
        result = await db.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()
        if user is None or not verify_password(password, user.hashed_password):
            raise ValueError("用户名或密码错误")
        if not user.is_active:
            raise ValueError("账号已停用")
        return user

    @staticmethod
    def issue_token(user: User) -> str:
        return create_access_token(user.id, extra={"username": user.username})

    @staticmethod
    async def login_by_wx(
        db: AsyncSession,
        code: str,
        nickname: str | None = None,
        avatar_url: str | None = None,
    ) -> User:
        """微信小程序一键登录：code -> openid -> 建/查用户 -> 签发 token。

        需要配置 wechat_appid / wechat_secret；未配置时抛出 ValueError。
        nickname / avatar_url 由前端 chooseAvatar 拿到，传进来后写库
        （首次注册时回填、后续每次也更新，让用户改微信头像能跟着同步）。
        """
        from app.config import settings
        if not (settings.wechat_appid and settings.wechat_secret):
            raise ValueError("后端未配置微信小程序 appid/secret")

        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.weixin.qq.com/sns/jscode2session",
                params={
                    "appid": settings.wechat_appid,
                    "secret": settings.wechat_secret,
                    "js_code": code,
                    "grant_type": "authorization_code",
                },
            )
            payload = resp.json()

        errcode = payload.get("errcode")
        if errcode or "openid" not in payload:
            raise ValueError(
                f"微信登录失败: {payload.get('errmsg') or payload.get('errcode')}"
            )

        openid: str = payload["openid"]
        # 微信可能也返回 unionid（同主体多小程序/公众号用）
        unionid: str | None = payload.get("unionid")

        user = (
            await db.execute(select(User).where(User.wechat_openid == openid))
        ).scalar_one_or_none()
        if user is not None:
            if not user.is_active:
                raise ValueError("账号已停用")
            # 每次登录都同步最新的微信昵称/头像（用户可能换过头像）
            if nickname:
                user.nickname = nickname
            if avatar_url:
                user.avatar_url = avatar_url
            if unionid and not user.wx_unionid:
                user.wx_unionid = unionid
            await db.commit()
            await db.refresh(user)
            return user

        # 首次登录：以微信 openid 创建新用户
        user = User(
            username=f"wx_{openid[-12:]}",
            wechat_openid=openid,
            wx_unionid=unionid,
            full_name="微信用户",
            nickname=nickname or "微信用户",
            avatar_url=avatar_url,
            hashed_password=hash_password(openid),  # 占位密码，禁止密码登录使用
        )
        db.add(user)
        try:
            await db.commit()
        except Exception:  # 并发下用户名/ openid 冲突则回滚后重查
            await db.rollback()
            user = (
                await db.execute(select(User).where(User.wechat_openid == openid))
            ).scalar_one_or_none()
            if user is None:
                raise
        await db.refresh(user)
        return user


# ============== Device ==============
class DeviceService:
    @staticmethod
    async def create(db: AsyncSession, owner: User, payload: DeviceCreate) -> Device:
        existing = await DeviceService.get_by_device_id(db, payload.device_id)
        if existing is not None:
            # 已存在：若为「待归属」设备（无 owner）则认领归属；否则拒绝
            if existing.owner_id is not None and existing.owner_id != owner.id:
                raise ValueError("设备 ID 已存在")
            existing.owner_id = owner.id
            existing.name = payload.name
            existing.device_type = payload.device_type
            await db.commit()
            await db.refresh(existing)
            return existing
        device = Device(
            device_id=payload.device_id,
            name=payload.name,
            device_type=payload.device_type,
            owner_id=owner.id,
        )
        db.add(device)
        await db.commit()
        await db.refresh(device)
        return device

    @staticmethod
    async def get_or_create_webhook(
        db: AsyncSession, device_id: str, name: str | None = None
    ) -> tuple[Device, bool]:
        """华为云侧按 device_id 自动建设备（A）。

        - 已存在：直接返回。
        - 不存在：新建一条「待归属」设备记录（owner_id 为空），
          返回 (device, created=True) 供后端回执确认。
        """
        device = await DeviceService.get_by_device_id(db, device_id)
        if device is not None:
            return device, False
        device = Device(
            device_id=device_id,
            name=name or f"SleepPillow-{device_id[-6:]}",
            device_type="sleep_monitor",
            owner_id=None,
            is_online=True,
        )
        db.add(device)
        await db.commit()
        await db.refresh(device)
        return device, True

    @staticmethod
    async def list_for_user(
        db: AsyncSession, owner: User, online_only: bool = False
    ) -> list[Device]:
        """列出当前用户名下的设备。

        online_only=True 时只返回 is_online=True 的（前端控制页 / 实时区使用）。
        """
        stmt = select(Device).where(Device.owner_id == owner.id)
        if online_only:
            stmt = stmt.where(Device.is_online.is_(True))
        stmt = stmt.order_by(Device.id.desc())
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def get_by_device_id(db: AsyncSession, device_id: str) -> Device | None:
        result = await db.execute(select(Device).where(Device.device_id == device_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def get(db: AsyncSession, device_pk: int, owner: User) -> Device:
        device = await db.get(Device, device_pk)
        if device is None:
            raise LookupError("设备不存在")
        if device.owner_id != owner.id:
            raise PermissionError("无权访问该设备")
        return device

    @staticmethod
    async def get_runtime(db: AsyncSession, device: Device) -> DeviceRuntime:
        """依据最近一条睡眠记录推断实时状态。"""
        latest = (
            await db.execute(
                select(SleepRecord)
                .where(SleepRecord.device_id == device.id)
                .order_by(SleepRecord.end_time.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        latest_bed_status: str | None = None
        latest_turnover_count: int | None = None
        if latest and latest.raw_data:
            vs = latest.raw_data.get("vital_signs") or []
            if vs:
                latest_bed_status = vs[-1].get("bed_status")
            # 翻身次数：优先用分析特征，否则累加原始样本
            feats = (latest.analysis or {}).get("features") or {}
            latest_turnover_count = feats.get("total_turnovers")
            if latest_turnover_count is None:
                latest_turnover_count = sum(
                    int(v.get("turnover_count") or 0) for v in vs
                )

        return DeviceRuntime(
            is_online=device.is_online,
            last_seen=device.last_seen,
            latest_heart_rate=latest.avg_heart_rate if latest else None,
            latest_respiratory_rate=latest.avg_respiratory_rate if latest else None,
            latest_bed_status=latest_bed_status,
            latest_turnover_count=latest_turnover_count,
        )

    @staticmethod
    async def create_command(
        db: AsyncSession, owner: User, device_pk: int, command: str, value: Any | None
    ) -> DeviceCommand:
        device = await DeviceService.get(db, device_pk, owner)
        cmd = DeviceCommand(device_id=device.id, command=command, value=value)
        db.add(cmd)
        await db.commit()
        await db.refresh(cmd)
        return cmd

    @staticmethod
    async def list_commands(
        db: AsyncSession, owner: User, device_pk: int, limit: int = 50
    ) -> list[DeviceCommand]:
        device = await DeviceService.get(db, device_pk, owner)
        result = await db.execute(
            select(DeviceCommand)
            .where(DeviceCommand.device_id == device.id)
            .order_by(DeviceCommand.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def list_pending_commands(
        db: AsyncSession, device: Device, limit: int = 20
    ) -> list[DeviceCommand]:
        """供设备侧/华为云轮询的待执行指令（按 device_id，无需登录归属校验）。"""
        result = await db.execute(
            select(DeviceCommand)
            .where(
                DeviceCommand.device_id == device.id,
                DeviceCommand.status == "pending",
            )
            .order_by(DeviceCommand.id.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def ack_command(
        db: AsyncSession, device: Device, command_id: int, status: str, error: str | None = None
    ) -> DeviceCommand | None:
        cmd = await db.get(DeviceCommand, command_id)
        if cmd is None or cmd.device_id != device.id:
            return None
        cmd.status = "failed" if error else status
        cmd.value = (cmd.value or {}) | {"error": error} if error else cmd.value
        await db.commit()
        await db.refresh(cmd)
        return cmd

    @staticmethod
    async def delete(db: AsyncSession, owner: User, device_pk: int) -> None:
        device = await DeviceService.get(db, device_pk, owner)
        await db.delete(device)  # 级联删除其指令与睡眠记录
        await db.commit()

    # ============== 30s frame / 上线报到 ==============
    # 注：三开关是设备本地状态（BLE 读写、不经云端），后端不存 cfg。
    @staticmethod
    async def register_online(db: AsyncSession, device_id: str) -> Device:
        """设备上线报到：自动注册（若不存在）+ 置在线。

        三开关全 OFF 时设备不发任何帧，若没有这条报到，
        首次配网的设备永远不会进 devices 表，小程序 bind 会失败。
        """
        device, _ = await DeviceService.get_or_create_webhook(db, device_id)
        device.last_seen = _utcnow()
        device.is_online = True
        await db.commit()
        await db.refresh(device)
        return device

    @staticmethod
    async def get_latest_frame(db: AsyncSession, device_pk: int, owner: User):
        """取设备最近一帧 30s 结构化数据（前端详情页「设备原始上传」用）。"""
        device = await DeviceService.get(db, device_pk, owner)
        from app.models import SleepFrame
        result = await db.execute(
            select(SleepFrame)
            .where(SleepFrame.device_id == device.id)
            .order_by(SleepFrame.window_end_ts.desc())
            .limit(1)
        )
        return result.scalars().first()

    @staticmethod
    async def analyze_latest_frame(db: AsyncSession, device_pk: int, owner: User) -> dict:
        """取设备最新一帧 30s 结构化数据，交给 LLM 做「这一帧」的分析。

        - 不落库：结果只回给前端展示，不写 sleep_records（避免污染报告列表）
        - LLM 失败时用规则兜底，保证前端不空白
        """
        from app.agents.frame_analysis import analyze_frame, fallback_frame_analysis
        from app.models import SleepFrame  # noqa: F401  (保持与 get_latest_frame 一致)

        frame = await DeviceService.get_latest_frame(db, device_pk, owner)
        if frame is None:
            raise LookupError("该设备暂无 30 秒数据帧（设备需先开启至少一个端侧开关）")
        try:
            text = await analyze_frame(frame)
            if not text:
                text = fallback_frame_analysis(frame)
        except Exception as e:  # noqa: BLE001
            logger.warning("帧 AI 分析失败，改用规则兜底: %s", e)
            text = fallback_frame_analysis(frame)
        return {"frame": frame, "analysis": text}

    @staticmethod
    async def write_frame_from_webhook(
        db: AsyncSession, device_id: str, payload: dict
    ):
        """IoTDA 规则引擎推送的 30s 帧：自动注册设备 + 写一行 sleep_frames。"""
        device, _ = await DeviceService.get_or_create_webhook(db, device_id)
        from app.models import SleepFrame
        frame = SleepFrame(
            device_id=device.id,
            window_start_ts=int(payload.get("start", 0)),
            window_end_ts=int(payload.get("end", 0)),
            stage=int(payload.get("stage", -1)),
            heart_rate=int(payload.get("hr", -1)),
            resp_rate=int(payload.get("rr", -1)),
            motion=int(payload.get("motion", -1)),
            snore_seconds=int(payload.get("snore_sec", -1)),
            flags=int(payload.get("flags", 0)),
            raw_json=json.dumps(payload, ensure_ascii=False),
        )
        db.add(frame)
        device.last_seen = _utcnow()
        device.is_online = True
        await db.commit()

        # ★ 事件源：重度鼾声（单帧 snore_sec >= 25）
        snore_sec = int(payload.get("snore_sec", -1))
        if snore_sec >= 25 and device.owner_id is not None:
            try:
                await MessageService.create_snore_heavy(
                    db, device.owner_id, device.id, frame.id, snore_sec
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("重度鼾声事件源失败: %s", e)

        return frame

    @staticmethod
    async def bind(
        db: AsyncSession, owner: User, device_id: str, name: str | None = None
    ) -> Device:
        """把已存在的「待归属」设备认领到当前用户（用户重配网时调用）。"""
        device = await DeviceService.get_by_device_id(db, device_id)
        if device is None:
            raise LookupError(f"设备 {device_id} 不存在")
        if device.owner_id is not None and device.owner_id != owner.id:
            raise PermissionError("设备已被其他用户绑定")
        is_first_bind = device.owner_id is None
        device.owner_id = owner.id
        if name:
            device.name = name
        await db.commit()
        await db.refresh(device)
        # ★ 事件源：设备首次绑定通知
        if is_first_bind:
            try:
                await MessageService.create_device_bound(
                    db, owner.id, device.id, device.name
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("设备绑定通知失败: %s", e)
        return device


# ============== Sleep ==============
class SleepService:
    @staticmethod
    async def ingest(
        db: AsyncSession, owner: User | None, payload: SleepDataIngest
    ) -> SleepRecord:
        device = await DeviceService.get_by_device_id(db, payload.device_id)
        if device is None:
            raise ValueError(f"设备 {payload.device_id} 未注册")
        if owner is None:
            # 待归属设备：允许落库，user_id 置空；要求设备本身未归属
            if device.owner_id is not None:
                raise PermissionError("该设备不属于当前用户")
        elif device.owner_id != owner.id:
            raise PermissionError("该设备不属于当前用户")

        raw = {
            "start_time": payload.start_time.isoformat(),
            "end_time": payload.end_time.isoformat(),
            "vital_signs": [vs.model_dump(mode="json") for vs in payload.vital_signs],
            "audio_segments": [a.model_dump(mode="json") for a in payload.audio_segments],
        }

        record = SleepRecord(
            user_id=owner.id if owner else None,
            device_id=device.id,
            start_time=payload.start_time,
            end_time=payload.end_time,
            raw_data=raw,
            analysis={},
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)
        return record

    @staticmethod
    async def analyze(db: AsyncSession, record_id: int, owner: User) -> SleepRecord:
        record = await SleepService._get_owned(db, record_id, owner)
        # 1) DataAgent
        result = await data_graph.ainvoke(
            {"sleep_record_id": record.id, "raw_data": record.raw_data}
        )
        record.analysis = result.get("analysis", {}) or {}
        feats = (record.analysis.get("features") or {})
        record.sleep_quality_score = record.analysis.get("sleep_quality_score")
        record.avg_heart_rate = feats.get("avg_heart_rate")
        record.avg_respiratory_rate = feats.get("avg_respiratory_rate")
        record.apnea_risk = record.analysis.get("apnea_risk")
        await db.commit()
        await db.refresh(record)
        return record

    @staticmethod
    async def generate_report(
        db: AsyncSession, record_id: int, owner: User, style: str = "detailed"
    ) -> SleepRecord:
        record = await SleepService._get_owned(db, record_id, owner)
        if not record.analysis:
            record = await SleepService.analyze(db, record.id, owner)
        result = await report_graph.ainvoke(
            {
                "sleep_record_id": record.id,
                "user_id": owner.id,
                "style": style,
                "analysis": record.analysis,
                "user_profile": {
                    "username": owner.username,
                    "full_name": owner.full_name,
                },
            }
        )
        record.report = result.get("report") or ""
        await db.commit()
        await db.refresh(record)
        return record

    @staticmethod
    async def prepare_range_report(
        db: AsyncSession,
        owner: User,
        device_id: int,
        start_ts: int,
        end_ts: int,
    ) -> dict[str, Any]:
        """★ 流式接口专用：只做「查帧 + 统计 + 降级判定」，不调 LLM。

        拆出来是为了让 SSE 接口能在响应开始前把 DB 活干完 —— 一旦
        StreamingResponse 开始推送，FastAPI 的 db 依赖就会关闭 session，
        生成器里再查库会炸。LLM 那部分交给生成器内部 astream。
        """
        from app.agents.report_range_graph import (
            assess_degradation,
            compute_coverage,
            compute_summary,
        )
        from app.models import Device, SleepFrame
        from sqlalchemy import select

        dev_row = await db.execute(select(Device).where(Device.id == device_id))
        device = dev_row.scalar_one_or_none()
        if device is None:
            raise LookupError(f"设备 {device_id} 不存在")
        if device.owner_id != owner.id:
            raise PermissionError("该设备不属于当前用户")

        # ★ 同上：只取统计/覆盖分析需要的列，不拉 raw_json 大字段。
        #   TiDB 在东京、FC 在上海，少传几 MB 就是少等几十秒。
        rows = (
            await db.execute(
                select(
                    SleepFrame.id,
                    SleepFrame.device_id,
                    SleepFrame.window_start_ts,
                    SleepFrame.window_end_ts,
                    SleepFrame.stage,
                    SleepFrame.heart_rate,
                    SleepFrame.resp_rate,
                    SleepFrame.motion,
                    SleepFrame.snore_seconds,
                    SleepFrame.flags,
                )
                .where(
                    SleepFrame.device_id == device_id,
                    SleepFrame.window_start_ts >= start_ts,
                    SleepFrame.window_start_ts < end_ts,
                )
                .order_by(SleepFrame.window_start_ts.asc())
            )
        ).all()

        frames = [
            {
                "id": f.id,
                "device_id": f.device_id,
                "window_start_ts": f.window_start_ts,
                "window_end_ts": f.window_end_ts,
                "stage": f.stage,
                "heart_rate": f.heart_rate,
                "resp_rate": f.resp_rate,
                "motion": f.motion,
                "snore_seconds": f.snore_seconds,
                "flags": f.flags,
            }
            for f in rows
        ]

        coverage = compute_coverage(frames, start_ts, end_ts)
        summary = compute_summary(frames)
        degradation = assess_degradation(coverage, summary)

        return {
            "device_id": device_id,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "frames": frames,
            "coverage": coverage,
            "summary": summary,
            "degradation": degradation,
        }

    @staticmethod
    async def generate_range_report(
        db: AsyncSession,
        owner: User,
        device_id: int,
        start_ts: int,
        end_ts: int,
    ) -> dict[str, Any]:
        """按用户指定时间区间生成睡眠报告。

        独立智能体（report_range_graph），与基于 sleep_records 的 ReportAgent 不复用：
        用户场景："25 号 12:00 到 26 号 12:00 这段时间的报告"。
        - 直接消费 sleep_frames 区间
        - 输出 report + coverage + summary + degradation，不落库
        - 降级 4 档：full / partial / insufficient / failed
        """
        from app.agents import report_range_graph

        prep = await SleepService.prepare_range_report(
            db, owner, device_id, start_ts, end_ts
        )
        frames = prep["frames"]
        coverage = prep["coverage"]
        summary = prep["summary"]
        degradation = prep["degradation"]

        # 跑 report_range_graph（纯 LLM 生成报告 + 降级兜底）
        result = await report_range_graph.ainvoke(
            {
                "user_id": owner.id,
                "device_id": device_id,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "frames": frames,
                "coverage": coverage,
                "summary": summary,
                "degradation": degradation,
            }
        )
        report_text = result.get("report") or ""

        # ★ 事件源：报告完成通知
        try:
            await MessageService.create_report_ready(
                db, owner.id, device_id, start_ts, end_ts
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("报告完成通知失败: %s", e)

        return {
            "device_id": device_id,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "report": report_text,
            "coverage": coverage,
            "summary": summary,
            "degradation": degradation,
            "frame_count": len(frames),
        }

    @staticmethod
    async def list_for_user(
        db: AsyncSession, owner: User, limit: int = 20, offset: int = 0
    ) -> list[SleepRecord]:
        result = await db.execute(
            select(SleepRecord)
            .where(SleepRecord.user_id == owner.id)
            .order_by(SleepRecord.start_time.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    # ============================================================
    # 每日摘要 + 长期趋势（on-demand，不依赖 APScheduler）
    # ============================================================
    @staticmethod
    async def get_or_compute_daily_summaries(
        db: AsyncSession,
        owner: User,
        device_id: int,
        days: int = 7,
    ) -> list[dict]:
        """取最近 N 天（每天 12:00 切分）的每日报告摘要。

        On-demand：
          - 查 daily_report_summaries 表
          - 缺哪条 → 拉 sleep_frames 重算 daily_report_graph 补一条
          - 全在同一次请求里补齐，返回 dict 列表

        时间锚点：day_start_ts 是某天 12:00 的 epoch 秒（UTC）
        """
        from datetime import datetime, timezone, timedelta
        from sqlalchemy import select
        from app.models import DailyReportSummary, SleepFrame, Device

        # 设备归属校验
        dev_row = await db.execute(select(Device).where(Device.id == device_id))
        device = dev_row.scalar_one_or_none()
        if device is None:
            raise LookupError(f"设备 {device_id} 不存在")
        if device.owner_id != owner.id:
            raise PermissionError("该设备不属于当前用户")

        # 锚定：当前时间往前推 N 天，12:00 切分
        now = datetime.now(tz=timezone.utc)
        today_noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
        if now < today_noon:
            # 12 点前用"昨天 12 点"作为区间 0
            today_noon = today_noon - timedelta(days=1)
        end_of_today = today_noon + timedelta(days=1)  # 区间 0 的 day_end
        day_starts = [int((today_noon - timedelta(days=i)).timestamp()) for i in range(days)]

        # 查已有
        existing = (
            await db.execute(
                select(DailyReportSummary)
                .where(
                    DailyReportSummary.user_id == owner.id,
                    DailyReportSummary.device_id == device_id,
                    DailyReportSummary.day_start_ts.in_(day_starts),
                )
            )
        ).scalars().all()
        existing_map = {r.day_start_ts: r for r in existing}

        from app.agents.daily_report_graph import compute_daily_summary_from_agg

        results: list[dict] = []
        to_insert: list[DailyReportSummary] = []
        missing_days = [ds for ds in day_starts if ds not in existing_map]

        # ★★ 性能根治：缺失的天数用**一次 GROUP BY 聚合查询**算完。
        #   原来是把整个区间的 sleep_frames 逐行拉回 Python 再统计：
        #     · select(SleepFrame) 会连带 raw_json（TEXT，每行几百字节~几 KB）
        #     · TiDB 在 ap-northeast-1（东京）、FC 在 cn-shanghai，
        #       7 天 ≈ 2 万帧跨国传输，实测 60 秒以上
        #   现在让数据库只回「每天一行」的聚合结果（7 行），传输量降三四个数量级。
        agg_map: dict[int, dict] = {}
        if missing_days:
            from sqlalchemy import case as _case
            from sqlalchemy import func as _func

            range_start = min(missing_days)
            range_end = max(missing_days) + 86400

            day_idx_expr = _func.floor(
                (SleepFrame.window_start_ts - range_start) / 86400
            )
            stmt = (
                select(
                    day_idx_expr.label("day_idx"),
                    _func.count().label("frame_count"),
                    _func.sum(_case((SleepFrame.stage == 2, 1), else_=0)).label("deep"),
                    _func.sum(_case((SleepFrame.stage == 1, 1), else_=0)).label("light"),
                    _func.sum(_case((SleepFrame.stage == 0, 1), else_=0)).label("wake"),
                    _func.sum(
                        _case((SleepFrame.heart_rate > 0, SleepFrame.heart_rate), else_=0)
                    ).label("hr_sum"),
                    _func.sum(_case((SleepFrame.heart_rate > 0, 1), else_=0)).label("hr_cnt"),
                    _func.sum(
                        _case((SleepFrame.resp_rate > 0, SleepFrame.resp_rate), else_=0)
                    ).label("rr_sum"),
                    _func.sum(_case((SleepFrame.resp_rate > 0, 1), else_=0)).label("rr_cnt"),
                    _func.sum(
                        _case((SleepFrame.snore_seconds >= 0, SleepFrame.snore_seconds), else_=0)
                    ).label("snore_sum"),
                )
                .where(
                    SleepFrame.device_id == device_id,
                    SleepFrame.window_start_ts >= range_start,
                    SleepFrame.window_start_ts < range_end,
                )
                .group_by(day_idx_expr)
            )
            agg_rows = (await db.execute(stmt)).all()
            for r in agg_rows:
                ds = range_start + int(r.day_idx) * 86400
                agg_map[ds] = {
                    "frame_count": int(r.frame_count or 0),
                    "deep": int(r.deep or 0),
                    "light": int(r.light or 0),
                    "wake": int(r.wake or 0),
                    "hr_sum": int(r.hr_sum or 0),
                    "hr_cnt": int(r.hr_cnt or 0),
                    "rr_sum": int(r.rr_sum or 0),
                    "rr_cnt": int(r.rr_cnt or 0),
                    "snore_sum": int(r.snore_sum or 0),
                }

        for ds in day_starts:
            de = ds + 86400
            if ds in existing_map:
                row = existing_map[ds]
                results.append({
                    "id": row.id,
                    "user_id": row.user_id,
                    "device_id": row.device_id,
                    "day_start_ts": row.day_start_ts,
                    "day_end_ts": row.day_end_ts,
                    "frame_count": row.frame_count,
                    "data_completeness": row.data_completeness,
                    "summary_json": row.summary_json,
                    "summary_text": row.summary_text,
                    "generated_at": row.generated_at,
                })
                continue
            # 用聚合结果直接算摘要（没有该天数据时 agg 为空 → 全零，仍会落库一条"无数据"）
            agg = agg_map.get(ds, {"frame_count": 0})
            frames_count = int(agg.get("frame_count") or 0)
            summary = compute_daily_summary_from_agg(agg, ds, de)
            import json as _json
            row = DailyReportSummary(
                user_id=owner.id,
                device_id=device_id,
                day_start_ts=ds,
                day_end_ts=de,
                frame_count=frames_count,
                data_completeness=summary["data_completeness"],
                summary_json=_json.dumps(summary, ensure_ascii=False),
                summary_text=summary.get("summary_text"),
            )
            to_insert.append(row)
            results.append({
                "id": None,
                "user_id": owner.id,
                "device_id": device_id,
                "day_start_ts": ds,
                "day_end_ts": de,
                "frame_count": frames_count,
                "data_completeness": summary["data_completeness"],
                "summary_json": _json.dumps(summary, ensure_ascii=False),
                "summary_text": summary.get("summary_text"),
                "generated_at": None,
            })
        if to_insert:
            for r in to_insert:
                db.add(r)
            await db.commit()
            # 重新查以拿到 id / generated_at
            refetched = (
                await db.execute(
                    select(DailyReportSummary)
                    .where(
                        DailyReportSummary.user_id == owner.id,
                        DailyReportSummary.device_id == device_id,
                        DailyReportSummary.day_start_ts.in_(day_starts),
                    )
                )
            ).scalars().all()
            refetched_map = {r.day_start_ts: r for r in refetched}
            for r in results:
                rr = refetched_map.get(r["day_start_ts"])
                if rr:
                    r["id"] = rr.id
                    r["generated_at"] = rr.generated_at
        results.sort(key=lambda x: x["day_start_ts"], reverse=True)
        return results

    @staticmethod
    async def list_with_report(
        db: AsyncSession, owner: User, limit: int = 50, offset: int = 0
    ) -> list[SleepRecord]:
        """已生成自然语言报告的记录，按开始时间倒序。"""
        result = await db.execute(
            select(SleepRecord)
            .where(SleepRecord.user_id == owner.id, SleepRecord.report.is_not(None))
            .order_by(SleepRecord.start_time.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    @staticmethod
    async def overview(db: AsyncSession, owner: User) -> dict[str, Any]:
        rows = (
            await db.execute(
                select(SleepRecord).where(SleepRecord.user_id == owner.id)
            )
        ).scalars().all()
        n = len(rows)
        if n == 0:
            return {
                "total_records": 0,
                "total_sleep_seconds": 0.0,
                "avg_sleep_seconds": None,
                "avg_score": None,
                "avg_heart_rate": None,
                "avg_respiratory_rate": None,
            }
        scores = [r.sleep_quality_score for r in rows if r.sleep_quality_score is not None]
        hrs = [r.avg_heart_rate for r in rows if r.avg_heart_rate is not None]
        resps = [r.avg_respiratory_rate for r in rows if r.avg_respiratory_rate is not None]
        total_sec = sum(max((r.end_time - r.start_time).total_seconds(), 0) for r in rows)
        return {
            "total_records": n,
            "total_sleep_seconds": total_sec,
            "avg_sleep_seconds": total_sec / n if n else None,
            "avg_score": (sum(scores) / len(scores)) if scores else None,
            "avg_heart_rate": (sum(hrs) / len(hrs)) if hrs else None,
            "avg_respiratory_rate": (sum(resps) / len(resps)) if resps else None,
        }

    @staticmethod
    async def trends(db: AsyncSession, owner: User, days: int = 7) -> dict[str, Any]:
        from datetime import datetime, timedelta, timezone

        start = datetime.now(tz=timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        rows = (
            await db.execute(
                select(SleepRecord)
                .where(SleepRecord.user_id == owner.id, SleepRecord.start_time >= start)
                .order_by(SleepRecord.start_time.asc())
            )
        ).scalars().all()

        by_day: dict[str, dict[str, float]] = {}
        for r in rows:
            st = r.start_time
            if st is None:
                continue
            if hasattr(st, "tzinfo") and st.tzinfo:
                st = st.astimezone(timezone.utc).replace(tzinfo=None)
            day = st.date().isoformat()
            bucket = by_day.setdefault(
                day, {"score_sum": 0.0, "hr_sum": 0.0, "resp_sum": 0.0, "dur": 0.0, "n": 0}
            )
            bucket["n"] += 1
            if r.sleep_quality_score is not None:
                bucket["score_sum"] += float(r.sleep_quality_score)
            if r.avg_heart_rate is not None:
                bucket["hr_sum"] += float(r.avg_heart_rate)
            if r.avg_respiratory_rate is not None:
                bucket["resp_sum"] += float(r.avg_respiratory_rate)
            bucket["dur"] += max((r.end_time - r.start_time).total_seconds(), 0) / 3600.0

        points = [
            {
                "date": day,
                "score": (round(b["score_sum"] / b["n"], 1) if b["score_sum"] else None),
                "duration_hours": round(b["dur"], 1) if b["dur"] else None,
                "avg_heart_rate": (round(b["hr_sum"] / b["n"], 1) if b["hr_sum"] else None),
                "avg_respiratory_rate": (round(b["resp_sum"] / b["n"], 1) if b["resp_sum"] else None),
            }
            for day, b in by_day.items()
        ]
        return {"days": days, "points": points}

    @staticmethod
    async def stage_distribution(db: AsyncSession, owner: User) -> dict[str, Any]:
        rows = (
            await db.execute(
                select(SleepRecord).where(SleepRecord.user_id == owner.id)
            )
        ).scalars().all()
        agg: dict[str, float] = {}
        for r in rows:
            stages = ((r.analysis or {}).get("sleep_stages")) or \
                     ((r.analysis or {}).get("stages")) or {}
            # 兼容多种 key 命名
            for key in ("deep", "light", "shallow", "rem", "awake"):
                v = stages.get(key)
                if v is not None:
                    agg.setdefault(key, 0.0)
                    agg[key] += float(v) if isinstance(v, (int, float)) else 0.0
        total = sum(agg.values()) or 1.0
        out = []
        for key in ("deep", "light", "shallow", "rem", "awake"):
            if key in agg:
                out.append(
                    {
                        "stage": "shallow" if key == "shallow" else key,
                        "minutes": round(agg[key], 1),
                        "ratio": round(agg[key] / total, 4),
                    }
                )
        return {"records": len(rows), "stages": out}

    @staticmethod
    async def get(db: AsyncSession, record_id: int, owner: User) -> SleepRecord:
        return await SleepService._get_owned(db, record_id, owner)

    @staticmethod
    async def _get_owned(
        db: AsyncSession, record_id: int, owner: User
    ) -> SleepRecord:
        result = await db.execute(
            select(SleepRecord).where(SleepRecord.id == record_id)
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise LookupError("睡眠记录不存在")
        if record.user_id != owner.id:
            raise PermissionError("无权访问该记录")
        return record


# ============== Message ==============
class MessageService:
    """用户通知：设备离线 / 重度鼾声 / 呼吸暂停高风险 / 报告完成 / 设备绑定。

    写入点（事件源）见各 create_*_message 静态方法，在 Service 其他位置触发。
    """

    @staticmethod
    async def list_for_user(
        db: AsyncSession, user_id: int, limit: int = 50, offset: int = 0
    ) -> tuple[list, int]:
        from sqlalchemy import select, func
        from app.models import UserMessage

        result = await db.execute(
            select(UserMessage)
            .where(UserMessage.user_id == user_id)
            .order_by(UserMessage.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        items = list(result.scalars().all())

        unread = await db.execute(
            select(func.count(UserMessage.id))
            .where(UserMessage.user_id == user_id, UserMessage.read_at.is_(None))
        )
        unread_count = int(unread.scalar() or 0)
        total = await db.execute(
            select(func.count(UserMessage.id)).where(UserMessage.user_id == user_id)
        )
        total_count = int(total.scalar() or 0)
        return items, unread_count, total_count

    @staticmethod
    async def mark_read(db: AsyncSession, user_id: int, message_id: int) -> bool:
        from datetime import datetime, timezone
        from sqlalchemy import select
        from app.models import UserMessage

        row = (await db.execute(
            select(UserMessage).where(
                UserMessage.id == message_id,
                UserMessage.user_id == user_id,
            )
        )).scalar_one_or_none()
        if row is None:
            return False
        if row.read_at is None:
            row.read_at = datetime.now(tz=timezone.utc)
            await db.commit()
        return True

    @staticmethod
    async def mark_all_read(db: AsyncSession, user_id: int) -> int:
        from datetime import datetime, timezone
        from sqlalchemy import select, update
        from app.models import UserMessage

        result = await db.execute(
            update(UserMessage)
            .where(UserMessage.user_id == user_id, UserMessage.read_at.is_(None))
            .values(read_at=datetime.now(tz=timezone.utc))
        )
        await db.commit()
        return int(result.rowcount or 0)

    # ============ 事件源（被其他 Service 调用）============
    @staticmethod
    async def create_snore_heavy(
        db: AsyncSession, user_id: int, device_id: int, frame_id: int, snore_sec: int
    ) -> None:
        from app.models import UserMessage
        m = UserMessage(
            user_id=user_id,
            type="snore_heavy",
            severity="warning",
            title="重度鼾声",
            content=f"本 30 秒窗口检出 {snore_sec} 秒鼾声（>=25s），请关注睡眠呼吸。",
            ref_device_id=device_id,
            ref_frame_id=frame_id,
        )
        db.add(m)
        await db.commit()

    @staticmethod
    async def create_apnea_high(
        db: AsyncSession, user_id: int, device_id: int, record_id: int | None = None
    ) -> None:
        from app.models import UserMessage
        m = UserMessage(
            user_id=user_id,
            type="apnea_high",
            severity="error",
            title="呼吸暂停高风险",
            content="系统检测到呼吸暂停高风险，建议尽快线下就医做睡眠呼吸监测。",
            ref_device_id=device_id,
            ref_record_id=record_id,
        )
        db.add(m)
        await db.commit()

    @staticmethod
    async def create_device_offline(
        db: AsyncSession, user_id: int, device_id: int, device_name: str, minutes: int
    ) -> None:
        from app.models import UserMessage
        m = UserMessage(
            user_id=user_id,
            type="device_offline",
            severity="warning",
            title="设备离线",
            content=f"设备 {device_name or device_id} 已 {minutes} 分钟未上报数据，请检查电源与网络。",
            ref_device_id=device_id,
        )
        db.add(m)
        await db.commit()

    @staticmethod
    async def create_report_ready(
        db: AsyncSession, user_id: int, device_id: int, start_ts: int, end_ts: int
    ) -> None:
        from datetime import datetime, timezone
        from app.models import UserMessage
        from_ts = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%m-%d %H:%M")
        to_ts = datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime("%m-%d %H:%M")
        m = UserMessage(
            user_id=user_id,
            type="report_ready",
            severity="info",
            title="报告已生成",
            content=f"{from_ts} 至 {to_ts} 的睡眠报告已生成，可在「睡眠报告」中查看。",
            ref_device_id=device_id,
        )
        db.add(m)
        await db.commit()

    @staticmethod
    async def create_device_bound(
        db: AsyncSession, user_id: int, device_id: int, device_name: str
    ) -> None:
        from app.models import UserMessage
        m = UserMessage(
            user_id=user_id,
            type="device_bound",
            severity="info",
            title="新设备已绑定",
            content=f"设备 {device_name or device_id} 已绑定到当前账号。",
            ref_device_id=device_id,
        )
        db.add(m)
        await db.commit()


# ============== Chat ==============
class ChatService:
    @staticmethod
    async def chat(db: AsyncSession, owner: User, req: ChatRequest) -> dict[str, Any]:
        sleep_ctx: dict[str, Any] | None = None
        if req.sleep_record_id is not None:
            try:
                rec = await SleepService.get(db, req.sleep_record_id, owner)
                sleep_ctx = {
                    "record_id": rec.id,
                    "start_time": rec.start_time.isoformat(),
                    "end_time": rec.end_time.isoformat(),
                    "sleep_quality_score": rec.sleep_quality_score,
                    "apnea_risk": rec.apnea_risk,
                    "avg_heart_rate": rec.avg_heart_rate,
                    "avg_respiratory_rate": rec.avg_respiratory_rate,
                    "features": (rec.analysis or {}).get("features"),
                    "notes": (rec.analysis or {}).get("notes"),
                }
            except (LookupError, PermissionError):
                sleep_ctx = None  # 不暴露不存在/无权限

        result = await chat_graph.ainvoke(
            {
                "user_id": owner.id,
                "message": req.message,
                "history": [m.model_dump() for m in req.history],
                "sleep_context": sleep_ctx,
            }
        )
        return {
            "reply": result.get("reply", ""),
            "citations": result.get("citations", []),
        }



