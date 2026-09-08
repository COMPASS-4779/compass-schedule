"""
LINE Messaging API Webhook 中継（Render側）

lw_relay.py（LINE WORKS 用）と同じ構成だが、LINE 固有の事情が2つある。

  1. 応答メッセージ（Reply API）は課金対象外だが、replyToken がすぐ失効する。
     ローカルエージェントのポーリング（60秒間隔）では間に合わないため、
     受領返信だけはこのサーバが webhook を受けた瞬間に返す。

  2. LINE のファイルメッセージは fileName / fileSize を含む。
     （LINE WORKS と違い、ダウンロード前にファイル名が分かる）

ファイルの実体はここでは扱わない。ローカルエージェントが
  GET https://api-data.line.me/v2/bot/message/{messageId}/content
から直接取得する。

必要な環境変数（Render の Environment に設定）:
  LINE_CHANNEL_SECRET       : Messaging API チャネルのチャネルシークレット（署名検証用）
  LINE_CHANNEL_ACCESS_TOKEN : チャネルアクセストークン（受領返信用）
  LINE_AGENT_TOKEN          : ローカルエージェント認証用（未設定なら LW_AGENT_TOKEN を流用）
  LINE_WATCH_GROUPS         : 保存対象の groupId をカンマ区切り。未設定なら全て受理
  LINE_ACK_TEXT             : 受領時の自動返信文。空なら返信しない
                              {filename} が使える（例: 受け取りました: {filename}）
"""

import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests
from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel
from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Text
from sqlalchemy.orm import Session

from database import Base, SessionLocal

log = logging.getLogger("line_relay")

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_AGENT_TOKEN = os.environ.get("LINE_AGENT_TOKEN", "") or os.environ.get(
    "LW_AGENT_TOKEN", ""
)
LINE_WATCH_GROUPS = [
    g.strip() for g in os.environ.get("LINE_WATCH_GROUPS", "").split(",") if g.strip()
]
LINE_ACK_TEXT = os.environ.get("LINE_ACK_TEXT", "")

REPLY_URL = "https://api.line.me/v2/bot/message/reply"
MAX_RETRY = 5

# 保存対象にするメッセージタイプ
SAVEABLE_TYPES = ("image", "file", "video", "audio")


# --------------------------------------------------------------------------
# モデル
# --------------------------------------------------------------------------
class LineEvent(Base):
    """LINE から届いた webhook イベントのキュー"""

    __tablename__ = "line_events"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, index=True)
    event_type = Column(String, index=True)       # image / file / text / join / other
    source_type = Column(String)                  # group / room / user
    group_id = Column(String, index=True)         # room の場合は roomId を入れる
    user_id = Column(String)
    message_id = Column(String, unique=True, index=True, nullable=True)
    file_name = Column(String, nullable=True)
    file_size = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=True)
    event_time = Column(DateTime(timezone=True), nullable=True)
    raw = Column(Text)
    status = Column(String, default="pending", index=True)
    retry_count = Column(Integer, default=0)
    last_error = Column(Text, nullable=True)
    saved_path = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# --------------------------------------------------------------------------
# 共通
# --------------------------------------------------------------------------
router = APIRouter(prefix="/line", tags=["line"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _require_agent(authorization: Optional[str]) -> bool:
    if not LINE_AGENT_TOKEN:
        return False
    if not authorization or not authorization.startswith("Bearer "):
        return False
    return hmac.compare_digest(authorization[7:], LINE_AGENT_TOKEN)


def _verify_signature(body: bytes, signature: Optional[str]) -> bool:
    """X-Line-Signature（チャネルシークレットによる HMAC-SHA256 → Base64）"""
    if not LINE_CHANNEL_SECRET or not signature:
        return False
    expected = base64.b64encode(
        hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def _source_id(source: dict):
    """グループ / 複数人トーク / 1:1 のいずれかの識別子を返す"""
    stype = source.get("type")
    if stype == "group":
        return source.get("groupId")
    if stype == "room":
        return source.get("roomId")
    return source.get("userId")


def _reply(reply_token: str, text: str):
    """
    応答メッセージ（課金対象外）。replyToken は短時間で失効するため、
    webhook を受けたこのタイミングで送る必要がある。
    """
    if not (reply_token and text and LINE_CHANNEL_ACCESS_TOKEN):
        return
    try:
        r = requests.post(
            REPLY_URL,
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"replyToken": reply_token, "messages": [{"type": "text", "text": text[:5000]}]},
            timeout=5,
        )
        if r.status_code >= 400:
            log.warning("reply 失敗 (%s): %s", r.status_code, r.text[:300])
    except requests.RequestException as e:
        log.warning("reply 送信エラー: %s", e)


# --------------------------------------------------------------------------
# (1) LINE からの Webhook
# --------------------------------------------------------------------------
@router.post("/callback")
async def line_callback(
    request: Request,
    x_line_signature: str = Header(None, alias="X-Line-Signature"),
    db: Session = Depends(get_db),
):
    body = await request.body()

    if not _verify_signature(body, x_line_signature):
        log.warning("line_callback: signature mismatch")
        return Response(status_code=200)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return Response(status_code=200)

    for ev in payload.get("events", []):
        try:
            _handle_event(ev, db)
        except Exception as e:                      # 1件の失敗で全体を落とさない
            db.rollback()
            log.exception("line_callback: event処理に失敗: %s", e)

    return Response(status_code=200)


def _handle_event(ev: dict, db: Session):
    etype = ev.get("type")
    source = ev.get("source") or {}
    src_id = _source_id(source)
    reply_token = ev.get("replyToken")
    ts = ev.get("timestamp")
    event_time = (
        datetime.fromtimestamp(ts / 1000, tz=timezone.utc) if isinstance(ts, int) else None
    )

    # グループへの参加イベントは groupId 採取に使えるので記録しておく
    if etype in ("join", "memberJoined"):
        db.add(
            LineEvent(
                event_type=etype,
                source_type=source.get("type"),
                group_id=src_id,
                user_id=source.get("userId"),
                event_time=event_time,
                raw=json.dumps(ev, ensure_ascii=False),
                status="skipped",
            )
        )
        db.commit()
        return

    if etype != "message":
        return

    msg = ev.get("message") or {}
    mtype = msg.get("type")
    message_id = msg.get("id")

    if mtype not in SAVEABLE_TYPES:
        # テキスト等は groupId 採取用に記録だけ残す
        db.add(
            LineEvent(
                event_type=mtype or "other",
                source_type=source.get("type"),
                group_id=src_id,
                user_id=source.get("userId"),
                message_id=message_id,
                event_time=event_time,
                raw=json.dumps(ev, ensure_ascii=False),
                status="skipped",
            )
        )
        db.commit()
        return

    if LINE_WATCH_GROUPS and src_id not in LINE_WATCH_GROUPS:
        status = "skipped"
    else:
        status = "pending"

    if message_id and db.query(LineEvent).filter(LineEvent.message_id == message_id).first():
        return                                       # 重複 webhook

    file_name = msg.get("fileName")                  # file タイプのみ含まれる
    db.add(
        LineEvent(
            event_type=mtype,
            source_type=source.get("type"),
            group_id=src_id,
            user_id=source.get("userId"),
            message_id=message_id,
            file_name=file_name,
            file_size=msg.get("fileSize"),
            event_time=event_time,
            raw=json.dumps(ev, ensure_ascii=False),
            status=status,
        )
    )
    db.commit()

    # 受領返信（Reply API は無料。ここで返さないと replyToken が失効する）
    if status == "pending" and LINE_ACK_TEXT:
        _reply(reply_token, LINE_ACK_TEXT.replace("{filename}", file_name or "画像"))


# --------------------------------------------------------------------------
# (2) ローカルエージェント向け
# --------------------------------------------------------------------------
@router.get("/queue")
def line_queue(
    limit: int = 20,
    authorization: str = Header(None),
    db: Session = Depends(get_db),
):
    if not _require_agent(authorization):
        return Response(status_code=401)

    limit = max(1, min(limit, 100))
    rows = (
        db.query(LineEvent)
        .filter(LineEvent.status == "pending", LineEvent.retry_count < MAX_RETRY)
        .order_by(LineEvent.id.asc())
        .limit(limit)
        .all()
    )
    return {
        "success": True,
        "events": [
            {
                "id": r.id,
                "event_type": r.event_type,
                "group_id": r.group_id,
                "source_type": r.source_type,
                "user_id": r.user_id,
                "message_id": r.message_id,
                "file_name": r.file_name,
                "file_size": r.file_size,
                "event_time": r.event_time.isoformat() if r.event_time else None,
                "retry_count": r.retry_count,
            }
            for r in rows
        ],
    }


class AckBody(BaseModel):
    saved_path: Optional[str] = None
    status: str = "done"


class FailBody(BaseModel):
    error: str = ""
    permanent: bool = False


@router.post("/events/{event_id}/ack")
def line_ack(
    event_id: int,
    body: AckBody,
    authorization: str = Header(None),
    db: Session = Depends(get_db),
):
    if not _require_agent(authorization):
        return Response(status_code=401)
    row = db.query(LineEvent).filter(LineEvent.id == event_id).first()
    if not row:
        return {"success": False, "message": "not found"}
    row.status = body.status if body.status in ("done", "skipped") else "done"
    row.saved_path = body.saved_path
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"success": True}


@router.post("/events/{event_id}/fail")
def line_fail(
    event_id: int,
    body: FailBody,
    authorization: str = Header(None),
    db: Session = Depends(get_db),
):
    if not _require_agent(authorization):
        return Response(status_code=401)
    row = db.query(LineEvent).filter(LineEvent.id == event_id).first()
    if not row:
        return {"success": False, "message": "not found"}
    row.retry_count = (row.retry_count or 0) + 1
    row.last_error = (body.error or "")[:2000]
    row.updated_at = datetime.now(timezone.utc)
    if body.permanent or row.retry_count >= MAX_RETRY:
        row.status = "failed"
    db.commit()
    return {"success": True, "retry_count": row.retry_count, "status": row.status}


# --------------------------------------------------------------------------
# (3) 運用・調査用
# --------------------------------------------------------------------------
@router.get("/discover")
def line_discover(
    limit: int = 30,
    authorization: str = Header(None),
    db: Session = Depends(get_db),
):
    """
    groupId 採取用。公式アカウントをグループに招待して誰かが発言すると、
    ここに groupId が現れる。
    """
    if not _require_agent(authorization):
        return Response(status_code=401)

    limit = max(1, min(limit, 100))
    rows = db.query(LineEvent).order_by(LineEvent.id.desc()).limit(limit).all()
    return {
        "success": True,
        "watch_groups": LINE_WATCH_GROUPS or "(未設定＝全グループ受理)",
        "events": [
            {
                "id": r.id,
                "type": r.event_type,
                "source_type": r.source_type,
                "group_id": r.group_id,
                "user_id": r.user_id,
                "file_name": r.file_name,
                "status": r.status,
                "event_time": r.event_time.isoformat() if r.event_time else None,
                "error": r.last_error,
            }
            for r in rows
        ],
    }


@router.get("/status")
def line_status(authorization: str = Header(None), db: Session = Depends(get_db)):
    if not _require_agent(authorization):
        return Response(status_code=401)
    counts = {}
    for st in ("pending", "done", "failed", "skipped"):
        counts[st] = db.query(LineEvent).filter(LineEvent.status == st).count()
    return {
        "success": True,
        "counts": counts,
        "channel_secret_configured": bool(LINE_CHANNEL_SECRET),
        "access_token_configured": bool(LINE_CHANNEL_ACCESS_TOKEN),
        "agent_token_configured": bool(LINE_AGENT_TOKEN),
        "ack_text": LINE_ACK_TEXT or "(未設定＝返信しない)",
        "watch_groups": LINE_WATCH_GROUPS or "(未設定＝全グループ受理)",
    }
