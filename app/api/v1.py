"""API v1 路由汇总。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import schemas
from app.database import get_db
from app.deps import get_current_user
from app.models import User
from app.services import AuthService, ChatService, DeviceService, MessageService, SleepService

logger = logging.getLogger(__name__)

api_router = APIRouter(prefix="/api/v1")


# ============== Auth ==============
@api_router.post(
    "/auth/register",
    response_model=schemas.ApiResponse[schemas.UserOut],
    tags=["认证"],
    summary="用户注册",
    description="创建新用户。用户名（3-64 字符）、密码（6-128 字符）必填，邮箱可选。",
)
async def register(payload: schemas.UserCreate, db: AsyncSession = Depends(get_db)):
    try:
        user = await AuthService.register(db, payload)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return schemas.ApiResponse(data=schemas.UserOut.model_validate(user))


@api_router.post(
    "/auth/login",
    response_model=schemas.ApiResponse[schemas.Token],
    tags=["认证"],
    summary="用户登录",
    description="用户名 + 密码登录，返回 JWT access_token，后续受保护接口需在 Authorization 头携带。",
)
async def login(payload: schemas.LoginRequest, db: AsyncSession = Depends(get_db)):
    try:
        user = await AuthService.authenticate(db, payload.username, payload.password)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    token = AuthService.issue_token(user)
    return schemas.ApiResponse(
        data=schemas.Token(access_token=token, user=schemas.UserOut.model_validate(user))
    )


@api_router.get(
    "/auth/me",
    response_model=schemas.ApiResponse[schemas.UserOut],
    tags=["认证"],
    summary="当前用户信息",
    description="返回当前已登录用户的资料（需要 Bearer Token）。",
)
async def me(current: User = Depends(get_current_user)):
    return schemas.ApiResponse(data=schemas.UserOut.model_validate(current))


@api_router.patch(
    "/auth/me",
    response_model=schemas.ApiResponse[schemas.UserOut],
    tags=["认证"],
    summary="更新当前用户资料",
    description=(
        "支持更新 nickname / avatar_url / full_name / email 任意子集。"
        "前端：用户在「我的」页修改昵称、点击「更换微信头像」时调用。"
    ),
)
async def update_me(
    payload: schemas.UserUpdate,
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    try:
        user = await AuthService.update_profile(db, current, payload)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return schemas.ApiResponse(data=schemas.UserOut.model_validate(user))


@api_router.post(
    "/auth/wx-login",
    response_model=schemas.ApiResponse[schemas.Token],
    tags=["认证"],
    summary="微信小程序一键登录",
    description=(
        "用 wx.login() 的临时 code 换取 openid，首次登录自动创建用户并返回 JWT。"
        "可选传 nickname/avatar_url（前端 chooseAvatar 拿到的），用于回填用户资料。"
    ),
)
async def wx_login(
    payload: schemas.WxLoginRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        user = await AuthService.login_by_wx(
            db, payload.code, payload.nickname, payload.avatar_url
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    token = AuthService.issue_token(user)
    return schemas.ApiResponse(
        data=schemas.Token(access_token=token, user=schemas.UserOut.model_validate(user))
    )


# ============== Device ==============
@api_router.post(
    "/devices",
    response_model=schemas.ApiResponse[schemas.DeviceOut],
    status_code=status.HTTP_201_CREATED,
    tags=["设备"],
    summary="注册设备",
    description="为当前用户注册一台睡眠监测设备。device_id 全局唯一，后续设备上报数据时用于归属校验。",
)
async def create_device(
    payload: schemas.DeviceCreate,
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    try:
        device = await DeviceService.create(db, current, payload)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return schemas.ApiResponse(data=schemas.DeviceOut.model_validate(device))


@api_router.get(
    "/devices",
    response_model=schemas.ApiResponse[list[schemas.DeviceOut]],
    tags=["设备"],
    summary="查询设备列表",
    description="返回当前用户名下的所有设备。online_only=true 时只返回已上云的。",
)
async def list_devices(
    online_only: bool = Query(False, description="只返回 is_online=true 的设备"),
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    devices = await DeviceService.list_for_user(db, current, online_only=online_only)
    return schemas.ApiResponse(data=[schemas.DeviceOut.model_validate(d) for d in devices])


@api_router.get(
    "/devices/{device_id}",
    response_model=schemas.ApiResponse[schemas.DeviceDetailOut],
    tags=["设备"],
    summary="查询设备详情",
    description="返回设备基础信息 + 实时运行状态（在线、最近体征推断）。",
)
async def get_device(
    device_id: int,
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    try:
        device = await DeviceService.get(db, device_id, current)
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    runtime = await DeviceService.get_runtime(db, device)
    base = schemas.DeviceOut.model_validate(device).model_dump()
    return schemas.ApiResponse(data=schemas.DeviceDetailOut(**base, runtime=runtime))


# 注：原 GET/PUT /devices/{id}/cfg 已删除。
#     三开关（snore/stage/bcg）是设备本地状态，走 BLE 读写（固件 0x04/0x05），
#     不经云端；当前配置随每一帧的 raw_json.cfg 可见，无需单独接口。


# ★ 最近一帧 30s 结构化数据：详情页「设备原始上传」用
@api_router.get(
    "/devices/{device_id}/raw-json",
    response_model=schemas.ApiResponse[schemas.SleepFrameOut | None],
    tags=["设备"],
    summary="读最近一帧 30s 结构化数据",
    description="设备端 EdgeAggregator 每 30s 写到 MQTT，IoTDA 转发到 /ingest/huawei/frame 落库 sleep_frames。",
)
async def get_device_raw_json(
    device_id: int,
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    try:
        frame = await DeviceService.get_latest_frame(db, device_id, current)
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    return schemas.ApiResponse(
        data=schemas.SleepFrameOut.model_validate(frame) if frame else None
    )


# ★ AI 分析：取最新一帧 30s 结构化数据 → 交 LLM 分析 → 返回文本（不落库）
@api_router.post(
    "/devices/{device_id}/frame/analyze",
    response_model=schemas.ApiResponse[schemas.FrameAnalysisOut],
    tags=["设备"],
    summary="对最新一帧 30s 数据做 AI 分析",
    description=(
        "取该设备最近一帧端侧结构化数据（sleep_frames），把这一帧的"
        "分期/心率/呼吸/体动/鼾声交给 LLM 解读，返回分析文本。"
        "不写入 sleep_records，不生成整晚报告。"
    ),
)
async def analyze_latest_frame(
    device_id: int,
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    try:
        result = await DeviceService.analyze_latest_frame(db, device_id, current)
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    return schemas.ApiResponse(
        data=schemas.FrameAnalysisOut(
            frame=schemas.SleepFrameOut.model_validate(result["frame"]),
            analysis=result["analysis"],
            generated_at=datetime.now(tz=timezone.utc),
        )
    )


# ★ 显式认领：用户重配网时调用（已存在则只更新 owner，不重复入库）
@api_router.post(
    "/devices/{device_id}/bind",
    response_model=schemas.ApiResponse[schemas.DeviceOut],
    tags=["设备"],
    summary="认领已存在的待归属设备",
    description="按 device_id 字符串（云端 device_id）把已存在的设备绑到当前用户。",
)
async def bind_device(
    device_id: str,
    name: str | None = Query(None, description="可选：覆盖设备名"),
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    try:
        device = await DeviceService.bind(db, current, device_id, name)
    except LookupError as e:
        # deviceId 不在云端数据库里（固件读出来的本地 ID，未曾上云）
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:  # noqa: BLE001
        # 不要让 SQLAlchemy / 列长度 / 未捕获错误直接 500，让前端能识别
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"认领失败: {type(e).__name__}: {e}",
        )
    return schemas.ApiResponse(data=schemas.DeviceOut.model_validate(device))


@api_router.post(
    "/devices/{device_id}/command",
    response_model=schemas.ApiResponse[schemas.DeviceCommandOut],
    tags=["设备"],
    summary="下发设备指令",
    description="把一条控制指令写入设备命令队列（status=pending），由设备侧/华为云轮询取走执行。",
)
async def send_device_command(
    device_id: int,
    payload: schemas.DeviceCommandCreate,
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    try:
        cmd = await DeviceService.create_command(
            db, current, device_id, payload.command, payload.value
        )
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    return schemas.ApiResponse(data=schemas.DeviceCommandOut.model_validate(cmd))


@api_router.post(
    "/devices/{device_id}/reboot",
    response_model=schemas.ApiResponse[schemas.DeviceCommandOut],
    tags=["设备"],
    summary="重启设备",
    description="下发 reboot 指令。",
)
async def reboot_device(
    device_id: int,
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    cmd = await DeviceService.create_command(db, current, device_id, "reboot", None)
    return schemas.ApiResponse(data=schemas.DeviceCommandOut.model_validate(cmd))


@api_router.post(
    "/devices/{device_id}/upgrade",
    response_model=schemas.ApiResponse[schemas.DeviceCommandOut],
    tags=["设备"],
    summary="升级固件",
    description="下发 upgrade 指令，value 可携带目标版本号。",
)
async def upgrade_device(
    device_id: int,
    payload: schemas.UpgradeRequest = None,
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    version = payload.version if payload else None
    cmd = await DeviceService.create_command(
        db, current, device_id, "upgrade", {"version": version}
    )
    return schemas.ApiResponse(data=schemas.DeviceCommandOut.model_validate(cmd))


@api_router.get(
    "/devices/{device_id}/logs",
    response_model=schemas.ApiResponse[list[schemas.DeviceCommandOut]],
    tags=["设备"],
    summary="设备日志/指令历史",
    description="返回该设备最近的指令下发记录（按时间倒序）。",
)
async def device_logs(
    device_id: int,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    try:
        cmds = await DeviceService.list_commands(db, current, device_id, limit)
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    return schemas.ApiResponse(
        data=[schemas.DeviceCommandOut.model_validate(c) for c in cmds]
    )


@api_router.delete(
    "/devices/{device_id}",
    response_model=schemas.ApiResponse[None],
    tags=["设备"],
    summary="解绑/删除设备",
    description="删除设备及其睡眠记录与指令队列（级联）。",
)
async def unbind_device(
    device_id: int,
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    try:
        await DeviceService.delete(db, current, device_id)
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    return schemas.ApiResponse(message="设备已解绑")


# ============== Sleep Records ==============
# 注：原 POST /sleep/ingest（小程序当网关、手机中继上报）已删除。
#     设备现在自己直连云端：EdgeAggregator 每 30s 发 /device/{id}/frame
#     → IoTDA 规则引擎 → POST /api/v1/ingest/huawei/frame → sleep_frames。


@api_router.post(
    "/sleep/{record_id}/analyze",
    response_model=schemas.ApiResponse[schemas.SleepRecordOut],
    tags=["睡眠"],
    summary="分析睡眠记录",
    description="对指定睡眠记录运行 DataAgent，生成睡眠分期、心率/呼吸统计、呼吸暂停风险等分析结果。",
)
async def analyze_sleep(
    record_id: int,
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    try:
        record = await SleepService.analyze(db, record_id, current)
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    return schemas.ApiResponse(data=schemas.SleepRecordOut.model_validate(record))


@api_router.get(
    "/sleep",
    response_model=schemas.ApiResponse[list[schemas.SleepRecordOut]],
    tags=["睡眠"],
    summary="查询睡眠记录列表",
    description="分页返回当前用户的睡眠记录，按开始时间倒序。",
)
async def list_sleep(
    limit: int = Query(20, ge=1, le=100, description="返回条数（1-100）"),
    offset: int = Query(0, ge=0, description="偏移量"),
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    records = await SleepService.list_for_user(db, current, limit=limit, offset=offset)
    return schemas.ApiResponse(
        data=[schemas.SleepRecordOut.model_validate(r) for r in records]
    )


@api_router.get(
    "/sleep/stats/overview",
    response_model=schemas.ApiResponse[schemas.StatsOverviewOut],
    tags=["睡眠"],
    summary="睡眠数据概览",
    description="返回当前用户的汇总统计（记录数、累计/平均时长、平均评分与体征均值）。",
)
async def sleep_overview(
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    data = await SleepService.overview(db, current)
    return schemas.ApiResponse(data=schemas.StatsOverviewOut(**data))


@api_router.get(
    "/sleep/stats/trends",
    response_model=schemas.ApiResponse[schemas.StatsTrendsOut],
    tags=["睡眠"],
    summary="睡眠趋势",
    description="按天聚合近 days 天的评分、时长、平均心/呼吸率，便于前端画折线图。",
)
async def sleep_trends(
    days: int = Query(7, ge=1, le=90, description="回溯天数"),
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    data = await SleepService.trends(db, current, days=days)
    return schemas.ApiResponse(data=schemas.StatsTrendsOut(**data))


@api_router.get(
    "/sleep/stats/stages",
    response_model=schemas.ApiResponse[schemas.StatsStagesOut],
    tags=["睡眠"],
    summary="睡眠分期分布",
    description="汇总各睡眠分期（深睡/浅睡/REM/清醒）总时长与占比。",
)
async def sleep_stages(
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    data = await SleepService.stage_distribution(db, current)
    return schemas.ApiResponse(data=schemas.StatsStagesOut(**data))


@api_router.get(
    "/sleep/{record_id}",
    response_model=schemas.ApiResponse[schemas.SleepRecordOut],
    tags=["睡眠"],
    summary="查询单条睡眠记录",
    description="按 ID 返回一条睡眠记录的原始数据、分析结果与报告。",
)
async def get_sleep(
    record_id: int,
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    try:
        record = await SleepService.get(db, record_id, current)
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    return schemas.ApiResponse(data=schemas.SleepRecordOut.model_validate(record))


# ============== Report ==============
@api_router.get(
    "/reports",
    response_model=schemas.ApiResponse[list[schemas.SleepRecordOut]],
    tags=["报告"],
    summary="报告列表",
    description="返回已生成自然语言报告的睡眠记录，按开始时间倒序。",
)
async def list_reports(
    limit: int = Query(50, ge=1, le=100, description="返回条数（1-100）"),
    offset: int = Query(0, ge=0, description="偏移量"),
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    records = await SleepService.list_with_report(db, current, limit=limit, offset=offset)
    return schemas.ApiResponse(
        data=[schemas.SleepRecordOut.model_validate(r) for r in records]
    )


@api_router.post(
    "/reports/generate",
    response_model=schemas.ApiResponse[schemas.SleepRecordOut],
    tags=["报告"],
    summary="生成睡眠报告",
    description="基于已分析的睡眠记录生成自然语言报告，style 可选 detailed / summary / pediatric。",
)
async def generate_report(
    payload: schemas.ReportRequest,
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    try:
        record = await SleepService.generate_report(
            db, payload.sleep_record_id, current, style=payload.style
        )
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    return schemas.ApiResponse(data=schemas.SleepRecordOut.model_validate(record))


# ★ 按区间生成报告（独立智能体，report_range_graph）
@api_router.post(
    "/reports/range",
    response_model=schemas.ApiResponse[schemas.ReportRangeOut],
    tags=["报告"],
    summary="按时间区间生成睡眠报告",
    description=(
        "独立智能体（report_range_graph），与基于 sleep_records 的 ReportAgent 不复用。"
        "直接消费 sleep_frames 区间。"
        "用户场景：选 [start_ts, end_ts)（epoch 秒，UTC），生成该段时间内的睡眠报告。"
        "输出含 report（自然语言）/ coverage（数据完整度）/ summary（汇总特征）/"
        "degradation（降级档位 + 警告）。"
    ),
)
async def generate_range_report(
    payload: schemas.ReportRangeRequest,
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    if payload.end_ts <= payload.start_ts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="end_ts 必须大于 start_ts",
        )
    if payload.end_ts - payload.start_ts > 7 * 86400:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="单次区间请不超过 7 天（604800 秒）",
        )
    try:
        result = await SleepService.generate_range_report(
            db, current, payload.device_id, payload.start_ts, payload.end_ts
        )
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    return schemas.ApiResponse(data=schemas.ReportRangeOut(**result))


# ★ 按区间生成报告（SSE 流式版）：报告正文边生成边推，前端打字机效果
@api_router.post(
    "/reports/range/stream",
    tags=["报告"],
    summary="按时间区间生成睡眠报告（SSE 流式）",
    description=(
        "与 /reports/range 同样的数据口径，但用 SSE 逐块推送报告正文。\n\n"
        "阿里云 FC 3.0 Web 函数：返回生成器即可流式，请求头带 "
        "`Accept: text/event-stream` 触发。\n\n"
        "事件序列（每条 `data: {json}\\n\\n`）：\n"
        "  meta  → {coverage, summary, degradation, frame_count, start_ts, end_ts}\n"
        "  delta → {text}  报告正文增量（可多次）\n"
        "  done  → {report, frame_count}  完整正文\n"
        "  error → {message}"
    ),
)
async def stream_range_report(
    payload: schemas.ReportRangeRequest,
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    import json as _json

    from fastapi.responses import StreamingResponse

    from app.agents.llm import get_chat_model
    from app.agents.report_range_graph import _fallback_report, build_range_messages

    if payload.end_ts <= payload.start_ts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="end_ts 必须大于 start_ts"
        )
    if payload.end_ts - payload.start_ts > 7 * 86400:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="单次区间请不超过 7 天（604800 秒）",
        )

    # ★ DB 活全部在响应开始前做完：StreamingResponse 一旦开始推，
    #   FastAPI 的 db 依赖就会关闭 session，生成器里再查库会炸。
    try:
        prep = await SleepService.prepare_range_report(
            db, current, payload.device_id, payload.start_ts, payload.end_ts
        )
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

    # 提前把 ORM 对象里的标量取出来：流式期间 session 已关闭，访问 current.id 会炸
    owner_id = current.id
    device_id = payload.device_id
    start_ts = payload.start_ts
    end_ts = payload.end_ts

    state = {
        "user_id": owner_id,
        "device_id": device_id,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "frames": prep["frames"],
        "coverage": prep["coverage"],
        "summary": prep["summary"],
        "degradation": prep["degradation"],
    }
    messages = build_range_messages(state)
    frame_count = len(prep["frames"])

    def sse(obj: dict) -> str:
        return "data: " + _json.dumps(obj, ensure_ascii=False) + "\n\n"

    async def event_gen():
        # 1) 先推统计信息，前端可以立刻渲染完整度/指标卡
        yield sse({
            "t": "meta",
            "device_id": device_id,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "frame_count": frame_count,
            "coverage": prep["coverage"],
            "summary": prep["summary"],
            "degradation": prep["degradation"],
        })

        # 2) LLM 逐块推正文
        parts: list[str] = []
        llm_error: str | None = None
        try:
            llm = get_chat_model()
            async for chunk in llm.astream(messages):
                piece = chunk.content
                # 部分 provider 的 content 是 [{type,text}] 列表
                if isinstance(piece, list):
                    buf = []
                    for p in piece:
                        if isinstance(p, dict):
                            buf.append(str(p.get("text", "")))
                        else:
                            buf.append(str(p))
                    piece = "".join(buf)
                if piece:
                    parts.append(piece)
                    yield sse({"t": "delta", "text": piece})
        except Exception as e:  # noqa: BLE001
            llm_error = f"{type(e).__name__}: {e}"
            logger.warning("流式 LLM 失败，走兜底报告: %s", llm_error)

        full = "".join(parts).strip()
        if not full:
            # LLM 没吐出任何内容（或直接失败）→ 兜底报告也走 delta，保持协议一致
            full = _fallback_report(state)
            yield sse({"t": "delta", "text": full})
        if len(full) > 4000:
            full = full[:3997] + "..."

        # 3) 报告完成通知：用独立 session，请求的 db 已随响应开始关闭
        try:
            from app.database import AsyncSessionLocal

            async with AsyncSessionLocal() as s2:
                await MessageService.create_report_ready(
                    s2, owner_id, device_id, start_ts, end_ts
                )
                await s2.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("报告完成通知失败: %s", e)

        yield sse({
            "t": "done",
            "report": full,
            "frame_count": frame_count,
            "llm_error": llm_error,
        })

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # 反向代理别缓冲
        },
    )


# ★ 长期趋势：按 12:00 切分取最近 N 天的每日摘要（on-demand 算 + 落库缓存）
@api_router.get(
    "/sleep/trend/daily",
    response_model=schemas.ApiResponse[list[dict]],
    tags=["趋势"],
    summary="最近 N 天每日报告摘要（长期趋势用）",
    description=(
        "时间锚点：每天 12:00 切分（即 25 号 12:00 ~ 26 号 12:00 算 '25 号的一天'）。"
        "On-demand：缺哪条现场算 daily_report_graph，补到 daily_report_summaries 表。"
    ),
)
async def get_trend_daily(
    device_id: int = Query(..., description="设备 ID"),
    days: int = Query(7, ge=1, le=30),
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    try:
        rows = await SleepService.get_or_compute_daily_summaries(
            db, current, device_id, days=days
        )
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    return schemas.ApiResponse(data=rows)


# ★ 长期趋势：看板（首页 / dashboard 用）
@api_router.get(
    "/sleep/trend/dashboard",
    response_model=schemas.ApiResponse[schemas.TrendDashboardOut],
    tags=["趋势"],
    summary="睡眠看板（7 天汇总）",
    description=(
        "返回睡眠看板：昨晚评分 / 7 晚均分 / 7 晚均时长 / 深睡占比均值 / 鼾声均值 / 趋势洞察一句话。"
        "On-demand：内部调 /sleep/trend/daily 拿最近 7 天，再喂 trend_graph 算 dashboard。"
    ),
)
async def get_trend_dashboard(
    device_id: int = Query(..., description="设备 ID"),
    days: int = Query(7, ge=1, le=30),
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    from app.agents.trend_graph import compute_dashboard, compute_weekly
    try:
        rows = await SleepService.get_or_compute_daily_summaries(
            db, current, device_id, days=days
        )
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    dashboard = compute_dashboard(rows, days=days)
    # ★ 顺手把每日明细一起返回（前端画 4 张图用），
    #   避免前端再发一次 /sleep/trend/weekly 触发同一批摘要重算。
    #   顺序与 /sleep/trend/weekly 保持一致（day_start_ts 降序，最新在前）。
    weekly = compute_weekly(rows, days=days)
    dashboard["items"] = weekly.get("items", [])
    return schemas.ApiResponse(data=schemas.TrendDashboardOut(**dashboard))


# ★ 长期趋势：7 天每日一行（history 页用）
@api_router.get(
    "/sleep/trend/weekly",
    response_model=schemas.ApiResponse[schemas.TrendWeeklyOut],
    tags=["趋势"],
    summary="最近 N 天每日报告（history 页用）",
    description="7 天每行一条摘要，按时间升序。",
)
async def get_trend_weekly(
    device_id: int = Query(..., description="设备 ID"),
    days: int = Query(7, ge=1, le=30),
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    from app.agents.trend_graph import compute_weekly
    try:
        rows = await SleepService.get_or_compute_daily_summaries(
            db, current, device_id, days=days
        )
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    weekly = compute_weekly(rows, days=days)
    return schemas.ApiResponse(data=schemas.TrendWeeklyOut(**weekly))


# ============== Chat ==============
@api_router.post(
    "/chat",
    response_model=schemas.ApiResponse[schemas.ChatResponse],
    tags=["对话"],
    summary="AI 对话问答",
    description="基于用户历史睡眠数据与 LangGraph 进行多轮问答，可绑定某条睡眠记录作为上下文。",
)
async def chat(
    payload: schemas.ChatRequest,
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    res = await ChatService.chat(db, current, payload)
    return schemas.ApiResponse(data=schemas.ChatResponse(**res))


# ============== Message ==============
@api_router.get(
    "/messages",
    response_model=schemas.ApiResponse[schemas.UserMessageListOut],
    tags=["消息"],
    summary="消息列表",
    description=(
        "返回当前用户的消息列表 + 未读数 + 总数。"
        "类型：device_offline / snore_heavy / apnea_high / report_ready / device_bound。"
    ),
)
async def list_messages(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    items, unread, total = await MessageService.list_for_user(
        db, current.id, limit=limit, offset=offset
    )
    return schemas.ApiResponse(
        data=schemas.UserMessageListOut(
            items=[schemas.UserMessageOut.model_validate(m) for m in items],
            unread_count=unread,
            total=total,
        )
    )


@api_router.post(
    "/messages/{message_id}/read",
    response_model=schemas.ApiResponse[dict],
    tags=["消息"],
    summary="标记单条已读",
)
async def mark_message_read(
    message_id: int,
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    ok = await MessageService.mark_read(db, current.id, message_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="消息不存在")
    return schemas.ApiResponse(data={"ok": True})


@api_router.post(
    "/messages/read-all",
    response_model=schemas.ApiResponse[dict],
    tags=["消息"],
    summary="标记全部已读",
)
async def mark_all_messages_read(
    db: AsyncSession = Depends(get_db),
    current: User = Depends(get_current_user),
):
    n = await MessageService.mark_all_read(db, current.id)
    return schemas.ApiResponse(data={"ok": True, "marked": n})