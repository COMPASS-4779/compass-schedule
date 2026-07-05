from pydantic import BaseModel
from typing import Any, Optional

# ユーザー登録時に受け取るデータのルール
class UserCreate(BaseModel):
    user_id: str
    password: str
    role: str = "student"

# 登録後にAPIが返すデータ
class UserResponse(BaseModel):
    id: int
    user_id: str
    role: str

    class Config:
        from_attributes = True

# 保存リクエスト用のデータ構造
class SaveDataRequest(BaseModel):
    action: str
    id: str
    data: Any  # appDataの中身をそのまま受け取ります