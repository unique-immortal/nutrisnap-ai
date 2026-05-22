import os
import uuid
import json
import sqlite3
import datetime
import re
import hashlib
import jwt
import functools
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from google import genai
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import PIL.Image

# 加载 .env 文件（仅本地开发使用，Cloud Run 通过环境变量注入）
load_dotenv()

app = Flask(__name__)
CORS(app)

# JWT 密钥（优先从环境变量读取）
JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY', 'nutrisnap-jwt-secret-change-in-production')
JWT_EXPIRATION_HOURS = int(os.environ.get('JWT_EXPIRATION_HOURS', '72'))

# Flask-Limiter 速率限制
app.config['RATELIMIT_STORAGE_URI'] = 'memory://'
app.config['RATELIMIT_DEFAULT'] = '60 per minute'
limiter = Limiter(key_func=get_remote_address, app=app)

# ==========================================
# 1. 基础配置
# ==========================================
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

import io
import base64

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY')

if not GEMINI_API_KEY and not OPENROUTER_API_KEY:
    raise ValueError("GEMINI_API_KEY 或 OPENROUTER_API_KEY 环境变量未设置！请在本地 .env 中配置后重启应用。")

client = None
if GEMINI_API_KEY:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"初始化 Google GenAI client 失败: {e}")

def call_llm(prompt_text, image=None, audio=None, mime_type=None, history=None, system_instruction=None, temperature=0.7):
    """
    Unified interface to call either OpenRouter API (if OPENROUTER_API_KEY is configured)
    or fall back to official Google Gemini API (using client.models.generate_content).
    """
    if OPENROUTER_API_KEY:
        # ----------------------------------------------------
        # OpenRouter API Path
        # ----------------------------------------------------
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://nutrisnap.ai",
            "X-Title": "NutriSnap AI"
        }
        
        # Build messages payload
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
            
        if history:
            for h in history:
                role = 'user' if h.get('role') == 'user' else 'assistant'
                content = h.get('content', '')
                # 如果 content 是列表（多模态内容），只提取 text 部分，
                # 避免 image_url 类型被发送到不支持多模态的模型
                if isinstance(content, list):
                    text_parts = [item.get('text', '') for item in content if isinstance(item, dict) and item.get('type') == 'text']
                    content = ' '.join(text_parts) if text_parts else ''
                messages.append({"role": role, "content": content})
                
        user_content = []
        if prompt_text:
            user_content.append({"type": "text", "text": prompt_text})
            
        if image:
            # Encode PIL Image to base64
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG")
            img_b64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{img_b64}"
                }
            })
            
        if audio:
            # Encode audio bytes to base64
            audio_b64 = base64.b64encode(audio).decode('utf-8')
            mtype = mime_type or 'audio/webm'
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mtype};base64,{audio_b64}"
                }
            })
            
        if user_content:
            messages.append({"role": "user", "content": user_content})
            
        # Model candidates for OpenRouter
        models_to_try = [
            'google/gemini-2.5-flash',
            'google/gemini-2.5-flash-lite',
            'google/gemini-2.0-flash',
            'google/gemini-1.5-flash',
        ]
        
        last_error = None
        for model in models_to_try:
            payload = {
                "model": model,
                "messages": messages,
                "temperature": temperature
            }
            try:
                print(f"Calling OpenRouter model: {model}")
                res = requests.post(url, headers=headers, json=payload, timeout=45)
                res_json = res.json()
                if res.status_code == 200 and 'choices' in res_json:
                    reply = res_json['choices'][0]['message']['content']
                    print(f"Success with OpenRouter model: {model}")
                    return reply
                else:
                    error_msg = res_json.get('error', {}).get('message', res.text)
                    print(f"OpenRouter model {model} failed: {error_msg}")
                    last_error = Exception(f"OpenRouter error: {error_msg}")
            except Exception as e:
                print(f"OpenRouter network/request error with model {model}: {e}")
                last_error = e
                
        raise last_error or Exception("OpenRouter request failed.")
        
    else:
        # ----------------------------------------------------
        # Google SDK Path (Fallback)
        # ----------------------------------------------------
        if not client:
            raise ValueError("Google SDK Client 未初始化，且没有设置 OPENROUTER_API_KEY。")
            
        models_to_try = [
            'gemini-3.5-flash',
            'gemini-2.5-flash',
            'gemini-2.5-flash-lite',
            'gemini-2.0-flash',
            'gemini-3.1-flash-lite',
        ]
        
        # GenAI SDK contents format
        contents = []
        if history:
            for h in history:
                role = 'user' if h.get('role') == 'user' else 'model'
                contents.append({
                    'role': role,
                    'parts': [{'text': h.get('content', '')}]
                })
                
        parts = []
        if prompt_text:
            parts.append(prompt_text)
        if image:
            parts.append(image)
        if audio:
            from google.genai import types
            parts.append(
                types.Part.from_bytes(
                    data=audio,
                    mime_type=mime_type or 'audio/webm'
                )
            )
            
        if parts:
            if history:
                contents.append({
                    'role': 'user',
                    'parts': [{'text': p} if isinstance(p, str) else p for p in parts]
                })
            else:
                contents = parts
                
        config = {'temperature': temperature}
        if system_instruction:
            config['system_instruction'] = system_instruction
            
        last_error = None
        for model in models_to_try:
            try:
                print(f"Calling Google SDK model: {model}")
                response = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config
                )
                print(f"Success with Google SDK model: {model}")
                return response.text
            except Exception as api_err:
                last_error = api_err
                err_str = str(api_err)
                if '429' in err_str or 'RESOURCE_EXHAUSTED' in err_str or 'quota' in err_str.lower():
                    print(f"Google SDK model {model} quota exhausted, skipping...")
                    continue
                print(f"Google SDK model {model} failed: {api_err}")
                continue
                
        raise last_error or Exception("Google SDK request failed.")

# ==========================================
# 2. 数据库配置
# ==========================================
def init_db():
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_path TEXT,
            food_name TEXT,
            calories INTEGER,
            protein INTEGER,
            carbs INTEGER,
            fat INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Migrate: add session_id, portion, weight, username, client_id, updated_at columns (idempotent)
    for col, col_def in [
        ('session_id', 'TEXT'),
        ('portion', 'REAL DEFAULT 1.0'),
        ('weight', 'INTEGER DEFAULT 100'),
        ('username', 'TEXT'),
        ('client_id', 'TEXT'),
        ('updated_at', 'TEXT')
    ]:
        try:
            cursor.execute(f'ALTER TABLE meals ADD COLUMN {col} {col_def}')
        except sqlite3.OperationalError:
            pass  # column already exists

    # Migrate users: add BMR physical data columns
    for col, col_def in [
        ('gender', 'TEXT'),
        ('age', 'INTEGER'),
        ('height', 'REAL'),
        ('weight', 'REAL'),
        ('activity_level', 'TEXT')
    ]:
        try:
            cursor.execute(f'ALTER TABLE users ADD COLUMN {col} {col_def}')
        except sqlite3.OperationalError:
            pass  # column already exists

    # Create exercises table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS exercises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exercise_name TEXT,
            calories INTEGER,
            duration INTEGER,
            exercise_type TEXT,
            target_muscles TEXT,
            username TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Create daily_summaries table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_summaries (
            username TEXT,
            date TEXT, -- YYYY-MM-DD
            total_calories INTEGER DEFAULT 0,
            total_protein INTEGER DEFAULT 0,
            total_carbs INTEGER DEFAULT 0,
            total_fat INTEGER DEFAULT 0,
            total_burn_calories INTEGER DEFAULT 0,
            total_exercise_duration INTEGER DEFAULT 0,
            PRIMARY KEY (username, date)
        )
    ''')

    # Backfill old records without session_id or username
    cursor.execute("UPDATE meals SET session_id = 'legacy_' || id WHERE session_id IS NULL")
    cursor.execute("UPDATE meals SET username = 'anonymous' WHERE username IS NULL")
    cursor.execute("UPDATE meals SET client_id = 'server_' || id WHERE client_id IS NULL")
    cursor.execute("UPDATE meals SET updated_at = COALESCE(created_at, CURRENT_TIMESTAMP) WHERE updated_at IS NULL")

    # Create weight_logs table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS weight_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            weight REAL NOT NULL,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY (username) REFERENCES users(username)
        )
    ''')
    cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_meals_username_client_id
        ON meals(username, client_id)
    ''')
    conn.commit()
    conn.close()

init_db()

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api/'):
        return jsonify({"error": "API endpoint not found", "path": request.path}), 404
    return render_template('index.html')

def get_db_connection():
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    return conn

# ==========================================
# 3. 页面路由
# ==========================================

@app.route('/api/health')
def health():
    return jsonify({
        "version": "v5.2.0",
        "architecture": "local-first + throttled-meal-sync + server-daily-summaries + OpenRouter",
        "models": [
            'gemini-3.5-flash',
            'gemini-2.5-flash',
            'gemini-2.5-flash-lite',
            'gemini-2.0-flash',
            'gemini-3.1-flash-lite',
        ]
    })

def parse_ai_multi_result(raw_text):
    """解析 AI JSON 输出，返回食物列表"""
    # Clean markdown wrappers
    text = re.sub(r'^```(?:json)?\s*', '', raw_text.strip())
    text = re.sub(r'\s*```$', '', text.strip())
    try:
        items = json.loads(text)
        if isinstance(items, dict) and 'error' in items:
            return None
        if not isinstance(items, list):
            items = [items]
    except json.JSONDecodeError:
        # Fallback: old key-value format (single food)
        result = {}
        for line in text.strip().split('\n'):
            if ':' in line:
                k, v = line.split(':', 1)
            elif '：' in line:
                k, v = line.split('：', 1)
            else:
                continue
            k = k.strip()
            num = re.search(r'\d+', v)
            n = int(num.group()) if num else 0
            if "食物名称" in k:
                result['food_name'] = v.strip()
            elif "热量" in k:
                result['calories'] = n
            elif "蛋白" in k:
                result['protein'] = n
            elif "碳水" in k:
                result['carbs'] = n
            elif "脂肪" in k:
                result['fat'] = n
        if not result.get('food_name'):
            return None
        items = [result]
    # Normalize
    normalized = []
    for item in items:
        normalized.append({
            'food_name': item.get('food_name', '未知食物'),
            'calories': item.get('calories', 0),
            'protein': item.get('protein', 0),
            'carbs': item.get('carbs', 0),
            'fat': item.get('fat', 0),
            'weight': item.get('weight', 100),
        })
    return normalized


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ==========================================
# 4. 用户认证 API
# ==========================================

def hash_password(password):
    return generate_password_hash(password, method='pbkdf2:sha256')

def verify_password(password, password_hash):
    return check_password_hash(password_hash, password)

def create_token(username):
    """生成 JWT token"""
    payload = {
        'username': username,
        'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=JWT_EXPIRATION_HOURS),
        'iat': datetime.datetime.utcnow()
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm='HS256')

def token_required(f):
    """JWT 验证装饰器"""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]
        
        if not token:
            return jsonify({"error": "未提供认证令牌"}), 401
        
        try:
            data = jwt.decode(token, JWT_SECRET_KEY, algorithms=['HS256'])
            request.current_user = data['username']
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "令牌已过期，请重新登录"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "无效的认证令牌"}), 401
        
        return f(*args, **kwargs)
    return decorated

def get_current_username():
    """获取当前请求的用户名"""
    # 优先从 JWT 获取
    if hasattr(request, 'current_user'):
        return request.current_user
    # 兼容旧的 X-User-Id header
    return request.headers.get('X-User-Id') or 'anonymous'

def validate_profile_payload(data):
    try:
        gender = data.get('gender')
        age = int(data.get('age'))
        height = float(data.get('height'))
        weight = float(data.get('weight'))
        activity_level = data.get('activity_level') or 'sedentary'
    except (TypeError, ValueError):
        return None, "请填写完整且有效的身体参数"

    if gender not in ('male', 'female'):
        return None, "请选择有效的性别"
    if not 15 <= age <= 120:
        return None, "年龄需在 15-120 岁之间"
    if not 80 <= height <= 250:
        return None, "身高需在 80-250 cm 之间"
    if not 20 <= weight <= 200:
        return None, "体重需在 20-200 kg 之间"
    if activity_level not in ('sedentary', 'lightly_active', 'moderately_active', 'very_active', 'highly_active'):
        return None, "请选择有效的活动量级别"

    return {
        'gender': gender,
        'age': age,
        'height': height,
        'weight': weight,
        'activity_level': activity_level
    }, None

def clamp_number(value, default=0, min_value=0, max_value=10000, integer=True):
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    if number < min_value:
        number = min_value
    if number > max_value:
        number = max_value
    return int(round(number)) if integer else round(number, 3)

def clean_text(value, default='', max_len=160):
    text = str(value or default).strip()
    return text[:max_len]

def normalize_client_meal(data):
    client_id = clean_text(data.get('client_id') or data.get('id'), max_len=96)
    if not client_id:
        return None
    created_at = clean_text(data.get('created_at'), max_len=40) or datetime.datetime.utcnow().isoformat()
    updated_at = clean_text(data.get('updated_at'), max_len=40) or created_at
    return {
        'client_id': client_id,
        'session_id': clean_text(data.get('session_id') or f'solo_{client_id}', max_len=96),
        'image_path': '',  # Keep server storage light: meal sync does not upload or persist photos.
        'food_name': clean_text(data.get('food_name'), default='未知食物', max_len=120),
        'calories': clamp_number(data.get('calories'), default=0, min_value=0, max_value=5000),
        'protein': clamp_number(data.get('protein'), default=0, min_value=0, max_value=300),
        'carbs': clamp_number(data.get('carbs'), default=0, min_value=0, max_value=500),
        'fat': clamp_number(data.get('fat'), default=0, min_value=0, max_value=300),
        'weight': clamp_number(data.get('weight'), default=100, min_value=1, max_value=2000),
        'portion': clamp_number(data.get('portion'), default=1.0, min_value=0.1, max_value=10, integer=False),
        'created_at': created_at,
        'updated_at': updated_at
    }

def upsert_daily_summary(cursor, username, summary):
    date_str = clean_text(summary.get('date'), max_len=10)
    if not date_str:
        return False
    cursor.execute('''
        INSERT OR REPLACE INTO daily_summaries (
            username, date, total_calories, total_protein, total_carbs, total_fat,
            total_burn_calories, total_exercise_duration
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        username,
        date_str,
        clamp_number(summary.get('total_calories'), 0, 0, 100000),
        clamp_number(summary.get('total_protein'), 0, 0, 10000),
        clamp_number(summary.get('total_carbs'), 0, 0, 10000),
        clamp_number(summary.get('total_fat'), 0, 0, 10000),
        clamp_number(summary.get('total_burn_calories'), 0, 0, 100000),
        clamp_number(summary.get('total_exercise_duration'), 0, 0, 10000)
    ))
    return True

@app.route('/api/register', methods=['POST'])
def register():
    data = request.get_json() or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400
    if len(username) < 3 or len(password) < 4:
        return jsonify({"error": "用户名至少3位，密码至少4位"}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT password_hash FROM users WHERE username = ?", (username,))
        existing = cursor.fetchone()
        pw_hash = hash_password(password)

        if existing:
            if existing['password_hash']:
                return jsonify({"error": "用户名已存在，请换一个用户名或直接登录"}), 409
            # Profiles can create placeholder rows before a real registration.
            cursor.execute("UPDATE users SET password_hash = ? WHERE username = ?", (pw_hash, username))
        else:
            cursor.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", (username, pw_hash))
        conn.commit()
        return jsonify({"success": True, "message": "注册成功", "username": username})
    except sqlite3.IntegrityError:
        return jsonify({"error": "用户名已存在，请换一个用户名或直接登录"}), 409
    except Exception as e:
        return jsonify({"error": f"注册失败: {str(e)}"}), 500
    finally:
        conn.close()

@app.route('/api/login', methods=['POST'])
@limiter.limit("5 per minute")
def login():
    data = request.get_json() or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT password_hash FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        if row and row['password_hash']:
            # 兼容旧 SHA256 密码和新的 werkzeug 密码
            stored_hash = row['password_hash']
            if verify_password(password, stored_hash):
                pass
            elif stored_hash == hashlib.sha256(password.encode('utf-8')).hexdigest():
                # 旧密码格式，自动升级为 werkzeug hash
                new_hash = hash_password(password)
                cursor.execute("UPDATE users SET password_hash = ? WHERE username = ?", (new_hash, username))
                conn.commit()
            else:
                return jsonify({"error": "用户名或密码错误"}), 400
            
            token = create_token(username)
            return jsonify({"success": True, "username": username, "token": token})
        else:
            return jsonify({"error": "用户名或密码错误"}), 400
    except Exception as e:
        return jsonify({"error": f"登录失败: {str(e)}"}), 500
    finally:
        conn.close()

# ==========================================
# 5. 核心 API
# ==========================================

@app.route('/api/analyze', methods=['POST'])
def analyze_food():
    if 'image' not in request.files:
        return jsonify({"error": "没有找到图片"}), 400

    file = request.files['image']
    if file.filename == '':
        return jsonify({"error": "图片名为空"}), 400

    is_app = request.form.get('is_app') == 'true' or request.args.get('is_app') == 'true'
    username = request.headers.get('X-User-Id') or 'anonymous'

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    try:
        img = PIL.Image.open(filepath)

        prompt = """分析这张图片中的所有食物。对每种食物分别返回以下信息，用 JSON 数组格式：
[
  {
    "food_name": "食物名称",
    "calories": 热量数字(大卡),
    "protein": 蛋白质数字(克),
    "carbs": 碳水数字(克),
    "fat": 脂肪数字(克),
    "weight": 估计重量数字(克)
  }
]
If there are no food items in the image, return:
{"error": "未检测到食物"}
Strictly output JSON only, do not add any explanation or markdown formatting."""

        result_text = call_llm(prompt_text=prompt, image=img)

        foods = parse_ai_multi_result(result_text)
        if foods is None:
            return jsonify({"error": "AI未检测到食物，请重新拍摄"}), 400

        # Return food data; frontend stores locally via MealStorage (localStorage).
        # Daily aggregated summaries are synced to server via /api/daily-summaries.
        session_id = str(uuid.uuid4())
        saved = []
        for i, food in enumerate(foods):
            food['id'] = int(datetime.datetime.now().timestamp() * 1000) + i
            food['portion'] = 1.0
            saved.append(food)

        return jsonify({
            "success": True,
            "session_id": session_id,
            "foods": saved,
            "image_url": ""
        })

    except Exception as e:
        print(f"Error calling AI API: {e}")
        return jsonify({"error": f"AI识别失败，详细错误: {str(e)}"}), 500
    finally:
        # Clean up temp file immediately — meals are stored locally, not on server
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception as delete_err:
                print(f"Error removing temp image file {filepath}: {delete_err}")


def parse_voice_input_result(raw_text):
    """解析 AI 分类 JSON，返回提取到的食物和运动列表"""
    text = re.sub(r'^```(?:json)?\s*', '', raw_text.strip())
    text = re.sub(r'\s*```$', '', text.strip())
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            return None
        
        # 规范化食物列表
        foods = data.get('foods', [])
        normalized_foods = []
        for f in foods:
            if isinstance(f, dict):
                normalized_foods.append({
                    'food_name': f.get('food_name', '未知食物'),
                    'calories': int(f.get('calories', 0)),
                    'protein': int(f.get('protein', 0)),
                    'carbs': int(f.get('carbs', 0)),
                    'fat': int(f.get('fat', 0)),
                    'weight': int(f.get('weight', 100))
                })
                
        # 规范化运动列表
        exercises = data.get('exercises', [])
        normalized_exercises = []
        for ex in exercises:
            if isinstance(ex, dict):
                muscles = ex.get('target_muscles', [])
                if isinstance(muscles, list):
                    muscles_str = ','.join([str(m).strip() for m in muscles])
                else:
                    muscles_str = str(muscles).strip()
                normalized_exercises.append({
                    'exercise_name': ex.get('exercise_name', '未知运动'),
                    'calories': int(ex.get('calories', 0)),
                    'duration': int(ex.get('duration', 0)),
                    'exercise_type': ex.get('exercise_type', 'aerobic'),
                    'target_muscles': muscles_str
                })
                
        return {
            'type': data.get('type', 'food'),
            'foods': normalized_foods,
            'exercises': normalized_exercises
        }
    except Exception as e:
        print(f"Error parsing voice JSON: {e}")
        # Fallback to empty structure
        return {
            'type': 'food',
            'foods': [],
            'exercises': []
        }

@app.route('/api/voice-input', methods=['POST'])
def voice_input():
    """语音输入：用 Gemini 自动分辨运动与食物并解析提取"""
    data = request.json or {}
    text = data.get('text', '')
    is_app = data.get('is_app') == True
    username = request.headers.get('X-User-Id') or 'anonymous'

    if not text:
        return jsonify({"error": "语音文本为空"}), 400

    prompt = f"""分析用户通过语音或文本输入的内容，自动识别并提取其中的食物摄入信息与运动消耗信息。
用户输入："{text}"

请返回符合以下格式的 JSON 对象：
{{
  "type": "food" | "exercise" | "mixed", // 如果仅包含饮食返回 "food"，仅包含运动返回 "exercise"，两者皆有返回 "mixed"
  "foods": [ // 如果没有饮食信息，返回空数组 []
    {{
      "food_name": "食物名称",
      "calories": 估计热量(大卡),
      "protein": 估计蛋白质(克),
      "carbs": 估计碳水(克),
      "fat": 估计脂肪(克),
      "weight": 估计重量(克)
    }}
  ],
  "exercises": [ // 如果没有运动信息，返回空数组 []
    {{
      "exercise_name": "运动名称",
      "calories": 估计运动消耗热量(大卡。如果输入没有提及消耗，请根据标准 MET 和时长估算),
      "duration": 运动时长(分钟),
      "exercise_type": "strength" | "aerobic", // 力量训练/抗阻训练返回 "strength"，有氧运动返回 "aerobic"
      "target_muscles": ["胸肌", "三头肌"] // 如果是力量训练，列出此次训练涉及的目标肌群（例如：胸部、背部、肩部、腿部、肱二头肌、肱三头肌、核心等），如果是纯有氧运动，返回空数组 []
    }}
  ]
}}
Strictly output JSON only, do not add any explanation or markdown formatting."""

    try:
        result_text = call_llm(prompt_text=prompt)

        parsed = parse_voice_input_result(result_text)
        if parsed is None or (not parsed['foods'] and not parsed['exercises']):
            return jsonify({"error": "未能提取出任何有效的食物或运动信息，请重新描述"}), 400

        session_id = str(uuid.uuid4())
        saved_foods = []
        saved_exercises = []

        # Generate temporary IDs for the client to store locally.
        for i, food in enumerate(parsed['foods']):
            food['id'] = int(datetime.datetime.now().timestamp() * 1000) + i
            food['portion'] = 1.0
            saved_foods.append(food)
            
        for i, ex in enumerate(parsed['exercises']):
            ex['id'] = int(datetime.datetime.now().timestamp() * 1000) + 100 + i
            saved_exercises.append(ex)

        return jsonify({
            "success": True,
            "type": parsed['type'],
            "session_id": session_id,
            "foods": saved_foods,
            "exercises": saved_exercises,
            "image_url": ""
        })

    except Exception as e:
        print(f"Voice input error: {e}")
        return jsonify({"error": f"语音识别失败: {str(e)}"}), 500


@app.route('/api/speech-to-text', methods=['POST'])
def speech_to_text():
    """将上传的语音文件通过 Gemini 2.5/2.0 转写为文字"""
    if 'audio' not in request.files:
        return jsonify({"error": "没有找到语音文件"}), 400

    audio_file = request.files['audio']
    if audio_file.filename == '':
        return jsonify({"error": "语音文件名为空"}), 400

    mime_type = audio_file.content_type
    # If mime_type is not provided, estimate it
    if not mime_type or mime_type == 'application/octet-stream':
        if audio_file.filename.endswith('.mp4'):
            mime_type = 'audio/mp4'
        elif audio_file.filename.endswith('.webm'):
            mime_type = 'audio/webm'
        else:
            mime_type = 'audio/webm' # Default fallback

    try:
        audio_bytes = audio_file.read()
        if len(audio_bytes) < 100:
            return jsonify({"error": "音频文件过小或无效"}), 400

        prompt = "请将这段录音直接转写成中文文本，不要包含任何额外的引导语、标点纠正解释，仅输出转写文本本身。如果是静音或没有说话，请直接返回空字符串。"
        result_text = call_llm(prompt_text=prompt, audio=audio_bytes, mime_type=mime_type)

        transcription = result_text.strip()
        # Clean quotes or markdown from the transcription
        transcription = re.sub(r'^["\'`]|["\'`]$', '', transcription).strip()
        
        print(f"Speech transcription result: {transcription}")
        return jsonify({"text": transcription})

    except Exception as e:
        print(f"Speech to text API error: {e}")
        return jsonify({"error": f"语音听写失败: {str(e)}"}), 500


@app.route('/api/meals', methods=['GET'])
def get_meals():
    username = request.headers.get('X-User-Id') or 'anonymous'
    conn = get_db_connection()
    meals = conn.execute('''
        SELECT id, client_id, image_path, food_name,
               CAST(calories * COALESCE(portion, 1.0) AS INTEGER) as calories,
               CAST(protein * COALESCE(portion, 1.0) AS INTEGER) as protein,
               CAST(carbs * COALESCE(portion, 1.0) AS INTEGER) as carbs,
               CAST(fat * COALESCE(portion, 1.0) AS INTEGER) as fat,
               weight, created_at, updated_at, session_id, COALESCE(portion, 1.0) as portion
        FROM meals
        WHERE username = ?
        ORDER BY created_at DESC LIMIT 50
    ''', (username,)).fetchall()
    conn.close()
    return jsonify({"data": [dict(m) for m in meals]})


@app.route('/api/meals/sync', methods=['GET', 'POST'])
def sync_meals():
    username = request.headers.get('X-User-Id') or 'anonymous'
    if username in ('anonymous', 'guest', 'local_user'):
        return jsonify({"error": "请登录后再同步饮食记录"}), 401

    if request.method == 'GET':
        limit = min(max(request.args.get('limit', default=500, type=int), 1), 1000)
        conn = get_db_connection()
        rows = conn.execute('''
            SELECT id as server_id, client_id, session_id, food_name, calories, protein, carbs, fat,
                   weight, COALESCE(portion, 1.0) as portion, created_at, updated_at
            FROM meals
            WHERE username = ?
            ORDER BY datetime(created_at) DESC
            LIMIT ?
        ''', (username, limit)).fetchall()
        conn.close()
        return jsonify({"success": True, "data": [dict(row) for row in rows]})

    data = request.get_json() or {}
    incoming_meals = data.get('meals') or []
    deleted_client_ids = data.get('deleted_client_ids') or []
    summaries = data.get('summaries') or []

    if not isinstance(incoming_meals, list) or not isinstance(deleted_client_ids, list) or not isinstance(summaries, list):
        return jsonify({"error": "同步数据格式不正确"}), 400
    if len(incoming_meals) > 250 or len(deleted_client_ids) > 500 or len(summaries) > 31:
        return jsonify({"error": "单次同步数据过多，请稍后自动分批同步"}), 413

    normalized_meals = []
    seen_client_ids = set()
    for item in incoming_meals:
        if not isinstance(item, dict):
            continue
        meal = normalize_client_meal(item)
        if not meal or meal['client_id'] in seen_client_ids:
            continue
        normalized_meals.append(meal)
        seen_client_ids.add(meal['client_id'])

    deleted_client_ids = [
        clean_text(client_id, max_len=96)
        for client_id in deleted_client_ids
        if clean_text(client_id, max_len=96)
    ][:500]

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        deleted_count = 0
        if deleted_client_ids:
            placeholders = ','.join(['?'] * len(deleted_client_ids))
            cursor.execute(
                f'DELETE FROM meals WHERE username = ? AND client_id IN ({placeholders})',
                [username] + deleted_client_ids
            )
            deleted_count = cursor.rowcount

        synced_count = 0
        for meal in normalized_meals:
            cursor.execute('''
                INSERT INTO meals (
                    username, client_id, image_path, food_name, calories, protein, carbs, fat,
                    weight, portion, created_at, updated_at, session_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username, client_id) DO UPDATE SET
                    image_path = excluded.image_path,
                    food_name = excluded.food_name,
                    calories = excluded.calories,
                    protein = excluded.protein,
                    carbs = excluded.carbs,
                    fat = excluded.fat,
                    weight = excluded.weight,
                    portion = excluded.portion,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    session_id = excluded.session_id
            ''', (
                username,
                meal['client_id'],
                meal['image_path'],
                meal['food_name'],
                meal['calories'],
                meal['protein'],
                meal['carbs'],
                meal['fat'],
                meal['weight'],
                meal['portion'],
                meal['created_at'],
                meal['updated_at'],
                meal['session_id']
            ))
            synced_count += 1

        summary_count = 0
        for summary in summaries:
            if isinstance(summary, dict) and upsert_daily_summary(cursor, username, summary):
                summary_count += 1

        conn.commit()
        return jsonify({
            "success": True,
            "synced": synced_count,
            "deleted": deleted_count,
            "summaries": summary_count,
            "server_time": datetime.datetime.utcnow().isoformat()
        })
    except Exception as e:
        conn.rollback()
        print(f"Meal sync error: {e}")
        return jsonify({"error": f"饮食记录同步失败: {str(e)}"}), 500
    finally:
        conn.close()


@app.route('/api/meals/<int:meal_id>', methods=['PATCH', 'DELETE'])
def meal_action(meal_id):
    username = request.headers.get('X-User-Id') or 'anonymous'
    if request.method == 'DELETE':
        conn = get_db_connection()
        conn.execute('DELETE FROM meals WHERE id = ? AND username = ?', (meal_id, username))
        conn.commit()
        conn.close()
        return jsonify({"success": True})

    elif request.method == 'PATCH':
        data = request.get_json() or {}
        portion = data.get('portion')
        if portion is None:
            return jsonify({"error": "缺少 portion 参数"}), 400
        conn = get_db_connection()
        conn.execute('UPDATE meals SET portion = ? WHERE id = ? AND username = ?', (float(portion), meal_id, username))
        conn.commit()
        row = conn.execute('''
            SELECT id, food_name,
                   CAST(calories * COALESCE(portion, 1.0) AS INTEGER) as calories,
                   protein, carbs, fat, portion, session_id
            FROM meals WHERE id = ? AND username = ?
        ''', (meal_id, username)).fetchone()
        conn.close()
        if row is None:
            return jsonify({"error": "未找到对应的记录"}), 404
        return jsonify({"success": True, "data": dict(row)})


@app.route('/api/meals/session/<session_id>', methods=['DELETE'])
def delete_session(session_id):
    username = request.headers.get('X-User-Id') or 'anonymous'
    conn = get_db_connection()
    conn.execute('DELETE FROM meals WHERE session_id = ? AND username = ?', (session_id, username))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route('/api/report/weekly', methods=['GET'])
@app.route('/api/weekly-report', methods=['GET'])
def weekly_report():
    username = request.headers.get('X-User-Id') or 'anonymous'
    conn = get_db_connection()
    
    # Get daily summaries
    rows = conn.execute('''
        SELECT
            date as day,
            total_calories,
            total_protein,
            total_carbs,
            total_fat,
            total_burn_calories,
            total_exercise_duration
        FROM daily_summaries
        WHERE date >= date('now', '-7 days') AND username = ?
        ORDER BY date ASC
    ''', (username,)).fetchall()
    
    # Get weight data for the same period
    weight_rows = conn.execute('''
        SELECT date(recorded_at) as day, weight
        FROM weight_logs
        WHERE username = ? AND date(recorded_at) >= date('now', '-7 days')
        GROUP BY date(recorded_at)
        ORDER BY day ASC
    ''', (username,)).fetchall()
    conn.close()

    # Fill missing dates
    data_map = {row['day']: dict(row) for row in rows}
    weight_map = {row['day']: row['weight'] for row in weight_rows}
    
    report = []
    today = datetime.date.today()
    for i in range(6, -1, -1):
        d = (today - datetime.timedelta(days=i)).isoformat()
        if d in data_map:
            item = data_map[d]
            report.append({
                'day': d,
                'total_calories': item.get('total_calories') or 0,
                'total_protein': item.get('total_protein') or 0,
                'total_carbs': item.get('total_carbs') or 0,
                'total_fat': item.get('total_fat') or 0,
                'total_burn_calories': item.get('total_burn_calories') or 0,
                'total_exercise_duration': item.get('total_exercise_duration') or 0,
                'weight': weight_map.get(d)
            })
        else:
            report.append({
                'day': d,
                'total_calories': 0,
                'total_protein': 0,
                'total_carbs': 0,
                'total_fat': 0,
                'total_burn_calories': 0,
                'total_exercise_duration': 0,
                'weight': weight_map.get(d)
            })
    return jsonify({"data": report})


@app.route('/api/coach/chat', methods=['POST'])
def coach_chat():
    username = request.headers.get('X-User-Id') or 'anonymous'
    data = request.get_json() or {}
    user_message = data.get('message', '')
    history = data.get('history', [])
    cal_target = data.get('calTarget', 2000)
    pro_target = data.get('proTarget', 120)
    meals_from_client = data.get('meals') # Optional local meals from app
    exercises_from_client = data.get('exercises') # Optional local exercises from app

    if not user_message and not history:
        return jsonify({"error": "消息内容为空"}), 400

    meals_summary = []
    total_cal = 0
    total_pro = 0
    total_carbs = 0
    total_fat = 0

    if meals_from_client is not None:
        for row in meals_from_client:
            portion = float(row.get('portion', 1.0))
            food_name = row.get('food_name', '未知食物')
            cal = int(row.get('calories', 0) * portion)
            pro = int(row.get('protein', 0) * portion)
            carbs = int(row.get('carbs', 0) * portion)
            fat = int(row.get('fat', 0) * portion)
            meals_summary.append(
                f"- {food_name}: {cal} kcal (蛋白质 {pro}g, 碳水 {carbs}g, 脂肪 {fat}g, 分量 {portion}x)"
            )
            total_cal += cal
            total_pro += pro
            total_carbs += carbs
            total_fat += fat
        meals_detail = "\n".join(meals_summary) if meals_summary else "无饮食记录"
        meals_context = f"用户今日饮食明细：\n{meals_detail}\n累计摄入：热量 {total_cal} kcal，蛋白质 {total_pro}g，碳水 {total_carbs}g，脂肪 {total_fat}g。"
    else:
        conn = get_db_connection()
        cursor = conn.cursor()
        today_str = datetime.date.today().isoformat()
        row = cursor.execute('''
            SELECT total_calories, total_protein, total_carbs, total_fat
            FROM daily_summaries
            WHERE date = ? AND username = ?
        ''', (today_str, username)).fetchone()
        conn.close()

        if row:
            total_cal = row['total_calories'] or 0
            total_pro = row['total_protein'] or 0
            total_carbs = row['total_carbs'] or 0
            total_fat = row['total_fat'] or 0
            meals_context = f"用户今日累计摄入：热量 {total_cal} kcal，蛋白质 {total_pro}g，碳水 {total_carbs}g，脂肪 {total_fat}g。"
        else:
            meals_context = "用户今日尚未记录任何饮食。"

    total_burn = 0
    total_duration = 0
    if exercises_from_client is not None:
        ex_summary = []
        for ex in exercises_from_client:
            ex_name = ex.get('exercise_name', '未知运动')
            cal = int(ex.get('calories', 0))
            dur = int(ex.get('duration', 0))
            ex_type = ex.get('exercise_type', 'aerobic')
            ex_summary.append(f"- {ex_name}: 消耗 {cal} kcal, 时长 {dur} 分钟 ({'有氧' if ex_type == 'aerobic' else '无氧'})")
            total_burn += cal
            total_duration += dur
        ex_detail = "\n".join(ex_summary) if ex_summary else "无运动记录"
        meals_context += f"\n\n用户今日运动明细：\n{ex_detail}\n累计消耗：{total_burn} kcal，运动时长 {total_duration} 分钟。"
    else:
        conn = get_db_connection()
        cursor = conn.cursor()
        today_str = datetime.date.today().isoformat()
        row = cursor.execute('''
            SELECT total_burn_calories, total_exercise_duration
            FROM daily_summaries
            WHERE date = ? AND username = ?
        ''', (today_str, username)).fetchone()
        conn.close()

        if row:
            total_burn = row['total_burn_calories'] or 0
            total_duration = row['total_exercise_duration'] or 0
            meals_context += f"\n\n累计消耗：{total_burn} kcal，运动时长 {total_duration} 分钟。"
        else:
            meals_context += "\n\n用户今日尚未记录任何运动。"

    system_instruction = f"""你是一位专业且亲切的 AI 营养教练 (NutriSnap AI Coach)。
你的任务是协助用户记录饮食、分析营养、解答疑问，并给出贴心的健康建议。

【当前用户的每日目标】
- 每日热量目标: {cal_target} kcal
- 每日蛋白质目标: {pro_target} g

【用户今日已摄入数据】
{meals_context}

【回复准则】
1. 语言亲切、专业、鼓励性强，多使用 emoji 让对话生动。
2. 结合用户今天摄入的实际情况与他们的每日目标，给出具体有针对性的建议。例如：如果用户蛋白质没吃够，建议吃什么；如果热量快超了，建议控制。
3. 如果用户还没记录饮食，提醒并鼓励他们去“首页”拍照或语音记录。
4. 回复保持简洁、重点突出，字数控制在 250 字以内，方便手机端阅读。
5. 只能回答跟饮食、营养、运动、健康相关的问题，其他无关话题请礼貌性拒绝。"""

    try:
        response_text = call_llm(
            prompt_text=user_message,
            history=history,
            system_instruction=system_instruction,
            temperature=0.7
        )
    except Exception as api_err:
        return jsonify({"error": f"AI 营养教练服务繁忙，请稍后再试。详细错误: {str(api_err)}"}), 500

    return jsonify({
        "success": True,
        "reply": response_text
    })


# ==========================================
# 6. BMR / 身体数据 & 运动 API
# ==========================================

@app.route('/api/profile', methods=['GET', 'POST'])
def profile_bmr():
    username = request.headers.get('X-User-Id') or 'anonymous'
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Ensure user exists in users table
    cursor.execute("SELECT 1 FROM users WHERE username = ?", (username,))
    if not cursor.fetchone():
        cursor.execute("INSERT INTO users (username, password_hash) VALUES (?, '')", (username,))
        conn.commit()
    
    if request.method == 'POST':
        data = request.get_json() or {}
        profile, validation_error = validate_profile_payload(data)
        if validation_error:
            conn.close()
            return jsonify({"error": validation_error}), 400
        
        cursor.execute('''
            UPDATE users 
            SET gender = ?, age = ?, height = ?, weight = ?, activity_level = ?
            WHERE username = ?
        ''', (
            profile['gender'],
            profile['age'],
            profile['height'],
            profile['weight'],
            profile['activity_level'],
            username
        ))
        conn.commit()
        
    row = cursor.execute('''
        SELECT gender, age, height, weight, activity_level
        FROM users WHERE username = ?
    ''', (username,)).fetchone()
    conn.close()
    
    if row is None or row['weight'] is None:
        return jsonify({
            "has_profile": False,
            "profile": None
        })
        
    w = float(row['weight'])
    h = float(row['height'])
    a = int(row['age'])
    gender = row['gender']
    lvl = row['activity_level'] or 'sedentary'
    
    if gender == 'female':
        bmr = 10 * w + 6.25 * h - 5 * a - 161
    else:
        bmr = 10 * w + 6.25 * h - 5 * a + 5
        
    multipliers = {
        'sedentary': 1.2,
        'lightly_active': 1.375,
        'moderately_active': 1.55,
        'very_active': 1.725,
        'highly_active': 1.9
    }
    multiplier = multipliers.get(lvl, 1.2)
    tdee = int(bmr * multiplier)
    protein_target = int(w * 1.6)
    
    return jsonify({
        "has_profile": True,
        "profile": {
            "gender": gender,
            "age": a,
            "height": h,
            "weight": w,
            "activity_level": lvl,
            "bmr": int(bmr),
            "tdee": tdee,
            "protein_target": protein_target
        }
    })

@app.route('/api/exercises', methods=['GET', 'POST'])
def manage_exercises():
    username = request.headers.get('X-User-Id') or 'anonymous'
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if request.method == 'POST':
        data = request.get_json() or {}
        name = data.get('exercise_name', '未知运动').strip()
        try:
            calories = int(data.get('calories', 0))
            duration = int(data.get('duration', 0))
        except (TypeError, ValueError):
            conn.close()
            return jsonify({"error": "运动时长和消耗热量必须是数字"}), 400
        if duration <= 0 or duration > 600:
            conn.close()
            return jsonify({"error": "运动时长需在 1-600 分钟之间"}), 400
        if calories < 0 or calories > 5000:
            conn.close()
            return jsonify({"error": "运动消耗需在 0-5000 kcal 之间"}), 400
        ex_type = data.get('exercise_type', 'aerobic')
        if ex_type not in ('aerobic', 'strength'):
            ex_type = 'aerobic'
        muscles = data.get('target_muscles', '')
        
        cursor.execute('''
            INSERT INTO exercises (exercise_name, calories, duration, exercise_type, target_muscles, username)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (name, calories, duration, ex_type, muscles, username))
        conn.commit()
        
    rows = cursor.execute('''
        SELECT id, exercise_name, calories, duration, exercise_type, target_muscles, created_at
        FROM exercises
        WHERE username = ?
        ORDER BY created_at DESC LIMIT 50
    ''', (username,)).fetchall()
    conn.close()
    
    return jsonify({"success": True, "data": [dict(r) for r in rows]})

@app.route('/api/exercises/<int:ex_id>', methods=['DELETE'])
def delete_exercise(ex_id):
    username = request.headers.get('X-User-Id') or 'anonymous'
    conn = get_db_connection()
    conn.execute('DELETE FROM exercises WHERE id = ? AND username = ?', (ex_id, username))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/api/report/suggestions', methods=['GET', 'POST'])
@app.route('/api/coach/suggestions', methods=['GET', 'POST'])
def report_suggestions():
    username = request.headers.get('X-User-Id') or 'anonymous'
    
    meals_list = None
    exercises_list = None
    user_row = None
    
    if request.method == 'POST':
        data = request.get_json() or {}
        meals_list = data.get('meals')
        exercises_list = data.get('exercises')
        profile_data = data.get('profile')
        if profile_data:
            user_row = {
                'gender': profile_data.get('gender'),
                'age': profile_data.get('age'),
                'height': profile_data.get('height'),
                'weight': profile_data.get('weight'),
                'activity_level': profile_data.get('activity_level')
            }
            
    if meals_list is None or exercises_list is None:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        if user_row is None:
            user_row = cursor.execute('''
                SELECT gender, age, height, weight, activity_level
                FROM users WHERE username = ?
            ''', (username,)).fetchone()
            if user_row:
                user_row = dict(user_row)
            
        # Retrieve past 7 days daily summaries
        summaries_db = cursor.execute('''
            SELECT total_calories, total_protein, total_burn_calories, total_exercise_duration
            FROM daily_summaries
            WHERE username = ? AND date >= date('now', '-7 days')
        ''', (username,)).fetchall()
        
        conn.close()
        
        total_cal = 0
        total_pro = 0
        total_burn = 0
        total_duration = 0
        days_count = len(summaries_db) or 1
        
        for row in summaries_db:
            total_cal += row['total_calories'] or 0
            total_pro += row['total_protein'] or 0
            total_burn += row['total_burn_calories'] or 0
            total_duration += row['total_exercise_duration'] or 0
            
        avg_cal = int(total_cal / days_count)
        avg_pro = int(total_pro / days_count)
        exercise_context = f"过去 7 天内累计进行了运动，共消耗运动热量 {total_burn} kcal，累计运动时间 {total_duration} 分钟。"
    else:
        # Standard client POST calculation (using client details payload)
        total_cal = 0
        total_pro = 0
        for m in meals_list:
            p = m.get('portion') or 1.0
            total_cal += (m.get('calories') or 0) * p
            total_pro += (m.get('protein') or 0) * p
            
        avg_cal = int(total_cal / 7) if meals_list else 0
        avg_pro = int(total_pro / 7) if meals_list else 0
        
        exercise_summary = []
        total_burn = 0
        for ex in exercises_list:
            total_burn += ex.get('calories') or 0
            desc = f"- {ex.get('exercise_name')} ({ex.get('duration')}分钟, 消耗 {ex.get('calories')} kcal"
            if ex.get('exercise_type') == 'strength' and ex.get('target_muscles'):
                desc += f", 训练肌群: {ex.get('target_muscles')}"
            desc += ")"
            exercise_summary.append(desc)
            
        exercise_context = "\n".join(exercise_summary) if exercise_summary else "无运动记录"
        
    user_info = "暂无身体数据"
    if user_row and user_row.get('weight'):
        user_info = f"性别: {user_row['gender']}, 年龄: {user_row['age']}岁, 身高: {user_row['height']}cm, 体重: {user_row['weight']}kg, 活动量级别: {user_row['activity_level']}"
    
    prompt = f"""你是一位资深的 AI 运动健身与营养教练。请根据用户过去 7 天的身体数据、饮食摄入和运动消耗，给出具体的运动训练、调整与恢复建议。

【用户基本身体信息】
{user_info}

【过去 7 天平均每日摄入】
- 热量：{avg_cal} kcal
- 蛋白质：{avg_pro} g

【过去 7 天已记录运动】
共消耗运动热量：{total_burn} kcal
运动明细：
{exercise_context}

【要求】
1. 评估用户的运动消耗是否充足，针对他们记录的运动（如有氧与力量的比例、力量训练部位）给出专业建议。
2. 结合饮食摄入与运动消耗，分析其是否合理，并给出接下来一周的具体训练及恢复建议。
3. 只能回答跟运动、训练、康复、营养相关的内容。
4. 语言亲切专业，使用列表和 Markdown 排版，字数控制在 250 字以内，多用 Emoji。"""

    try:
        response_text = call_llm(prompt_text=prompt)
            
        return jsonify({"success": True, "suggestions": response_text})
    except Exception as e:
        print(f"Suggestions generation error: {e}")
        return jsonify({"success": False, "suggestions": f"AI 营养教练服务繁忙，请稍后再试。详细错误: {str(e)}"})


@app.route('/api/daily-summaries', methods=['GET', 'POST'])
def handle_daily_summaries():
    username = request.headers.get('X-User-Id') or 'anonymous'
    
    if request.method == 'GET':
        conn = get_db_connection()
        cursor = conn.cursor()
        rows = cursor.execute('''
            SELECT date, total_calories, total_protein, total_carbs, total_fat, total_burn_calories, total_exercise_duration
            FROM daily_summaries
            WHERE username = ?
            ORDER BY date ASC
        ''', (username,)).fetchall()
        conn.close()
        
        result = [dict(row) for row in rows]
        return jsonify(result)
        
    elif request.method == 'POST':
        data = request.json or {}
        date_str = data.get('date')
        if not date_str:
            return jsonify({"error": "缺少日期参数"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        upsert_daily_summary(cursor, username, data)
        conn.commit()
        conn.close()

        return jsonify({"success": True})


# ==========================================
# 7. 食物数据库 & 条形码 API
# ==========================================

OFF_SEARCH_URL = "https://world.openfoodfacts.org/api/v2/search"
OFF_PRODUCT_URL = "https://world.openfoodfacts.org/api/v2/product"

def map_off_nutriments(nutriments):
    """映射 Open Food Facts nutriments 字段到标准格式"""
    if not nutriments:
        return {'calories': 0, 'protein': 0, 'carbs': 0, 'fat': 0}
    
    def safe_float(val, default=0):
        try:
            return float(val) if val is not None else default
        except (TypeError, ValueError):
            return default
    
    energy_kcal = (
        safe_float(nutriments.get('energy-kcal_100g')) or
        safe_float(nutriments.get('energy-kcal')) or
        (safe_float(nutriments.get('energy_100g')) / 4.184 if nutriments.get('energy_100g') else 0) or
        0
    )
    
    return {
        'calories': round(energy_kcal),
        'protein': round(safe_float(nutriments.get('proteins_100g'))),
        'carbs': round(safe_float(nutriments.get('carbohydrates_100g'))),
        'fat': round(safe_float(nutriments.get('fat_100g')))
    }

@app.route('/api/food/search', methods=['GET'])
def food_search():
    """搜索 Open Food Facts 食物数据库"""
    query = request.args.get('q', '').strip()
    if not query or len(query) < 2:
        return jsonify({"error": "搜索关键词至少2个字符"}), 400
    
    page = request.args.get('page', 1, type=int)
    if page < 1:
        page = 1
    
    try:
        params = {
            'search_terms': query,
            'search_simple': 1,
            'json': 1,
            'page': page,
            'page_size': 20,
            'fields': 'product_name,nutriments,code,image_url,brands'
        }
        resp = requests.get(OFF_SEARCH_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        products = data.get('products', [])
        results = []
        for p in products:
            if not p.get('product_name'):
                continue
            
            nutriments = map_off_nutriments(p.get('nutriments'))
            results.append({
                'code': p.get('code', ''),
                'product_name': p.get('product_name', ''),
                'brands': p.get('brands', ''),
                'image_url': p.get('image_url', ''),
                'calories': nutriments['calories'],
                'protein': nutriments['protein'],
                'carbs': nutriments['carbs'],
                'fat': nutriments['fat']
            })
        
        return jsonify({
            "success": True,
            "query": query,
            "page": page,
            "count": data.get('count', 0),
            "results": results
        })
    except requests.exceptions.Timeout:
        return jsonify({"error": "食物数据库请求超时，请稍后重试"}), 504
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"食物数据库请求失败: {str(e)}"}), 502
    except Exception as e:
        return jsonify({"error": f"搜索失败: {str(e)}"}), 500

@app.route('/api/food/barcode/<barcode>', methods=['GET'])
def food_barcode(barcode):
    """通过条形码查询 Open Food Facts 食物信息"""
    if not barcode or not barcode.isdigit():
        return jsonify({"error": "无效的条形码"}), 400
    
    try:
        resp = requests.get(f"{OFF_PRODUCT_URL}/{barcode}.json", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        if data.get('status') != 1 or not data.get('product'):
            return jsonify({"error": "未找到该条形码对应的食物"}), 404
        
        product = data['product']
        if not product.get('product_name'):
            return jsonify({"error": "该条形码对应的食物信息不完整"}), 404
        
        nutriments = map_off_nutriments(product.get('nutriments'))
        
        return jsonify({
            "success": True,
            "code": barcode,
            "product_name": product.get('product_name', ''),
            "brands": product.get('brands', ''),
            "image_url": product.get('image_url', ''),
            "calories": nutriments['calories'],
            "protein": nutriments['protein'],
            "carbs": nutriments['carbs'],
            "fat": nutriments['fat']
        })
    except requests.exceptions.Timeout:
        return jsonify({"error": "条形码查询超时，请稍后重试"}), 504
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"条形码查询失败: {str(e)}"}), 502
    except Exception as e:
        return jsonify({"error": f"条形码查询失败: {str(e)}"}), 500

# ==========================================
# 8. 体重追踪 API
# ==========================================

@app.route('/api/weight', methods=['GET'])
def get_weight_logs():
    """获取体重历史记录"""
    username = get_current_username()
    days = request.args.get('days', 90, type=int)
    if days not in (7, 30, 90, 180, 365):
        days = 90
    
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    
    conn = get_db_connection()
    cursor = conn.cursor()
    rows = cursor.execute('''
        SELECT id, weight, recorded_at
        FROM weight_logs
        WHERE username = ?
        AND recorded_at >= ?
        ORDER BY recorded_at ASC
    ''', (username, since)).fetchall()
    conn.close()
    
    return jsonify({
        "success": True,
        "days": days,
        "data": [{"id": r['id'], "weight": r['weight'], "recorded_at": r['recorded_at']} for r in rows]
    })

@app.route('/api/weight', methods=['POST'])
def record_weight():
    """记录体重"""
    username = get_current_username()
    data = request.get_json() or {}
    
    try:
        weight = float(data.get('weight', 0))
    except (TypeError, ValueError):
        return jsonify({"error": "体重必须是有效数字"}), 400
    
    if weight < 20 or weight > 300:
        return jsonify({"error": "体重需在 20-300 kg 之间"}), 400
    
    recorded_at = data.get('recorded_at', datetime.date.today().isoformat())
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 同一天已有记录则更新
    cursor.execute('''
        SELECT id FROM weight_logs
        WHERE username = ? AND date(recorded_at) = date(?)
    ''', (username, recorded_at))
    existing = cursor.fetchone()
    
    if existing:
        cursor.execute('''
            UPDATE weight_logs SET weight = ?, recorded_at = ?
            WHERE id = ?
        ''', (weight, recorded_at, existing['id']))
    else:
        cursor.execute('''
            INSERT INTO weight_logs (username, weight, recorded_at)
            VALUES (?, ?, ?)
        ''', (username, weight, recorded_at))
    
    conn.commit()
    conn.close()
    
    return jsonify({"success": True, "weight": weight, "recorded_at": recorded_at})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"Backend server started! Running on port {port}")
    app.run(host='0.0.0.0', debug=True, port=port)
