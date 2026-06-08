import sqlite3
import urllib.parse
import urllib.request
import json
import re
import os
from fastapi import FastAPI, HTTPException, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 【デプロイ対応】Renderの「消えないフォルダ」に対応させる設定
# ローカル環境ならそのまま、本番環境なら /data/spatch.db を使うように自動で切り替えます
if os.path.exists("/data"):
    DB_FILE = "/data/spatch.db"
    UPLOAD_DIR = "/data/uploads"
else:
    DB_FILE = "spatch.db"
    UPLOAD_DIR = "uploads"

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


def get_lat_lng(address: str):
    try:
        zenkaku = "０１２３４５６７８９－ー"
        hankaku = "0123456789--"
        trans_table = str.maketrans(zenkaku, hankaku)
        clean_address = address.translate(trans_table).strip("- ")
        clean_address = re.sub(r'(ビル|マンション|アパート|コーポ|ハイツ|メゾン|シャトー|室|階).*$', '', clean_address).strip("- ")

        encoded_address = urllib.parse.quote(clean_address)
        url = f"https://msearch.gsi.go.jp/address-search/AddressSearch?q={encoded_address}"
        
        req = urllib.request.Request(url, headers={'User-Agent': 'spatch-app-v1.7'})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            if data and len(data) > 0:
                lon, lat = data[0]['geometry']['coordinates']
                return lat, lon
    except Exception as e:
        print(f"⚠️ 住所検索エラー: {e}")
    return 35.1208, 137.1393

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE,
            password TEXT,
            role TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS spaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            landlord_id INTEGER,
            title TEXT,
            address TEXT,
            size_sqft TEXT,
            price_monthly TEXT,
            status TEXT,
            latitude REAL,
            longitude REAL,
            image_url TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            space_id INTEGER,
            sender_id INTEGER,
            text TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

# --- ユーザー・ログインAPI ---
@app.post("/api/signup")
def signup(email: str = Form(...), password: str = Form(...), role: str = Form(...)):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users (email, password, role) VALUES (?, ?, ?)", (email, password, role))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="このメールアドレスは既に登録されています。")
    user_id = cursor.lastrowid
    conn.close()
    return {"message": "ユーザー登録が成功しました！", "user_id": user_id, "role": role, "email": email}

@app.post("/api/login")
def login(email: str = Form(...), password: str = Form(...)):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE email = ? AND password = ?", (email, password))
    user = cursor.fetchone()
    conn.close()
    if user is None:
        raise HTTPException(status_code=400, detail="メールアドレスまたはパスワードが間違っています。")
    return {"message": "ログイン成功！", "user_id": user["id"], "role": user["role"], "email": user["email"]}

@app.post("/api/users/switch-role")
def switch_role(user_id: int = Form(...), new_role: str = Form(...)):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
    conn.commit()
    conn.close()
    return {"message": f"役割を {new_role} に切り替えました。", "role": new_role}


# --- 土地に関するAPI ---
@app.get("/api/spaces")
def get_all_spaces():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM spaces ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

@app.get("/api/spaces/search")
def search_spaces(keyword: str):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    sql_query = "SELECT * FROM spaces WHERE (address LIKE ? OR title LIKE ?) ORDER BY id DESC"
    search_keyword = f"%{keyword}%"
    cursor.execute(sql_query, (search_keyword, search_keyword))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

@app.post("/api/spaces")
async def create_space(
    title: str = Form(...),
    address: str = Form(...),
    size_sqft: str = Form(...),
    price_monthly: str = Form(...),
    landlord_id: int = Form(...),
    image: UploadFile = File(None)
):
    lat, lng = get_lat_lng(address)
    image_url = ""
    if image:
        file_extension = os.path.splitext(image.filename)[1]
        # 本番環境の画像URLドメインは Render 側で自動解決させるため、相対パスか環境変数を使いますが、
        # 今回は動的にドメインを付与できるよう、保存時はファイル名だけをトリガーにしやすく設計します
        saved_filename = f"space_{landlord_id}_{int(os.path.getmtime(DB_FILE))}_{title[:5]}{file_extension}"
        file_path = os.path.join(UPLOAD_DIR, saved_filename)
        with open(file_path, "wb") as f:
            f.write(await image.read())
        
        # デプロイ後にURLが確定するため、ひとまず固定ではなくホスト情報を後から付与しやすい形で保存
        image_url = f"/uploads/{saved_filename}"

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    sql_query = """
        INSERT INTO spaces (landlord_id, title, address, size_sqft, price_monthly, status, latitude, longitude, image_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    cursor.execute(sql_query, (landlord_id, title, address, size_sqft, price_monthly, "募集中", lat, lng, image_url))
    conn.commit()
    conn.close()
    return {"message": "土地の登録が成功しました！"}

@app.post("/api/spaces/{space_id}/update-image")
async def update_space_image(
    space_id: int,
    landlord_id: int = Form(...),
    image: UploadFile = File(...)
):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT landlord_id FROM spaces WHERE id = ?", (space_id,))
    space = cursor.fetchone()
    if not space:
        conn.close()
        raise HTTPException(status_code=404, detail="土地が見つかりません。")
    if space[0] != landlord_id:
        conn.close()
        raise HTTPException(status_code=403, detail="自分以外の土地の写真を変更することはできません。")
        
    file_extension = os.path.splitext(image.filename)[1]
    saved_filename = f"update_{space_id}_{landlord_id}{file_extension}"
    file_path = os.path.join(UPLOAD_DIR, saved_filename)
    
    with open(file_path, "wb") as f:
        f.write(await image.read())
        
    image_url = f"/uploads/{saved_filename}"
    
    cursor.execute("UPDATE spaces SET image_url = ? WHERE id = ?", (image_url, space_id))
    conn.commit()
    conn.close()
    
    return {"message": "写真を新しく追加・更新しました！", "image_url": image_url}

@app.delete("/api/spaces/{space_id}")
def delete_space(space_id: int, landlord_id: int = Form(...)):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT landlord_id FROM spaces WHERE id = ?", (space_id,))
    space = cursor.fetchone()
    if not space:
        conn.close()
        raise HTTPException(status_code=404, detail="指定された土地が見つかりません。")
    if space[0] != landlord_id:
        conn.close()
        raise HTTPException(status_code=403, detail="自分以外の土地を削除することはできません。")
    cursor.execute("DELETE FROM spaces WHERE id = ?", (space_id,))
    conn.commit()
    conn.close()
    return {"message": "土地情報を削除しました。"}


# --- チャットに関するAPI ---
@app.get("/api/messages/{space_id}")
def get_messages(space_id: int):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    query = """
        SELECT m.*, u.email as sender_email 
        FROM messages m 
        JOIN users u ON m.sender_id = u.id 
        WHERE m.space_id = ? 
        ORDER BY m.timestamp ASC
    """
    cursor.execute(query, (space_id,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

@app.post("/api/messages")
def send_message(space_id: int = Form(...), sender_id: int = Form(...), text: str = Form(...)):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO messages (space_id, sender_id, text) VALUES (?, ?, ?)", (space_id, sender_id, text))
    conn.commit()
    conn.close()
    return {"message": "メッセージを送信しました！"}