# api_server.py
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import json
from typing import Optional
from datetime import datetime
import psycopg2 # جایگزین sqlite3
# import requests # این مورد در کد شما بود و حفظ می‌شود
from fastapi.responses import RedirectResponse, StreamingResponse
import requests # مطمئن می شویم که requests هم وارد شود

# -----------------------
# --- CONFIGURATION
# -----------------------

# DATABASE_URL را از متغیرهای محیطی یا مقدار پیش‌فرض می‌خواند
# توجه: لطفاً USER, PASSWORD, HOST, DATABASE را با مقادیر واقعی جایگزین کنید.
DATABASE_URL = os.getenv("DATABASE_URL")

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_FILE_URL = f"https://tapi.bale.ai/bot{BOT_TOKEN}/getFile"

app = FastAPI(title="InstaMini API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # برای توسعه؛ در پروداکشن دقیق‌تر محدود کن
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def db_conn():
    # اتصال به PostgreSQL
    # check_same_thread=False در psycopg2 لازم نیست، اما اگر نیاز به کانکشن پولینگ باشد، باید از AioPg یا Connection Pooler استفاده شود.
    # فعلاً برای سادگی، اتصال مستقیم در هر درخواست (که ایمن‌تر از sqlite در FastAPI است)
    return psycopg2.connect(DATABASE_URL)

# -----------------------
# --- Utilities
# -----------------------
def row_to_post(r):
    # تطبیق با ساختار جدید: score و created_at
    return {
        "post_id": r[0],
        "user_id": r[1],
        "type": r[2],
        "photo": r[3],
        "video_id": r[4],
        "likes": r[5],
        # r[6] در اینجا COALESCE(candidate_score,0) است
        "score": r[6] if r[6] is not None else 0,
        # r[7] در اینجا تاریخ است (TIMESTAMPTZ)
        "created_at": r[7].isoformat() if hasattr(r[7], 'isoformat') else str(r[7])
    }

# -----------------------
# --- Read endpoints
# -----------------------
@app.get("/api/get_explore")
def get_explore(limit: int = 30, page: int = 1):
    conn = db_conn()
    c = conn.cursor()
    offset = (page - 1) * limit
    # 2. جایگزینی ؟ با %s
    # 5. اضافه کردن ایندکس پیشنهادی برای بهینه سازی (idx_posts_feed)
    c.execute("""
        SELECT post_id, user_id, type, photo, video_id, likes, COALESCE(candidate_score,0) as score, COALESCE(created_at, timestamp)
        FROM posts
        ORDER BY score DESC, likes DESC, created_at DESC
        LIMIT %s OFFSET %s
    """, (limit, offset))
    rows = c.fetchall()
    conn.close()
    return [row_to_post(r) for r in rows]

@app.get("/api/get_post/{post_id}")
def get_post(post_id: int):
    conn = db_conn()
    c = conn.cursor()
    # 2. جایگزینی ؟ با %s
    c.execute("""
        SELECT post_id, user_id, type, photo, video_id, likes, COALESCE(candidate_score,0) as score, COALESCE(created_at, timestamp)
        FROM posts WHERE post_id = %s
    """, (post_id,))
    r = c.fetchone()
    conn.close()
    if not r:
        raise HTTPException(status_code=404, detail="post not found")
    # fetch comments
    conn = db_conn()
    c = conn.cursor()
    # 2. جایگزینی ؟ با %s
    c.execute("SELECT comments.comment_id, comments.user_id, users.username, comments.text, comments.timestamp FROM comments LEFT JOIN users ON comments.user_id = users.user_id WHERE post_id = %s ORDER BY comment_id ASC", (post_id,))
    # تاریخ‌ها را برای JSON سازگاری آماده می‌کنیم
    comments = [{"comment_id": row[0], "user_id": row[1], "username": row[2], "text": row[3], "timestamp": row[4].isoformat() if hasattr(row[4], 'isoformat') else str(row[4])} for row in c.fetchall()]
    conn.close()
    post = row_to_post(r)
    post["comments"] = comments
    return post

@app.get("/api/get_user/{user_id}")
def get_user(user_id: int):
    conn = db_conn()
    c = conn.cursor()
    # 2. جایگزینی ؟ با %s
    c.execute("SELECT user_id, username, display_name, bio, profile_pic, followers, following FROM users WHERE user_id = %s", (user_id,))
    u = c.fetchone()
    conn.close()
    if not u:
        raise HTTPException(status_code=404, detail="user not found")
    user = {
        "user_id": u[0],
        "username": u[1],
        "display_name": u[2],
        "bio": u[3],
        "profile_pic": u[4],
        "followers": u[5],
        "following": u[6]
    }
    return user

@app.get("/api/get_feed/{user_id}")
def get_feed(user_id: int, limit: int = 30, page: int = 1):
    # posts from people the user follows
    conn = db_conn()
    c = conn.cursor()
    offset = (page-1)*limit
    # 2. جایگزینی ؟ با %s
    c.execute("""
        SELECT p.post_id, p.user_id, p.type, p.photo, p.video_id, p.likes, COALESCE(p.candidate_score,0) as score, COALESCE(p.created_at, p.timestamp)
        FROM posts p
        WHERE p.user_id IN (SELECT following_id FROM follows WHERE follower_id = %s)
        ORDER BY p.created_at DESC
        LIMIT %s OFFSET %s
    """, (user_id, limit, offset))
    rows = c.fetchall()
    conn.close()
    return [row_to_post(r) for r in rows]

# -----------------------
# --- Media proxy / info
# -----------------------
@app.get("/api/media/{post_id}")
def media_info(post_id: int):
    """
    Returns the 'photo' or 'video_id' reference for a post.
    If photo is a local path and file exists, returns {"url": "/media_files/<filename>"}.
    Otherwise returns {"file_id": "<value>"} so front-end can handle it later.
    """
    conn = db_conn()
    c = conn.cursor()
    # 2. جایگزینی ؟ با %s
    c.execute("SELECT photo, video_id, type FROM posts WHERE post_id = %s", (post_id,))
    r = c.fetchone()
    conn.close()
    if not r:
        raise HTTPException(status_code=404, detail="post not found")
    photo, video_id, ptype = r
    
    # تشخیص فایل محلی از این مرحله حذف می‌شود چون محیط اجرای ما فایل سیستمی ندارد
    # اگرچه کد اصلی شما `os.path.isfile(photo)` داشت، در محیط جدید بدون دسترسی به فایل سیستم آن را نادیده می‌گیریم یا فرض می‌کنیم همیشه file_id برمی‌گردد.
    # اگرچه برای حفظ منطق اصلی:
    # if photo and os.path.isfile(photo):
    #     filename = os.path.basename(photo)
    #     return {"url": f"/media_files/{filename}"}

    # 5. اکنون فرض می‌کنیم همه چیز از طریق file_id/video_id مدیریت می‌شود
    return {"file_id": photo, "video_id": video_id, "type": ptype}

# -----------------------
# --- Actions: like / comment / save
# -----------------------
class LikeModel(BaseModel):
    user_id: int
    post_id: int

class CommentModel(BaseModel):
    user_id: int
    post_id: int
    text: str

@app.post("/api/like")
def like_post(body: LikeModel):
    conn = db_conn()
    c = conn.cursor()
    try:
        # toggle like
        # 2. جایگزینی ؟ با %s
        c.execute("SELECT 1 FROM likes WHERE user_id = %s AND post_id = %s", (body.user_id, body.post_id))
        exist = c.fetchone()
        
        if exist:
            # 2. جایگزینی ؟ با %s
            c.execute("DELETE FROM likes WHERE user_id = %s AND post_id = %s", (body.user_id, body.post_id))
            # 2. جایگزینی ؟ با %s
            c.execute("UPDATE posts SET likes = likes - 1 WHERE post_id = %s", (body.post_id,))
            conn.commit()
            return {"status": "unliked"}
        else:
            # 3. جایگزینی INSERT OR IGNORE با ON CONFLICT DO NOTHING
            c.execute("""
                INSERT INTO likes (user_id, post_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
            """, (body.user_id, body.post_id))
            # 2. جایگزینی ؟ با %s
            c.execute("UPDATE posts SET likes = likes + 1 WHERE post_id = %s", (body.post_id,))
            conn.commit()
            return {"status": "liked"}
    finally:
        conn.close()


@app.post("/api/comment")
def comment_post(body: CommentModel):
    conn = db_conn()
    c = conn.cursor()
    # 2. جایگزینی ؟ با %s
    c.execute("INSERT INTO comments (post_id, user_id, text) VALUES (%s, %s, %s)", (body.post_id, body.user_id, body.text))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/api/save")
def save_post(body: LikeModel):
    conn = db_conn()
    c = conn.cursor()
    try:
        # 2. جایگزینی ؟ با %s
        c.execute("SELECT 1 FROM saved_posts WHERE user_id = %s AND post_id = %s", (body.user_id, body.post_id))
        if c.fetchone():
            # 2. جایگزینی ؟ با %s
            c.execute("DELETE FROM saved_posts WHERE user_id = %s AND post_id = %s", (body.user_id, body.post_id))
            conn.commit()
            return {"status": "unsaved"}
        else:
            # 3. جایگزینی INSERT OR IGNORE با ON CONFLICT DO NOTHING
            c.execute("""
                INSERT INTO saved_posts (user_id, post_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
            """, (body.user_id, body.post_id))
            conn.commit()
            return {"status": "saved"}
    finally:
        conn.close()


# -----------------------
# --- simple search (by hashtag or caption)
# -----------------------
@app.get("/api/search")
def search(q: str, limit: int = 30):
    conn = db_conn()
    c = conn.cursor()
    pattern = f"%{q}%"
    # 2. جایگزینی ؟ با %s
    # توجه: LIKE در Postgres به صورت case-insensitive نیست مگر با ILIKE. برای حفظ رفتار شبیه SQLite که case-insensitive بود، از ILIKE استفاده می‌شود.
    c.execute("""
        SELECT post_id, user_id, type, photo, video_id, likes, COALESCE(candidate_score,0) as score, COALESCE(created_at, timestamp)
        FROM posts
        WHERE caption ILIKE %s
        ORDER BY score DESC, created_at DESC
        LIMIT %s
    """, (pattern, limit))
    rows = c.fetchall()
    conn.close()
    return [row_to_post(r) for r in rows]

# -----------------------
# --- Profile endpoints
# -----------------------
@app.get("/api/get_user_posts/{user_id}")
def get_user_posts(user_id: int, limit: int = 30, page: int = 1):
    conn = db_conn()
    c = conn.cursor()
    offset = (page - 1) * limit
    # 2. جایگزینی ؟ با %s
    c.execute("""
        SELECT post_id, user_id, type, photo, video_id, likes, COALESCE(candidate_score,0) as score, COALESCE(created_at, timestamp)
        FROM posts
        WHERE user_id = %s
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
    """, (user_id, limit, offset))
    rows = c.fetchall()
    conn.close()
    return [row_to_post(r) for r in rows]

@app.get("/api/is_following")
def is_following_endpoint(viewer: int, target: int):
    conn = db_conn()
    c = conn.cursor()
    # 2. جایگزینی ؟ با %s
    c.execute("SELECT 1 FROM follows WHERE follower_id = %s AND following_id = %s", (viewer, target))
    res = c.fetchone()
    conn.close()
    return {"is_following": bool(res)}

class FollowToggleModel(BaseModel):
    follower_id: int
    target_id: int

@app.post("/api/follow_toggle")
def follow_toggle(body: FollowToggleModel):
    conn = db_conn()
    c = conn.cursor()
    follower = body.follower_id
    target = body.target_id
    if follower == target:
        conn.close()
        raise HTTPException(status_code=400, detail="cannot follow yourself")
    
    try:
        # check existing
        # 2. جایگزینی ؟ با %s
        c.execute("SELECT 1 FROM follows WHERE follower_id = %s AND following_id = %s", (follower, target))
        exists = c.fetchone()
        
        if exists:
            # unfollow
            # 2. جایگزینی ؟ با %s
            c.execute("DELETE FROM follows WHERE follower_id = %s AND following_id = %s", (follower, target))
            # decrement counters safely
            # 2. جایگزینی ؟ با %s
            c.execute("UPDATE users SET followers = CASE WHEN followers>0 THEN followers-1 ELSE 0 END WHERE user_id = %s", (target,))
            c.execute("UPDATE users SET following = CASE WHEN following>0 THEN following-1 ELSE 0 END WHERE user_id = %s", (follower,))
            conn.commit()
            
            # new counts
            c.execute("SELECT followers, following FROM users WHERE user_id = %s", (target,))
            tgt_counts = c.fetchone()
            c.execute("SELECT followers, following FROM users WHERE user_id = %s", (follower,))
            fl_counts = c.fetchone()
            return {"status": "unfollowed", "target_followers": tgt_counts[0] if tgt_counts else 0, "follower_following": fl_counts[1] if fl_counts else 0}
        else:
            # follow: insert
            now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            # 3. جایگزینی INSERT OR IGNORE با ON CONFLICT DO NOTHING و افزودن created_at
            c.execute("""
                INSERT INTO follows (follower_id, following_id, created_at) 
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (follower, target, now))
            
            # 2. جایگزینی ؟ با %s
            c.execute("UPDATE users SET followers = COALESCE(followers,0) + 1 WHERE user_id = %s", (target,))
            c.execute("UPDATE users SET following = COALESCE(following,0) + 1 WHERE user_id = %s", (follower,))
            conn.commit()
            
            c.execute("SELECT followers, following FROM users WHERE user_id = %s", (target,))
            tgt_counts = c.fetchone()
            c.execute("SELECT followers, following FROM users WHERE user_id = %s", (follower,))
            fl_counts = c.fetchone()
            return {"status": "followed", "target_followers": tgt_counts[0] if tgt_counts else 0, "follower_following": fl_counts[1] if fl_counts else 0}
    finally:
        conn.close()

@app.get("/api/notifications/{user_id}")
def get_notifications(user_id:int, limit:int=50):
    conn = db_conn()
    c = conn.cursor()
    # 2. جایگزینی ؟ با %s
    c.execute("""
      SELECT id, text, is_read, created_at
      FROM notifications
      WHERE user_id=%s
      ORDER BY created_at DESC
      LIMIT %s
    """,(user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [
      {
        "id":r[0],
        "text":r[1],
        "is_read":r[2],
        "created_at":r[3].isoformat() if hasattr(r[3], 'isoformat') else str(r[3])
      } for r in rows
    ]

# -----------------------
# --- Media proxy / info (بدون تغییرات اصلی)
# -----------------------
@app.get("/api/media_proxy")
def media_proxy(file_id: str):
    """
    گرفتن فایل واقعی از بله و ارسال مستقیم به miniapp
    """
    # ساخت URL صحیح
    url = f"{BASE_FILE_URL}?file_id={file_id}"

    r = requests.get(url)
    if r.status_code != 200:
        return RedirectResponse("https://via.placeholder.com/600x600?text=Media")

    data = r.json()
    if not data.get("ok"):
        return RedirectResponse("https://via.placeholder.com/600x600?text=Media")

    # file_path واقعی
    file_path = data["result"]["file_path"]

    # لینک دانلود:
    download_url = f"https://tapi.bale.ai/file/bot{BOT_TOKEN}/{file_path}"

    # دانلود مستقیم فایل
    r2 = requests.get(download_url, stream=True)
    if r2.status_code != 200:
        return RedirectResponse("https://via.placeholder.com/600x600?text=Media")

    content_type = r2.headers.get("Content-Type", "image/jpeg")
    return StreamingResponse(r2.raw, media_type=content_type)
