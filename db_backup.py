# -*- coding: utf-8 -*-
"""データベースのバックアップ（全テーブルを JSON にして gzip。Google Drive の「バックアップ」フォルダに30日分）。

- 新しい版が起動したとき（その版のバックアップがまだ無ければ）に1回
- 毎日 3:00（日本時間）に1回
本番だけ動かす（テスト環境では動かさない）。戻すときは tools/restore_db.py（手順は docs/リリース手順.md）。
"""
import datetime as _dt
import gzip
import io
import json
import logging
import os

from sqlalchemy import MetaData, select

import envmode
from database import engine

log = logging.getLogger("db_backup")
FOLDER = "バックアップ_課題送信DB"
KEEP_DAYS = 30


def _commit():
    return (os.environ.get("RENDER_GIT_COMMIT") or "")[:7] or "local"


def dump_bytes():
    """全テーブルを {テーブル名: [行]} の JSON（gzip）にする。"""
    md = MetaData()
    md.reflect(bind=engine)
    out = {"_meta": {"at": _dt.datetime.utcnow().isoformat() + "Z", "commit": _commit(), "tables": []}}
    with engine.connect() as con:
        for t in md.sorted_tables:
            rows = [dict(r._mapping) for r in con.execute(select(t))]
            out[t.name] = rows
            out["_meta"]["tables"].append({"name": t.name, "rows": len(rows)})
    raw = json.dumps(out, ensure_ascii=False, default=str).encode("utf-8")
    return gzip.compress(raw)


def _folder_id():
    import hw_api
    return hw_api.ensure_path(FOLDER)


def list_backups():
    import hw_api
    fid = _folder_id()
    res = hw_api.drive().files().list(q=f"'{fid}' in parents and trashed = false",
                                      fields="files(id,name,size,createdTime)", orderBy="name desc",
                                      pageSize=200).execute()
    return res.get("files", [])


def make(reason="manual"):
    import hw_api
    from googleapiclient.http import MediaIoBaseUpload
    data = dump_bytes()
    stamp = (_dt.datetime.utcnow() + _dt.timedelta(hours=9)).strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}_{_commit()}_{reason}.json.gz"
    hw_api.drive().files().create(body={"name": name, "parents": [_folder_id()]},
                                  media_body=MediaIoBaseUpload(io.BytesIO(data), mimetype="application/gzip"),
                                  fields="id").execute()
    prune()
    log.info("DBをバックアップしました: %s（%dKB）", name, len(data) // 1024)
    return name


def prune():
    """30日より古いものを消す（最新の10個は残す）。"""
    import hw_api
    limit = (_dt.datetime.utcnow() + _dt.timedelta(hours=9) - _dt.timedelta(days=KEEP_DAYS)).strftime("%Y%m%d")
    for i, f in enumerate(list_backups()):
        if i >= 10 and f["name"][:8] < limit:
            try:
                hw_api.drive().files().delete(fileId=f["id"]).execute()
            except Exception:
                pass


def on_start():
    """新しい版の起動時: この版のバックアップがまだ無ければ取る。"""
    try:
        if not any(f"_{_commit()}_" in f["name"] for f in list_backups()[:20]):
            make("release")
    except Exception as e:
        log.warning("起動時のDBバックアップに失敗: %s", e)


def daily():
    try:
        make("daily")
    except Exception as e:
        log.warning("毎日のDBバックアップに失敗: %s", e)


def add_jobs(sched):
    """本番だけ: 起動時と毎日3時。"""
    if envmode.IS_STAGING:
        return
    from apscheduler.triggers.cron import CronTrigger
    import datetime as dt
    sched.add_job(on_start, "date", run_date=dt.datetime.now() + dt.timedelta(seconds=30), id="db:backup-start")
    sched.add_job(daily, CronTrigger(hour=3, minute=0), id="db:backup-daily", max_instances=1, coalesce=True)
