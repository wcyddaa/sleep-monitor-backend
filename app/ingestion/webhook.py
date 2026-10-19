"""华为云 HTTP 规则引擎 HTTPS 转发 webhook 端点。

华为云控制台：数据转发 -> 规则 -> 目标类型选「第三方应用/HTTPS」。

★ 只需要配**一个地址**：

    {后端}/api/v1/ingest/huawei

设备把所有上行都发到标准系统主题
`$oc/devices/{device_id}/sys/messages/up`（平台一定放行，无需产品侧声明），
本端点按 payload 里的 `evt` 字段自动分流：

    {"evt":"online"}                     -> 自动注册 + 置在线
    {"evt":"frame", ...}                 -> 落 sleep_frames（顺带刷新 last_seen/在线）
    {"evt":"cfg","cfg":{...}}            -> 刷 device_cfg 快照（BLE 切开关时设备补发）
    （无 evt，平台设备状态变更报文）      -> 按 status/event 字段置 在线/离线

安全（可选）：设置 HTTP_WEBHOOK_TOKEN 后，需在请求头带上 X-Webhook-Token。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Header, HTTPException, Request, status
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Device
from app.services import DeviceService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ingest", tags=["ingest"])


def _deep_find(obj, key: str):
    """递归在嵌套 dict/list 中查找第一个匹配 key 的值（华为云报文结构多变）。"""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _deep_find(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find(v, key)
            if found is not None:
                return found
    return None


# ----------------------------------------------------------------------
# 平台「设备状态变更」报文的在线/离线解析
# 华为云状态变更报文里，顶层往往是 {"event":"device.status"}（事件名，无法解析），
# 真实状态在同级的 notify_data.body.status（如 "OFFLINE"）。所以遍历所有状态类
# 字段的值，返回第一个能解析出在线/离线的结果。
# ----------------------------------------------------------------------
_STATUS_KEYS = {"status", "event", "event_type", "state", "online", "offline"}


def _parse_online_state(value) -> bool | None:
    """把状态字段值解析为 在线(True)/离线(False)；无法识别返回 None。"""
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if "offline" in s or "off" in s or "离线" in s or "下线" in s or "disconnect" in s:
        return False
    if "online" in s or "on" in s or "在线" in s or "上线" in s or "connect" in s:
        return True
    return None


def _deep_find_online_state(obj) -> bool | None:
    """递归查找设备状态字段并解析为在线(True)/离线(False)。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in _STATUS_KEYS and isinstance(v, (str, bool, int)):
                parsed = _parse_online_state(v)
                if parsed is not None:
                    return parsed
        for v in obj.values():
            found = _deep_find_online_state(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find_online_state(v)
            if found is not None:
                return found
    return None


# ======================================================================
# ★ 统一入口：一条规则、一个地址搞定「注册 + 上下线 + 消息上报」
# ======================================================================
@router.post(
    "/huawei",
    summary="设备统一接入 webhook（免登录，自动注册 / 自动上下线 / 落帧）",
    description=(
        "IoTDA 规则引擎把设备上行（系统主题 $oc/devices/{id}/sys/messages/up）"
        "以及平台设备状态变更事件都转发到本端点。\n"
        "按 payload 的 evt 分流：online=注册+在线；frame=落 sleep_frames；"
        "无 evt 且有 status/event 字段=平台状态变更，据此置在线/离线。"
    ),
)
async def huawei_webhook(
    request: Request,
    x_webhook_token: str | None = Header(default=None),
) -> dict:
    if settings.http_webhook_token and x_webhook_token != settings.http_webhook_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的 webhook token"
        )
    body = await request.json()
    logger.info("收到接入报文: %s", body)

    from app.ingestion.base import _to_dict, _unwrap_envelope
    data = _unwrap_envelope(_to_dict(body))
    device_id = data.get("device_id") or _deep_find(body, "device_id")
    if not device_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="无法从报文中识别 device_id"
        )

    evt = data.get("evt")

    # ---- A) 30s 结构化帧 ----
    if evt == "frame":
        async with AsyncSessionLocal() as db:
            frame = await DeviceService.write_frame_from_webhook(db, device_id, data)
        return {"code": 0, "message": "ok", "kind": "frame",
                "device_id": device_id, "frame_id": frame.id}

    # ---- B) 设备上线报到（固件 MQTT 每次连上发一次）----
    if evt == "online":
        async with AsyncSessionLocal() as db:
            device = await DeviceService.register_online(db, device_id)
            owner_bound = device.owner_id is not None
            device_pk = device.id
        return {"code": 0, "message": "ok", "kind": "online", "device_id": device_id,
                "device_pk": device_pk, "registered": True, "owner_bound": owner_bound}

    # ---- B2) cfg 快照：BLE 切开关时设备补发（云端可查镜像，非权威；不补传）----
    if evt == "cfg":
        async with AsyncSessionLocal() as db:
            cfg = await DeviceService.upsert_device_cfg(db, device_id, data.get("cfg") or {})
        return {"code": 0, "message": "ok", "kind": "cfg",
                "device_id": device_id, "cfg": cfg}

    # ---- C) 平台「设备状态变更」事件（上线/离线）----
    online_state = _deep_find_online_state(body)
    if online_state is None:
        online_state = _deep_find_online_state(data)
    if online_state is not None:
        async with AsyncSessionLocal() as db:
            device = await DeviceService.register_online(db, device_id)
            device.is_online = online_state
            await db.commit()
            owner_bound = device.owner_id is not None
            device_pk = device.id
        return {"code": 0, "message": "ok", "kind": "status", "device_id": device_id,
                "device_pk": device_pk, "online": online_state,
                "owner_bound": owner_bound}

    # ---- D) 其它报文兜底：注册 + 置在线（保证设备一定能进 devices 表）----
    async with AsyncSessionLocal() as db:
        device = await DeviceService.register_online(db, device_id)
        owner_bound = device.owner_id is not None
        device_pk = device.id
    return {"code": 0, "message": "ok", "kind": "register", "device_id": device_id,
            "device_pk": device_pk, "registered": True, "owner_bound": owner_bound}


@router.post(
    "/huawei/online",
    summary="设备上线报到 webhook（免登录，自动注册）",
    description=(
        "IoTDA 规则引擎把设备 /device/{id}/online 主题转发到本端点。\n"
        "payload: {\"device_id\":\"..\",\"evt\":\"online\"}\n"
        "作用：自动注册设备（待归属）+ 置 is_online=true + 刷新 last_seen。\n"
        "为什么必需：三开关全 OFF 时设备不发任何帧，若没有这条报到，"
        "首次配网的设备永远不会进 devices 表，小程序 bind 会失败。"
    ),
)
async def huawei_online_webhook(
    request: Request,
    x_webhook_token: str | None = Header(default=None),
) -> dict:
    if settings.http_webhook_token and x_webhook_token != settings.http_webhook_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的 webhook token"
        )
    body = await request.json()
    logger.info("收到上线报到: %s", body)

    from app.ingestion.base import _to_dict, _unwrap_envelope
    data = _unwrap_envelope(_to_dict(body))
    device_id = data.get("device_id") or _deep_find(body, "device_id")
    if not device_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="无法从报文中识别 device_id"
        )

    async with AsyncSessionLocal() as db:
        device = await DeviceService.register_online(db, device_id)
        owner_bound = device.owner_id is not None
        device_pk = device.id

    return {
        "code": 0,
        "message": "ok",
        "device_id": device_id,
        "device_pk": device_pk,
        "registered": True,
        "owner_bound": owner_bound,
    }


@router.get(
    "/devices/{device_id}/registered",
    summary="设备注册状态确认（B）",
    description=(
        "设备/华为云在上报前查询设备是否已注册及归属状态："
        "registered 是否已注册；owner_bound 是否已被用户认领。"
    ),
)
async def device_registered(device_id: str) -> dict:
    # ⚠️ 必须和其它接口一样把业务字段放进 data ——
    #    小程序 httpClient 统一 resolve(res.data.data)，
    #    以前这里是扁平结构（无 data），前端拿到 undefined，
    #    导致设备明明注册成功、前端却永远认为"尚未注册"。
    async with AsyncSessionLocal() as db:
        device = await DeviceService.get_by_device_id(db, device_id)
    if device is None:
        return {
            "code": 0,
            "message": "ok",
            "data": {
                "device_id": device_id,
                "registered": False,
                "owner_bound": False,
                "online": False,
            },
        }
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "device_id": device_id,
            "registered": True,
            "owner_bound": device.owner_id is not None,
            "online": device.is_online,
        },
    }


async def _resolve_device(device_id: str) -> Device:
    async with AsyncSessionLocal() as db:
        device = (
            await db.execute(select(Device).where(Device.device_id == device_id))
        ).scalar_one_or_none()
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="设备不存在，请先在客户端注册"
        )
    return device


@router.get(
    "/devices/{device_id}/commands/pending",
    summary="设备待执行指令（设备轮询）",
    description=(
        "设备/华为云侧按 device_id 轮询待执行的控制指令，无需登录。"
        "可与 /api/v1/ingest/huawei 相同的 X-Webhook-Token 保护。"
    ),
)
async def list_pending_commands(device_id: str) -> dict:
    device = await _resolve_device(device_id)
    async with AsyncSessionLocal() as db:
        cmds = await DeviceService.list_pending_commands(db, device)
        data = [
            {"id": c.id, "command": c.command, "value": c.value, "created_at": c.created_at.isoformat()}
            for c in cmds
        ]
    return {"code": 0, "message": "ok", "data": data}


@router.post(
    "/devices/{device_id}/commands/{command_id}/ack",
    summary="指令执行结果回执",
    description="设备执行完成后回写状态（done / failed）。",
)
async def ack_command(
    device_id: str,
    command_id: int,
    result: dict = Body(default={"status": "done"}),
) -> dict:
    device = await _resolve_device(device_id)
    error = result.get("error") if result.get("status") == "failed" else None
    async with AsyncSessionLocal() as db:
        updated = await DeviceService.ack_command(
            db, device, command_id, result.get("status", "done"), error=error
        )
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="指令不存在")
    return {"code": 0, "message": "ok"}


# ★ 30s 结构化帧接入（IoTDA 规则引擎转发 /device/{id}/frame）
@router.post(
    "/huawei/frame",
    summary="30s 结构化数据帧 webhook（免登录）",
    description=(
        "IoTDA 规则引擎把设备 /device/{id}/frame 主题转发到本端点。"
        "自动注册设备（若不存在）+ 落 sleep_frames 表。\n"
        "payload: {\"device_id\":\"..\",\"evt\":\"frame\",\"start\":..,\"end\":..,"
        "\"stage\":..,\"hr\":..,\"rr\":..,\"motion\":..,\"snore_sec\":..,\"flags\":..,"
        "\"cfg\":{...}}；未启用通道为 -1。"
    ),
)
async def huawei_frame_webhook(
    request: Request,
    x_webhook_token: str | None = Header(default=None),
) -> dict:
    if settings.http_webhook_token and x_webhook_token != settings.http_webhook_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的 webhook token"
        )
    body = await request.json()
    logger.info("收到 30s 帧推送: %s", body)

    # 华为云规则引擎可能把 payload 包在 notify_data.body 里，先解包
    from app.ingestion.base import _to_dict, _unwrap_envelope
    data = _unwrap_envelope(_to_dict(body))
    # device_id 在 frame payload 顶层
    device_id = data.get("device_id") or _deep_find(body, "device_id")
    if not device_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="无法从报文中识别 device_id"
        )

    async with AsyncSessionLocal() as db:
        frame = await DeviceService.write_frame_from_webhook(db, device_id, data)
    return {"code": 0, "message": "ok", "device_id": device_id, "frame_id": frame.id}


# 注：原 POST /ingest/huawei/cfg 已删除（2026-10-08）。v2.4 起恢复 cfg 上云，
#     但不再需要独立端点：设备把 cfg 作为 evt:"cfg" 走同一个系统主题上行
#     （见本文件 B2 分流），权威状态仍在设备本地 NVS，device_cfg 表只是快照。