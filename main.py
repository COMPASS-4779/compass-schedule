"""
英文法学習サイト v3 バックエンド（FastAPI）
- 静的配信：/      → HP/grammar_v3.html
              /v1  → HP/grammar.html（旧版・互換用）
              /pdf → ../第NN章/* （問題・解答PDF）
              /HP/data/* → curriculum.json 等
- API：v1互換（/getProgress, /saveProgress, /getWeaknesses, /saveWeaknesses, /getUsers, /saveUser）
        v3新設（/getStageStatus, /saveStageStatus）
"""
import os
import json
from datetime import datetime
from typing import Dict

from fastapi import FastAPI, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, Column, String, Integer, Boolean, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from pydantic import BaseModel
import uvicorn

# ─────────────────────────────────────────────
# パス・データベース
# ─────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))      # .../HP
ROOT = os.path.dirname(HERE)                           # .../英文法_PDFまとめ

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./local.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ─────────────────────────────────────────────
# テーブル
# ─────────────────────────────────────────────
class UserDB(Base):
    __tablename__ = "users"
    uid = Column(String, primary_key=True, index=True)
    name = Column(String)
    password = Column(String)
    role = Column(String, default="student")
    created = Column(String)

class ProgressDB(Base):
    __tablename__ = "progress"
    id = Column(Integer, primary_key=True, index=True)
    uid = Column(String, index=True)
    sid = Column(String)
    score = Column(Integer)
    passed = Column(Boolean)
    date = Column(String)

class WeaknessDB(Base):
    __tablename__ = "weaknesses"
    id = Column(Integer, primary_key=True, index=True)
    uid = Column(String, index=True)
    category = Column(String)
    count = Column(Integer, default=0)

class StageStatusDB(Base):
    """v3 段階制ロック用：項目×段階ごとの集計値"""
    __tablename__ = "stage_status"
    id = Column(Integer, primary_key=True, index=True)
    uid = Column(String, index=True)
    item_id = Column(String, index=True)              # ch02_001 等
    stage = Column(String)                            # enshu / shujuku / shutoku
    best_score = Column(Integer, default=0)
    attempts = Column(Integer, default=0)
    passed = Column(Boolean, default=False)
    passed_at = Column(String, default="")
    last_at = Column(String, default="")
    __table_args__ = (UniqueConstraint("uid", "item_id", "stage", name="uq_uid_item_stage"),)

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ─────────────────────────────────────────────
# FastAPI
# ─────────────────────────────────────────────
app = FastAPI(title="英文法学習サイト API", version="3.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ─────────────────────────────────────────────
# 健康確認
# ─────────────────────────────────────────────
@app.get("/ping")
def ping(): return "ok"

@app.get("/health")
def health(): return {"ok": True, "ts": datetime.utcnow().isoformat()}

# ─────────────────────────────────────────────
# v1 互換 API（既存クライアント用）
# ─────────────────────────────────────────────
@app.get("/getUsers")
def get_users(db: Session = Depends(get_db)):
    users = db.query(UserDB).all()
    return {"ok": True, "users": {u.uid: {"name": u.name, "password": u.password, "role": u.role, "created": u.created} for u in users}}

@app.get("/saveUser")
def save_user(uid: str, name: str, password: str, role: str = "student", created: str = "", db: Session = Depends(get_db)):
    user = db.query(UserDB).filter(UserDB.uid == uid).first()
    if not user:
        user = UserDB(uid=uid); db.add(user)
    user.name, user.password, user.role, user.created = name, password, role, created
    db.commit()
    return {"ok": True, "saved": True}

@app.get("/getProgress")
def get_progress(uid: str, db: Session = Depends(get_db)):
    rows = db.query(ProgressDB).filter(ProgressDB.uid == uid).all()
    return {"ok": True, "progress": {p.sid: {"score": p.score, "passed": p.passed, "date": p.date} for p in rows}}

@app.get("/saveProgress")
def save_progress(uid: str, sid: str, score: int, passed: str, db: Session = Depends(get_db)):
    is_passed = passed.lower() == "true"
    item = db.query(ProgressDB).filter(ProgressDB.uid == uid, ProgressDB.sid == sid).first()
    if not item:
        item = ProgressDB(uid=uid, sid=sid); db.add(item)
    item.score, item.passed = score, is_passed
    item.date = datetime.now().strftime("%Y/%m/%d")
    db.commit()
    return {"ok": True, "saved": True}

@app.get("/getWeaknesses")
def get_weaknesses(uid: str, db: Session = Depends(get_db)):
    rows = db.query(WeaknessDB).filter(WeaknessDB.uid == uid).all()
    return {"ok": True, "weaknesses": {w.category: w.count for w in rows}}

@app.get("/saveWeaknesses")
def save_weaknesses(uid: str, data: str, db: Session = Depends(get_db)):
    weak = json.loads(data)
    for cat, count in weak.items():
        item = db.query(WeaknessDB).filter(WeaknessDB.uid == uid, WeaknessDB.category == cat).first()
        if not item:
            item = WeaknessDB(uid=uid, category=cat); db.add(item)
        item.count = count
    db.commit()
    return {"ok": True, "saved": True}

# ─────────────────────────────────────────────
# v3 新設：段階制ロック用 API
# ─────────────────────────────────────────────
@app.get("/getStageStatus")
def get_stage_status(uid: str, db: Session = Depends(get_db)):
    rows = db.query(StageStatusDB).filter(StageStatusDB.uid == uid).all()
    out = {}
    for r in rows:
        out.setdefault(r.item_id, {})[r.stage] = {
            "best": r.best_score, "attempts": r.attempts,
            "passed": r.passed, "passed_at": r.passed_at, "last": r.last_at
        }
    return {"ok": True, "status": out}

@app.get("/saveStageStatus")
def save_stage_status(uid: str, item_id: str, stage: str, score: int, db: Session = Depends(get_db)):
    PASS_MARK = 85
    row = db.query(StageStatusDB).filter(
        StageStatusDB.uid == uid, StageStatusDB.item_id == item_id, StageStatusDB.stage == stage
    ).first()
    if not row:
        row = StageStatusDB(uid=uid, item_id=item_id, stage=stage,
                            best_score=0, attempts=0, passed=False, passed_at="", last_at="")
        db.add(row)
    row.attempts = (row.attempts or 0) + 1
    row.best_score = max(row.best_score or 0, score)
    now_str = datetime.now().strftime("%Y/%m/%d %H:%M")
    row.last_at = now_str
    just_passed = False
    if score >= PASS_MARK and not row.passed:
        row.passed = True
        row.passed_at = now_str
        just_passed = True
    db.commit()
    next_unlock = None
    if just_passed:
        next_unlock = "shujuku" if stage == "enshu" else ("shutoku" if stage == "shujuku" else None)
    return {"ok": True, "saved": True,
            "best": row.best_score, "passed": row.passed,
            "just_passed": just_passed, "next_unlock": next_unlock}

# ─────────────────────────────────────────────
# 静的配信
# ─────────────────────────────────────────────
@app.get("/v1")
def page_v1():
    return FileResponse(os.path.join(HERE, "grammar.html"))

@app.get("/")
def index():
    p = os.path.join(HERE, "grammar_v3.html")
    if os.path.exists(p):
        return FileResponse(p)
    return FileResponse(os.path.join(HERE, "grammar.html"))

# 章PDFを /pdf/第NN章/... で配信
if os.path.isdir(ROOT):
    app.mount("/pdf", StaticFiles(directory=ROOT), name="pdf")
if os.path.isdir(os.path.join(HERE, "data")):
    app.mount("/data", StaticFiles(directory=os.path.join(HERE, "data")), name="data")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
