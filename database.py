import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# 環境変数 DATABASE_URL があればそれを使用し、なければローカルのSQLiteを使用する設定
# Renderでは自動的に DATABASE_URL が割り当てられます
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./compass.db")

# PostgreSQLの場合、URLの先頭が postgres:// になっているとSQLAlchemyでエラーが出る場合があるため修正
if SQLALCHEMY_DATABASE_URL.startswith("postgres://"):
    SQLALCHEMY_DATABASE_URL = SQLALCHEMY_DATABASE_URL.replace("postgres://", "postgresql://", 1)

# SQLiteの場合は connect_args={"check_same_thread": False} が必要だが、PostgreSQLでは不要
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
    )
else:
    engine = create_engine(SQLALCHEMY_DATABASE_URL)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# 使用中のDB種別。SQLiteの場合、Renderではインスタンスごとに別ファイルとなり
# 再起動でも消えるため、保存したデータが失われる。起動時に警告を出す。
DB_BACKEND = "sqlite" if SQLALCHEMY_DATABASE_URL.startswith("sqlite") else "postgresql"
IS_EPHEMERAL_DB = DB_BACKEND == "sqlite"

if IS_EPHEMERAL_DB:
    print("!!! 警告: DATABASE_URL が未設定のため SQLite を使用しています。", flush=True)
    print("!!! Render ではインスタンスごとに別ファイルとなり、再起動で消えます。", flush=True)
    print("!!! 保存データが失われます。DATABASE_URL に PostgreSQL を設定してください。", flush=True)
