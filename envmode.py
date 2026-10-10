# -*- coding: utf-8 -*-
"""本番／テスト環境の切り替え（環境変数 ENVIRONMENT）。

テスト環境（ENVIRONMENT=staging または development）では、生徒・先生に何も届かず、本番のデータも触らない:
  - LINE（課題の配信・リマインド・応答）とメールは送らない（ログと /hw/api/outbox に記録だけ）
  - Google Drive・スプレッドシートは、テスト用のフォルダとシートを設定して STAGING_GOOGLE_OK=1 にしたときだけ使う
    （本番の受信先フォルダや結果シートを、テスト環境が書き換えないように）
"""
import collections
import logging
import os
import time

ENVIRONMENT = (os.environ.get("ENVIRONMENT") or "production").strip().lower()
IS_STAGING = ENVIRONMENT in ("staging", "stg", "development", "dev", "test")
GOOGLE_OK = (not IS_STAGING) or os.environ.get("STAGING_GOOGLE_OK", "").strip() == "1"

log = logging.getLogger("envmode")
_OUTBOX = collections.deque(maxlen=300)


def hold(channel, to, text):
    """テスト環境で、外部への送信の代わりに記録する。"""
    _OUTBOX.appendleft({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "channel": channel,
                        "to": str(to)[:40], "text": str(text)[:2000]})
    log.info("[テスト環境] %s への送信を止めました（%s…）: %s", channel, str(to)[:12], str(text)[:80])
    return True


def outbox():
    return list(_OUTBOX)


def require_google(what="Google"):
    """テスト環境で、テスト用のフォルダ・シートの設定（STAGING_GOOGLE_OK=1）が無ければ止める。"""
    if not GOOGLE_OK:
        raise RuntimeError(f"テスト環境のため {what} は使いません（本番のフォルダ・シートを触らないように。"
                           "テスト用を設定したら STAGING_GOOGLE_OK=1）")
