"""
LINE WORKS Bot Callback 中継（Render側）

役割はこれだけ:
  1. LINE WORKS からの Callback を署名検証して受け取り、DB にイベントを積む
  2. ローカルエージェントに未処理イベントを渡し、完了報告(ACK)を受ける

ファイルの実体はここでは扱わない（Render のディスクは揮発性のため）。
実体のダウンロードはローカルエージェントが LINE WORKS から直接行う。

必要な環境変数（Render の Environment に設定）:
  LW_BOT_SECRET     : Developer Console の Bot Secret（署名検証用・必須）
  LW_AGENT_TOKEN    : ローカルエージェント認証用の任意の長い文字列（必須）
  LW_WATCH_CHANNELS : 保存対象の channelId をカンマ区切り。
                      未設定なら全チャンネルを pending にする（channelId 採取モード）
"""

import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel
from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Text
from sqlalchemy.orm import Session

from database import Base, SessionLocal

log = logging.getLogger("lw_relay")

LW_BOT_SECRET = os.environ.get("LW_BOT_SECRET", "")
LW_AGENT_TOKEN = os.environ.get("LW_AGENT_TOKEN", "")
LW_WATCH_CHANNELS = [
    c.strip() for c in os.environ.get("LW_WATCH_CHANNELS", "").split(",") if c.strip()
]

MAX_RETRY = 5


# --------------------------------------------------------------------------
# モデル
# --------------------------------------------------------------------------
class LWEvent(Base):
    """LINE WORKS から届いた Callback イベントのキュー"""

    __tablename__ = "lw_events"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, index=True)
    event_type = Column(String, index=True)      # file / image / text / other
    channel_id = Column(String, index=True)
    user_id = Column(String)
    file_id = Column(String, unique=True, index=True, nullable=True)
    issued_time = Column(DateTime(timezone=True), nullable=True)
    raw = Column(Text)
    status = Column(String, default="pending", index=True)  # pending/done/failed/skipped
    retry_count = Column(Integer, default=0)
    last_error = Column(Text, nullable=True)
    saved_path = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class LWHeartbeat(Base):
    """ローカルエージェントの死活監視"""

    __tablename__ = "lw_heartbeat"

    id = Column(Integer, primary_key=True)
    agent_name = Column(String, unique=True, index=True)
    last_seen = Column(DateTime(timezone=True))
    note = Column(Text, nullable=True)


# --------------------------------------------------------------------------
# 共通
# --------------------------------------------------------------------------
router = APIRouter(prefix="/lw", tags=["lineworks"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _require_agent(authorization: Optional[str]) -> bool:
    """ローカルエージェントからのアクセスかを検証する"""
    if not LW_AGENT_TOKEN:
        return False
    if not authorization or not authorization.startswith("Bearer "):
        return False
    return hmac.compare_digest(authorization[7:], LW_AGENT_TOKEN)


def _verify_signature(body: bytes, signature: Optional[str]) -> bool:
    """X-WORKS-Signature（Bot Secret による HMAC-SHA256 → Base64）の検証"""
    if not LW_BOT_SECRET or not signature:
        return False
    expected = base64.b64encode(
        hmac.new(LW_BOT_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def _parse_issued_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# (1) LINE WORKS からの Callback
# --------------------------------------------------------------------------
@router.post("/callback")
async def lw_callback(
    request: Request,
    x_works_signature: str = Header(None, alias="X-WORKS-Signature"),
    db: Session = Depends(get_db),
):
    """
    Callback は再送されないため、
      「検証 → 最小限の INSERT → 200 を返す」
    以外の処理をここでやらないこと（重い処理はローカルエージェント側）。
    """
    body = await request.body()

    if not _verify_signature(body, x_works_signature):
        # 攻撃者に情報を与えない。かつ再送させる意味もないので 200 で握る
        log.warning("lw_callback: signature mismatch")
        return Response(status_code=200)

    try:
        ev = json.loads(body)
    except json.JSONDecodeError:
        log.warning("lw_callback: invalid json")
        return Response(status_code=200)

    if ev.get("type") != "message":
        return Response(status_code=200)

    source = ev.get("source") or {}
    content = ev.get("content") or {}
    ctype = content.get("type")
    channel_id = source.get("channelId")   # 1:1 トークルームでは付かない
    file_id = content.get("fileId")

    # 保存対象かどうかの判定
    if ctype not in ("file", "image"):
        status = "skipped"                  # テキスト等は channelId 採取用に記録だけ残す
    elif LW_WATCH_CHANNELS and channel_id not in LW_WATCH_CHANNELS:
        status = "skipped"
    else:
        status = "pending"

    try:
        if file_id:
            dup = db.query(LWEvent).filter(LWEvent.file_id == file_id).first()
            if dup:
                return Response(status_code=200)   # 重複 Callback

        db.add(
            LWEvent(
                event_type=ctype or "other",
                channel_id=channel_id,
                user_id=source.get("userId"),
                file_id=file_id,
                issued_time=_parse_issued_time(ev.get("issuedTime")),
                raw=json.dumps(ev, ensure_ascii=False),
                status=status,
            )
        )
        db.commit()
    except Exception as e:                       # DB 障害でも 200 は返す（再送されないため）
        db.rollback()
        log.exception("lw_callback: insert failed: %s", e)

    return Response(status_code=200)


# --------------------------------------------------------------------------
# (2) ローカルエージェント向け
# --------------------------------------------------------------------------
@router.get("/queue")
def lw_queue(
    limit: int = 20,
    authorization: str = Header(None),
    db: Session = Depends(get_db),
):
    """未処理（pending / リトライ待ち）のイベントを古い順に返す"""
    if not _require_agent(authorization):
        return Response(status_code=401)

    limit = max(1, min(limit, 100))
    rows = (
        db.query(LWEvent)
        .filter(LWEvent.status == "pending", LWEvent.retry_count < MAX_RETRY)
        .order_by(LWEvent.id.asc())
        .limit(limit)
        .all()
    )
    return {
        "success": True,
        "events": [
            {
                "id": r.id,
                "event_type": r.event_type,
                "channel_id": r.channel_id,
                "user_id": r.user_id,
                "file_id": r.file_id,
                "issued_time": r.issued_time.isoformat() if r.issued_time else None,
                "retry_count": r.retry_count,
            }
            for r in rows
        ],
    }


class AckBody(BaseModel):
    saved_path: Optional[str] = None
    status: str = "done"          # done / skipped


class FailBody(BaseModel):
    error: str = ""
    permanent: bool = False       # True なら retry せず failed 確定


@router.post("/events/{event_id}/ack")
def lw_ack(
    event_id: int,
    body: AckBody,
    authorization: str = Header(None),
    db: Session = Depends(get_db),
):
    if not _require_agent(authorization):
        return Response(status_code=401)

    row = db.query(LWEvent).filter(LWEvent.id == event_id).first()
    if not row:
        return {"success": False, "message": "not found"}
    row.status = body.status if body.status in ("done", "skipped") else "done"
    row.saved_path = body.saved_path
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"success": True}


@router.post("/events/{event_id}/fail")
def lw_fail(
    event_id: int,
    body: FailBody,
    authorization: str = Header(None),
    db: Session = Depends(get_db),
):
    if not _require_agent(authorization):
        return Response(status_code=401)

    row = db.query(LWEvent).filter(LWEvent.id == event_id).first()
    if not row:
        return {"success": False, "message": "not found"}
    row.retry_count = (row.retry_count or 0) + 1
    row.last_error = (body.error or "")[:2000]
    row.updated_at = datetime.now(timezone.utc)
    if body.permanent or row.retry_count >= MAX_RETRY:
        row.status = "failed"
    db.commit()
    return {"success": True, "retry_count": row.retry_count, "status": row.status}


@router.post("/heartbeat")
def lw_heartbeat(
    agent: str = "local",
    authorization: str = Header(None),
    db: Session = Depends(get_db),
):
    if not _require_agent(authorization):
        return Response(status_code=401)

    row = db.query(LWHeartbeat).filter(LWHeartbeat.agent_name == agent).first()
    if not row:
        row = LWHeartbeat(agent_name=agent)
        db.add(row)
    row.last_seen = datetime.now(timezone.utc)
    db.commit()
    return {"success": True}


# --------------------------------------------------------------------------
# (3) 運用・調査用
# --------------------------------------------------------------------------
@router.get("/discover")
def lw_discover(
    limit: int = 30,
    authorization: str = Header(None),
    db: Session = Depends(get_db),
):
    """
    channelId 採取用。Bot をトークルームに招待して何か発言すると
    ここに channelId が現れるので、それを schedule.yaml に転記する。
    """
    if not _require_agent(authorization):
        return Response(status_code=401)

    limit = max(1, min(limit, 100))
    rows = db.query(LWEvent).order_by(LWEvent.id.desc()).limit(limit).all()
    return {
        "success": True,
        "watch_channels": LW_WATCH_CHANNELS or "(未設定＝全チャンネル受理)",
        "events": [
            {
                "id": r.id,
                "type": r.event_type,
                "channel_id": r.channel_id,
                "user_id": r.user_id,
                "status": r.status,
                "issued_time": r.issued_time.isoformat() if r.issued_time else None,
                "error": r.last_error,
            }
            for r in rows
        ],
    }


@router.get("/status")
def lw_status(authorization: str = Header(None), db: Session = Depends(get_db)):
    if not _require_agent(authorization):
        return Response(status_code=401)

    counts = {}
    for st in ("pending", "done", "failed", "skipped"):
        counts[st] = db.query(LWEvent).filter(LWEvent.status == st).count()
    hb = db.query(LWHeartbeat).all()
    return {
        "success": True,
        "counts": counts,
        "agents": [
            {"name": h.agent_name, "last_seen": h.last_seen.isoformat() if h.last_seen else None}
            for h in hb
        ],
        "bot_secret_configured": bool(LW_BOT_SECRET),
        "agent_token_configured": bool(LW_AGENT_TOKEN),
    }
