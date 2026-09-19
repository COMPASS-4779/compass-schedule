"""
宿題自動配信：課題（送付したテスト）の管理と、提出の自動記録・リマインド・復習ループ（Render側）

  テスト作成システム ──POST /hw/api/assignments──▶ 課題を登録し、テストのリンクを LINE で配信
  配信された時点 : 結果スプレッドシートの「送付テスト」タブに1行登録（処理F=1）
                  答案が届いたら同じ行の 提出F=1・提出日時・正解率、リマインド回数・状態も更新する
  生徒が丸付けした答案を LINE に送る ──▶ 受信先/生徒名 に保存（hw_api の既存の受信処理）
  5分ごと : 受信先に届いた新しい答案を Gemini で読み取り、用紙のテストIDで課題を特定して
            結果シートへ記録する（L列 提出F=1 / M列 テストのタイトル）。正解率を課題に残す。
            正解率が目標未満なら、テスト作成システムに復習テストの下書き作成を依頼する
            （講師が承認すると、テスト作成システムがこの API で次の課題として送付する）
  10分ごと: 送付から一定日数たっても提出が無い課題に、同じ LINE ボットでリマインドを送る

必要な環境変数（未設定の機能は動かさないだけで、エラーにはしない）:
  HW_API_TOKEN              : 既存。テスト作成システムからの呼び出しもこのトークンで認証する
  HW_RESULT_SPREADSHEET_ID  : 結果シート（答案自動採点システムと同じスプレッドシート）。未設定なら自動記録しない
  GEMINI_API_KEY            : 既存。答案の読み取りに使う
  HW_GRADE_MODEL            : 既定 gemini-2.5-flash
  HW_GRADE_SETTLE_MINUTES   : 最後の写真が届いてから読み取るまでの待ち時間（既定 10。複数枚をまとめるため）
  HW_TARGET_ACCURACY        : 目標正解率（%）の既定値（既定 80。課題ごとに上書きできる）
  HW_REMIND_AFTER_DAYS      : 送付から何日後に最初のリマインドを送るか（既定 3）
  HW_REMIND_INTERVAL_DAYS   : 2回目以降の間隔（日、既定 2）
  HW_REMIND_MAX             : リマインドの最大回数（既定 3）
  HW_REMIND_HOURS           : リマインドを送ってよい時間帯（JST、既定 "17-21" ＝17時台〜20時台）
  TESTGEN_URL               : テスト作成システムのURL（例 https://compass-test-generator.onrender.com）
  TESTGEN_TOKEN             : テスト作成システムの /api/hw/review_draft を呼ぶためのトークン
"""
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests
from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Session

import hw_api
from database import Base, SessionLocal
from hw_api import BIGINT, TZ, HwDelivery, HwStudent, get_db, to_local

log = logging.getLogger("hw_assign")


def _int_env(key, default):
    try:
        return int(os.environ.get(key, "").strip() or default)
    except ValueError:
        return default


RESULT_SPREADSHEET_ID = os.environ.get("HW_RESULT_SPREADSHEET_ID", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GRADE_MODEL = os.environ.get("HW_GRADE_MODEL", "").strip() or "gemini-2.5-flash"
SETTLE_MINUTES = _int_env("HW_GRADE_SETTLE_MINUTES", 10)
TARGET_ACCURACY = _int_env("HW_TARGET_ACCURACY", 80)
REMIND_AFTER_DAYS = _int_env("HW_REMIND_AFTER_DAYS", 3)
REMIND_INTERVAL_DAYS = _int_env("HW_REMIND_INTERVAL_DAYS", 2)
REMIND_MAX = _int_env("HW_REMIND_MAX", 3)
TESTGEN_URL = os.environ.get("TESTGEN_URL", "").strip().rstrip("/")
TESTGEN_TOKEN = os.environ.get("TESTGEN_TOKEN", "").strip()


def _remind_hours():
    m = re.match(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$", os.environ.get("HW_REMIND_HOURS", "") or "17-21")
    return (int(m.group(1)), int(m.group(2))) if m else (17, 21)


# 用紙に印字するテストID（読み違えやすい 0/O, 1/I/L, 2/Z, 5/S, 8/B を除いた英数字）
CODE_CHARS = "ACDEFGHJKMNPQRTUVWXY34679"
ANSWER_MIME = ("image/", "application/pdf")


# ======================================================================
# モデル（新しいテーブルのみ。既存テーブルには手を入れない）
# ======================================================================
class HwAssignment(Base):
    """送付したテスト1回分（＝課題）"""

    __tablename__ = "hw_assignments"

    id = Column(BIGINT, primary_key=True, index=True)
    student_id = Column(BIGINT, ForeignKey("hw_students.id"), index=True)
    sheet_student = Column(String, default="")            # 結果シートB列に書く生徒名（空なら hw の生徒名）
    code = Column(String, unique=True, index=True)        # 用紙に印字するテストID
    kind = Column(String, default="理解度確認テスト")        # 理解度確認テスト / 復習テスト
    round = Column(Integer, default=1)                    # 復習ループの何回目か
    parent_id = Column(BIGINT, ForeignKey("hw_assignments.id"), nullable=True, index=True)
    title = Column(String, default="")
    test_url = Column(Text, default="")
    subject = Column(String, default="")
    book = Column(String, default="")                     # 出題元の教材名
    units = Column(Text, default="[]")                    # JSON [{"chapter","section","pages"}]
    target_accuracy = Column(Integer, default=TARGET_ACCURACY)
    # scheduled(配信待ち) / sent(送付済み・未提出) / submitted(提出済み・判定待ち) / done(完了) / canceled / failed
    status = Column(String, default="scheduled", index=True)
    delivery_id = Column(BIGINT, ForeignKey("hw_deliveries.id"), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    accuracy = Column(Float, nullable=True)               # 0〜100
    result = Column(Text, nullable=True)                  # JSON（読み取り結果の要約）
    remind_count = Column(Integer, default=0)
    last_reminded_at = Column(DateTime(timezone=True), nullable=True)
    review_status = Column(String, default="")            # requested / drafted / failed / sent
    review_note = Column(Text, nullable=True)
    external_ref = Column(String, default="")             # テスト作成システム側のID（sid）
    ledger = Column(String, default="")                   # 「送付テスト」タブへの登録（"" / written）
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class HwGradedFile(Base):
    """読み取り済みの受信ファイル（同じ答案を二重に記録しないための控え）"""

    __tablename__ = "hw_graded_files"

    id = Column(BIGINT, primary_key=True, index=True)
    drive_file_id = Column(String, unique=True, index=True)
    student_id = Column(BIGINT, index=True)
    assignment_id = Column(BIGINT, nullable=True, index=True)
    file_name = Column(String, default="")
    status = Column(String, default="recorded")           # recorded / unmatched / error
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# ======================================================================
# 共通
# ======================================================================
def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt(dt):
    loc = to_local(dt)
    return loc.strftime("%Y-%m-%d %H:%M") if loc else None


def new_code(db: Session):
    for _ in range(100):
        c = "".join(secrets.choice(CODE_CHARS) for _ in range(6))
        if not db.query(HwAssignment).filter(HwAssignment.code == c).first():
            return c
    raise RuntimeError("テストIDを発行できませんでした")


def assignment_message(a: HwAssignment, extra: str = ""):
    head = f"📝 {a.title}" + (f"（{a.round}回目の復習）" if a.kind == "復習テスト" else "")
    parts = [head]
    if extra.strip():
        parts.append(extra.strip())
    parts.append("リンクを開いて問題を解き、最後の解答を見て自分で丸付けをしてください。\n"
                 "丸付けした答案の写真をこのトークに送ると提出になります。")
    parts.append(a.test_url)
    parts.append(f"テストID: {a.code}")
    return "\n\n".join(parts)


def extract_json_array(text):
    """Gemini の応答から最初の JSON 配列を取り出す（採点システムと同じ方式）。失敗時は []。"""
    if not text:
        return []
    s = str(text)
    m = re.search(r"```(?:json)?\s*(.+?)```", s, re.DOTALL)
    if m:
        s = m.group(1)
    for start, ch in enumerate(s):
        if ch != "[":
            continue
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(s[start:i + 1])
                    except Exception:
                        break
                    return v if isinstance(v, list) else []
    return []


def _to_int(v):
    m = re.search(r"\d+", str(v if v is not None else "").translate(str.maketrans("０１２３４５６７８９", "0123456789")))
    return int(m.group(0)) if m else None


def _units(a):
    try:
        u = json.loads(a.units or "[]")
        return u if isinstance(u, list) else []
    except Exception:
        return []


def assignment_json(a: HwAssignment, db: Optional[Session] = None):
    student, dstatus = None, None
    if db is not None:
        student = db.query(HwStudent).filter(HwStudent.id == a.student_id).first()
        if a.delivery_id:
            d = db.query(HwDelivery).filter(HwDelivery.id == a.delivery_id).first()
            dstatus = d.status if d else None
    try:
        result = json.loads(a.result) if a.result else None
    except Exception:
        result = None
    return {
        "id": a.id, "student_id": a.student_id, "student_name": student.name if student else None,
        "sheet_student": a.sheet_student, "code": a.code, "kind": a.kind, "round": a.round,
        "parent_id": a.parent_id, "title": a.title, "test_url": a.test_url, "subject": a.subject,
        "book": a.book, "units": _units(a), "target_accuracy": a.target_accuracy, "status": a.status,
        "delivery_status": dstatus, "sent_at": _fmt(a.sent_at), "submitted_at": _fmt(a.submitted_at),
        "accuracy": a.accuracy, "result": result, "remind_count": a.remind_count,
        "last_reminded_at": _fmt(a.last_reminded_at), "review_status": a.review_status,
        "review_note": a.review_note, "external_ref": a.external_ref, "created_at": _fmt(a.created_at),
    }


# ======================================================================
# 1) 配信状況の反映（配信予定が送られたら課題を「送付済み」にする）
# ======================================================================
def sync_sent(db: Session):
    n = 0
    for a in db.query(HwAssignment).filter(HwAssignment.status == "scheduled").all():
        d = db.query(HwDelivery).filter(HwDelivery.id == a.delivery_id).first() if a.delivery_id else None
        if d is None:
            continue
        if d.status == "sent":
            a.status, a.sent_at = "sent", d.sent_at or _now()
        elif d.status == "failed":
            a.status, a.review_note = "failed", f"配信に失敗: {d.error or ''}"[:2000]
        elif d.status == "canceled":
            a.status = "canceled"
        else:
            continue
        a.updated_at = _now()
        n += 1
    if n:
        db.commit()
    return n


# ======================================================================
# 2) 提出の自動記録（受信先の答案 → 読み取り → 結果シート → 正解率 → 復習依頼）
# ======================================================================
_MARK_RULES = (
    "【採点記号の意味 ＝ 最重要ルール】\n"
    "・問題番号が赤い〇（丸・楕円）で囲まれている → その問題は【正解】。wrong に入れない。\n"
    "・問題番号のそばに赤い『レ点』『チェック(✓)』『斜線(／)』『×』のいずれかが付いている → 【間違い】。wrong に入れる。\n"
    "・□（チェックボックス）が黒く塗りつぶされている → 【間違い】。\n"
    "・重要：赤い〇（丸）は必ず【正解】です。丸を間違いと誤認しないこと。\n"
    "・〇でも×系でもなく、□も塗られていない無印は、正解として扱う。\n"
    "・計算の途中式や答えの数値（例: -8, 3/4）は問題番号ではない。\n"
)


def grade_prompt(n_files, candidates):
    cand = "\n".join(f"・{a.code}：{a.title}" for a in candidates)
    return (
        f"これは生徒が自分で丸付けしたテスト答案の写真です（全{n_files}枚）。赤ペンの採点記号を1問ずつ判定してください。\n\n"
        + _MARK_RULES +
        "\n【答案の構成】用紙の上部にテスト名と『テストID: 英数字6文字』が印字されています。"
        "太字・色付きの見出しが『単元』、その下の 1. 2. 3. が『大問』、(1)(2)… が『小問』です。"
        "大問番号は単元ごとに 1 から振り直されることがあります。\n"
        + (f"\n【この生徒に送ったテスト（テストIDの候補）】\n{cand}\n" if cand else "")
        + "\n【出力形式】JSON配列のみ（説明文は不要）。大問ごとに1要素。\n"
        '[{"test_id":"K7F3QA","test_title":"第3回 理解度確認テスト","text":"新中学問題集 数学1年",'
        '"chapter":"第2章 文字と式","section":"第1節 文字を使った式","unit":"文字式",'
        '"daimon":"1","total":4,"wrong":[{"number":"(2)"}]}]\n'
        "・test_id  = 用紙に印字されたテストID（読めなければ \"\"）\n"
        "・text / chapter / section = 出題元のテキスト名・章・節（書かれていなければ \"\"）\n"
        "・unit = 大問の上の単元見出し（無ければ \"\"）\n"
        "・total = その大問の小問の個数（数えられなければ 0）／ wrong = 間違いだった小問の番号だけ\n"
        "推測で埋めないこと。読めない項目は必ず空文字にする。"
    )


def read_answers(files, candidates):
    """files: [(bytes, mime)]。Gemini で読み取り、大問ごとの dict のリストを返す。"""
    import google.generativeai as genai
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GRADE_MODEL)
    parts = [grade_prompt(len(files), candidates)] + [{"mime_type": m, "data": b} for b, m in files]
    resp = model.generate_content(parts)
    return [x for x in extract_json_array(getattr(resp, "text", "")) if isinstance(x, dict)]


def _sheets():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(
        token=None, refresh_token=os.environ.get("HW_GOOGLE_REFRESH_TOKEN", ""),
        client_id=os.environ.get("HW_GOOGLE_CLIENT_ID", ""),
        client_secret=os.environ.get("HW_GOOGLE_CLIENT_SECRET", ""),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/drive"],   # Sheets API は drive スコープでも書き込める
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def append_result_rows(rows):
    if rows:
        _sheets().spreadsheets().values().append(
            spreadsheetId=RESULT_SPREADSHEET_ID, range="A1",
            valueInputOption="USER_ENTERED", body={"values": rows}).execute()


# ---- 「送付テスト」タブ（送付した記録と提出状況の台帳）------------------------------
LEDGER_TAB = "送付テスト"
LEDGER_HEADER = ["送付日時", "生徒名", "科目", "テスト名", "種類", "回", "出典テキスト", "章・節", "テストID",
                 "テストURL", "処理F", "提出F", "提出日時", "正解率", "目標正解率", "リマインド回数", "状態",
                 "元のテストID"]
LEDGER_ID_COL, LEDGER_DYN_FROM, LEDGER_DYN_TO = "I", "K", "Q"      # テストID列 / 更新する範囲（処理F〜状態）


def ledger_state(a: HwAssignment):
    if a.status == "canceled":
        return "取消"
    if a.status == "failed":
        return "配信失敗"
    if a.status == "scheduled":
        return "配信待ち"
    if a.status == "sent":
        return "送付済・未提出" + (f"（リマインド{a.remind_count}回）" if a.remind_count else "")
    rs = a.review_status or ""
    if a.status == "done":
        return "復習テスト送付済" if rs == "sent" else "目標達成"
    return {"requested": "提出済・復習テスト作成中", "drafted": "提出済・復習テスト承認待ち",
            "failed": "提出済・復習テスト作成失敗"}.get(rs, "提出済・確認待ち")


def ledger_dynamic(a: HwAssignment):
    """K〜Q列（処理F・提出F・提出日時・正解率・目標正解率・リマインド回数・状態）"""
    return ["1", "1" if a.submitted_at else "", _fmt(a.submitted_at) or "",
            "" if a.accuracy is None else f"{a.accuracy:g}", str(a.target_accuracy or ""),
            str(a.remind_count or 0), ledger_state(a)]


def ledger_row(a: HwAssignment, db: Session):
    student = db.query(HwStudent).filter(HwStudent.id == a.student_id).first()
    parent = db.query(HwAssignment).filter(HwAssignment.id == a.parent_id).first() if a.parent_id else None
    units = " / ".join(" ".join(x for x in (u.get("chapter", ""), u.get("section", "")) if x) for u in _units(a))
    return ([_fmt(a.sent_at) or "", a.sheet_student or (student.name if student else ""), a.subject, a.title,
             a.kind, str(a.round or 1), a.book, units, a.code, a.test_url]
            + ledger_dynamic(a) + [parent.code if parent else ""])


def ensure_ledger_tab(svc):
    meta = svc.spreadsheets().get(spreadsheetId=RESULT_SPREADSHEET_ID).execute()
    if LEDGER_TAB not in [s["properties"]["title"] for s in meta.get("sheets", [])]:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=RESULT_SPREADSHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": LEDGER_TAB}}}]}).execute()
        svc.spreadsheets().values().update(
            spreadsheetId=RESULT_SPREADSHEET_ID, range=f"{LEDGER_TAB}!A1",
            valueInputOption="RAW", body={"values": [LEDGER_HEADER]}).execute()


def ledger_flush(db: Session):
    """配信された課題のうち、まだ「送付テスト」タブに無いものを登録する（処理F=1）。失敗しても次回やり直す。"""
    if not RESULT_SPREADSHEET_ID:
        return 0
    from sqlalchemy import or_
    todo = (db.query(HwAssignment)
            .filter(or_(HwAssignment.ledger == "", HwAssignment.ledger.is_(None)),
                    HwAssignment.status.in_(["sent", "submitted", "done"]))
            .order_by(HwAssignment.id.asc()).all())
    if not todo:
        return 0
    svc = _sheets()
    ensure_ledger_tab(svc)
    svc.spreadsheets().values().append(
        spreadsheetId=RESULT_SPREADSHEET_ID, range=f"{LEDGER_TAB}!A1",
        valueInputOption="RAW", body={"values": [ledger_row(a, db) for a in todo]}).execute()
    for a in todo:
        a.ledger = "written"
    db.commit()
    return len(todo)


def ledger_update(a: HwAssignment):
    """「送付テスト」タブの該当行（テストIDで探す）の 処理F〜状態 を最新にする。失敗しても本処理は止めない。"""
    if not RESULT_SPREADSHEET_ID or a.ledger != "written":
        return False
    try:
        svc = _sheets()
        col = svc.spreadsheets().values().get(
            spreadsheetId=RESULT_SPREADSHEET_ID, range=f"{LEDGER_TAB}!{LEDGER_ID_COL}:{LEDGER_ID_COL}",
        ).execute().get("values", [])
        idx = next((i for i, r in enumerate(col) if r and str(r[0]).strip() == a.code), None)
        if idx is None:
            return False
        svc.spreadsheets().values().update(
            spreadsheetId=RESULT_SPREADSHEET_ID,
            range=f"{LEDGER_TAB}!{LEDGER_DYN_FROM}{idx + 1}:{LEDGER_DYN_TO}{idx + 1}",
            valueInputOption="RAW", body={"values": [ledger_dynamic(a)]}).execute()
        return True
    except Exception as e:
        log.warning("送付テストタブの更新に失敗 (%s): %s", a.code, e)
        return False


def build_rows(a: HwAssignment, student_name: str, sections, link: str, when_text: str):
    """読み取った大問 → 結果シートの行（A:M）。間違えた小問ごとに1行。L列＝提出F=1、M列＝テストのタイトル。"""
    units = _units(a)
    one_unit = units[0] if len(units) == 1 else {}
    rows, items = [], []
    for s in sections:
        wrongs = [w for w in (s.get("wrong") or []) if isinstance(w, dict) and str(w.get("number", "")).strip()]
        if not wrongs:
            continue
        unit = str(s.get("unit", "") or "").strip()
        ch = str(s.get("chapter", "") or "").strip() or str(one_unit.get("chapter", "") or "")
        se = str(s.get("section", "") or "").strip() or str(one_unit.get("section", "") or "")
        if not se:
            se = unit
        elif not ch:
            ch = unit
        book = str(s.get("text", "") or "").strip() or a.book
        dm = _to_int(s.get("daimon"))
        total = _to_int(s.get("total")) or 0
        for w in wrongs:
            rows.append([when_text, a.sheet_student or student_name, a.subject, book,
                         str(dm) if dm else "", ch, se, str(w.get("number")).strip(),
                         link, total, "", "1", a.title])
        items.append({"book": book, "chapter": ch, "section": se, "pages": []})
    uniq, seen = [], set()
    for it in items:
        k = (it["book"], it["chapter"], it["section"])
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    return rows, uniq


def score(sections):
    total = sum(_to_int(s.get("total")) or 0 for s in sections)
    wrong = sum(len([w for w in (s.get("wrong") or []) if isinstance(w, dict) and str(w.get("number", "")).strip()])
                for s in sections)
    if total <= 0:
        return None, total, wrong
    return round(max(0.0, (total - wrong) / total * 100), 1), total, wrong


def request_review(a: HwAssignment, student_name: str, items):
    """テスト作成システムに、間違えた単元の復習テストの下書き作成を依頼する（承認待ちになる）。"""
    if not (TESTGEN_URL and TESTGEN_TOKEN):
        a.review_status, a.review_note = "failed", "TESTGEN_URL / TESTGEN_TOKEN が未設定のため復習テストを依頼できません"
        return False
    payload = {"assignment_id": a.id, "code": a.code, "hw_student_id": a.student_id,
               "student": a.sheet_student or student_name, "subject": a.subject,
               "book": (items[0]["book"] if items else "") or a.book, "title": a.title,
               "round": (a.round or 1) + 1, "target_accuracy": a.target_accuracy, "accuracy": a.accuracy,
               "items": [{"chapter": it["chapter"], "section": it["section"], "pages": it["pages"]} for it in items]}
    try:
        r = requests.post(f"{TESTGEN_URL}/api/hw/review_draft", json=payload,
                          headers={"Authorization": f"Bearer {TESTGEN_TOKEN}"}, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        a.review_status, a.review_note = "requested", None
        return True
    except Exception as e:
        a.review_status, a.review_note = "failed", f"復習テストの依頼に失敗: {e}"[:2000]
        return False


def _parse_time(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def list_answer_files(student: HwStudent, since):
    """受信先/生徒名 にある、since 以降に届いた答案（画像・PDF）"""
    folder = hw_api.ensure_path(f"受信先/{student.name}")
    res = hw_api.drive().files().list(
        q=f"'{folder}' in parents and trashed = false",
        fields="files(id,name,mimeType,createdTime,webViewLink)", orderBy="createdTime", pageSize=200,
    ).execute()
    out = []
    for f in res.get("files", []):
        t = _parse_time(f.get("createdTime"))
        if t and since and t < since - timedelta(minutes=5):
            continue
        if str(f.get("mimeType", "")).startswith(ANSWER_MIME):
            out.append({**f, "_t": t})
    return out


def grade_student(db: Session, student: HwStudent, open_as: List[HwAssignment], now=None):
    """1人分：新しく届いた答案をまとめて読み取り、課題に記録する。処理した課題（無ければ None）を返す。"""
    now = now or _now()
    since = min(_aware(a.sent_at) or now for a in open_as)
    files = [f for f in list_answer_files(student, since)
             if not db.query(HwGradedFile).filter(HwGradedFile.drive_file_id == f["id"]).first()]
    if not files:
        return None
    newest = max(f["_t"] or now for f in files)
    if now - newest < timedelta(minutes=SETTLE_MINUTES):      # まだ続きの写真が届くかもしれない
        return None

    blobs = [(hw_api.drive().files().get_media(fileId=f["id"]).execute(), f["mimeType"]) for f in files]
    sections = read_answers(blobs, open_as)
    codes = {str(s.get("test_id", "")).strip().upper() for s in sections if s.get("test_id")}
    a = next((x for x in open_as if x.code in codes), None)
    match = "テストID"
    if a is None:                                            # IDが読めない → いちばん古い未提出の課題
        a, match = sorted(open_as, key=lambda x: _aware(x.sent_at) or now)[0], "未提出の最古の課題"

    acc, total, wrong = score(sections)
    link = "\n".join(f.get("webViewLink") or f"https://drive.google.com/file/d/{f['id']}/view" for f in files)
    rows, items = build_rows(a, student.name, sections, link, to_local(now).strftime("%Y-%m-%d %H:%M:%S"))
    append_result_rows(rows)

    a.status, a.submitted_at, a.accuracy, a.updated_at = "submitted", now, acc, now
    a.result = json.dumps({"total": total, "wrong": wrong, "match": match, "rows": len(rows),
                           "files": [f["name"] for f in files], "units": items}, ensure_ascii=False)
    for f in files:
        db.add(HwGradedFile(drive_file_id=f["id"], student_id=student.id, assignment_id=a.id,
                            file_name=f.get("name", ""), status="recorded"))
    if acc is None:
        a.review_note = "小問数が読み取れず正解率を判定できませんでした。講師が確認してください。"
    elif acc >= (a.target_accuracy or TARGET_ACCURACY):
        a.status = "done"                                    # 目標達成 → ループ終了
    else:
        request_review(a, student.name, items)
    db.commit()
    ledger_update(a)                                         # 送付テストタブ：提出F=1・提出日時・正解率・状態
    log.info("答案を記録しました: %s %s 正解率=%s（%s）", student.name, a.code, acc, match)
    return a


def run_grading():
    if not (RESULT_SPREADSHEET_ID and GEMINI_API_KEY):
        return 0
    db = SessionLocal()
    n = 0
    try:
        sync_sent(db)
        by_student = {}
        for a in db.query(HwAssignment).filter(HwAssignment.status == "sent").all():
            by_student.setdefault(a.student_id, []).append(a)
        for sid, open_as in by_student.items():
            student = db.query(HwStudent).filter(HwStudent.id == sid).first()
            if student is None:
                continue
            try:
                if grade_student(db, student, open_as):
                    n += 1
            except Exception as e:
                db.rollback()
                log.error("答案の記録に失敗 (%s): %s", student.name, e)
    finally:
        db.close()
    return n


# ======================================================================
# 3) 未提出のリマインド（同じ LINE ボットで生徒のグループへ）
# ======================================================================
def reminder_due(a: HwAssignment, now):
    if a.status != "sent" or a.sent_at is None or (a.remind_count or 0) >= REMIND_MAX:
        return False
    due = _aware(a.sent_at) + timedelta(days=REMIND_AFTER_DAYS + REMIND_INTERVAL_DAYS * (a.remind_count or 0))
    if now < due:
        return False
    last = to_local(a.last_reminded_at)
    return not (last and last.date() == to_local(now).date())      # 同じ日に2回は送らない


def remind_text(a: HwAssignment, now):
    days = max(1, (now - _aware(a.sent_at)).days)
    return (f"⏰ リマインド\n「{a.title}」の答案がまだ届いていません（送付から{days}日）。\n"
            f"丸付けした答案の写真をこのトークに送ってください。\n\n{a.test_url}\nテストID: {a.code}")


def run_reminders(now=None):
    now = now or _now()
    start, end = _remind_hours()
    if not (start <= to_local(now).hour < end):
        return 0
    db = SessionLocal()
    n = 0
    try:
        sync_sent(db)
        for a in db.query(HwAssignment).filter(HwAssignment.status == "sent").all():
            if not reminder_due(a, now):
                continue
            student = db.query(HwStudent).filter(HwStudent.id == a.student_id).first()
            if student is None or not student.group_id or not student.enabled:
                continue
            try:   # 提出済みでまだ読み取り待ちの答案があるなら送らない
                if any(not db.query(HwGradedFile).filter(HwGradedFile.drive_file_id == f["id"]).first()
                       for f in list_answer_files(student, _aware(a.sent_at))):
                    continue
            except Exception as e:
                log.warning("受信先の確認に失敗 (%s): %s", student.name, e)
            try:
                hw_api.line_push(student.group_id, remind_text(a, now))
                a.remind_count, a.last_reminded_at, a.updated_at = (a.remind_count or 0) + 1, now, now
                db.commit()
                ledger_update(a)
                n += 1
            except Exception as e:
                db.rollback()
                log.error("リマインドの送信に失敗 (%s %s): %s", student.name, a.code, e)
    finally:
        db.close()
    return n


def run_sync():
    """配信状況を課題に反映し、配信された課題を「送付テスト」タブに登録する（毎分）"""
    db = SessionLocal()
    try:
        n = sync_sent(db)
        try:
            ledger_flush(db)
        except Exception as e:
            db.rollback()
            log.warning("送付テストタブへの登録に失敗（次回やり直します）: %s", e)
        return n
    finally:
        db.close()


def add_jobs(sched):
    """hw_api.start_scheduler() が返すスケジューラに、課題の処理を追加する"""
    from apscheduler.triggers.interval import IntervalTrigger
    sched.add_job(run_sync, IntervalTrigger(minutes=1), id="hw:assign-sync", max_instances=1, coalesce=True)
    sched.add_job(run_grading, IntervalTrigger(minutes=5), id="hw:assign-grade", max_instances=1, coalesce=True)
    sched.add_job(run_reminders, IntervalTrigger(minutes=10), id="hw:assign-remind", max_instances=1, coalesce=True)
    log.info("課題（提出の自動記録・リマインド）の処理を開始しました")


# ======================================================================
# API（テスト作成システム・コントロール画面から呼ぶ。HW_API_TOKEN で認証）
# ======================================================================
router = APIRouter(prefix="/hw/api", tags=["homework-assignments"])


class AssignmentIn(BaseModel):
    student_id: Optional[int] = None
    student_name: str = ""
    sheet_student: str = ""
    title: str
    test_url: str
    kind: str = "理解度確認テスト"
    subject: str = ""
    book: str = ""
    units: list = []
    target_accuracy: Optional[int] = None
    parent_id: Optional[int] = None
    send_at: str = ""
    message: str = ""
    external_ref: str = ""


class ReviewStatusIn(BaseModel):
    status: str                      # drafted / failed
    note: str = ""
    external_ref: str = ""


@router.get("/assignments/settings")
def assignment_settings(authorization: str = Header(None)):
    err = hw_api._auth_or_401(authorization)
    if err:
        return err
    s, e = _remind_hours()
    return {"ok": True, "grading_enabled": bool(RESULT_SPREADSHEET_ID and GEMINI_API_KEY),
            "review_enabled": bool(TESTGEN_URL and TESTGEN_TOKEN), "target_accuracy": TARGET_ACCURACY,
            "remind_after_days": REMIND_AFTER_DAYS, "remind_interval_days": REMIND_INTERVAL_DAYS,
            "remind_max": REMIND_MAX, "remind_hours": f"{s}-{e}", "settle_minutes": SETTLE_MINUTES}


@router.get("/assignments")
def list_assignments(status: str = "", student_id: int = 0, limit: int = 200,
                     authorization: str = Header(None), db: Session = Depends(get_db)):
    err = hw_api._auth_or_401(authorization)
    if err:
        return err
    sync_sent(db)
    q = db.query(HwAssignment)
    if status:
        q = q.filter(HwAssignment.status.in_([x.strip() for x in status.split(",") if x.strip()]))
    if student_id:
        q = q.filter(HwAssignment.student_id == student_id)
    rows = q.order_by(HwAssignment.id.desc()).limit(max(1, min(limit, 1000))).all()
    return {"ok": True, "assignments": [assignment_json(a, db) for a in rows]}


@router.post("/assignments")
def create_assignment(body: AssignmentIn, authorization: str = Header(None), db: Session = Depends(get_db)):
    err = hw_api._auth_or_401(authorization)
    if err:
        return err
    q = db.query(HwStudent)
    student = (q.filter(HwStudent.id == body.student_id).first() if body.student_id
               else q.filter(HwStudent.name == body.student_name.strip()).first())
    if student is None:
        return {"ok": False, "error": "送付先の生徒が見つかりません"}
    if not student.enabled or not student.group_id:
        return {"ok": False, "error": f"「{student.name}」は配信が無効か、LINEグループが未設定です"}
    if not body.title.strip() or not body.test_url.strip():
        return {"ok": False, "error": "タイトルとテストのURLは必須です"}
    parent = None
    if body.parent_id:
        parent = db.query(HwAssignment).filter(HwAssignment.id == body.parent_id).first()
        if parent is None:
            return {"ok": False, "error": "元の課題が見つかりません"}
    try:
        when = hw_api.parse_local(body.send_at) if body.send_at.strip() else _now()
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    a = HwAssignment(
        student_id=student.id, sheet_student=body.sheet_student.strip(), code=new_code(db),
        kind=body.kind or "理解度確認テスト", round=((parent.round or 1) + 1) if parent else 1,
        parent_id=parent.id if parent else None, title=body.title.strip(), test_url=body.test_url.strip(),
        subject=body.subject.strip() or (parent.subject if parent else ""),
        book=body.book.strip() or (parent.book if parent else ""),
        units=json.dumps(body.units or [], ensure_ascii=False),
        target_accuracy=body.target_accuracy or (parent.target_accuracy if parent else TARGET_ACCURACY),
        external_ref=body.external_ref, status="scheduled",
    )
    if parent and not a.sheet_student:
        a.sheet_student = parent.sheet_student
    db.add(a)
    db.flush()
    d = HwDelivery(student_id=student.id, scheduled_at=when, message=assignment_message(a, body.message),
                   status="pending")
    db.add(d)
    db.flush()
    a.delivery_id = d.id
    if parent:
        parent.status, parent.review_status, parent.updated_at = "done", "sent", _now()
    db.commit()
    if parent:
        ledger_update(parent)                     # 元のテストの状態を「復習テスト送付済」に
    return {"ok": True, "assignment": assignment_json(a, db)}


@router.post("/assignments/{assignment_id}/cancel")
def cancel_assignment(assignment_id: int, authorization: str = Header(None), db: Session = Depends(get_db)):
    err = hw_api._auth_or_401(authorization)
    if err:
        return err
    a = db.query(HwAssignment).filter(HwAssignment.id == assignment_id).first()
    if a is None:
        return Response(status_code=404)
    a.status, a.updated_at = "canceled", _now()
    d = db.query(HwDelivery).filter(HwDelivery.id == a.delivery_id).first() if a.delivery_id else None
    if d is not None and d.status == "pending":
        d.status = "canceled"
    db.commit()
    ledger_update(a)
    return {"ok": True, "assignment": assignment_json(a, db)}


@router.post("/assignments/{assignment_id}/review")
def review_status(assignment_id: int, body: ReviewStatusIn, authorization: str = Header(None),
                  db: Session = Depends(get_db)):
    """テスト作成システムからの報告（復習テストの下書きができた / 作れなかった）"""
    err = hw_api._auth_or_401(authorization)
    if err:
        return err
    a = db.query(HwAssignment).filter(HwAssignment.id == assignment_id).first()
    if a is None:
        return Response(status_code=404)
    a.review_status, a.review_note, a.updated_at = body.status, (body.note or None), _now()
    db.commit()
    ledger_update(a)
    return {"ok": True}


@router.post("/assignments/run")
def run_now(authorization: str = Header(None)):
    """動作確認用：配信状況の反映・提出の読み取り・リマインドをその場で1回実行する"""
    err = hw_api._auth_or_401(authorization)
    if err:
        return err
    return {"ok": True, "synced": run_sync(), "graded": run_grading(), "reminded": run_reminders()}
