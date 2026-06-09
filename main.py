from fastapi import FastAPI, Depends, Query, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
import models, schemas
from database import engine, SessionLocal
import json
import google.generativeai as genai
from pydantic import BaseModel
import os

# データベースの初期化
models.Base.metadata.create_all(bind=engine)

# Gemini APIの設定（環境変数から取得）
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# ★ここで「app」を定義しています
app = FastAPI(title="COMPASS式 学習管理システム API")

# CORS設定（ブラウザからの通信を許可）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class UserEdit(BaseModel):
    password: str
    role: str

@app.get("/")
def read_root():
    return {"message": "COMPASS API is ready with AI! 🤖"}

@app.get("/api/config")
def get_config():
    """フロントエンド用の設定情報を返す（APIキーは環境変数から取得）"""
    return {"gemini_api_key": GEMINI_API_KEY}

# --- ユーザー管理系 API ---
@app.post("/api/users/", response_model=schemas.UserResponse)
def create_user(user: schemas.UserCreate, db: Session = Depends(get_db)):
    db_user = models.User(user_id=user.user_id, password=user.password, role=user.role)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

@app.get("/api/users/all")
def get_all_users(db: Session = Depends(get_db)):
    users = db.query(models.User).all()
    return {"success": True, "users": [{"user_id": u.user_id, "password": u.password, "role": u.role} for u in users]}

@app.post("/api/users/edit/{user_id}")
def edit_user(user_id: str, user_edit: UserEdit, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.user_id == user_id).first()
    if user:
        user.password = user_edit.password
        user.role = user_edit.role
        db.commit()
        return {"success": True, "message": "更新しました"}
    return {"success": False, "message": "ユーザーが見つかりません"}

@app.delete("/api/users/delete/{user_id}")
def delete_user(user_id: str, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.user_id == user_id).first()
    if user:
        db.delete(user)
        db.commit()
        return {"success": True, "message": "削除しました"}
    return {"success": False, "message": "ユーザーが見つかりません"}

# --- ログイン・保存系 API ---
@app.get("/api")
def api_get_handler(action: str, id: str, password: str = Query(alias="pass"), folderId: str = None, db: Session = Depends(get_db)):
    if action == "login":
        user = db.query(models.User).filter(models.User.user_id == id, models.User.password == password).first()
        if user:
            saved_data = json.loads(user.data) if user.data else {
                "profile": None, "curriculum": {}, "progress": {}, 
                "testResults": [], "customPresets": [], "hiddenPresets": [], 
                "currentWeek": 1, "currentDay": 1
            }
            return {"success": True, "message": "ログイン成功", "appData": saved_data}
        return {"success": False, "message": "IDまたはパスワードが間違っています。"}

@app.post("/api")
async def api_post_handler(request: Request, db: Session = Depends(get_db)):
    body_bytes = await request.body()
    body = json.loads(body_bytes)
    action = body.get("action")
    user_id = body.get("id")
    app_data = body.get("data")

    if action == "save":
        user = db.query(models.User).filter(models.User.user_id == user_id).first()
        if user:
            user.data = json.dumps(app_data, ensure_ascii=False)
            db.commit()
            return {"success": True, "message": "保存完了しました！"}
        return {"success": False, "message": "ユーザーが見つかりません。"}

# --- AI画像解析系 API ---
@app.post("/api/analyze-test")
async def analyze_test(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        prompt = """
        あなたはプロのデータ入力アシスタントです。添付された成績表の画像から、以下のルールに絶対に従ってデータを読み取り、JSON形式の配列（リスト）のみで出力してください。
        【出力JSONフォーマット】
        [
          { "subject": "英語", "score": 得点(数値), "average": 平均点(数値), "deviation": 偏差値(数値) }
        ]
        ※余計な文章（```jsonなど）を含めずに直接JSONのみを返してください。
        """
        image_parts = [{"mime_type": file.content_type, "data": contents}]
        model = genai.GenerativeModel('gemini-2.5-flash')
        response = model.generate_content([prompt, image_parts[0]])
        text = response.text.replace("```json", "").replace("```", "").strip()
        result_data = json.loads(text)
        return {"success": True, "data": result_data}
    except Exception as e:
        return {"success": False, "message": f"AI解析エラー: {str(e)}"}

@app.post("/api/analyze-task")
async def analyze_task(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        
        prompt = """
        あなたはプロの教育プランナーです。添付された年間行事予定表、月間予定表、または宿題プリントの画像・PDFを解析し、記載されているイベントを**すべて**漏らさず抽出し、JSONの配列形式で出力してください。

        【抽出対象】
        1. 定期テスト（1学期中間、期末テストなどすべて）
        2. 模試（進研模試、全統模試、実力テストなどすべて）
        3. 学校行事（始業式、終業式、修学旅行、体育祭、夏休みの開始・終了などすべて）
        4. 宿題・提出物・小テスト

        【日付(deadline)に関する厳格なルール】
        - 必ず「YYYY-MM-DD」の形式で実際の日付を出力してください（例: 2026-05-15）。
        - プリントに「月/日」しか書かれていない場合、現在の年（2026年）を補って日付を作成してください。
        - 日本の学校年度（4月始まり）を考慮し、1月〜3月の行事の場合は「2027年」として扱うなど、文脈から正しい年を推測してください。
        - 期間（例：7/20〜8/31）で書かれている場合は、そのイベントの「開始日」を deadline として設定してください。

        【出力JSONフォーマット】
        [
          {
            "type": "定期テスト" または "模試" または "行事" または "宿題",
            "subject": "科目名（行事や複数科目の場合は '総合'）",
            "content": "内容（例: 1学期期末テスト、夏休み開始、英語ワークなど）",
            "amount": "分量（予定や行事の場合は '-'、宿題の場合は 'P10-20' など）",
            "deadline": "YYYY-MM-DD"
          }
        ]
        ※画像のどこにも記載されていない架空の宿題（問題集 P10-15など）は絶対に作らないでください。画像にある情報のみを忠実に抽出すること。
        ※余計な挨拶やマークダウン(```json)は含めず、純粋なJSONの配列のみを出力してください。
        """
        
        image_parts = [{"mime_type": file.content_type, "data": contents}]
        model = genai.GenerativeModel('gemini-2.5-flash')
        response = model.generate_content([prompt, image_parts[0]])
        text = response.text.replace("```json", "").replace("```", "").strip()
        result_data = json.loads(text)
        return {"success": True, "data": result_data}
        
    except Exception as e:
        return {"success": False, "message": f"AI解析エラー: {str(e)}"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
