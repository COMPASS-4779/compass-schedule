# -*- coding: utf-8 -*-
"""DBのバックアップ（db_backup.py が作る .json.gz）から、テーブルの中身を戻す。

本番で使うときは必ず先に今のDBのバックアップを取り（管理の手順どおり）、Render の Shell などで実行する:
  python tools/restore_db.py <バックアップ.json.gz> --tables hw_assignments,hw_students   … 指定のテーブルだけ
  python tools/restore_db.py <バックアップ.json.gz> --all --yes                          … 全テーブル
戻すテーブルは、いまの行を消してからバックアップの行を入れ直す（同じトランザクション内）。
"""
import argparse
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import MetaData  # noqa: E402

from database import engine  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--tables", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    data = json.loads(gzip.open(a.file).read().decode("utf-8"))
    md = MetaData()
    md.reflect(bind=engine)
    names = [t.name for t in md.sorted_tables] if a.all else [x.strip() for x in a.tables.split(",") if x.strip()]
    if not names:
        sys.exit("--tables か --all を指定してください")
    print("バックアップ:", data.get("_meta", {}).get("at"), data.get("_meta", {}).get("commit"))
    for n in names:
        print(f"  {n}: {len(data.get(n) or [])}行に戻す")
    if not a.yes and input("戻しますか？ (yes/no): ").strip().lower() != "yes":
        sys.exit("やめました")
    tables = [t for t in md.sorted_tables if t.name in names]
    with engine.begin() as con:
        for t in reversed(tables):                 # 子のテーブルから消す
            con.execute(t.delete())
        for t in tables:                           # 親のテーブルから入れる
            rows = data.get(t.name) or []
            if rows:
                con.execute(t.insert(), rows)
    print("戻しました")


if __name__ == "__main__":
    main()
