import os
import uuid
import json
import sqlite3
try:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
except ImportError:
    psycopg2 = None

import datetime
import time
import re
import hashlib
import hmac
import jwt
import functools
import requests
from urllib.parse import urlparse
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, send_from_directory, make_response, redirect
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from google import genai
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import PIL.Image
from PIL import UnidentifiedImageError
import glob

# 加载 .env 文件（仅本地开发使用，Cloud Run 通过环境变量注入）
load_dotenv()

app = Flask(__name__)


@app.before_request
def serve_latest_frontend_entry_before_legacy_routes():
    """Serve SPA entry points with no-store headers for fresh PWA updates."""
    from flask import make_response, request

    if request.method != "GET":
        return None

    if request.path in ("/", "/index.html"):
        response = make_response(render_template("index.html"))
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    if request.path == "/sw.js":
        response = make_response(send_from_directory("static", "sw.js"))
        response.headers["Content-Type"] = "application/javascript"
        response.headers["Service-Worker-Allowed"] = "/"
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    return None
default_cors_origins = [
    'http://localhost',
    'https://localhost',
    'capacitor://localhost',
    'http://localhost:5000',
    'http://127.0.0.1:5000',
    'http://localhost:8080',
    'http://localhost:8100',
]
cors_origins = [o.strip() for o in os.environ.get('CORS_ORIGINS', '').split(',') if o.strip()]
CORS(app, origins=cors_origins or default_cors_origins, supports_credentials=True)

# JWT 密钥（优先从环境变量读取）
DEFAULT_JWT_SECRET_KEY = 'nutrisnap-jwt-secret-change-in-production'
JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY')
if not JWT_SECRET_KEY:
    if os.environ.get('K_SERVICE'):
        raise RuntimeError('JWT_SECRET_KEY must be set in production')
    JWT_SECRET_KEY = DEFAULT_JWT_SECRET_KEY
JWT_EXPIRATION_HOURS = int(os.environ.get('JWT_EXPIRATION_HOURS', '72'))

# Flask-Limiter 速率限制
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024))
app.config['RATELIMIT_STORAGE_URI'] = os.environ.get('RATELIMIT_STORAGE_URI', 'memory://')
if os.environ.get('K_SERVICE') and app.config['RATELIMIT_STORAGE_URI'] == 'memory://' and os.environ.get('TEST_MODE') != 'true':
    raise RuntimeError('RATELIMIT_STORAGE_URI must use a shared store in production')
app.config['RATELIMIT_DEFAULT'] = '60 per minute'
if os.environ.get('TEST_MODE') == 'true':
    app.config['RATELIMIT_ENABLED'] = False
limiter = Limiter(key_func=get_remote_address, app=app)

MAX_TEXT_INPUT_CHARS = int(os.environ.get('MAX_TEXT_INPUT_CHARS', '4000'))
MAX_CHAT_HISTORY_ITEMS = int(os.environ.get('MAX_CHAT_HISTORY_ITEMS', '20'))
MAX_AUDIO_UPLOAD_BYTES = int(os.environ.get('MAX_AUDIO_UPLOAD_BYTES', 8 * 1024 * 1024))

def get_bearer_token():
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:]
    return request.args.get('token')

def user_or_ip_limit_key():
    token = get_bearer_token()
    if token:
        try:
            data = jwt.decode(token, JWT_SECRET_KEY, algorithms=['HS256'])
            username = data.get('username')
            if username:
                return f'user:{username}'
        except jwt.InvalidTokenError:
            pass
    return f'ip:{get_remote_address()}'

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
SPEECH_TO_TEXT_MODEL = os.environ.get('OPENROUTER_STT_MODEL', 'openai/whisper-large-v3')
SPEECH_TO_TEXT_LANGUAGE = os.environ.get('SPEECH_TO_TEXT_LANGUAGE', 'zh')
USE_OPENROUTER_LLM = os.environ.get('USE_OPENROUTER_LLM', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
OPENROUTER_CHAT_TIMEOUT_SECONDS = float(os.environ.get('OPENROUTER_CHAT_TIMEOUT_SECONDS', '8'))
OPENROUTER_STT_TIMEOUT_SECONDS = float(os.environ.get('OPENROUTER_STT_TIMEOUT_SECONDS', '20'))
OPENROUTER_MAX_MODEL_ATTEMPTS = max(1, int(os.environ.get('OPENROUTER_MAX_MODEL_ATTEMPTS', '2')))
OPENROUTER_PROVIDER_SORT = os.environ.get('OPENROUTER_PROVIDER_SORT', 'latency').strip().lower()
if OPENROUTER_PROVIDER_SORT not in ('latency', 'throughput', 'price'):
    OPENROUTER_PROVIDER_SORT = 'latency'

def parse_model_list(value, default_models):
    models = [m.strip() for m in (value or '').split(',') if m.strip()]
    return models or default_models

def openrouter_provider_config():
    return {
        "sort": OPENROUTER_PROVIDER_SORT,
        "allow_fallbacks": True,
        "data_collection": "allow",
    }

OPENROUTER_NUTRITION_MODELS = parse_model_list(os.environ.get('OPENROUTER_NUTRITION_MODELS'), [
    'deepseek/deepseek-v4-flash:free',
    'qwen/qwen3-next-80b-a3b-instruct:free',
])

OPENROUTER_TEXT_MODELS = parse_model_list(os.environ.get('OPENROUTER_TEXT_MODELS'), [
    'z-ai/glm-4.5-air:free',
    'deepseek/deepseek-v4-flash:free',
    'openai/gpt-oss-20b:free',
    'moonshotai/kimi-k2.6:free',
    'qwen/qwen3-next-80b-a3b-instruct:free',
    'openai/gpt-oss-120b:free',
    'meta-llama/llama-3.3-70b-instruct:free',
    'google/gemma-4-26b-a4b-it:free',
    'openrouter/free',
])

OPENROUTER_VISION_MODELS = parse_model_list(os.environ.get('OPENROUTER_VISION_MODELS'), [
    'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free',
    'google/gemma-4-31b-it:free',
    'nvidia/nemotron-nano-12b-v2-vl:free',
    'google/gemma-4-26b-a4b-it:free',
    'moonshotai/kimi-k2.6:free',
    'openrouter/free',
])

if not GEMINI_API_KEY and not OPENROUTER_API_KEY:
    raise ValueError("GEMINI_API_KEY 或 OPENROUTER_API_KEY 环境变量未设置！请在本地 .env 中配置后重启应用。")

client = None
if GEMINI_API_KEY:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"初始化 Google GenAI client 失败: {e}")

def call_llm(
    prompt_text,
    image=None,
    audio=None,
    mime_type=None,
    history=None,
    system_instruction=None,
    temperature=0.7,
    preferred_provider='auto',
    openrouter_models=None,
    openrouter_max_attempts=None,
    openrouter_timeout=None,
    return_metadata=False,
):
    """
    Unified interface to call either OpenRouter API (if OPENROUTER_API_KEY is configured)
    or fall back to official Google Gemini API (using client.models.generate_content).
    """
    use_openrouter = (
        bool(OPENROUTER_API_KEY)
        and preferred_provider != 'google'
        and (preferred_provider == 'openrouter' or USE_OPENROUTER_LLM or not client)
        and audio is None
    )
    if use_openrouter:
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
            audio_format = mtype.split(';', 1)[0].split('/')[-1].lower()
            user_content.append({
                "type": "input_audio",
                "input_audio": {
                    "data": audio_b64,
                    "format": audio_format
                }
            })
            
        if user_content:
            messages.append({"role": "user", "content": user_content})
            
        default_models = OPENROUTER_VISION_MODELS if image is not None else OPENROUTER_TEXT_MODELS
        attempt_limit = max(1, openrouter_max_attempts or OPENROUTER_MAX_MODEL_ATTEMPTS)
        timeout_seconds = openrouter_timeout or OPENROUTER_CHAT_TIMEOUT_SECONDS
        models_to_try = (openrouter_models or default_models)[:attempt_limit]
        
        last_error = None
        for model in models_to_try:
            payload = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "provider": openrouter_provider_config()
            }
            try:
                print(f"Calling OpenRouter model: {model}")
                started_at = time.perf_counter()
                res = requests.post(url, headers=headers, json=payload, timeout=timeout_seconds)
                latency_ms = int((time.perf_counter() - started_at) * 1000)
                res_json = res.json()
                if res.status_code == 200 and 'choices' in res_json:
                    message = res_json['choices'][0].get('message', {})
                    reply = message.get('content', '')
                    if isinstance(reply, list):
                        reply = ''.join(
                            part.get('text', '') for part in reply
                            if isinstance(part, dict) and part.get('type') in ('text', 'output_text')
                        )
                    reply = (reply or '').strip()
                    if not reply:
                        raise RuntimeError(f"OpenRouter model {model} returned empty content")
                    print(f"Success with OpenRouter model: {model}")
                    if return_metadata:
                        return {
                            "text": reply,
                            "provider": "openrouter",
                            "model": model,
                            "latency_ms": latency_ms,
                        }
                    return reply
                else:
                    error_msg = res_json.get('error', {}).get('message', res.text)
                    print(f"OpenRouter model {model} failed: {error_msg}")
                    last_error = Exception(f"OpenRouter error: {error_msg}")
            except Exception as e:
                print(f"OpenRouter network/request error with model {model}: {e}")
                last_error = e
                
        if client:
            print(f"OpenRouter models failed, falling back to Google SDK: {last_error}")
        else:
            raise last_error or Exception("OpenRouter request failed.")
        
    # ----------------------------------------------------
    # Google SDK Path (Fallback)
    # ----------------------------------------------------
    if not client:
        raise ValueError("Google SDK Client 未初始化，且没有设置 OPENROUTER_API_KEY。")
            
    models_to_try = [
        'gemini-2.5-flash-lite',
        'gemini-2.5-flash',
        'gemini-2.0-flash',
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
            started_at = time.perf_counter()
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config
            )
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            print(f"Success with Google SDK model: {model}")
            if return_metadata:
                return {
                    "text": response.text,
                    "provider": "google",
                    "model": model,
                    "latency_ms": latency_ms,
                }
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


def normalize_audio_format(mime_type, filename=''):
    raw = (mime_type or '').split(';', 1)[0].strip().lower()
    if raw.startswith('audio/'):
        raw = raw.split('/', 1)[1]
    if not raw and filename:
        lower_name = filename.lower()
        if lower_name.endswith('.mp4') or lower_name.endswith('.m4a'):
            raw = 'mp4'
        elif lower_name.endswith('.wav'):
            raw = 'wav'
        elif lower_name.endswith('.mp3'):
            raw = 'mp3'
        elif lower_name.endswith('.ogg'):
            raw = 'ogg'
        elif lower_name.endswith('.flac'):
            raw = 'flac'
        elif lower_name.endswith('.aac'):
            raw = 'aac'
        else:
            raw = 'webm'
    mapping = {
        'x-wav': 'wav',
        'wave': 'wav',
        'wav': 'wav',
        'mpeg': 'mp3',
        'mp3': 'mp3',
        'x-m4a': 'm4a',
        'm4a': 'm4a',
        'mp4': 'mp4',
        'm4v': 'mp4',
        'webm': 'webm',
        'ogg': 'ogg',
        'oga': 'ogg',
        'flac': 'flac',
        'aac': 'aac',
        'aiff': 'aiff',
        'opus': 'webm',
    }
    return mapping.get(raw, 'webm')


def transcribe_audio_with_openrouter(audio_bytes, mime_type, filename=''):
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY 未设置")

    audio_format = normalize_audio_format(mime_type, filename)
    audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://nutrisnap.ai",
        "X-Title": "NutriSnap AI"
    }
    payload = {
        "input_audio": {
            "data": audio_b64,
            "format": audio_format
        },
        "model": SPEECH_TO_TEXT_MODEL,
        "temperature": 0,
        "provider": openrouter_provider_config()
    }
    if SPEECH_TO_TEXT_LANGUAGE:
        payload["language"] = SPEECH_TO_TEXT_LANGUAGE

    res = requests.post(
        "https://openrouter.ai/api/v1/audio/transcriptions",
        headers=headers,
        json=payload,
        timeout=OPENROUTER_STT_TIMEOUT_SECONDS
    )
    try:
        res_json = res.json()
    except Exception:
        res_json = {}

    if res.status_code == 200:
        text = (res_json.get("text") or "").strip()
        if text:
            return text
        raise RuntimeError("OpenRouter STT 返回空文本")

    error_msg = None
    if isinstance(res_json.get("error"), dict):
        error_msg = res_json["error"].get("message")
    elif isinstance(res_json.get("error"), str):
        error_msg = res_json.get("error")
    if not error_msg:
        error_msg = (res.text or "").strip()[:500] or 'unknown'
    raise RuntimeError(f"OpenRouter STT failed ({res.status_code}): {error_msg}")


def transcribe_audio_with_google(audio_bytes, mime_type):
    if not client:
        raise ValueError("Google SDK Client 未初始化")
    prompt = "请将这段录音直接转写成中文文本，不要包含任何额外的引导语、标点纠正解释，仅输出转写文本本身。如果是静音或没有说话，请直接返回空字符串。"
    result_text = call_llm(
        prompt_text=prompt,
        audio=audio_bytes,
        mime_type=mime_type or 'audio/webm',
        temperature=0,
        preferred_provider='google'
    )
    return (result_text or '').strip()


def optimize_image_for_fast_vision(img, max_side=1280):
    """Keep vision requests fast and predictable by bounding image dimensions."""
    if not img:
        return img
    width, height = img.size
    longest = max(width, height)
    if longest <= max_side:
        return img
    scale = max_side / float(longest)
    target = (max(1, int(width * scale)), max(1, int(height * scale)))
    return img.resize(target, PIL.Image.Resampling.LANCZOS)

# ==========================================
# 2. 数据库配置
# ==========================================
def get_sqlite_path():
    if 'K_SERVICE' in os.environ:
        raise RuntimeError('DATABASE_URL must be set in Cloud Run; refusing to use ephemeral SQLite')
    return 'database.db'

def column_exists(cursor, table_name, column_name, is_postgres):
    if is_postgres:
        cursor.execute('''
            SELECT 1 
            FROM information_schema.columns 
            WHERE table_name = %s AND column_name = %s
        ''', (table_name.lower(), column_name.lower()))
        return cursor.fetchone() is not None
    else:
        cursor.execute(f"PRAGMA table_info({table_name})")
        for r in cursor.fetchall():
            if r[1] == column_name:
                return True
        return False

def translate_sql(sql, is_postgres):
    if not is_postgres:
        return sql
    
    # 1. Translate daily summaries INSERT OR REPLACE to ON CONFLICT DO UPDATE
    if "INSERT OR REPLACE INTO DAILY_SUMMARIES" in sql.upper():
        return """
            INSERT INTO daily_summaries (
                username, date, total_calories, total_protein, total_carbs, total_fat,
                total_burn_calories, total_exercise_duration, total_water
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (username, date) DO UPDATE SET
                total_calories = EXCLUDED.total_calories,
                total_protein = EXCLUDED.total_protein,
                total_carbs = EXCLUDED.total_carbs,
                total_fat = EXCLUDED.total_fat,
                total_burn_calories = EXCLUDED.total_burn_calories,
                total_exercise_duration = EXCLUDED.total_exercise_duration,
                total_water = EXCLUDED.total_water
        """

    # 2. Convert SQLite date functions
    # Replace date('now', '-7 days')
    sql = re.sub(r"date\(\s*'now'\s*,\s*'-7 days'\s*\)", "CURRENT_DATE - INTERVAL '7 days'", sql, flags=re.IGNORECASE)
    # Replace date(recorded_at)
    sql = re.sub(r"date\(\s*recorded_at\s*\)", "CAST(recorded_at AS DATE)", sql, flags=re.IGNORECASE)
    # Replace date(?) -> CAST(? AS DATE) (which then becomes CAST(%s AS DATE))
    sql = re.sub(r"date\(\s*\?\s*\)", "CAST(? AS DATE)", sql, flags=re.IGNORECASE)
    # Replace datetime(created_at) -> CAST(created_at AS TIMESTAMP)
    sql = re.sub(r"datetime\(\s*created_at\s*\)", "CAST(created_at AS TIMESTAMP)", sql, flags=re.IGNORECASE)

    # 3. Replace SQLite placeholder ? with PostgreSQL %s
    sql = replace_sqlite_placeholders(sql)
    
    return sql

def replace_sqlite_placeholders(sql):
    result = []
    in_single_quote = False
    in_double_quote = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'" and not in_double_quote:
            if i + 1 < len(sql) and sql[i + 1] == "'":
                result.append("''")
                i += 2
                continue
            in_single_quote = not in_single_quote
        elif ch == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
        result.append('%s' if ch == '?' and not in_single_quote and not in_double_quote else ch)
        i += 1
    return ''.join(result)

class PgRowWrapper:
    def __init__(self, col_names, values):
        self._row = dict(zip(col_names, values))
    
    def keys(self):
        return self._row.keys()
    
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self._row.values())[key]
        return self._row[key]

    def __contains__(self, key):
        return key in self._row

class DbCursor:
    def __init__(self, cursor, is_postgres):
        self._cursor = cursor
        self._is_postgres = is_postgres

    def execute(self, sql, params=None):
        translated = translate_sql(sql, self._is_postgres)
        if params is None:
            self._cursor.execute(translated)
        else:
            self._cursor.execute(translated, params)
        return self

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        if self._is_postgres:
            col_names = [desc[0] for desc in self._cursor.description]
            return PgRowWrapper(col_names, row)
        return row

    def fetchall(self):
        rows = self._cursor.fetchall()
        if self._is_postgres:
            col_names = [desc[0] for desc in self._cursor.description]
            return [PgRowWrapper(col_names, r) for r in rows]
        return rows

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def __getattr__(self, name):
        return getattr(self._cursor, name)

class DbConnection:
    def __init__(self, conn, is_postgres, pool_ref=None):
        self._conn = conn
        self._is_postgres = is_postgres
        self._pool_ref = pool_ref
        if not is_postgres:
            self._conn.row_factory = sqlite3.Row

    def cursor(self):
        return DbCursor(self._conn.cursor(), self._is_postgres)

    def execute(self, sql, params=None):
        cursor = self.cursor()
        cursor.execute(sql, params)
        return cursor

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._conn is None:
            return
        if self._pool_ref is not None:
            self._pool_ref.putconn(self._conn)
        else:
            self._conn.close()
        self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    @property
    def row_factory(self):
        if self._is_postgres:
            return None
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, val):
        if not self._is_postgres:
            self._conn.row_factory = val

    def __getattr__(self, name):
        return getattr(self._conn, name)

INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg2.IntegrityError) if psycopg2 else (sqlite3.IntegrityError,)
POSTGRES_POOL = None

def is_postgres_url(db_url):
    return bool(db_url and (db_url.startswith('postgres://') or db_url.startswith('postgresql://')))

def normalize_postgres_url(db_url):
    if db_url.startswith('postgres://'):
        return db_url.replace('postgres://', 'postgresql://', 1)
    return db_url

def validate_postgres_url(conn_str):
    parsed = urlparse(conn_str)
    hostname = parsed.hostname or ''
    port = parsed.port or 5432
    require_pooler = os.environ.get('REQUIRE_SUPABASE_POOLER', 'true').lower() == 'true'
    if os.environ.get('K_SERVICE') and require_pooler and 'supabase' in hostname and port != 6543:
        raise RuntimeError('Supabase DATABASE_URL must use the connection pooler port 6543 in production')

def open_postgres_connection(db_url):
    if not psycopg2:
        raise RuntimeError('DATABASE_URL is set but psycopg2-binary is not installed')
    conn_str = normalize_postgres_url(db_url)
    validate_postgres_url(conn_str)
    return psycopg2.connect(conn_str)

def get_postgres_pool(db_url):
    global POSTGRES_POOL
    if not psycopg2:
        raise RuntimeError('DATABASE_URL is set but psycopg2-binary is not installed')
    if POSTGRES_POOL is None:
        conn_str = normalize_postgres_url(db_url)
        validate_postgres_url(conn_str)
        POSTGRES_POOL = psycopg2.pool.ThreadedConnectionPool(
            minconn=int(os.environ.get('POSTGRES_POOL_MINCONN', '1')),
            maxconn=int(os.environ.get('POSTGRES_POOL_MAXCONN', '8')),
            dsn=conn_str
        )
    return POSTGRES_POOL

def init_db():
    is_postgres = False
    db_url = os.environ.get('DATABASE_URL')
    if is_postgres_url(db_url):
        is_postgres = True

    if is_postgres:
        conn_raw = open_postgres_connection(db_url)
    else:
        conn_raw = sqlite3.connect(get_sqlite_path())

    conn = DbConnection(conn_raw, is_postgres)
    cursor = conn.cursor()
    
    # 1. users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 2. meals table
    id_type = "SERIAL PRIMARY KEY" if is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS meals (
            id {id_type},
            image_path TEXT,
            food_name TEXT,
            calories INTEGER,
            protein INTEGER,
            carbs INTEGER,
            fat INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    for col, col_def in [
        ('session_id', 'TEXT'),
        ('portion', 'REAL DEFAULT 1.0' if not is_postgres else 'DOUBLE PRECISION DEFAULT 1.0'),
        ('weight', 'INTEGER DEFAULT 100'),
        ('username', 'TEXT'),
        ('client_id', 'TEXT'),
        ('updated_at', 'TEXT')
    ]:
        if not column_exists(cursor, 'meals', col, is_postgres):
            try:
                cursor.execute(f'ALTER TABLE meals ADD COLUMN {col} {col_def}')
            except Exception:
                pass  # column already exists / fallback

    # Migrate users: add BMR physical data columns
    for col, col_def in [
        ('gender', 'TEXT'),
        ('age', 'INTEGER'),
        ('height', 'REAL' if not is_postgres else 'DOUBLE PRECISION'),
        ('weight', 'REAL' if not is_postgres else 'DOUBLE PRECISION'),
        ('activity_level', 'TEXT'),
        ('nutrition_goal', 'TEXT')
    ]:
        if not column_exists(cursor, 'users', col, is_postgres):
            try:
                cursor.execute(f'ALTER TABLE users ADD COLUMN {col} {col_def}')
            except Exception:
                pass  # column already exists / fallback

    # 3. exercises table
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS exercises (
            id {id_type},
            exercise_name TEXT,
            calories INTEGER,
            duration INTEGER,
            exercise_type TEXT,
            target_muscles TEXT,
            username TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 4. daily_summaries table
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

    if not column_exists(cursor, 'daily_summaries', 'total_water', is_postgres):
        try:
            cursor.execute('ALTER TABLE daily_summaries ADD COLUMN total_water INTEGER DEFAULT 0')
        except Exception:
            pass  # column already exists / fallback

    # Backfill old records
    cursor.execute("UPDATE meals SET session_id = 'legacy_' || CAST(id AS TEXT) WHERE session_id IS NULL")
    cursor.execute("UPDATE meals SET username = 'anonymous' WHERE username IS NULL")
    cursor.execute("UPDATE meals SET client_id = 'server_' || CAST(id AS TEXT) WHERE client_id IS NULL")
    if is_postgres:
        cursor.execute("""
            UPDATE meals
            SET updated_at = to_char(COALESCE(created_at, CURRENT_TIMESTAMP), 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
            WHERE updated_at IS NULL
        """)
    else:
        cursor.execute("""
            UPDATE meals
            SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', COALESCE(created_at, CURRENT_TIMESTAMP))
            WHERE updated_at IS NULL
        """)

    # 5. weight_logs table
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS weight_logs (
            id {id_type},
            username TEXT,
            weight REAL NOT NULL,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY (username) REFERENCES users(username)
        )
    ''')

    # 6. Index
    cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_meals_username_client_id
        ON meals(username, client_id)
    ''')

    conn.commit()
    conn.close()

if os.environ.get('RUN_DB_MIGRATIONS') == 'true' or not os.environ.get('K_SERVICE'):
    init_db()

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api/'):
        return jsonify({"error": "API endpoint not found", "path": request.path}), 404
    return render_template('index.html'), 404

def get_db_connection():
    is_postgres = False
    db_url = os.environ.get('DATABASE_URL')
    if is_postgres_url(db_url):
        is_postgres = True

    if is_postgres:
        pool_ref = get_postgres_pool(db_url)
        conn_raw = pool_ref.getconn()
        return DbConnection(conn_raw, True, pool_ref)
    else:
        conn_raw = sqlite3.connect(get_sqlite_path())

    return DbConnection(conn_raw, is_postgres)

# ==========================================
# 3. 页面路由
# ==========================================

def get_latest_release_info():
    # Target regex for update_release.py: "version": "v5.6.35"
    fallback_version = "v5.6.35"
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        files = glob.glob(os.path.join(base_dir, "RELEASE_NOTES_*.md"))
        if not files:
            return fallback_version, "本次更新包含性能优化与体验改进。"
        
        def version_key(filename):
            match = re.search(r'RELEASE_NOTES_v?(\d+\.\d+\.\d+)\.md', filename)
            if match:
                return [int(x) for x in match.group(1).split('.')]
            return [0, 0, 0]
        
        latest_file = max(files, key=version_key)
        ver_match = re.search(r'RELEASE_NOTES_v?(\d+\.\d+\.\d+)\.md', latest_file)
        version = f"v{ver_match.group(1)}" if ver_match else fallback_version
        
        with open(latest_file, "r", encoding="utf-8") as f:
            content = f.read()
            
        changelog = ""
        lines = content.splitlines()
        capture = False
        for line in lines:
            if line.startswith("## "):
                capture = True
            if capture:
                changelog += line + "\n"
        
        return version, changelog.strip() or "优化了系统性能和无障碍体验。"
    except Exception as e:
        print(f"Error reading release notes: {e}")
        return fallback_version, "本次更新包含性能优化与体验改进。"

@app.route('/api/health')
def health():
    version, _ = get_latest_release_info()
    return jsonify({
        "version": version,
        "architecture": "local-first + throttled-meal-sync + server-daily-summaries + OpenRouter",
        "models": [
            'gemini-2.5-flash-lite',
            'gemini-2.5-flash',
            'gemini-2.0-flash',
        ],
        "openrouter_llm_enabled": bool(OPENROUTER_API_KEY and USE_OPENROUTER_LLM),
        "openrouter_text_models": OPENROUTER_TEXT_MODELS,
        "openrouter_nutrition_models": OPENROUTER_NUTRITION_MODELS,
        "openrouter_vision_models": OPENROUTER_VISION_MODELS,
        "openrouter_chat_timeout_seconds": OPENROUTER_CHAT_TIMEOUT_SECONDS,
        "openrouter_max_model_attempts": OPENROUTER_MAX_MODEL_ATTEMPTS,
    })

@app.route('/api/update/info')
def update_info():
    version, changelog = get_latest_release_info()
    return jsonify({
        "version": version,
        "changelog": changelog,
        "download_url": request.host_url + "api/update/download"
    })

@app.route('/api/update/download')
def download_update():
    return send_from_directory('static', 'app-debug.apk', as_attachment=True)

def first_present(data, *keys, default=None):
    for key in keys:
        if key in data and data.get(key) not in (None, ''):
            return data.get(key)
    return default

def parse_ai_multi_result(raw_text):
    """解析 AI JSON 输出，返回食物列表"""
    if not raw_text:
        return None
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
        if not isinstance(item, dict):
            continue
        normalized.append({
            'food_name': clean_text(first_present(item, 'food_name', 'name', 'food', 'label'), default='Unknown food', max_len=120),
            'calories': clamp_number(first_present(item, 'calories', 'calories_kcal', 'kcal'), default=0, min_value=0, max_value=5000),
            'protein': clamp_number(first_present(item, 'protein', 'protein_g'), default=0, min_value=0, max_value=300),
            'carbs': clamp_number(first_present(item, 'carbs', 'carbs_g', 'carbohydrates', 'carbohydrates_g'), default=0, min_value=0, max_value=500),
            'fat': clamp_number(first_present(item, 'fat', 'fat_g'), default=0, min_value=0, max_value=300),
            'weight': clamp_number(first_present(item, 'weight', 'estimated_grams', 'grams', 'weight_g'), default=100, min_value=1, max_value=2000),
            'sodium_mg': clamp_number(first_present(item, 'sodium_mg', 'sodium'), default=0, min_value=0, max_value=100000),
            'sugar_g': clamp_number(first_present(item, 'sugar_g', 'sugar'), default=0, min_value=0, max_value=500),
            'fiber_g': clamp_number(first_present(item, 'fiber_g', 'fiber'), default=0, min_value=0, max_value=200),
        })
    return normalized or None

def parse_json_payload(raw_text):
    if not raw_text:
        return None
    text = re.sub(r'^```(?:json)?\s*', '', raw_text.strip())
    text = re.sub(r'\s*```$', '', text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'(\{.*\}|\[.*\])', text, re.S)
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None

def normalize_visual_observations(payload):
    if not isinstance(payload, dict):
        return None
    foods = payload.get('foods') or payload.get('items') or []
    if isinstance(foods, dict):
        foods = [foods]
    normalized_foods = []
    for item in foods:
        if not isinstance(item, dict):
            continue
        normalized_foods.append({
            'name': clean_text(item.get('name') or item.get('food_name') or item.get('label'), default='Unknown food', max_len=120),
            'visible_ingredients': item.get('visible_ingredients') or item.get('ingredients') or [],
            'portion_visual': clean_text(item.get('portion_visual') or item.get('portion') or item.get('amount_description'), max_len=160),
            'estimated_weight_g': clamp_number(item.get('estimated_weight_g') or item.get('weight'), default=100, min_value=1, max_value=2000),
            'confidence': clamp_number(item.get('confidence'), default=0.65, min_value=0, max_value=1, integer=False),
        })
    if not normalized_foods:
        return None
    return {
        'scene_type': clean_text(payload.get('scene_type'), default='prepared_food', max_len=80),
        'is_packaged_food': bool(payload.get('is_packaged_food')),
        'package_text': clean_text(payload.get('package_text') or payload.get('ocr_text'), max_len=2000),
        'nutrition_label': payload.get('nutrition_label') if isinstance(payload.get('nutrition_label'), dict) else {},
        'foods': normalized_foods,
        'notes': clean_text(payload.get('notes'), max_len=300),
    }

def build_visual_prompt():
    return """You are the vision layer for a food photo logging app.
Only inspect the image. Do not calculate calories or macro nutrients unless they are explicitly printed on a package label.
Return strict JSON in this schema:
{
  "scene_type": "prepared_food | packaged_food | mixed | not_food",
  "is_packaged_food": true/false,
  "package_text": "visible OCR text from packaging or nutrition label, empty if none",
  "nutrition_label": {
    "serving_size": "printed serving size if visible",
    "calories": number_or_null,
    "protein": number_or_null,
    "carbs": number_or_null,
    "fat": number_or_null
  },
  "foods": [
    {
      "name": "Chinese food name if possible",
      "visible_ingredients": ["ingredient names visible in the photo"],
      "portion_visual": "short visual portion description",
      "estimated_weight_g": number,
      "confidence": 0.0
    }
  ],
  "notes": "important uncertainty or missing context"
}
If no food is visible, return {"scene_type":"not_food","foods":[]}."""

def build_nutrition_from_visual_prompt(visual_data):
    return f"""You are the nutrition reasoning layer for a food photo logging app.
Use the visual observation JSON below to estimate realistic nutrition. If a packaging nutrition label is present, prefer the printed label over visual estimation. Output strict JSON array only. No markdown.

Visual observation JSON:
{json.dumps(visual_data, ensure_ascii=False)}

Required output schema:
[
  {{
    "food_name": "食物名称",
    "calories": 热量数字(大卡),
    "protein": 蛋白质数字(克),
    "carbs": 碳水数字(克),
    "fat": 脂肪数字(克),
    "weight": 估计重量数字(克)
  }}
]"""

def confidence_label(score):
    try:
        value = float(score)
    except (TypeError, ValueError):
        value = 0.55
    if value >= 0.78:
        return 'high'
    if value >= 0.52:
        return 'medium'
    return 'low'

def build_weight_range(weight, confidence):
    grams = clamp_number(weight, default=100, min_value=1, max_value=2000)
    spread = 0.2 if confidence == 'high' else 0.32 if confidence == 'medium' else 0.5
    return {
        'min': max(1, int(round(grams * (1 - spread)))),
        'max': max(1, int(round(grams * (1 + spread)))),
    }

def build_nutrition_per_100g(food):
    weight = clamp_number(food.get('weight'), default=100, min_value=1, max_value=2000)
    factor = 100.0 / weight
    return {
        'calories_kcal': clamp_number(clamp_number(food.get('calories'), default=0, min_value=0, max_value=5000) * factor, default=0, min_value=0, max_value=5000),
        'carbs_g': clamp_number(clamp_number(food.get('carbs'), default=0, min_value=0, max_value=500) * factor, default=0, min_value=0, max_value=500),
        'protein_g': clamp_number(clamp_number(food.get('protein'), default=0, min_value=0, max_value=300) * factor, default=0, min_value=0, max_value=300),
        'fat_g': clamp_number(clamp_number(food.get('fat'), default=0, min_value=0, max_value=300) * factor, default=0, min_value=0, max_value=300),
    }

def enrich_foods_with_analysis_metadata(foods, visual_data, visual_meta, nutrition_meta, pipeline_latency_ms):
    visual_items = visual_data.get('foods') or []
    has_label = bool(visual_data.get('is_packaged_food') or visual_data.get('package_text') or visual_data.get('nutrition_label'))
    data_source = 'package_ocr' if has_label else 'vision_estimate'
    model_parts = []
    if visual_meta:
        model_parts.append(f"vision:{visual_meta.get('provider')}/{visual_meta.get('model')}")
    if nutrition_meta:
        model_parts.append(f"nutrition:{nutrition_meta.get('provider')}/{nutrition_meta.get('model')}")
    model_used = ';'.join(model_parts)
    enriched = []
    for idx, food in enumerate(foods or []):
        visual_item = visual_items[idx] if idx < len(visual_items) else {}
        confidence = confidence_label(visual_item.get('confidence'))
        weight = clamp_number(food.get('weight') or visual_item.get('estimated_weight_g'), default=100, min_value=1, max_value=2000)
        item = dict(food)
        item['weight'] = weight
        item['estimated_grams'] = weight
        item['ingredients'] = visual_item.get('visible_ingredients') or []
        item['portion_visual'] = visual_item.get('portion_visual') or ''
        item['weight_range'] = build_weight_range(weight, confidence)
        item['confidence'] = confidence
        item['needs_user_confirmation'] = confidence == 'low'
        item['data_source'] = data_source
        item['nutrition_per_100g'] = build_nutrition_per_100g(item)
        item['nutrition_total'] = {
            'calories_kcal': clamp_number(item.get('calories'), default=0, min_value=0, max_value=5000),
            'carbs_g': clamp_number(item.get('carbs'), default=0, min_value=0, max_value=500),
            'protein_g': clamp_number(item.get('protein'), default=0, min_value=0, max_value=300),
            'fat_g': clamp_number(item.get('fat'), default=0, min_value=0, max_value=300),
            'sodium_mg': clamp_number(item.get('sodium_mg'), default=0, min_value=0, max_value=100000),
            'sugar_g': clamp_number(item.get('sugar_g'), default=0, min_value=0, max_value=500),
            'fiber_g': clamp_number(item.get('fiber_g'), default=0, min_value=0, max_value=200),
        }
        item['model_used'] = model_used
        item['latency_ms'] = pipeline_latency_ms
        item['disclaimer'] = '营养数据为估算值，仅供参考，不可用于医疗诊断。'
        enriched.append(item)

    return enriched, {
        'data_source': data_source,
        'model_used': model_used,
        'visual_model': visual_meta.get('model') if visual_meta else '',
        'nutrition_model': nutrition_meta.get('model') if nutrition_meta else '',
        'visual_latency_ms': visual_meta.get('latency_ms') if visual_meta else 0,
        'nutrition_latency_ms': nutrition_meta.get('latency_ms') if nutrition_meta else 0,
        'latency_ms': pipeline_latency_ms,
        'needs_user_confirmation': any(item.get('needs_user_confirmation') for item in enriched),
        'disclaimer': '营养数据为估算值，仅供参考，不可用于医疗诊断。',
    }

def analyze_food_with_two_stage_pipeline(img):
    pipeline_started_at = time.perf_counter()
    visual_prompt = build_visual_prompt()
    visual_response = call_llm(
        prompt_text=visual_prompt,
        image=img,
        temperature=0.1,
        openrouter_max_attempts=1,
        openrouter_timeout=6,
        return_metadata=True,
    )
    visual_meta = {k: visual_response.get(k) for k in ('provider', 'model', 'latency_ms')}
    visual_data = normalize_visual_observations(parse_json_payload(visual_response.get('text')))
    if not visual_data and client and OPENROUTER_API_KEY and USE_OPENROUTER_LLM:
        print("Vision observation parse failed with OpenRouter result, retrying Google SDK once")
        visual_response = call_llm(prompt_text=visual_prompt, image=img, preferred_provider='google', temperature=0.1, return_metadata=True)
        visual_meta = {k: visual_response.get(k) for k in ('provider', 'model', 'latency_ms')}
        visual_data = normalize_visual_observations(parse_json_payload(visual_response.get('text')))
    if not visual_data:
        return None

    nutrition_prompt = build_nutrition_from_visual_prompt(visual_data)
    nutrition_response = call_llm(
        prompt_text=nutrition_prompt,
        temperature=0.1,
        openrouter_models=OPENROUTER_NUTRITION_MODELS,
        openrouter_max_attempts=1,
        openrouter_timeout=5,
        return_metadata=True,
    )
    nutrition_meta = {k: nutrition_response.get(k) for k in ('provider', 'model', 'latency_ms')}
    foods = parse_ai_multi_result(nutrition_response.get('text'))
    if foods is None and client and OPENROUTER_API_KEY and USE_OPENROUTER_LLM:
        print("Nutrition JSON parse failed with OpenRouter result, retrying Google SDK once")
        nutrition_response = call_llm(prompt_text=nutrition_prompt, preferred_provider='google', temperature=0.1, return_metadata=True)
        nutrition_meta = {k: nutrition_response.get(k) for k in ('provider', 'model', 'latency_ms')}
        foods = parse_ai_multi_result(nutrition_response.get('text'))
    if foods is None:
        return None
    pipeline_latency_ms = int((time.perf_counter() - pipeline_started_at) * 1000)
    enriched, analysis_meta = enrich_foods_with_analysis_metadata(foods, visual_data, visual_meta, nutrition_meta, pipeline_latency_ms)
    return {'foods': enriched, 'analysis_meta': analysis_meta}


@app.route('/')
def index():
    response = make_response(render_template('index.html'))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return response

@app.route('/sw.js')
def serve_sw():
    response = make_response(send_from_directory('static', 'sw.js'))
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return response

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
        token = get_bearer_token()
        
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

def request_has_local_app_marker():
    if str(request.args.get('is_app', '')).lower() == 'true':
        return True
    if str(request.form.get('is_app', '')).lower() == 'true':
        return True
    if request.is_json:
        data = request.get_json(silent=True) or {}
        return data.get('is_app') is True or str(data.get('is_app', '')).lower() == 'true'
    return False

def token_or_local_app_required(f):
    """Allow signed-in users and local mobile/offline voice flows."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        token = get_bearer_token()
        if not token:
            if request_has_local_app_marker():
                request.current_user = 'local_user'
                return f(*args, **kwargs)
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
    return None

def validate_profile_payload(data):
    try:
        gender = data.get('gender')
        age = int(data.get('age'))
        height = float(data.get('height'))
        weight = float(data.get('weight'))
        activity_level = data.get('activity_level') or 'sedentary'
        nutrition_goal = data.get('nutrition_goal') or data.get('goal') or 'maintain'
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

    if nutrition_goal not in ('fat_loss', 'maintain', 'muscle_gain'):
        return None, "Invalid nutrition goal"

    return {
        'gender': gender,
        'age': age,
        'height': height,
        'weight': weight,
        'activity_level': activity_level,
        'nutrition_goal': nutrition_goal
    }, None

NUTRITION_GOAL_CONFIGS = {
    'fat_loss': {'label': 'fat_loss', 'energy_multiplier': 0.85, 'protein_g_per_kg': 2.0, 'fat_ratio': 0.25},
    'maintain': {'label': 'maintain', 'energy_multiplier': 1.0, 'protein_g_per_kg': 1.6, 'fat_ratio': 0.30},
    'muscle_gain': {'label': 'muscle_gain', 'energy_multiplier': 1.10, 'protein_g_per_kg': 1.8, 'fat_ratio': 0.25},
}

def calculate_profile_targets(gender, age, height, weight, activity_level, nutrition_goal='maintain'):
    w = float(weight)
    h = float(height)
    a = int(age)
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
    tdee = int(round(bmr * multipliers.get(activity_level, 1.2)))
    goal = nutrition_goal if nutrition_goal in NUTRITION_GOAL_CONFIGS else 'maintain'
    config = NUTRITION_GOAL_CONFIGS[goal]
    calorie_target = int(round((tdee * config['energy_multiplier']) / 10) * 10)
    protein_target = max(1, int(round(w * config['protein_g_per_kg'])))
    fat_target = max(1, int(round((calorie_target * config['fat_ratio']) / 9)))
    carbs_target = max(1, int(round((calorie_target - protein_target * 4 - fat_target * 9) / 4)))
    return {
        'bmr': int(round(bmr)),
        'tdee': tdee,
        'calorie_target': calorie_target,
        'target_calories': calorie_target,
        'protein_target': protein_target,
        'carbs_target': carbs_target,
        'fat_target': fat_target,
        'nutrition_goal': goal
    }

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
            total_burn_calories, total_exercise_duration, total_water
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        username,
        date_str,
        clamp_number(summary.get('total_calories'), 0, 0, 100000),
        clamp_number(summary.get('total_protein'), 0, 0, 10000),
        clamp_number(summary.get('total_carbs'), 0, 0, 10000),
        clamp_number(summary.get('total_fat'), 0, 0, 10000),
        clamp_number(summary.get('total_burn_calories'), 0, 0, 100000),
        clamp_number(summary.get('total_exercise_duration'), 0, 0, 10000),
        clamp_number(summary.get('total_water'), 0, 0, 50000)
    ))
    return True

@app.route('/api/register', methods=['POST'])
@limiter.limit("5 per minute")
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
            return jsonify({"error": "用户名已存在，请换一个用户名或直接登录"}), 409
        else:
            cursor.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", (username, pw_hash))
        conn.commit()
        token = create_token(username)
        return jsonify({"success": True, "message": "注册成功", "username": username, "token": token})
    except INTEGRITY_ERRORS:
        return jsonify({"error": "用户名已存在，请换一个用户名或直接登录"}), 409
    except Exception as e:
        print(f"Register error: {e}")
        return jsonify({"error": "注册失败，请稍后重试"}), 500
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
            elif hmac.compare_digest(stored_hash, hashlib.sha256(password.encode('utf-8')).hexdigest()):
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
        print(f"Login error: {e}")
        return jsonify({"error": "登录失败，请稍后重试"}), 500
    finally:
        conn.close()

# ==========================================
# 5. 核心 API
# ==========================================

@app.route('/api/analyze', methods=['POST'])
@token_required
@limiter.limit("10 per minute", key_func=user_or_ip_limit_key)
def analyze_food():
    if 'image' not in request.files:
        return jsonify({"error": "没有找到图片"}), 400

    file = request.files['image']
    if file.filename == '':
        return jsonify({"error": "图片名为空"}), 400

    is_app = request.form.get('is_app') == 'true' or request.args.get('is_app') == 'true'
    username = get_current_username()

    if not file.mimetype or not file.mimetype.startswith('image/'):
        return jsonify({"error": "Only image uploads are supported"}), 400

    original_name = secure_filename(file.filename or '')
    ext = os.path.splitext(original_name)[1].lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.webp'):
        ext = '.jpg'
    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    try:
        with PIL.Image.open(filepath) as probe:
            probe.verify()
        with PIL.Image.open(filepath) as opened:
            img = optimize_image_for_fast_vision(opened.convert('RGB'))

        analysis_result = analyze_food_with_two_stage_pipeline(img)
        if not analysis_result or not analysis_result.get('foods'):
            return jsonify({"error": "AI未检测到食物，请重新拍摄"}), 400
        foods = analysis_result.get('foods', [])
        analysis_meta = analysis_result.get('analysis_meta', {})

        # Return food data; frontend stores locally via MealStorage (localStorage).
        # Daily aggregated summaries are synced to server via /api/daily-summaries.
        session_id = str(uuid.uuid4())
        saved = []
        for i, food in enumerate(foods):
            food['id'] = uuid.uuid4().hex
            food['portion'] = 1.0
            saved.append(food)

        return jsonify({
            "success": True,
            "session_id": session_id,
            "foods": saved,
            "image_url": "",
            "analysis_meta": analysis_meta
        })

    except UnidentifiedImageError:
        return jsonify({"error": "Invalid image file"}), 400
    except Exception as e:
        print(f"Error calling AI API: {e}")
        return jsonify({"error": "AI image analysis failed. Please try again later."}), 500
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
                    'food_name': clean_text(first_present(f, 'food_name', 'name', 'food', 'label'), default='Unknown food', max_len=120),
                    'calories': clamp_number(first_present(f, 'calories', 'calories_kcal', 'kcal'), default=0, min_value=0, max_value=5000),
                    'protein': clamp_number(first_present(f, 'protein', 'protein_g'), default=0, min_value=0, max_value=300),
                    'carbs': clamp_number(first_present(f, 'carbs', 'carbs_g', 'carbohydrates', 'carbohydrates_g'), default=0, min_value=0, max_value=500),
                    'fat': clamp_number(first_present(f, 'fat', 'fat_g'), default=0, min_value=0, max_value=300),
                    'weight': clamp_number(first_present(f, 'weight', 'estimated_grams', 'grams', 'weight_g'), default=100, min_value=1, max_value=2000)
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
                    'exercise_name': clean_text(first_present(ex, 'exercise_name', 'name', 'exercise'), default='Unknown exercise', max_len=120),
                    'calories': clamp_number(first_present(ex, 'calories', 'calories_kcal', 'kcal', 'calories_burned'), default=0, min_value=0, max_value=5000),
                    'duration': clamp_number(first_present(ex, 'duration', 'duration_min', 'minutes'), default=0, min_value=0, max_value=600),
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
@token_or_local_app_required
@limiter.limit("10 per minute", key_func=user_or_ip_limit_key)
def voice_input():
    """语音输入：用 Gemini 自动分辨运动与食物并解析提取"""
    data = request.json or {}
    text = data.get('text', '')
    is_app = data.get('is_app') == True
    username = get_current_username()

    if not text:
        return jsonify({"error": "语音文本为空"}), 400
    if len(text) > MAX_TEXT_INPUT_CHARS:
        return jsonify({"error": "Input text is too long"}), 413

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
        try:
            result_text = call_llm(
                prompt_text=prompt,
                openrouter_models=OPENROUTER_TEXT_MODELS,
                openrouter_max_attempts=1,
                openrouter_timeout=5,
            )
        except Exception as openrouter_first_err:
            print(f"Voice OpenRouter-first parse failed, trying Google SDK: {openrouter_first_err}")
            result_text = call_llm(prompt_text=prompt, preferred_provider='google')
        parsed = parse_voice_input_result(result_text)
        if parsed is None or (not parsed['foods'] and not parsed['exercises']):
            print("Voice JSON parse failed, retrying Google SDK once")
            try:
                result_text = call_llm(prompt_text=prompt, preferred_provider='google')
                parsed = parse_voice_input_result(result_text)
            except Exception as google_retry_err:
                print(f"Voice Google retry failed: {google_retry_err}")
        if parsed is None or (not parsed['foods'] and not parsed['exercises']):
            return jsonify({"error": "未能提取出任何有效的食物或运动信息，请重新描述"}), 400

        session_id = str(uuid.uuid4())
        saved_foods = []
        saved_exercises = []

        # Generate temporary IDs for the client to store locally.
        for i, food in enumerate(parsed['foods']):
            food['id'] = uuid.uuid4().hex
            food['portion'] = 1.0
            saved_foods.append(food)
            
        for i, ex in enumerate(parsed['exercises']):
            ex['id'] = uuid.uuid4().hex
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
        return jsonify({"error": "语音识别失败，请稍后重试"}), 500


@app.route('/api/speech-to-text', methods=['POST'])
@token_or_local_app_required
@limiter.limit("10 per minute", key_func=user_or_ip_limit_key)
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
        if len(audio_bytes) > MAX_AUDIO_UPLOAD_BYTES:
            return jsonify({"error": "Audio file is too large"}), 413

        transcription = ""
        errors = []
        if OPENROUTER_API_KEY:
            try:
                transcription = transcribe_audio_with_openrouter(audio_bytes, mime_type, audio_file.filename)
            except Exception as openrouter_err:
                errors.append(str(openrouter_err))
                print(f"OpenRouter STT error: {openrouter_err}")

        if not transcription and client:
            try:
                transcription = transcribe_audio_with_google(audio_bytes, mime_type)
            except Exception as google_err:
                errors.append(str(google_err))
                print(f"Google STT fallback error: {google_err}")

        transcription = re.sub(r'^["\'`]|["\'`]$', '', (transcription or '')).strip()
        if not transcription:
            if errors:
                print(f"Speech-to-text failed with errors: {' | '.join(errors)}")
            return jsonify({"error": "语音听写失败，请稍后重试"}), 500

        print(f"Speech transcription result: {transcription}")
        return jsonify({"text": transcription})

    except Exception as e:
        print(f"Speech to text API error: {e}")
        return jsonify({"error": "语音听写失败，请稍后重试"}), 500


@app.route('/api/meals', methods=['GET'])
@token_required
def get_meals():
    username = get_current_username()
    with get_db_connection() as conn:
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
    return jsonify({"data": [dict(m) for m in meals]})


@app.route('/api/meals/sync', methods=['GET', 'POST'])
@token_required
def sync_meals():
    username = get_current_username()
    if username in ('anonymous', 'guest', 'local_user'):
        return jsonify({"error": "请登录后再同步饮食记录"}), 401

    if request.method == 'GET':
        limit = min(max(request.args.get('limit', default=500, type=int), 1), 1000)
        with get_db_connection() as conn:
            rows = conn.execute('''
                SELECT id as server_id, client_id, session_id, food_name, calories, protein, carbs, fat,
                       weight, COALESCE(portion, 1.0) as portion, created_at, updated_at
                FROM meals
                WHERE username = ?
                ORDER BY datetime(created_at) DESC
                LIMIT ?
            ''', (username, limit)).fetchall()
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
        return jsonify({"error": "饮食记录同步失败，请稍后重试"}), 500
    finally:
        conn.close()


@app.route('/api/meals/<int:meal_id>', methods=['PATCH', 'DELETE'])
@token_required
def meal_action(meal_id):
    username = get_current_username()
    if request.method == 'DELETE':
        with get_db_connection() as conn:
            conn.execute('DELETE FROM meals WHERE id = ? AND username = ?', (meal_id, username))
            conn.commit()
        return jsonify({"success": True})

    elif request.method == 'PATCH':
        data = request.get_json() or {}
        portion = data.get('portion')
        if portion is None:
            return jsonify({"error": "缺少 portion 参数"}), 400
        with get_db_connection() as conn:
            conn.execute('UPDATE meals SET portion = ? WHERE id = ? AND username = ?', (float(portion), meal_id, username))
            conn.commit()
            row = conn.execute('''
                SELECT id, food_name,
                       CAST(calories * COALESCE(portion, 1.0) AS INTEGER) as calories,
                       protein, carbs, fat, portion, session_id
                FROM meals WHERE id = ? AND username = ?
            ''', (meal_id, username)).fetchone()
        if row is None:
            return jsonify({"error": "未找到对应的记录"}), 404
        return jsonify({"success": True, "data": dict(row)})


@app.route('/api/meals/session/<session_id>', methods=['DELETE'])
@token_required
def delete_session(session_id):
    username = get_current_username()
    with get_db_connection() as conn:
        conn.execute('DELETE FROM meals WHERE session_id = ? AND username = ?', (session_id, username))
        conn.commit()
    return jsonify({"success": True})


@app.route('/api/report/weekly', methods=['GET'])
@app.route('/api/weekly-report', methods=['GET'])
@token_required
def weekly_report():
    username = get_current_username()
    since = (datetime.date.today() - datetime.timedelta(days=6)).isoformat()
    
    # Get daily summaries
    with get_db_connection() as conn:
        rows = conn.execute('''
            SELECT
                date as day,
                total_calories,
                total_protein,
                total_carbs,
                total_fat,
                total_burn_calories,
                total_exercise_duration,
                total_water
            FROM daily_summaries
            WHERE date >= ? AND username = ?
            ORDER BY date ASC
        ''', (since, username)).fetchall()
        
        # Get weight data for the same period
        weight_rows = conn.execute('''
            SELECT substr(recorded_at, 1, 10) as day, weight
            FROM weight_logs
            WHERE username = ? AND recorded_at >= ?
            ORDER BY recorded_at DESC, id DESC
        ''', (username, since)).fetchall()

    # Fill missing dates
    data_map = {row['day']: dict(row) for row in rows}
    weight_map = {}
    for row in weight_rows:
        day = str(row['day'])
        if day not in weight_map:
            weight_map[day] = row['weight']
    
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
                'total_water': item.get('total_water') or 0,
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
                'total_water': 0,
                'weight': weight_map.get(d)
            })
    return jsonify({"data": report})


@app.route('/api/coach/chat', methods=['POST'])
@token_required
@limiter.limit("10 per minute", key_func=user_or_ip_limit_key)
def coach_chat():
    username = get_current_username()
    data = request.get_json() or {}
    user_message = data.get('message', '')
    history = data.get('history', [])
    cal_target = data.get('calTarget', 2000)
    pro_target = data.get('proTarget', 120)
    meals_from_client = data.get('meals') # Optional local meals from app
    exercises_from_client = data.get('exercises') # Optional local exercises from app
    water_from_client = data.get('water') # Optional local water from app
    today_summary_from_client = data.get('todaySummary') or data.get('today_summary')

    if not user_message and not history:
        return jsonify({"error": "消息内容为空"}), 400
    if len(str(user_message)) > MAX_TEXT_INPUT_CHARS:
        return jsonify({"error": "Message is too long"}), 413
    if not isinstance(history, list) or len(history) > MAX_CHAT_HISTORY_ITEMS:
        return jsonify({"error": "Chat history is too large"}), 413

    meals_summary = []
    total_cal = 0
    total_pro = 0
    total_carbs = 0
    total_fat = 0
    total_water = 0

    if meals_from_client is not None:
        for row in meals_from_client:
            portion = clamp_number(row.get('portion'), default=1.0, min_value=0.1, max_value=10, integer=False)
            food_name = clean_text(row.get('food_name'), default='Unknown food', max_len=80).replace('\n', ' ')
            cal = clamp_number(row.get('calories'), default=0, min_value=0, max_value=5000)
            pro = clamp_number(row.get('protein'), default=0, min_value=0, max_value=300)
            carbs = clamp_number(row.get('carbs'), default=0, min_value=0, max_value=500)
            fat = clamp_number(row.get('fat'), default=0, min_value=0, max_value=300)
            weighted_cal = int(round(cal * portion))
            weighted_pro = int(round(pro * portion))
            weighted_carbs = int(round(carbs * portion))
            weighted_fat = int(round(fat * portion))
            meals_summary.append(
                f"- {food_name}: {weighted_cal} kcal (蛋白质 {weighted_pro}g, 碳水 {weighted_carbs}g, 脂肪 {weighted_fat}g, 分量 {portion}x)"
            )
            total_cal += weighted_cal
            total_pro += weighted_pro
            total_carbs += weighted_carbs
            total_fat += weighted_fat
        meals_detail = "\n".join(meals_summary) if meals_summary else "无饮食记录"
        
        # Calculate water
        if water_from_client is not None:
            total_water = int(water_from_client)
        if isinstance(today_summary_from_client, dict):
            total_cal = clamp_number(today_summary_from_client.get('total_calories'), default=total_cal, min_value=0, max_value=100000)
            total_pro = clamp_number(today_summary_from_client.get('total_protein'), default=total_pro, min_value=0, max_value=10000)
            total_carbs = clamp_number(today_summary_from_client.get('total_carbs'), default=total_carbs, min_value=0, max_value=10000)
            total_fat = clamp_number(today_summary_from_client.get('total_fat'), default=total_fat, min_value=0, max_value=10000)
            total_water = clamp_number(today_summary_from_client.get('total_water'), default=total_water, min_value=0, max_value=20000)

        meals_context = f"用户今日饮食明细：\n{meals_detail}\n累计摄入：热量 {total_cal} kcal，蛋白质 {total_pro}g，碳水 {total_carbs}g，脂肪 {total_fat}g，饮水量 {total_water}ml。"
    else:
        today_str = datetime.date.today().isoformat()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            row = cursor.execute('''
                SELECT total_calories, total_protein, total_carbs, total_fat, total_water
                FROM daily_summaries
                WHERE date = ? AND username = ?
            ''', (today_str, username)).fetchone()

        if row:
            total_cal = row['total_calories'] or 0
            total_pro = row['total_protein'] or 0
            total_carbs = row['total_carbs'] or 0
            total_fat = row['total_fat'] or 0
            total_water = row['total_water'] or 0
            meals_context = f"用户今日累计摄入：热量 {total_cal} kcal，蛋白质 {total_pro}g，碳水 {total_carbs}g，脂肪 {total_fat}g，饮水量 {total_water}ml。"
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
        if isinstance(today_summary_from_client, dict):
            total_burn = clamp_number(today_summary_from_client.get('total_burn_calories'), default=total_burn, min_value=0, max_value=100000)
            total_duration = clamp_number(today_summary_from_client.get('total_exercise_duration'), default=total_duration, min_value=0, max_value=10000)
        meals_context += f"\n\n用户今日运动明细：\n{ex_detail}\n累计消耗：{total_burn} kcal，运动时长 {total_duration} 分钟。"
    else:
        today_str = datetime.date.today().isoformat()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            row = cursor.execute('''
                SELECT total_burn_calories, total_exercise_duration
                FROM daily_summaries
                WHERE date = ? AND username = ?
            ''', (today_str, username)).fetchone()

        if row:
            total_burn = row['total_burn_calories'] or 0
            total_duration = row['total_exercise_duration'] or 0
            meals_context += f"\n\n累计消耗：{total_burn} kcal，运动时长 {total_duration} 分钟。"
        else:
            meals_context += "\n\n用户今日尚未记录任何运动。"

    net_cal = total_cal - total_burn
    remaining_cal = clamp_number(cal_target, default=2000, min_value=1, max_value=10000) - net_cal
    protein_gap = max(clamp_number(pro_target, default=120, min_value=1, max_value=500) - total_pro, 0)
    if remaining_cal >= 0:
        balance_context = f"今日净摄入 {net_cal} kcal，距离热量目标还剩 {remaining_cal} kcal；蛋白质还差 {protein_gap}g。"
    else:
        balance_context = f"今日净摄入 {net_cal} kcal，已超过热量目标 {abs(remaining_cal)} kcal；蛋白质还差 {protein_gap}g。"

    system_instruction = f"""你是一位专业且亲切的 AI 营养教练 (NutriSnap AI Coach)。
你的任务是协助用户记录饮食、分析营养、解答疑问，并给出贴心的健康建议。

【当前用户的每日目标】
- 每日热量目标: {cal_target} kcal
- 每日蛋白质目标: {pro_target} g

【用户今日已摄入数据】
{meals_context}

【首页同源今日结论】
{balance_context}

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
        print(f"Coach chat error: {api_err}")
        return jsonify({"error": "AI 营养教练服务繁忙，请稍后再试。"}), 500

    return jsonify({
        "success": True,
        "reply": response_text
    })


# ==========================================
# 6. BMR / 身体数据 & 运动 API
# ==========================================

@app.route('/api/profile', methods=['GET', 'POST'])
@token_required
def profile_bmr():
    username = get_current_username()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        cursor.execute("SELECT 1 FROM users WHERE username = ?", (username,))
        if not cursor.fetchone():
            if request.method == 'POST':
                return jsonify({"error": "User does not exist"}), 404
            return jsonify({"has_profile": False, "profile": None})
        
        if request.method == 'POST':
            data = request.get_json() or {}
            profile, validation_error = validate_profile_payload(data)
            if validation_error:
                return jsonify({"error": validation_error}), 400
            
            cursor.execute('''
                UPDATE users 
                SET gender = ?, age = ?, height = ?, weight = ?, activity_level = ?, nutrition_goal = ?
                WHERE username = ?
            ''', (
                profile['gender'],
                profile['age'],
                profile['height'],
                profile['weight'],
                profile['activity_level'],
                profile['nutrition_goal'],
                username
            ))
            conn.commit()
            
        row = cursor.execute('''
            SELECT gender, age, height, weight, activity_level, nutrition_goal
            FROM users WHERE username = ?
        ''', (username,)).fetchone()
    
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
    goal = row['nutrition_goal'] if 'nutrition_goal' in row.keys() else 'maintain'
    targets = calculate_profile_targets(gender, a, h, w, lvl, goal)
    
    return jsonify({
        "has_profile": True,
        "profile": {
            "gender": gender,
            "age": a,
            "height": h,
            "weight": w,
            "activity_level": lvl,
            **targets
        }
    })

@app.route('/api/exercises', methods=['GET', 'POST'])
@token_required
def manage_exercises():
    username = get_current_username()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        if request.method == 'POST':
            data = request.get_json() or {}
            name = clean_text(data.get('exercise_name'), default='Unknown exercise', max_len=120)
            calories = clamp_number(data.get('calories'), default=0, min_value=0, max_value=5000)
            duration = clamp_number(data.get('duration'), default=0, min_value=0, max_value=600)
            if duration <= 0:
                return jsonify({"error": "运动时长需在 1-600 分钟之间"}), 400
            ex_type = data.get('exercise_type', 'aerobic')
            if ex_type not in ('aerobic', 'strength'):
                ex_type = 'aerobic'
            muscles = clean_text(data.get('target_muscles', ''), max_len=240)
            
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
    
    return jsonify({"success": True, "data": [dict(r) for r in rows]})

@app.route('/api/exercises/<int:ex_id>', methods=['DELETE'])
@token_required
def delete_exercise(ex_id):
    username = get_current_username()
    with get_db_connection() as conn:
        conn.execute('DELETE FROM exercises WHERE id = ? AND username = ?', (ex_id, username))
        conn.commit()
    return jsonify({"success": True})

@app.route('/api/report/suggestions', methods=['GET', 'POST'])
@app.route('/api/coach/suggestions', methods=['GET', 'POST'])
@token_required
@limiter.limit("10 per minute", key_func=user_or_ip_limit_key)
def report_suggestions():
    username = get_current_username()
    
    meals_list = None
    exercises_list = None
    user_row = None
    today_summary = None
    weekly_summary = None
    cal_target = 2000
    pro_target = 120
    carbs_target = None
    fat_target = None
    nutrition_goal = 'maintain'
    
    if request.method == 'POST':
        data = request.get_json() or {}
        meals_list = data.get('meals')
        exercises_list = data.get('exercises')
        today_summary = data.get('todaySummary') or data.get('today_summary')
        weekly_summary = data.get('weeklySummary') or data.get('weekly_summary')
        cal_target = clamp_number(data.get('calTarget'), default=2000, min_value=1, max_value=10000)
        pro_target = clamp_number(data.get('proTarget'), default=120, min_value=1, max_value=500)
        carbs_target = clamp_number(data.get('carbsTarget'), default=0, min_value=0, max_value=1000)
        fat_target = clamp_number(data.get('fatTarget'), default=0, min_value=0, max_value=500)
        nutrition_goal = clean_text(data.get('nutritionGoal') or data.get('nutrition_goal') or 'maintain', default='maintain', max_len=40)
        profile_data = data.get('profile')
        if profile_data:
            user_row = {
                'gender': profile_data.get('gender'),
                'age': profile_data.get('age'),
                'height': profile_data.get('height'),
                'weight': profile_data.get('weight'),
                'activity_level': profile_data.get('activity_level'),
                'nutrition_goal': profile_data.get('nutrition_goal') or profile_data.get('goal')
            }
            
    today_str = datetime.date.today().isoformat()
    today_cal = 0
    today_pro = 0
    today_carbs = 0
    today_fat = 0
    today_burn = 0
    today_duration = 0
    active_days = 0
    calendar_avg_cal = 0
    calendar_avg_pro = 0

    def summary_value(summary, key, default=0, max_value=100000):
        if not isinstance(summary, dict):
            return default
        return clamp_number(summary.get(key), default=default, min_value=0, max_value=max_value)

    if meals_list is None or exercises_list is None:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            if user_row is None:
                user_row = cursor.execute('''
                    SELECT gender, age, height, weight, activity_level, nutrition_goal
                    FROM users WHERE username = ?
                ''', (username,)).fetchone()
                if user_row:
                    user_row = dict(user_row)
                
            # Retrieve past 7 days daily summaries
            since = (datetime.date.today() - datetime.timedelta(days=6)).isoformat()
            summaries_db = cursor.execute('''
                SELECT date, total_calories, total_protein, total_carbs, total_fat,
                       total_burn_calories, total_exercise_duration
                FROM daily_summaries
                WHERE username = ? AND date >= ?
            ''', (username, since)).fetchall()
        
        total_cal = 0
        total_pro = 0
        total_carbs = 0
        total_fat = 0
        total_burn = 0
        total_duration = 0
        active_days = 0
        
        for row in summaries_db:
            row_cal = row['total_calories'] or 0
            row_pro = row['total_protein'] or 0
            row_carbs = row['total_carbs'] or 0
            row_fat = row['total_fat'] or 0
            row_burn = row['total_burn_calories'] or 0
            row_duration = row['total_exercise_duration'] or 0
            total_cal += row_cal
            total_pro += row_pro
            total_carbs += row_carbs
            total_fat += row_fat
            total_burn += row_burn
            total_duration += row_duration
            if row_cal or row_pro or row_burn or row_duration:
                active_days += 1
            if row['date'] == today_str:
                today_cal = row_cal
                today_pro = row_pro
                today_carbs = row_carbs
                today_fat = row_fat
                today_burn = row_burn
                today_duration = row_duration
            
        active_days = active_days or len(summaries_db)
        avg_divisor = active_days or 1
        avg_cal = int(round(total_cal / avg_divisor)) if active_days else 0
        avg_pro = int(round(total_pro / avg_divisor)) if active_days else 0
        calendar_avg_cal = int(round(total_cal / 7))
        calendar_avg_pro = int(round(total_pro / 7))
        exercise_context = f"过去 7 天内累计进行了运动，共消耗运动热量 {total_burn} kcal，累计运动时间 {total_duration} 分钟。"
    else:
        # Standard client POST calculation (using client details payload)
        total_cal = 0
        total_pro = 0
        total_carbs = 0
        total_fat = 0
        active_dates = set()
        for m in meals_list:
            p = clamp_number(m.get('portion'), default=1.0, min_value=0, max_value=10, integer=False)
            cal = clamp_number(m.get('calories'), default=0, min_value=0, max_value=5000)
            pro = clamp_number(m.get('protein'), default=0, min_value=0, max_value=300)
            carbs = clamp_number(m.get('carbs'), default=0, min_value=0, max_value=500)
            fat = clamp_number(m.get('fat'), default=0, min_value=0, max_value=300)
            weighted_cal = int(round(cal * p))
            weighted_pro = int(round(pro * p))
            weighted_carbs = int(round(carbs * p))
            weighted_fat = int(round(fat * p))
            total_cal += weighted_cal
            total_pro += weighted_pro
            total_carbs += weighted_carbs
            total_fat += weighted_fat
            item_day = str(m.get('created_at') or m.get('date') or '')[:10]
            if item_day:
                active_dates.add(item_day)
            if item_day == today_str:
                today_cal += weighted_cal
                today_pro += weighted_pro
                today_carbs += weighted_carbs
                today_fat += weighted_fat
            
        exercise_summary = []
        total_burn = 0
        total_duration = 0
        for ex in exercises_list:
            ex_cal = clamp_number(ex.get('calories'), default=0, min_value=0, max_value=5000)
            ex_duration = clamp_number(ex.get('duration'), default=0, min_value=0, max_value=1000)
            total_burn += ex_cal
            total_duration += ex_duration
            item_day = str(ex.get('created_at') or ex.get('date') or '')[:10]
            if item_day:
                active_dates.add(item_day)
            if item_day == today_str:
                today_burn += ex_cal
                today_duration += ex_duration
            desc = f"- {ex.get('exercise_name')} ({ex_duration}分钟, 消耗 {ex_cal} kcal"
            if ex.get('exercise_type') == 'strength' and ex.get('target_muscles'):
                desc += f", 训练肌群: {ex.get('target_muscles')}"
            desc += ")"
            exercise_summary.append(desc)

        if isinstance(weekly_summary, dict):
            total_cal = summary_value(weekly_summary, 'total_calories', default=total_cal)
            total_pro = summary_value(weekly_summary, 'total_protein', default=total_pro, max_value=10000)
            total_carbs = summary_value(weekly_summary, 'total_carbs', default=total_carbs, max_value=10000)
            total_fat = summary_value(weekly_summary, 'total_fat', default=total_fat, max_value=10000)
            total_burn = summary_value(weekly_summary, 'total_burn_calories', default=total_burn)
            total_duration = summary_value(weekly_summary, 'total_exercise_duration', default=total_duration, max_value=10000)
            active_days = summary_value(weekly_summary, 'active_days', default=len(active_dates), max_value=7)
            avg_cal = summary_value(weekly_summary, 'avg_calories_recorded_days', default=int(round(total_cal / max(active_days or 1, 1))))
            avg_pro = summary_value(weekly_summary, 'avg_protein_recorded_days', default=int(round(total_pro / max(active_days or 1, 1))), max_value=10000)
            calendar_avg_cal = summary_value(weekly_summary, 'avg_calories_calendar_days', default=int(round(total_cal / 7)))
            calendar_avg_pro = summary_value(weekly_summary, 'avg_protein_calendar_days', default=int(round(total_pro / 7)), max_value=10000)
        else:
            active_days = len(active_dates)
            avg_cal = int(round(total_cal / max(active_days or 1, 1))) if active_days else 0
            avg_pro = int(round(total_pro / max(active_days or 1, 1))) if active_days else 0
            calendar_avg_cal = int(round(total_cal / 7))
            calendar_avg_pro = int(round(total_pro / 7))

        if isinstance(today_summary, dict):
            today_cal = summary_value(today_summary, 'total_calories', default=today_cal)
            today_pro = summary_value(today_summary, 'total_protein', default=today_pro, max_value=10000)
            today_carbs = summary_value(today_summary, 'total_carbs', default=today_carbs, max_value=10000)
            today_fat = summary_value(today_summary, 'total_fat', default=today_fat, max_value=10000)
            today_burn = summary_value(today_summary, 'total_burn_calories', default=today_burn)
            today_duration = summary_value(today_summary, 'total_exercise_duration', default=today_duration, max_value=10000)

        exercise_context = "\n".join(exercise_summary) if exercise_summary else "无运动记录"
        
    user_info = "暂无身体数据"
    if user_row and user_row.get('weight'):
        user_info = f"性别: {user_row['gender']}, 年龄: {user_row['age']}岁, 身高: {user_row['height']}cm, 体重: {user_row['weight']}kg, 活动量级别: {user_row['activity_level']}"
    net_today_cal = today_cal - today_burn
    remaining_today_cal = cal_target - net_today_cal
    today_status = (
        f"今日净摄入 {net_today_cal} kcal，距离目标还剩 {remaining_today_cal} kcal。"
        if remaining_today_cal >= 0
        else f"今日净摄入 {net_today_cal} kcal，已超过目标 {abs(remaining_today_cal)} kcal。"
    )
    protein_status = f"今日蛋白质还差 {max(pro_target - today_pro, 0)}g。"
    macro_target_context = f"每日目标：热量 {cal_target} kcal，蛋白质 {pro_target}g"
    if carbs_target:
        macro_target_context += f"，碳水 {carbs_target}g"
    if fat_target:
        macro_target_context += f"，脂肪 {fat_target}g"
    macro_target_context += f"；当前目标类型：{nutrition_goal}。"
    
    prompt = f"""你是一位资深的 AI 运动健身与营养教练。请根据用户过去 7 天的身体数据、饮食摄入和运动消耗，给出具体的运动训练、调整与恢复建议。

【用户基本身体信息】
{user_info}

【每日目标】
{macro_target_context}

【今日真实记录（与首页同一数据源，不可被周均覆盖）】
- 摄入：{today_cal} kcal，蛋白质 {today_pro}g，碳水 {today_carbs}g，脂肪 {today_fat}g
- 运动：消耗 {today_burn} kcal，运动 {today_duration} 分钟
- 结论：{today_status} {protein_status}

【过去 7 天有记录日均】
- 有记录天数：{active_days} / 7 天
- 按有记录天数计算的日均摄入：热量 {avg_cal} kcal，蛋白质 {avg_pro}g
- 按完整 7 天摊平的参考值：热量 {calendar_avg_cal} kcal，蛋白质 {calendar_avg_pro}g（仅作连续性参考，不能用来判断今天摄入不足）

【过去 7 天已记录运动】
共消耗运动热量：{total_burn} kcal
运动明细：
{exercise_context}

【要求】
1. 必须优先依据“今日真实记录”判断今天是否超标、剩余或不足；不要把 7 日摊平值当作今天事实。
2. 评估过去 7 天运动消耗是否充足，针对他们记录的运动（如有氧与力量比例、力量训练部位）给出专业建议。
3. 结合今日摄入、目标和过去 7 天趋势，给出接下来一周的训练、饮食调整与恢复建议。
4. 只能回答跟运动、训练、康复、营养相关的内容。
5. 语言亲切专业，使用列表和 Markdown 排版，字数控制在 250 字以内，多用 Emoji。"""

    try:
        response_text = call_llm(prompt_text=prompt)
            
        return jsonify({"success": True, "suggestions": response_text})
    except Exception as e:
        print(f"Suggestions generation error: {e}")
        return jsonify({"success": False, "suggestions": "AI 营养教练服务繁忙，请稍后再试。"}), 500


@app.route('/api/daily-summaries', methods=['GET', 'POST'])
@token_required
def handle_daily_summaries():
    username = get_current_username()
    
    if request.method == 'GET':
        with get_db_connection() as conn:
            cursor = conn.cursor()
            rows = cursor.execute('''
                SELECT date, total_calories, total_protein, total_carbs, total_fat, total_burn_calories, total_exercise_duration, total_water
                FROM daily_summaries
                WHERE username = ?
                ORDER BY date ASC
            ''', (username,)).fetchall()
        
        result = [dict(row) for row in rows]
        return jsonify(result)
        
    elif request.method == 'POST':
        data = request.json or {}
        date_str = data.get('date')
        if not date_str:
            return jsonify({"error": "缺少日期参数"}), 400

        with get_db_connection() as conn:
            cursor = conn.cursor()
            upsert_daily_summary(cursor, username, data)
            conn.commit()

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
@token_required
@limiter.limit("30 per minute", key_func=user_or_ip_limit_key)
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
        print(f"Food search request error: {e}")
        return jsonify({"error": "食物数据库请求失败，请稍后重试"}), 502
    except Exception as e:
        print(f"Food search error: {e}")
        return jsonify({"error": "搜索失败，请稍后重试"}), 500

@app.route('/api/food/barcode/<barcode>', methods=['GET'])
@token_required
@limiter.limit("30 per minute", key_func=user_or_ip_limit_key)
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
        print(f"Barcode request error: {e}")
        return jsonify({"error": "条形码查询失败，请稍后重试"}), 502
    except Exception as e:
        print(f"Barcode lookup error: {e}")
        return jsonify({"error": "条形码查询失败，请稍后重试"}), 500

# ==========================================
# 8. 体重追踪 API
# ==========================================

@app.route('/api/weight', methods=['GET'])
@token_required
def get_weight_logs():
    """获取体重历史记录"""
    username = get_current_username()
    days = request.args.get('days', 90, type=int)
    if days not in (7, 30, 90, 180, 365):
        days = 90
    
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        rows = cursor.execute('''
            SELECT id, weight, recorded_at
            FROM weight_logs
            WHERE username = ?
            AND recorded_at >= ?
            ORDER BY recorded_at ASC
        ''', (username, since)).fetchall()
    
    return jsonify({
        "success": True,
        "days": days,
        "data": [{"id": r['id'], "weight": r['weight'], "recorded_at": r['recorded_at']} for r in rows]
    })

@app.route('/api/weight', methods=['POST'])
@token_required
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
    
    recorded_at = str(data.get('recorded_at', datetime.date.today().isoformat())).strip()
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:T[0-9:.+-]+Z?)?', recorded_at):
        return jsonify({"error": "recorded_at must be an ISO date"}), 400
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # 同一天已有记录则更新
        cursor.execute('''
            SELECT id FROM weight_logs
            WHERE username = ? AND substr(recorded_at, 1, 10) = substr(?, 1, 10)
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
    
    return jsonify({"success": True, "weight": weight, "recorded_at": recorded_at})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"Backend server started! Running on port {port}")
    app.run(host='127.0.0.1', debug=os.environ.get('FLASK_DEBUG') == '1', port=port)
