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
  毎分    : 「問題と解答用紙のみ」で送った課題は、生徒から「<問題名> できました」が届いたら解答を送る
  判別できない答案（テストIDもテスト名も一致しない／解答を送る前に届いた）や完了連絡は自動で割り当てず、
            管理者にメールし、テスト作成アプリの「LINE課題」画面の判定フォームで管理者が決める

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
  SENDER_EMAIL / APP_PASSWORD : 管理者への連絡メールの送信元（Gmail とアプリパスワード。採点システムと同じ）
  HW_ADMIN_EMAIL            : 連絡先（既定 info@compassesonline.com）
"""
import json
import logging
import os
import re
import secrets
import smtplib
import unicodedata
from datetime import datetime, timedelta, timezone
from email.header import Header as MailHeader   # fastapi の Header と区別する
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import List, Optional

import requests
from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Session

import hw_api
from database import Base, SessionLocal
from hw_api import BIGINT, HwDelivery, HwStudent, get_db, to_local

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
MAIL_FROM = os.environ.get("SENDER_EMAIL", "").strip()
MAIL_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
ADMIN_EMAIL = os.environ.get("HW_ADMIN_EMAIL", "").strip() or "info@compassesonline.com"


def _remind_hours():
    m = re.match(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$", os.environ.get("HW_REMIND_HOURS", "") or "17-21")
    return (int(m.group(1)), int(m.group(2))) if m else (17, 21)


# 用紙に印字するテストID（読み違えやすい 0/O, 1/I/L, 2/Z, 5/S, 8/B を除いた英数字）
CODE_CHARS = "ACDEFGHJKMNPQRTUVWXY34679"
ANSWER_MIME = ("image/", "application/pdf")
# 復習テストを自動で作る種類（テスト作成システムで作ったテスト）。
# 過去問・プリントなど PDF で直接送ったものは、元教材の目次が無いので提出したら完了にする
REVIEW_KINDS = ("理解度確認テスト", "復習テスト")
# リマインドの設定を分ける単位。過去問・プリント・その他はまとめて「過去問」の設定を使う。
REMIND_GROUPS = ("理解度確認テスト", "復習テスト", "過去問")


def remind_group(kind):
    k = (kind or "").strip()
    return k if k in REVIEW_KINDS else "過去問"
# 生徒の「解き終わった」連絡とみなす言葉（問題名が読めないときに管理者へ回すかの判断に使う）
DONE_WORDS = ("できました", "できた", "出来ました", "終わりました", "おわりました", "終わった", "おわった",
              "完了", "終了", "解けました", "とけました")


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
    with_answers = Column(Boolean, default=True)          # True=解答まで送付 / False=問題と解答用紙のみ
    label = Column(String, default="")                    # 問題名（例 20260911理解度確認テスト数学Ⅰ3-5）
    answer_url = Column(Text, default="")                 # 解答のリンク（「できました」で送る）
    answers_sent_at = Column(DateTime(timezone=True), nullable=True)
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


class HwSetting(Base):
    """設定の保存（キーと値）。リマインドのタイミングなど、画面から変えられる設定に使う。"""

    __tablename__ = "hw_settings"

    key = Column(String, primary_key=True, index=True)
    value = Column(Text, default="")
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class HwPending(Base):
    """自動で判別できなかった答案・連絡（管理者が判定フォームで決める）"""

    __tablename__ = "hw_pending"

    id = Column(BIGINT, primary_key=True, index=True)
    student_id = Column(BIGINT, index=True)
    kind = Column(String, default="answer")               # answer（答案の写真）/ text（できました等の連絡）
    reason = Column(Text, default="")
    files = Column(Text, default="[]")                    # JSON [{"id","name","link"}]
    reading = Column(Text, default="[]")                  # JSON 読み取り結果（大問ごと）
    text = Column(Text, default="")
    status = Column(String, default="open", index=True)   # open / resolved / ignored
    assignment_id = Column(BIGINT, nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    resolved_at = Column(DateTime(timezone=True), nullable=True)


class HwTextHandled(Base):
    """確認済みのテキストメッセージ（同じ連絡を二重に処理しないための控え）"""

    __tablename__ = "hw_text_handled"

    id = Column(BIGINT, primary_key=True, index=True)
    event_id = Column(BIGINT, unique=True, index=True)
    student_id = Column(BIGINT, nullable=True)
    assignment_id = Column(BIGINT, nullable=True)
    result = Column(String, default="")
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


def norm_key(v):
    """問題名・テストIDの照合用（全角半角・大文字小文字・空白や記号の違いを無視する）"""
    s = unicodedata.normalize("NFKC", str(v or "")).upper()
    return re.sub(r"[\s・、。,.!?「」『』()\[\]【】/_\-ー－〜~:：]+", "", s)


def default_label(a: HwAssignment, when=None):
    return f"{to_local(when or _now()):%Y%m%d}{a.kind}{a.subject}"


def assignment_message(a: HwAssignment, extra: str = ""):
    head = f"📝 {a.title}" + (f"（{a.round}回目の復習）" if a.kind == "復習テスト" else "")
    parts = [head]
    if extra.strip():
        parts.append(extra.strip())
    if not a.with_answers:
        parts.append("リンクを開いて問題を解いてください（解答はまだ付いていません）。\n"
                     "解き終わったら、このトークに次のように送ってください。解答をお送りします。\n"
                     f"「{a.label} できました」")
        parts.append(a.test_url)
    else:
        if a.kind in REVIEW_KINDS:
            parts.append("リンクを開いて問題を解き、最後の解答を見て自分で丸付けをしてください。\n"
                         "丸付けした答案の写真をこのトークに送ると提出になります。")
        else:
            parts.append("リンクのPDFを開いて問題を解き、丸付けをしてください。\n"
                         "丸付けした答案の写真（右上のテストIDが写るように）をこのトークに送ると提出になります。")
        parts.append(a.test_url + (f"\n解答: {a.answer_url}" if a.answer_url and a.answer_url != a.test_url else ""))
    parts.append(f"問題名: {a.label}\nテストID: {a.code}")
    return "\n\n".join(parts)


def answers_message(a: HwAssignment):
    return (f"✅ 「{a.label}」の解答です。\n"
            "解答を見て自分で丸付けをし、丸付けした答案の写真（テストIDが写るように）をこのトークに送ってください。\n\n"
            f"{a.answer_url}\nテストID: {a.code}")


def notify_admin(subject, body):
    """管理者（HW_ADMIN_EMAIL）へメールで知らせる。未設定・失敗でも処理は止めない。"""
    if not (MAIL_FROM and MAIL_PASSWORD and ADMIN_EMAIL):
        log.warning("管理者へのメールが未設定のため送れません: %s", subject)
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = MailHeader(subject, "utf-8")
        msg["From"], msg["To"], msg["Date"] = MAIL_FROM, ADMIN_EMAIL, formatdate(localtime=True)
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(MAIL_FROM, MAIL_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception as e:
        log.error("管理者へのメール送信に失敗: %s", e)
        return False


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
    student, dstatus, dscheduled = None, None, None
    if db is not None:
        student = db.query(HwStudent).filter(HwStudent.id == a.student_id).first()
        if a.delivery_id:
            d = db.query(HwDelivery).filter(HwDelivery.id == a.delivery_id).first()
            dstatus = d.status if d else None
            dscheduled = _fmt(d.scheduled_at) if d else None      # 送付の予定日時
    try:
        result = json.loads(a.result) if a.result else None
    except Exception:
        result = None
    return {
        "id": a.id, "student_id": a.student_id, "student_name": student.name if student else None,
        "sheet_student": a.sheet_student, "code": a.code, "kind": a.kind, "round": a.round,
        "parent_id": a.parent_id, "title": a.title, "test_url": a.test_url, "subject": a.subject,
        "book": a.book, "units": _units(a), "target_accuracy": a.target_accuracy, "status": a.status,
        "delivery_status": dstatus, "scheduled_at": dscheduled,
        "sent_at": _fmt(a.sent_at), "submitted_at": _fmt(a.submitted_at),
        "accuracy": a.accuracy, "result": result, "remind_count": a.remind_count,
        "last_reminded_at": _fmt(a.last_reminded_at), "review_status": a.review_status,
        "review_note": a.review_note, "external_ref": a.external_ref, "created_at": _fmt(a.created_at),
        "with_answers": a.with_answers, "label": a.label, "answer_url": a.answer_url,
        "answers_sent_at": _fmt(a.answers_sent_at),
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
    "・解答用紙の問題番号の前（□の中や番号の左側）に ✕（バツ）が書かれている → 【間違い】。"
    "✕は赤ペンでも黒・鉛筆でも同じく【間違い】として扱う（例:「☒(1)」「✕(2)」）。\n"
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
        base = ("送付済・未提出" if a.with_answers
                else ("解答送付済・丸付け待ち" if a.answers_sent_at else "送付済・できました待ち"))
        return base + (f"（リマインド{a.remind_count}回）" if a.remind_count else "")
    rs = a.review_status or ""
    if a.status == "done":
        return {"sent": "復習テスト送付済", "n/a": "提出済"}.get(rs, "目標達成")
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


def build_rows(a: HwAssignment, student_name: str, sections, link: str, when_text: str, fields=None):
    """読み取った大問 → 結果シートの行（A:M）。間違えた小問ごとに1行。L列＝提出F=1、M列＝テストのタイトル。
    fields = {(大問, 問題番号): (見出し, タイトル)} があれば、その問題の章＝見出し・節＝タイトルにする
    （PDFで送った課題で、送った問題PDFから分野を読めたとき）。"""
    fields = fields or {}
    units = _units(a)
    one_unit = units[0] if len(units) == 1 else {}

    def unit_page(ch, se):
        """結果シートのページ列に書く「教材のページ」。課題の単元（章・節）から探す。"""
        for u in units:
            if (ch and norm_key(u.get("chapter")) == norm_key(ch)) or \
               (se and norm_key(u.get("section")) == norm_key(se)):
                pg = (u.get("pages") or [None])[0]
                if pg:
                    return str(pg)
        pg = (one_unit.get("pages") or [None])[0]
        return str(pg) if pg else ""
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
            num = str(w.get("number")).strip()
            h, t = fields.get((str(dm) if dm else "", num), ("", ""))
            wch, wse = (h or ch), (t or se)
            rows.append([when_text, a.sheet_student or student_name, a.subject, book,
                         unit_page(wch, wse), wch, wse, num, link, total, "", "1", a.title])
            items.append({"book": book, "chapter": wch, "section": wse, "pages": [],
                          "wrong": [{"daimon": str(dm) if dm else "", "number": num}]})
    uniq, seen = [], {}
    for it in items:
        k = (it["book"], it["chapter"], it["section"])
        if k not in seen:
            seen[k] = it
            uniq.append(it)
        else:
            seen[k]["wrong"] += it["wrong"]
    return rows, uniq


def read_fields(pdf_bytes, targets):
    """問題PDFを Gemini で読み、各問題の分野（見出し・タイトル）を返す。戻り値: [{"key","heading","title"}]"""
    import google.generativeai as genai
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
    lines = "\n".join(f"{t['key']}: " + " ".join(x for x in (f"大問{t['daimon']}" if t["daimon"] else "", t["number"]) if x)
                      for t in targets)
    prompt = (
        "これは問題（問題用紙）のPDFです。下の一覧の各問題がどこにあるかを探し、その問題の分野を次の2つで答えてください。\n"
        "・heading = 問題用紙の先頭（上部）の見出し。その問題が含まれる単元名・章名など"
        "（例:「第3章 生物の体内環境」「第2章 二次関数」「Ⅰ 長文読解」）\n"
        "・title   = 問題番号の横に書かれたタイトル。小問（(1)(2)…）に題が無いときは、その小問が属する大問の"
        "番号（「1」「Ⅰ」など）の横のタイトルを使う（例:「1 酸素解離曲線と赤血球の働き」の(1)なら"
        "「酸素解離曲線と赤血球の働き」）。番号の数字自体は含めない。どちらにも題が無ければ \"\"\n"
        "用紙に書かれている文字をそのまま使い、推測で作らないこと。見つからない問題は両方 \"\" にする。\n\n"
        f"【問題の一覧】\n{lines}\n\n【出力形式】JSON配列のみ。\n"
        '[{"key":"1","heading":"Ⅰ 長文読解","title":"内容一致"}]')
    model = genai.GenerativeModel(GRADE_MODEL)
    resp = model.generate_content([prompt, {"mime_type": "application/pdf", "data": pdf_bytes}])
    return [x for x in extract_json_array(getattr(resp, "text", "")) if isinstance(x, dict)]


def classify_wrongs_with_question(a: HwAssignment, sections):
    """PDFで送った課題（過去問・プリントなど）：送った問題PDF（公開リンク）から、間違えた問題の分野を読む。
    戻り値: {(大問, 問題番号): (見出し, タイトル)}。読めない・対象外なら {}（記録はそのまま続ける）。"""
    if a.kind in REVIEW_KINDS or not a.test_url:
        return {}
    targets = []
    for s in sections:
        dm = _to_int(s.get("daimon"))
        for w in (s.get("wrong") or []):
            if isinstance(w, dict) and str(w.get("number", "")).strip():
                targets.append({"key": str(len(targets) + 1), "daimon": str(dm) if dm else "",
                                "number": str(w.get("number")).strip()})
    if not targets:
        return {}
    try:
        r = requests.get(a.test_url, timeout=60)
        if r.status_code != 200 or not r.content.startswith(b"%PDF"):
            return {}
        got = {str(x.get("key", "")).strip(): x for x in read_fields(r.content, targets)}
    except Exception as e:
        log.warning("問題PDFから分野を読めませんでした (%s): %s", a.code, e)
        return {}
    out = {}
    for t in targets:
        x = got.get(t["key"]) or {}
        h, ti = str(x.get("heading", "") or "").strip(), str(x.get("title", "") or "").strip()
        if h or ti:
            out[(t["daimon"], t["number"])] = (h, ti)
    return out


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
               "sid": a.external_ref or "",
               "items": [{"chapter": it["chapter"], "section": it["section"], "pages": it["pages"],
                          "wrong": it.get("wrong") or []} for it in items]}
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


def add_pending(db: Session, student: HwStudent, kind, reason, files=None, reading=None, text=""):
    """判別できなかった答案・連絡を「判定待ち」にして、管理者へメールで知らせる。"""
    p = HwPending(student_id=student.id, kind=kind, reason=reason, text=text or "", status="open",
                  files=json.dumps(files or [], ensure_ascii=False),
                  reading=json.dumps(reading or [], ensure_ascii=False))
    db.add(p)
    db.flush()
    read = [f"テストID「{r.get('test_id') or '読めず'}」・テスト名「{r.get('test_title') or '読めず'}」" for r in (reading or [])[:1]]
    form = f"{TESTGEN_URL}/hw#pending" if TESTGEN_URL else "テスト作成アプリの「LINE課題」画面"
    body = (f"生徒: {student.name}\n内容: {reason}\n"
            + (f"メッセージ: {text}\n" if text else "")
            + "".join(f"写真: {f.get('link')}\n" for f in (files or []))
            + (f"読み取り結果: {read[0]}\n" if read else "")
            + f"\nどの課題に当てはまるか判断し、こちらの判定フォームで記録・修正してください:\n{form}\n")
    notify_admin(f"【要確認】{student.name} さんの{'答案' if kind == 'answer' else '連絡'}を判別できませんでした", body)
    return p


def match_assignment(open_as: List[HwAssignment], sections):
    """読み取り結果と送った課題を照合する。テストIDが1件だけ一致、またはテスト名（問題名）が1件だけ一致したときだけ決める。"""
    codes = {norm_key(s.get("test_id")) for s in sections if s.get("test_id")}
    hit = [a for a in open_as if a.code in codes]
    if len(hit) == 1:
        return hit[0], "テストID"
    titles = {norm_key(s.get("test_title")) for s in sections if s.get("test_title")} - {""}
    if titles:
        hit = [a for a in open_as
               if any(t == norm_key(a.title) or (a.label and norm_key(a.label) in t) for t in titles)]
        if len(hit) == 1:
            return hit[0], "テスト名"
    return None, ""


def apply_submission(db: Session, a: HwAssignment, student: HwStudent, files_meta, sections, match, now=None):
    """読み取った答案を課題 a の提出として記録する（結果シート・正解率・台帳・復習テストの依頼）。"""
    now = now or _now()
    acc, total, wrong = score(sections)
    link = "\n".join(f["link"] for f in files_meta)
    fields = classify_wrongs_with_question(a, sections)      # PDFで送った課題だけ（送った問題から分野を読む）
    rows, items = build_rows(a, student.name, sections, link, to_local(now).strftime("%Y-%m-%d %H:%M:%S"),
                             fields=fields)
    append_result_rows(rows)

    a.status, a.submitted_at, a.accuracy, a.updated_at = "submitted", now, acc, now
    a.result = json.dumps({"total": total, "wrong": wrong, "match": match, "rows": len(rows), "fields": len(fields),
                           "files": [f["name"] for f in files_meta], "units": items}, ensure_ascii=False)
    for f in files_meta:
        g = db.query(HwGradedFile).filter(HwGradedFile.drive_file_id == f["id"]).first()
        if g is None:
            db.add(HwGradedFile(drive_file_id=f["id"], student_id=student.id, assignment_id=a.id,
                                file_name=f.get("name", ""), status="recorded"))
        else:
            g.assignment_id, g.status = a.id, "recorded"
    if a.kind not in REVIEW_KINDS:
        a.status, a.review_status = "done", "n/a"          # 過去問・プリントは提出で完了（復習テストは作らない）
    elif acc is None:
        a.review_note = "小問数が読み取れず正解率を判定できませんでした。講師が確認してください。"
    elif acc >= (a.target_accuracy or TARGET_ACCURACY):
        a.status = "done"                                    # 目標達成 → ループ終了
    else:
        request_review(a, student.name, items)
    db.commit()
    ledger_update(a)                                         # 送付テストタブ：提出F=1・提出日時・正解率・状態
    log.info("答案を記録しました: %s %s 正解率=%s（%s）", student.name, a.code, acc, match)
    return a


def grade_student(db: Session, student: HwStudent, open_as: List[HwAssignment], now=None):
    """1人分：新しく届いた答案をまとめて読み取り、課題に記録する。処理した課題（無ければ None）を返す。
    どの課題の答案か判別できない・解答を送る前に届いた場合は、判定待ちにして管理者へ知らせる。"""
    now = now or _now()
    since = min(_aware(a.sent_at) or now for a in open_as)
    files = [f for f in list_answer_files(student, since)
             if not db.query(HwGradedFile).filter(HwGradedFile.drive_file_id == f["id"]).first()]
    if not files:
        return None
    newest = max(f["_t"] or now for f in files)
    if now - newest < timedelta(minutes=SETTLE_MINUTES):      # まだ続きの写真が届くかもしれない
        return None

    files_meta = [{"id": f["id"], "name": f.get("name", ""),
                   "link": f.get("webViewLink") or f"https://drive.google.com/file/d/{f['id']}/view"} for f in files]
    blobs = [(hw_api.drive().files().get_media(fileId=f["id"]).execute(), f["mimeType"]) for f in files]
    sections = read_answers(blobs, open_as)
    a, match = match_assignment(open_as, sections)
    reason = ""
    if a is None:
        reason = ("テストIDもテスト名も読み取れた内容と一致せず、どのテストの答案か判別できませんでした"
                  f"（未提出の課題: {'、'.join(x.label or x.title for x in open_as)}）")
    elif not a.with_answers and a.answers_sent_at is None:
        reason = f"解答を送る前に「{a.label or a.title}」の答案が届きました（丸付け前の可能性があります）"
    if reason:
        for f in files_meta:
            db.add(HwGradedFile(drive_file_id=f["id"], student_id=student.id, assignment_id=None,
                                file_name=f["name"], status="unmatched"))
        add_pending(db, student, "answer", reason, files_meta, sections)
        db.commit()
        log.info("判定待ちにしました: %s %s", student.name, reason)
        return None
    return apply_submission(db, a, student, files_meta, sections, match, now)


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
# 2b) 「<問題名> できました」→ 解答を送る（問題と解答用紙のみで送った課題）
# ======================================================================
def send_answers(db: Session, a: HwAssignment, student: HwStudent, now=None):
    now = now or _now()
    if not a.answer_url:
        raise RuntimeError(f"「{a.label or a.title}」の解答のリンクが登録されていません")
    hw_api.line_push(student.group_id, answers_message(a))
    a.answers_sent_at, a.updated_at = now, now
    db.commit()
    ledger_update(a)
    log.info("解答を送りました: %s %s", student.name, a.code)


def _event_text(ev):
    try:
        return str(((json.loads(ev.raw or "{}").get("message") or {}).get("text")) or "")
    except Exception:
        return ""


def run_texts(now=None):
    """生徒のトークに届いたテキストを確認し、問題名（またはテストID）を含む完了連絡なら解答を送る（毎分）。"""
    from line_relay import LineHwEvent
    now = now or _now()
    db = SessionLocal()
    n = 0
    try:
        sync_sent(db)
        waiting = (db.query(HwAssignment)
                   .filter(HwAssignment.status == "sent", HwAssignment.with_answers.is_(False),
                           HwAssignment.answers_sent_at.is_(None)).all())
        if not waiting:
            return 0
        since = min(_aware(a.sent_at) or now for a in waiting) - timedelta(minutes=1)
        done_ids = {r[0] for r in db.query(HwTextHandled.event_id).all()}
        events = (db.query(LineHwEvent).filter(LineHwEvent.event_type == "text", LineHwEvent.created_at >= since)
                  .order_by(LineHwEvent.id.asc()).limit(300).all())
        for ev in events:
            if ev.id in done_ids:
                continue
            student = (db.query(HwStudent)
                       .filter(HwStudent.group_id == ev.group_id, HwStudent.enabled.is_(True)).first())
            text = _event_text(ev)
            cands = [a for a in waiting if student is not None and a.student_id == student.id]
            result, aid = "ignored", None
            if student is None or not cands:
                result = "no_candidates"
            else:
                nt = norm_key(text)
                hit = [a for a in cands if (a.label and norm_key(a.label) in nt) or a.code in nt]
                if len(hit) == 1:
                    try:
                        send_answers(db, hit[0], student, now)
                        result, aid, n = "answers_sent", hit[0].id, n + 1
                    except Exception as e:
                        db.rollback()
                        add_pending(db, student, "text", f"解答を送れませんでした: {e}", text=text)
                        result = "pending"
                elif hit or any(w in text for w in DONE_WORDS):
                    add_pending(db, student, "text",
                                ("問題名が複数のテストに一致しました" if hit else
                                 "完了の連絡ですが、どのテストか判別できませんでした")
                                + f"（解答待ちの課題: {'、'.join(a.label or a.title for a in cands)}）", text=text)
                    result = "pending"
            db.add(HwTextHandled(event_id=ev.id, student_id=student.id if student else None,
                                 assignment_id=aid, result=result))
            db.commit()
    finally:
        db.close()
    return n


# ======================================================================
# 3) 未提出のリマインド（同じ LINE ボットで生徒のグループへ）
# ======================================================================
_SETTING_KEY = "remind"


def _default_remind_settings():
    s, e = _remind_hours()
    return {"hours": f"{s}-{e}",
            "kinds": {g: {"enabled": True, "after_days": REMIND_AFTER_DAYS,
                          "interval_days": REMIND_INTERVAL_DAYS, "max": REMIND_MAX}
                      for g in REMIND_GROUPS}}


def remind_settings(db: Optional[Session] = None):
    """保存された設定（無ければ環境変数の既定値）。画面のフォームから変更できる。"""
    out = _default_remind_settings()
    own = db is None
    db = db or SessionLocal()
    try:
        row = db.query(HwSetting).filter(HwSetting.key == _SETTING_KEY).first()
        saved = json.loads(row.value) if row and row.value else {}
    except Exception as e:
        log.warning("リマインド設定の読み出しに失敗: %s", e)
        saved = {}
    finally:
        if own:
            db.close()
    if isinstance(saved.get("hours"), str) and re.match(r"^\d{1,2}-\d{1,2}$", saved["hours"].strip()):
        out["hours"] = saved["hours"].strip()
    for g in REMIND_GROUPS:
        v = (saved.get("kinds") or {}).get(g) or {}
        if not isinstance(v, dict):
            continue
        cur = out["kinds"][g]
        if "enabled" in v:
            cur["enabled"] = bool(v["enabled"])
        for k, lo, hi in (("after_days", 0, 60), ("interval_days", 1, 60), ("max", 0, 20)):
            if k in v:
                try:
                    cur[k] = max(lo, min(hi, int(v[k])))
                except (TypeError, ValueError):
                    pass
    return out


def save_remind_settings(patch: dict):
    """画面から来た設定を保存して、保存後の内容を返す。"""
    cur = remind_settings()
    if isinstance(patch.get("hours"), str) and re.match(r"^\s*\d{1,2}\s*-\s*\d{1,2}\s*$", patch["hours"]):
        a, b = [int(x) for x in patch["hours"].replace(" ", "").split("-")]
        if 0 <= a <= 23 and 1 <= b <= 24 and a < b:
            cur["hours"] = f"{a}-{b}"
    for g in REMIND_GROUPS:
        v = (patch.get("kinds") or {}).get(g)
        if not isinstance(v, dict):
            continue
        if "enabled" in v:
            cur["kinds"][g]["enabled"] = bool(v["enabled"])
        for k, lo, hi in (("after_days", 0, 60), ("interval_days", 1, 60), ("max", 0, 20)):
            if k in v and str(v[k]).strip() != "":
                try:
                    cur["kinds"][g][k] = max(lo, min(hi, int(v[k])))
                except (TypeError, ValueError):
                    pass
    db = SessionLocal()
    try:
        row = db.query(HwSetting).filter(HwSetting.key == _SETTING_KEY).first()
        if row is None:
            row = HwSetting(key=_SETTING_KEY)
            db.add(row)
        row.value = json.dumps(cur, ensure_ascii=False)
        row.updated_at = _now()
        db.commit()
    finally:
        db.close()
    return cur


_ASSIGN_KEY = "remind_a_"


def assignment_remind(db: Session, ids):
    """課題1件ごとのリマインド設定（課題IDごと）。設定が無い課題は入らない。"""
    if not ids:
        return {}
    keys = [f"{_ASSIGN_KEY}{i}" for i in ids]
    out = {}
    for row in db.query(HwSetting).filter(HwSetting.key.in_(keys)).all():
        try:
            out[int(row.key[len(_ASSIGN_KEY):])] = json.loads(row.value or "{}")
        except (ValueError, TypeError):
            pass
    return out


def save_assignment_remind(assignment_id: int, patch: dict):
    """課題1件のリマインド設定を保存する。reset=True なら種類ごとの設定に戻す。"""
    db = SessionLocal()
    try:
        key = f"{_ASSIGN_KEY}{int(assignment_id)}"
        row = db.query(HwSetting).filter(HwSetting.key == key).first()
        if patch.get("reset"):
            if row is not None:
                db.delete(row)
                db.commit()
            return None
        cur = {}
        if row is not None and row.value:
            try:
                cur = json.loads(row.value) or {}
            except ValueError:
                cur = {}
        if "enabled" in patch:
            cur["enabled"] = bool(patch["enabled"])
        for k, lo, hi in (("after_days", 0, 60), ("interval_days", 1, 60), ("max", 0, 20)):
            if k in patch and str(patch[k]).strip() != "":
                try:
                    cur[k] = max(lo, min(hi, int(patch[k])))
                except (TypeError, ValueError):
                    pass
        if row is None:
            row = HwSetting(key=key)
            db.add(row)
        row.value = json.dumps(cur, ensure_ascii=False)
        row.updated_at = _now()
        db.commit()
        return cur
    finally:
        db.close()


def effective_remind(a: HwAssignment, st=None, override=None):
    """この課題に実際に使うリマインド設定（種類ごとの設定に、課題1件の設定を重ねる）。"""
    st = st or remind_settings()
    conf = dict(st["kinds"][remind_group(a.kind)])
    for k, v in (override or {}).items():
        if k in ("enabled", "after_days", "interval_days", "max"):
            conf[k] = v
    return conf


def _remind_hours_setting(st):
    m = re.match(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$", st.get("hours") or "")
    return (int(m.group(1)), int(m.group(2))) if m else _remind_hours()


def reminder_due(a: HwAssignment, now, st=None, override=None):
    conf = effective_remind(a, st, override)
    if not conf.get("enabled", True):
        return False
    if a.status != "sent" or a.sent_at is None or (a.remind_count or 0) >= conf["max"]:
        return False
    due = _aware(a.sent_at) + timedelta(days=conf["after_days"] + conf["interval_days"] * (a.remind_count or 0))
    if now < due:
        return False
    last = to_local(a.last_reminded_at)
    return not (last and last.date() == to_local(now).date())      # 同じ日に2回は送らない


def remind_text(a: HwAssignment, now):
    days = max(1, (now - _aware(a.sent_at)).days)
    if not a.with_answers and a.answers_sent_at is None:
        return (f"⏰ リマインド\n「{a.title}」の「できました」の連絡がまだありません（送付から{days}日）。\n"
                f"解き終わったら「{a.label} できました」と送ってください。解答をお送りします。\n\n"
                f"{a.test_url}\nテストID: {a.code}")
    return (f"⏰ リマインド\n「{a.title}」の答案がまだ届いていません（送付から{days}日）。\n"
            f"丸付けした答案の写真をこのトークに送ってください。\n\n{a.test_url}\nテストID: {a.code}")


def run_reminders(now=None):
    now = now or _now()
    st = remind_settings()
    start, end = _remind_hours_setting(st)
    if not (start <= to_local(now).hour < end):
        return 0
    db = SessionLocal()
    n = 0
    try:
        sync_sent(db)
        sent = db.query(HwAssignment).filter(HwAssignment.status == "sent").all()
        overrides = assignment_remind(db, [x.id for x in sent])
        for a in sent:
            if not reminder_due(a, now, st, overrides.get(a.id)):
                continue
            student = db.query(HwStudent).filter(HwStudent.id == a.student_id).first()
            if student is None or not student.group_id or not student.enabled:
                continue
            if db.query(HwPending).filter(HwPending.student_id == student.id, HwPending.status == "open").first():
                continue   # 管理者の判定待ちの答案・連絡があるなら、それが提出かもしれないので送らない
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
    sched.add_job(run_texts, IntervalTrigger(minutes=1), id="hw:assign-texts", max_instances=1, coalesce=True)
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
    with_answers: bool = True        # False なら「問題と解答用紙のみ」送り、「できました」で解答を送る
    label: str = ""                  # 問題名（空なら 送付日＋種類＋科目）
    answer_url: str = ""             # 解答のリンク


class ReviewStatusIn(BaseModel):
    status: str                      # drafted / failed
    note: str = ""
    external_ref: str = ""


def _settings_json(st):
    """画面に返す設定一式。remind_* は「理解度確認テスト」の値（従来の表示との互換）。"""
    base = st["kinds"]["理解度確認テスト"]
    return {"ok": True, "grading_enabled": bool(RESULT_SPREADSHEET_ID and GEMINI_API_KEY),
            "review_enabled": bool(TESTGEN_URL and TESTGEN_TOKEN), "target_accuracy": TARGET_ACCURACY,
            "remind_after_days": base["after_days"], "remind_interval_days": base["interval_days"],
            "remind_max": base["max"], "remind_hours": st["hours"], "settle_minutes": SETTLE_MINUTES,
            "remind": st, "remind_groups": list(REMIND_GROUPS)}


@router.get("/assignments/settings")
def assignment_settings(authorization: str = Header(None)):
    err = hw_api._auth_or_401(authorization)
    if err:
        return err
    return _settings_json(remind_settings())


@router.post("/assignments/settings")
def update_assignment_settings(payload: dict, authorization: str = Header(None)):
    """リマインドのタイミングを保存する（種類ごと）。"""
    err = hw_api._auth_or_401(authorization)
    if err:
        return err
    st = save_remind_settings(payload.get("remind") or payload)
    log.info("リマインド設定を更新しました: %s", st)
    return _settings_json(st)


@router.post("/assignments/{assignment_id}/mark_sent")
def mark_assignment_sent(assignment_id: int, authorization: str = Header(None),
                         db: Session = Depends(get_db)):
    """配信失敗になっているが実際にはLINEに届いていた課題を「送付済み」に直す。
    （提出の自動記録・リマインドの対象に戻す）"""
    err = hw_api._auth_or_401(authorization)
    if err:
        return err
    a = db.query(HwAssignment).filter(HwAssignment.id == assignment_id).first()
    if a is None:
        return {"ok": False, "error": "課題が見つかりません"}
    if a.status not in ("failed", "scheduled"):
        return {"ok": False, "error": f"この課題は「{a.status}」なので変更できません"}
    now = _now()
    d = db.query(HwDelivery).filter(HwDelivery.id == a.delivery_id).first() if a.delivery_id else None
    if d is not None and d.status != "sent":
        d.status, d.sent_at, d.error = "sent", d.sent_at or now, None
    a.status = "sent"
    a.sent_at = a.sent_at or (d.sent_at if d is not None else None) or now
    a.review_note = None
    a.updated_at = now
    db.commit()
    ledger_update(a)
    log.info("配信失敗の課題を送付済みに直しました: %s", a.code)
    return {"ok": True, "assignment": assignment_json(a, db)}


@router.post("/assignments/{assignment_id}/remind")
def update_assignment_remind(assignment_id: int, payload: dict,
                             authorization: str = Header(None), db: Session = Depends(get_db)):
    """課題1件のリマインド設定（送る/送らない・何日後・何日おき・最大何回）。reset=True で種類ごとの設定に戻す。"""
    err = hw_api._auth_or_401(authorization)
    if err:
        return err
    a = db.query(HwAssignment).filter(HwAssignment.id == assignment_id).first()
    if a is None:
        return {"ok": False, "error": "課題が見つかりません"}
    cur = save_assignment_remind(assignment_id, payload or {})
    log.info("課題のリマインド設定を更新: %s %s", a.code, cur)
    return {"ok": True, "remind": cur, "remind_effective": effective_remind(a, None, cur)}


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
    st = remind_settings(db)
    overrides = assignment_remind(db, [a.id for a in rows])
    out = []
    for a in rows:
        j = assignment_json(a, db)
        j["remind"] = overrides.get(a.id)                      # 課題ごとの設定（無ければ None）
        j["remind_effective"] = effective_remind(a, st, overrides.get(a.id))
        out.append(j)
    return {"ok": True, "assignments": out}


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
    if not body.with_answers and not body.answer_url.strip():
        return {"ok": False, "error": "「問題と解答用紙のみ」で送るときは、あとで送る解答のリンクが必要です"}
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
        with_answers=bool(body.with_answers), answer_url=body.answer_url.strip(), label=body.label.strip(),
    )
    if not a.label:
        a.label = default_label(a, when)
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
    return {"ok": True, "synced": run_sync(), "answers_sent": run_texts(), "graded": run_grading(),
            "reminded": run_reminders()}


# ---- 判定待ち（管理者の判定フォーム用）-------------------------------------------
class PendingResolveIn(BaseModel):
    action: str                      # record（この課題の答案として記録）/ send_answers（解答を送る）/ ignore
    assignment_id: Optional[int] = None
    note: str = ""


def pending_json(p: HwPending, db: Session):
    student = db.query(HwStudent).filter(HwStudent.id == p.student_id).first()
    cands = (db.query(HwAssignment)
             .filter(HwAssignment.student_id == p.student_id, HwAssignment.status.in_(["sent", "submitted"]))
             .order_by(HwAssignment.id.desc()).all())
    try:
        files, reading = json.loads(p.files or "[]"), json.loads(p.reading or "[]")
    except Exception:
        files, reading = [], []
    return {"id": p.id, "student_id": p.student_id, "student_name": student.name if student else None,
            "kind": p.kind, "reason": p.reason, "files": files, "text": p.text,
            "reading": [{"test_id": r.get("test_id", ""), "test_title": r.get("test_title", ""),
                         "unit": r.get("unit", ""), "daimon": r.get("daimon", ""), "total": r.get("total", 0),
                         "wrong": [w.get("number") for w in (r.get("wrong") or []) if isinstance(w, dict)]}
                        for r in reading if isinstance(r, dict)],
            "status": p.status, "assignment_id": p.assignment_id, "note": p.note,
            "created_at": _fmt(p.created_at), "resolved_at": _fmt(p.resolved_at),
            "candidates": [{"id": a.id, "code": a.code, "title": a.title, "label": a.label, "status": a.status,
                            "with_answers": a.with_answers, "answers_sent": a.answers_sent_at is not None,
                            "sent_at": _fmt(a.sent_at)} for a in cands]}


@router.get("/pending")
def list_pending(status: str = "open", authorization: str = Header(None), db: Session = Depends(get_db)):
    err = hw_api._auth_or_401(authorization)
    if err:
        return err
    q = db.query(HwPending)
    if status:
        q = q.filter(HwPending.status.in_([x.strip() for x in status.split(",") if x.strip()]))
    return {"ok": True, "pending": [pending_json(p, db) for p in q.order_by(HwPending.id.desc()).limit(200).all()]}


@router.post("/pending/{pending_id}/resolve")
def resolve_pending(pending_id: int, body: PendingResolveIn, authorization: str = Header(None),
                    db: Session = Depends(get_db)):
    """管理者の判定：選んだ課題の答案として記録する / 選んだ課題の解答を送る / 無視する"""
    err = hw_api._auth_or_401(authorization)
    if err:
        return err
    p = db.query(HwPending).filter(HwPending.id == pending_id).first()
    if p is None:
        return Response(status_code=404)
    if p.status != "open":
        return {"ok": False, "error": "この件はすでに判定済みです"}
    student = db.query(HwStudent).filter(HwStudent.id == p.student_id).first()
    a = None
    if body.action in ("record", "send_answers"):
        a = db.query(HwAssignment).filter(HwAssignment.id == body.assignment_id,
                                          HwAssignment.student_id == p.student_id).first()
        if a is None or student is None:
            return {"ok": False, "error": "この生徒の課題を選んでください"}
    try:
        if body.action == "record":
            if p.kind != "answer":
                return {"ok": False, "error": "答案の写真ではないため記録できません"}
            apply_submission(db, a, student, json.loads(p.files or "[]"), json.loads(p.reading or "[]"),
                             "管理者の判定")
        elif body.action == "send_answers":
            send_answers(db, a, student)
        elif body.action != "ignore":
            return {"ok": False, "error": "action は record / send_answers / ignore のいずれかです"}
    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}
    p.status = "ignored" if body.action == "ignore" else "resolved"
    p.assignment_id, p.note, p.resolved_at = (a.id if a else None), (body.note or body.action), _now()
    db.commit()
    return {"ok": True, "pending": pending_json(p, db)}
