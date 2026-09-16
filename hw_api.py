"""
宿題自動配信 コントロールAPI（Render側）

お名前.com に置くコントロール画面（PHP）から呼ばれる API と、
配信予定を実際に送るスケジューラをまとめたもの。

これまでローカルPCの常駐エージェントがやっていた「時刻が来たら Drive を見て
LINE に送る」処理を、こちらへ移している。PC が落ちていても配信される。

必要な環境変数:
  HW_API_TOKEN               : コントロール画面からのアクセスを認証するトークン
  HW_GOOGLE_CLIENT_ID        : Drive の OAuth クライアントID
  HW_GOOGLE_CLIENT_SECRET    : Drive の OAuth クライアントシークレット
  HW_GOOGLE_REFRESH_TOKEN    : Drive のリフレッシュトークン
  HW_DRIVE_PARENT_FOLDER_ID  : Drive の作業フォルダ（配信元/配信済み/受信先 の親）
  LINE_HW_CHANNEL_ACCESS_TOKEN : 配信に使う（line_relay.py と共用）
  HW_TIMEZONE                : 既定 Asia/Tokyo
"""

import hmac
import io
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests
from fastapi import APIRouter, Depends, File, Form, Header, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import (BigInteger, Boolean, Column, DateTime, ForeignKey,
                        Integer, String, Text)
from sqlalchemy.orm import Session, relationship

from database import Base, SessionLocal

log = logging.getLogger("hw_api")

HW_API_TOKEN = os.environ.get("HW_API_TOKEN", "")
HW_DRIVE_PARENT_FOLDER_ID = os.environ.get("HW_DRIVE_PARENT_FOLDER_ID", "")
LINE_TOKEN = os.environ.get("LINE_HW_CHANNEL_ACCESS_TOKEN", "")
TZ_NAME = os.environ.get("HW_TIMEZONE", "Asia/Tokyo")

PUSH_URL = "https://api.line.me/v2/bot/message/push"
QUOTA_URL = "https://api.line.me/v2/bot/message/quota"
QUOTA_USED_URL = "https://api.line.me/v2/bot/message/quota/consumption"

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(TZ_NAME)
except Exception:                                     # pragma: no cover
    TZ = timezone(timedelta(hours=9))

BIGINT = BigInteger().with_variant(Integer, "sqlite")


# ======================================================================
# モデル
# ======================================================================
class HwStudent(Base):
    """配信先の生徒（＝LINEグループ1つ）"""

    __tablename__ = "hw_students"

    id = Column(BIGINT, primary_key=True, index=True)
    name = Column(String, nullable=False)             # Drive のフォルダ名にもなる
    group_id = Column(String, index=True)             # LINE の groupId
    enabled = Column(Boolean, default=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class HwDelivery(Base):
    """1回分の配信予定"""

    __tablename__ = "hw_deliveries"

    id = Column(BIGINT, primary_key=True, index=True)
    student_id = Column(BIGINT, ForeignKey("hw_students.id"), index=True)
    scheduled_at = Column(DateTime(timezone=True), index=True)
    message = Column(Text, default="")
    status = Column(String, default="pending", index=True)   # pending/sent/failed/canceled
    sent_at = Column(DateTime(timezone=True), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    student = relationship("HwStudent")
    files = relationship("HwDeliveryFile", cascade="all, delete-orphan")


class HwDeliveryFile(Base):
    """配信予定に紐づくファイル（実体は Drive 上）"""

    __tablename__ = "hw_delivery_files"

    id = Column(BIGINT, primary_key=True, index=True)
    delivery_id = Column(BIGINT, ForeignKey("hw_deliveries.id"), index=True)
    drive_file_id = Column(String, nullable=False)
    file_name = Column(String)
    size = Column(BIGINT, nullable=True)


# ======================================================================
# 共通
# ======================================================================
router = APIRouter(prefix="/hw/api", tags=["homework"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_token(authorization: Optional[str]) -> bool:
    if not HW_API_TOKEN:
        return False
    if not authorization or not authorization.startswith("Bearer "):
        return False
    return hmac.compare_digest(authorization[7:], HW_API_TOKEN)


def _auth_or_401(authorization):
    if not require_token(authorization):
        return Response(status_code=401)
    return None


def to_local(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


def parse_local(text):
    """画面から来る "2026-09-21 08:30" を UTC の datetime にする"""
    if not text:
        return None
    s = str(text).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[: len(fmt) + 2].strip(), fmt)
            return dt.replace(tzinfo=TZ).astimezone(timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"日時の形式が不正です: {text}")


# ======================================================================
# Google Drive（リフレッシュトークンから組み立てる）
# ======================================================================
_drive_service = None
_folder_cache = {}


def drive():
    """Drive サービスを返す（初回だけ組み立てる）"""
    global _drive_service
    if _drive_service is not None:
        return _drive_service

    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    cid = os.environ.get("HW_GOOGLE_CLIENT_ID", "")
    csec = os.environ.get("HW_GOOGLE_CLIENT_SECRET", "")
    rtok = os.environ.get("HW_GOOGLE_REFRESH_TOKEN", "")
    if not (cid and csec and rtok):
        raise RuntimeError(
            "Drive の認証情報が未設定です"
            "（HW_GOOGLE_CLIENT_ID / HW_GOOGLE_CLIENT_SECRET / HW_GOOGLE_REFRESH_TOKEN）"
        )

    creds = Credentials(
        token=None,
        refresh_token=rtok,
        client_id=cid,
        client_secret=csec,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    _drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive_service


def ensure_folder(name, parent_id):
    key = (parent_id, name)
    if key in _folder_cache:
        return _folder_cache[key]
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    q = (f"name = '{safe}' and '{parent_id}' in parents "
         "and mimeType = 'application/vnd.google-apps.folder' and trashed = false")
    res = drive().files().list(q=q, fields="files(id)", pageSize=1).execute()
    hit = res.get("files", [])
    if hit:
        fid = hit[0]["id"]
    else:
        fid = drive().files().create(
            body={"name": name, "mimeType": "application/vnd.google-apps.folder",
                  "parents": [parent_id]},
            fields="id",
        ).execute()["id"]
    _folder_cache[key] = fid
    return fid


def ensure_path(path, parent_id=None):
    fid = parent_id or HW_DRIVE_PARENT_FOLDER_ID
    for part in [p for p in str(path).replace("\\", "/").split("/") if p.strip()]:
        fid = ensure_folder(part.strip(), fid)
    return fid


def share_link(file_id):
    """リンクを知っている全員が閲覧できる状態にしてURLを返す"""
    perms = drive().permissions().list(fileId=file_id, fields="permissions(type)").execute()
    if not any(p.get("type") == "anyone" for p in perms.get("permissions", [])):
        drive().permissions().create(
            fileId=file_id, body={"role": "reader", "type": "anyone"}
        ).execute()
    return f"https://drive.google.com/file/d/{file_id}/view"


def move_file(file_id, new_parent_id):
    meta = drive().files().get(fileId=file_id, fields="parents").execute()
    drive().files().update(
        fileId=file_id, addParents=new_parent_id,
        removeParents=",".join(meta.get("parents", [])), fields="id",
    ).execute()


# ======================================================================
# LINE 配信
# ======================================================================
def line_push(group_id, text):
    if not LINE_TOKEN:
        raise RuntimeError("LINE_HW_CHANNEL_ACCESS_TOKEN が設定されていません")
    r = requests.post(
        PUSH_URL,
        headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
        json={"to": group_id, "messages": [{"type": "text", "text": text[:5000]}]},
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"LINE送信に失敗 ({r.status_code}): {r.text[:300]}")


def send_delivery(db: Session, d: HwDelivery):
    """
    1件の配信予定を送る。
    複数ファイルでも必ず1通にまとめる（課金が「メッセージ数×人数」のため）。
    """
    student = db.query(HwStudent).filter(HwStudent.id == d.student_id).first()
    if student is None or not student.group_id:
        raise RuntimeError("配信先の生徒またはgroupIdが未設定です")

    files = db.query(HwDeliveryFile).filter(HwDeliveryFile.delivery_id == d.id).all()
    if not files:
        raise RuntimeError("配信するファイルがありません")

    parts = []
    if d.message:
        parts.append(d.message)
    for f in files:
        parts.append(f"{f.file_name}\n{share_link(f.drive_file_id)}")

    line_push(student.group_id, "\n\n".join(parts))

    # 送信済みフォルダへ移す（配信元に残さない）
    done = ensure_path(f"配信済み/{student.name}")
    for f in files:
        try:
            move_file(f.drive_file_id, done)
        except Exception as e:
            log.warning("配信済みへの移動に失敗 (%s): %s", f.file_name, e)

    d.status = "sent"
    d.sent_at = datetime.now(timezone.utc)
    d.error = None
    db.commit()
    log.info("配信しました: %s (%d件)", student.name, len(files))


def run_due_deliveries():
    """スケジューラから毎分呼ばれる。時刻が来た配信予定を送る"""
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        due = (
            db.query(HwDelivery)
            .filter(HwDelivery.status == "pending", HwDelivery.scheduled_at <= now)
            .order_by(HwDelivery.scheduled_at.asc())
            .limit(20)
            .all()
        )
        for d in due:
            try:
                send_delivery(db, d)
            except Exception as e:
                db.rollback()
                d.status = "failed"
                d.error = str(e)[:2000]
                db.commit()
                log.error("配信に失敗 (id=%s): %s", d.id, e)
    except Exception as e:
        log.exception("配信処理で想定外のエラー: %s", e)
    finally:
        db.close()


# ======================================================================
# 受信（LINEに投稿された画像・ファイルを Drive へ保存）
# ======================================================================
CONTENT_URL = "https://api-data.line.me/v2/bot/message/{message_id}/content"

MIME_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "video/mp4": ".mp4", "audio/x-m4a": ".m4a",
    "application/pdf": ".pdf",
}

INVALID_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')      # ファイル名に使えない文字


def received_filename(student_name, event_time, file_name, message_id, content_type):
    """受信ファイルの保存名を組み立てる（生徒名_日付_時刻.拡張子）"""
    dt = to_local(event_time) or datetime.now(TZ)
    if file_name:
        ext = os.path.splitext(file_name)[1] or ".bin"
    else:
        # 画像・動画にはファイル名が無いので Content-Type から拡張子を決める
        ext = MIME_EXT.get((content_type or "").split(";")[0].strip(), ".bin")
    name = f"{student_name}_{dt:%Y%m%d_%H%M}{ext}"
    return INVALID_CHARS.sub("_", name).strip(". ") or f"{message_id}{ext}"


def save_received(db: Session, ev):
    """キューに積まれた1件を Drive の 受信先/生徒名 へ保存する"""
    from googleapiclient.http import MediaIoBaseUpload

    student = (
        db.query(HwStudent)
        .filter(HwStudent.group_id == ev.group_id, HwStudent.enabled.is_(True))
        .first()
    )
    if student is None:
        return "skipped"          # 登録されていないグループからの投稿は無視する

    if not LINE_TOKEN:
        raise RuntimeError("LINE_HW_CHANNEL_ACCESS_TOKEN が未設定です")

    r = requests.get(
        CONTENT_URL.format(message_id=ev.message_id),
        headers={"Authorization": f"Bearer {LINE_TOKEN}"}, timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"コンテンツ取得に失敗 ({r.status_code}): {r.text[:200]}")

    name = received_filename(student.name, ev.event_time, ev.file_name,
                             ev.message_id, r.headers.get("Content-Type"))
    folder = ensure_path(f"受信先/{student.name}")
    media = MediaIoBaseUpload(
        io.BytesIO(r.content),
        mimetype=r.headers.get("Content-Type") or "application/octet-stream",
        resumable=False,
    )
    f = drive().files().create(
        body={"name": name, "parents": [folder]}, media_body=media, fields="id,name"
    ).execute()

    ev.status = "done"
    ev.saved_path = f"受信先/{student.name}/{f.get('name')}"
    ev.updated_at = datetime.now(timezone.utc)
    db.commit()
    log.info("受信を保存しました: %s", ev.saved_path)
    return "done"


def run_pending_receives():
    """スケジューラから毎分呼ばれる。未処理の受信イベントを Drive へ保存する"""
    from line_relay import MAX_RETRY, LineHwEvent

    db = SessionLocal()
    try:
        rows = (
            db.query(LineHwEvent)
            .filter(LineHwEvent.status == "pending", LineHwEvent.retry_count < MAX_RETRY)
            .order_by(LineHwEvent.id.asc())
            .limit(20)
            .all()
        )
        for ev in rows:
            try:
                if save_received(db, ev) == "skipped":
                    ev.status = "skipped"
                    ev.updated_at = datetime.now(timezone.utc)
                    db.commit()
            except Exception as e:
                db.rollback()
                ev.retry_count = (ev.retry_count or 0) + 1
                ev.last_error = str(e)[:2000]
                ev.updated_at = datetime.now(timezone.utc)
                if ev.retry_count >= MAX_RETRY:
                    ev.status = "failed"
                db.commit()
                log.error("受信の保存に失敗 (id=%s): %s", ev.id, e)
    except Exception as e:
        log.exception("受信処理で想定外のエラー: %s", e)
    finally:
        db.close()


def start_scheduler():
    """FastAPI 起動時に呼ぶ。1分ごとに配信予定を確認する"""
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    sched = BackgroundScheduler(timezone=TZ_NAME)
    sched.add_job(
        run_due_deliveries, IntervalTrigger(minutes=1),
        id="hw:due", max_instances=1, coalesce=True,
    )
    sched.add_job(
        run_pending_receives, IntervalTrigger(minutes=1),
        id="hw:receive", max_instances=1, coalesce=True,
    )
    sched.start()
    log.info("宿題の配信・受信スケジューラを開始しました（1分間隔）")
    return sched


# ======================================================================
# API
# ======================================================================
def _student_json(s: HwStudent):
    return {"id": s.id, "name": s.name, "group_id": s.group_id,
            "enabled": bool(s.enabled), "note": s.note}


def _delivery_json(d: HwDelivery, files):
    local = to_local(d.scheduled_at)
    return {
        "id": d.id,
        "student_id": d.student_id,
        "scheduled_at": local.strftime("%Y-%m-%d %H:%M") if local else None,
        "date": local.strftime("%Y-%m-%d") if local else None,
        "time": local.strftime("%H:%M") if local else None,
        "message": d.message or "",
        "status": d.status,
        "sent_at": to_local(d.sent_at).strftime("%Y-%m-%d %H:%M") if d.sent_at else None,
        "error": d.error,
        "files": [{"id": f.id, "drive_file_id": f.drive_file_id,
                   "name": f.file_name, "size": f.size} for f in files],
    }


@router.get("/health")
def health(authorization: str = Header(None)):
    if (r := _auth_or_401(authorization)):
        return r
    return {
        "success": True,
        "timezone": TZ_NAME,
        "drive_configured": bool(os.environ.get("HW_GOOGLE_REFRESH_TOKEN")),
        "line_configured": bool(LINE_TOKEN),
        "parent_folder_configured": bool(HW_DRIVE_PARENT_FOLDER_ID),
    }


# --- 生徒 -------------------------------------------------------------
class StudentIn(BaseModel):
    name: str
    group_id: str = ""
    enabled: bool = True
    note: str = ""


@router.get("/students")
def list_students(authorization: str = Header(None), db: Session = Depends(get_db)):
    if (r := _auth_or_401(authorization)):
        return r
    rows = db.query(HwStudent).order_by(HwStudent.name).all()
    return {"success": True, "students": [_student_json(s) for s in rows]}


@router.post("/students")
def create_student(body: StudentIn, authorization: str = Header(None),
                   db: Session = Depends(get_db)):
    if (r := _auth_or_401(authorization)):
        return r
    if not body.name.strip():
        return {"success": False, "message": "名前は必須です"}
    s = HwStudent(name=body.name.strip(), group_id=body.group_id.strip(),
                  enabled=body.enabled, note=body.note)
    db.add(s)
    db.commit()
    db.refresh(s)
    # Drive のフォルダも用意しておく
    try:
        for base in ("配信元", "配信済み", "受信先"):
            ensure_path(f"{base}/{s.name}")
    except Exception as e:
        log.warning("Drive フォルダの作成に失敗: %s", e)
    return {"success": True, "student": _student_json(s)}


@router.patch("/students/{student_id}")
def update_student(student_id: int, body: StudentIn, authorization: str = Header(None),
                   db: Session = Depends(get_db)):
    if (r := _auth_or_401(authorization)):
        return r
    s = db.query(HwStudent).filter(HwStudent.id == student_id).first()
    if not s:
        return {"success": False, "message": "見つかりません"}
    s.name = body.name.strip() or s.name
    s.group_id = body.group_id.strip()
    s.enabled = body.enabled
    s.note = body.note
    db.commit()
    return {"success": True, "student": _student_json(s)}


@router.delete("/students/{student_id}")
def delete_student(student_id: int, authorization: str = Header(None),
                   db: Session = Depends(get_db)):
    if (r := _auth_or_401(authorization)):
        return r
    s = db.query(HwStudent).filter(HwStudent.id == student_id).first()
    if not s:
        return {"success": False, "message": "見つかりません"}
    pending = db.query(HwDelivery).filter(
        HwDelivery.student_id == student_id, HwDelivery.status == "pending"
    ).count()
    if pending:
        return {"success": False,
                "message": f"未送信の配信予定が {pending} 件あります。先に削除してください"}
    db.delete(s)
    db.commit()
    return {"success": True}


# --- 配信予定 ---------------------------------------------------------
class DeliveryIn(BaseModel):
    student_id: int
    scheduled_at: str                 # "2026-09-21 08:30"
    message: str = ""
    drive_file_ids: List[str] = []
    file_names: List[str] = []


@router.get("/deliveries")
def list_deliveries(date_from: str = "", date_to: str = "", student_id: int = 0,
                    status: str = "", authorization: str = Header(None),
                    db: Session = Depends(get_db)):
    """カレンダー描画用。期間で絞って返す"""
    if (r := _auth_or_401(authorization)):
        return r
    q = db.query(HwDelivery)
    if date_from:
        q = q.filter(HwDelivery.scheduled_at >= parse_local(date_from))
    if date_to:
        q = q.filter(HwDelivery.scheduled_at < parse_local(date_to) + timedelta(days=1))
    if student_id:
        q = q.filter(HwDelivery.student_id == student_id)
    if status:
        q = q.filter(HwDelivery.status == status)
    rows = q.order_by(HwDelivery.scheduled_at.asc()).limit(500).all()
    out = []
    for d in rows:
        files = db.query(HwDeliveryFile).filter(HwDeliveryFile.delivery_id == d.id).all()
        out.append(_delivery_json(d, files))
    return {"success": True, "deliveries": out}


@router.post("/deliveries")
def create_delivery(body: DeliveryIn, authorization: str = Header(None),
                    db: Session = Depends(get_db)):
    if (r := _auth_or_401(authorization)):
        return r
    try:
        when = parse_local(body.scheduled_at)
    except ValueError as e:
        return {"success": False, "message": str(e)}
    if not db.query(HwStudent).filter(HwStudent.id == body.student_id).first():
        return {"success": False, "message": "生徒が見つかりません"}

    d = HwDelivery(student_id=body.student_id, scheduled_at=when,
                   message=body.message, status="pending")
    db.add(d)
    db.commit()
    db.refresh(d)

    for i, fid in enumerate(body.drive_file_ids):
        name = body.file_names[i] if i < len(body.file_names) else fid
        db.add(HwDeliveryFile(delivery_id=d.id, drive_file_id=fid, file_name=name))
    db.commit()

    files = db.query(HwDeliveryFile).filter(HwDeliveryFile.delivery_id == d.id).all()
    return {"success": True, "delivery": _delivery_json(d, files)}


class DeliveryPatch(BaseModel):
    scheduled_at: Optional[str] = None
    message: Optional[str] = None
    status: Optional[str] = None


@router.patch("/deliveries/{delivery_id}")
def update_delivery(delivery_id: int, body: DeliveryPatch,
                    authorization: str = Header(None), db: Session = Depends(get_db)):
    if (r := _auth_or_401(authorization)):
        return r
    d = db.query(HwDelivery).filter(HwDelivery.id == delivery_id).first()
    if not d:
        return {"success": False, "message": "見つかりません"}
    if d.status == "sent":
        return {"success": False, "message": "送信済みの予定は変更できません"}
    if body.scheduled_at:
        try:
            d.scheduled_at = parse_local(body.scheduled_at)
        except ValueError as e:
            return {"success": False, "message": str(e)}
    if body.message is not None:
        d.message = body.message
    if body.status in ("pending", "canceled"):
        d.status = body.status
    db.commit()
    files = db.query(HwDeliveryFile).filter(HwDeliveryFile.delivery_id == d.id).all()
    return {"success": True, "delivery": _delivery_json(d, files)}


@router.delete("/deliveries/{delivery_id}")
def delete_delivery(delivery_id: int, authorization: str = Header(None),
                    db: Session = Depends(get_db)):
    if (r := _auth_or_401(authorization)):
        return r
    d = db.query(HwDelivery).filter(HwDelivery.id == delivery_id).first()
    if not d:
        return {"success": False, "message": "見つかりません"}
    db.query(HwDeliveryFile).filter(HwDeliveryFile.delivery_id == d.id).delete()
    db.delete(d)
    db.commit()
    return {"success": True}


@router.post("/deliveries/{delivery_id}/send-now")
def send_now(delivery_id: int, authorization: str = Header(None),
             db: Session = Depends(get_db)):
    """予定時刻を待たずに今すぐ送る"""
    if (r := _auth_or_401(authorization)):
        return r
    d = db.query(HwDelivery).filter(HwDelivery.id == delivery_id).first()
    if not d:
        return {"success": False, "message": "見つかりません"}
    if d.status == "sent":
        return {"success": False, "message": "既に送信済みです"}
    try:
        send_delivery(db, d)
    except Exception as e:
        db.rollback()
        d.status = "failed"
        d.error = str(e)[:2000]
        db.commit()
        return {"success": False, "message": str(e)}
    files = db.query(HwDeliveryFile).filter(HwDeliveryFile.delivery_id == d.id).all()
    return {"success": True, "delivery": _delivery_json(d, files)}


@router.get("/groups")
def recent_groups(limit: int = 50, authorization: str = Header(None),
                  db: Session = Depends(get_db)):
    """
    最近イベントが届いたLINEグループの一覧。

    groupId の採取に使う。/line-hw/discover でも同じことができるが、
    あちらはエージェント用トークンを要求するため、コントロール画面からは
    こちらを使う（画面が持つトークンを1つで済ませるため）。
    """
    if (r := _auth_or_401(authorization)):
        return r

    from line_relay import LineHwEvent

    limit = max(1, min(limit, 200))
    rows = (
        db.query(LineHwEvent)
        .filter(LineHwEvent.group_id.isnot(None))
        .order_by(LineHwEvent.id.desc())
        .limit(limit)
        .all()
    )

    assigned = {
        s.group_id: s.name
        for s in db.query(HwStudent).filter(HwStudent.group_id != "").all()
        if s.group_id
    }

    groups = {}
    for ev in rows:
        g = groups.setdefault(ev.group_id, {"group_id": ev.group_id, "count": 0, "last_at": None})
        g["count"] += 1
        if g["last_at"] is None and ev.event_time:
            local = to_local(ev.event_time)
            g["last_at"] = local.strftime("%Y-%m-%d %H:%M") if local else None

    out = []
    for gid, g in groups.items():
        g["assigned_to"] = assigned.get(gid)
        out.append(g)
    return {"success": True, "groups": out}


# --- 配信予定のファイル操作 -------------------------------------------
class FileIn(BaseModel):
    drive_file_id: str
    file_name: str = ""


@router.post("/deliveries/{delivery_id}/files")
def add_file(delivery_id: int, body: FileIn, authorization: str = Header(None),
             db: Session = Depends(get_db)):
    """既存の配信予定にファイルを足す（カレンダーで後から追加する用）"""
    if (r := _auth_or_401(authorization)):
        return r
    d = db.query(HwDelivery).filter(HwDelivery.id == delivery_id).first()
    if not d:
        return {"success": False, "message": "見つかりません"}
    if d.status == "sent":
        return {"success": False, "message": "送信済みの予定は変更できません"}
    dup = db.query(HwDeliveryFile).filter(
        HwDeliveryFile.delivery_id == delivery_id,
        HwDeliveryFile.drive_file_id == body.drive_file_id,
    ).first()
    if dup:
        return {"success": False, "message": "同じファイルが既に追加されています"}
    db.add(HwDeliveryFile(delivery_id=delivery_id,
                          drive_file_id=body.drive_file_id,
                          file_name=body.file_name or body.drive_file_id))
    db.commit()
    files = db.query(HwDeliveryFile).filter(HwDeliveryFile.delivery_id == d.id).all()
    return {"success": True, "delivery": _delivery_json(d, files)}


@router.delete("/deliveries/{delivery_id}/files/{file_id}")
def remove_file(delivery_id: int, file_id: int, authorization: str = Header(None),
                db: Session = Depends(get_db)):
    """配信予定からファイルを外す（Drive のファイル自体は消さない）"""
    if (r := _auth_or_401(authorization)):
        return r
    d = db.query(HwDelivery).filter(HwDelivery.id == delivery_id).first()
    if not d:
        return {"success": False, "message": "見つかりません"}
    if d.status == "sent":
        return {"success": False, "message": "送信済みの予定は変更できません"}
    db.query(HwDeliveryFile).filter(
        HwDeliveryFile.id == file_id, HwDeliveryFile.delivery_id == delivery_id
    ).delete()
    db.commit()
    files = db.query(HwDeliveryFile).filter(HwDeliveryFile.delivery_id == d.id).all()
    return {"success": True, "delivery": _delivery_json(d, files)}


@router.get("/students/{student_id}/files")
def student_files(student_id: int, authorization: str = Header(None),
                  db: Session = Depends(get_db)):
    """配信元フォルダにある、まだ送っていないファイルを返す（画面の選択肢用）"""
    if (r := _auth_or_401(authorization)):
        return r
    s = db.query(HwStudent).filter(HwStudent.id == student_id).first()
    if not s:
        return {"success": False, "message": "生徒が見つかりません"}
    try:
        folder = ensure_path(f"配信元/{s.name}")
        res = drive().files().list(
            q=f"'{folder}' in parents and trashed = false "
              "and mimeType != 'application/vnd.google-apps.folder'",
            orderBy="modifiedTime", fields="files(id,name,size,modifiedTime)", pageSize=200,
        ).execute()
    except Exception as e:
        return {"success": False, "message": f"Driveの一覧取得に失敗しました: {e}"}

    used = {
        f.drive_file_id
        for f in db.query(HwDeliveryFile)
        .join(HwDelivery, HwDelivery.id == HwDeliveryFile.delivery_id)
        .filter(HwDelivery.student_id == student_id,
                HwDelivery.status.in_(("pending", "sent")))
        .all()
    }
    return {"success": True, "files": [
        {"drive_file_id": f["id"], "name": f["name"], "size": f.get("size"),
         "assigned": f["id"] in used}
        for f in res.get("files", [])
    ]}


@router.get("/students/{student_id}/received")
def student_received(student_id: int, limit: int = 60,
                     authorization: str = Header(None), db: Session = Depends(get_db)):
    """
    受信先フォルダにある提出物の一覧を返す（新しい順）。
    画面で「誰が何を出したか」を確認するために使う。
    """
    if (r := _auth_or_401(authorization)):
        return r
    s = db.query(HwStudent).filter(HwStudent.id == student_id).first()
    if not s:
        return {"success": False, "message": "生徒が見つかりません"}

    limit = max(1, min(limit, 200))
    try:
        folder = ensure_path(f"受信先/{s.name}")
        res = drive().files().list(
            q=f"'{folder}' in parents and trashed = false "
              "and mimeType != 'application/vnd.google-apps.folder'",
            orderBy="createdTime desc",
            fields="files(id,name,size,createdTime,mimeType,webViewLink)",
            pageSize=limit,
        ).execute()
    except Exception as e:
        return {"success": False, "message": f"Driveの一覧取得に失敗しました: {e}"}

    out = []
    for f in res.get("files", []):
        created = f.get("createdTime")
        local = None
        if created:
            try:
                local = to_local(datetime.fromisoformat(created.replace("Z", "+00:00")))
            except ValueError:
                local = None
        out.append({
            "id": f["id"],
            "name": f.get("name"),
            "size": f.get("size"),
            "mime": f.get("mimeType"),
            "url": f.get("webViewLink"),
            "received_at": local.strftime("%Y-%m-%d %H:%M") if local else None,
            "date": local.strftime("%Y-%m-%d") if local else None,
        })
    return {"success": True, "student": s.name, "files": out}


# --- アップロード -----------------------------------------------------
@router.post("/upload")
async def upload(student_id: int = Form(...), file: UploadFile = File(...),
                 delivery_id: int = Form(0),
                 authorization: str = Header(None), db: Session = Depends(get_db)):
    """画面から届いたファイルを Drive の 配信元/生徒名 に置く"""
    if (r := _auth_or_401(authorization)):
        return r
    s = db.query(HwStudent).filter(HwStudent.id == student_id).first()
    if not s:
        return {"success": False, "message": "生徒が見つかりません"}

    import io as _io
    from googleapiclient.http import MediaIoBaseUpload

    data = await file.read()
    if not data:
        return {"success": False, "message": "ファイルが空です"}
    if len(data) > 50 * 1024 * 1024:
        return {"success": False, "message": "ファイルが大きすぎます（50MBまで）"}

    try:
        folder = ensure_path(f"配信元/{s.name}")
        media = MediaIoBaseUpload(_io.BytesIO(data),
                                  mimetype=file.content_type or "application/octet-stream",
                                  resumable=False)
        f = drive().files().create(
            body={"name": file.filename, "parents": [folder]},
            media_body=media, fields="id,name,size",
        ).execute()
    except Exception as e:
        log.exception("アップロードに失敗: %s", e)
        return {"success": False, "message": f"Driveへの保存に失敗しました: {e}"}

    # delivery_id が指定されていれば、その予定にそのまま紐づける
    if delivery_id:
        d = db.query(HwDelivery).filter(HwDelivery.id == delivery_id).first()
        if d and d.status != "sent":
            db.add(HwDeliveryFile(delivery_id=delivery_id, drive_file_id=f["id"],
                                  file_name=f.get("name"), size=int(f.get("size") or 0)))
            db.commit()

    return {"success": True,
            "file": {"drive_file_id": f["id"], "name": f.get("name"), "size": f.get("size")}}


# --- 通数 -------------------------------------------------------------
@router.get("/quota")
def quota(authorization: str = Header(None)):
    """当月の送信可能通数と消化数（画面のヘッダに出す）"""
    if (r := _auth_or_401(authorization)):
        return r
    if not LINE_TOKEN:
        return {"success": False, "message": "LINEのトークンが未設定です"}
    h = {"Authorization": f"Bearer {LINE_TOKEN}"}
    try:
        r1 = requests.get(QUOTA_URL, headers=h, timeout=15)
        r2 = requests.get(QUOTA_USED_URL, headers=h, timeout=15)
    except Exception as e:
        return {"success": False, "message": str(e)}

    # トークンが無効だと 401 が返る。黙って None を返すと原因が分からないので明示する
    if r1.status_code != 200:
        return {"success": False,
                "message": f"LINE APIがエラーを返しました ({r1.status_code}): {r1.text[:200]}",
                "hint": "Render の LINE_HW_CHANNEL_ACCESS_TOKEN を確認してください"}

    lim, used = r1.json(), (r2.json() if r2.status_code == 200 else {})
    limit = lim.get("value")
    total = used.get("totalUsage", 0)
    return {
        "success": True, "type": lim.get("type"), "limit": limit, "used": total,
        "remaining": (limit - total) if isinstance(limit, int) else None,
        "warning": bool(isinstance(limit, int) and limit and total / limit >= 0.8),
    }
