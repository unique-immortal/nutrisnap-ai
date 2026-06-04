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
import threading

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(APP_DIR, '.env')
load_dotenv(ENV_PATH)

# 显式从项目目录加载 .env，避免因为启动目录不同导致本地配置未生效。
load_dotenv(ENV_PATH)

app = Flask(__name__)
_REQUEST_TRACE_LOCAL = threading.local()


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


def reset_request_trace(action_name=''):
    _REQUEST_TRACE_LOCAL.trace = {
        'action': action_name or '',
        'upstream_calls': [],
    }


def get_request_trace():
    trace = getattr(_REQUEST_TRACE_LOCAL, 'trace', None)
    if trace is None:
        reset_request_trace()
        trace = _REQUEST_TRACE_LOCAL.trace
    return trace


def record_upstream_call(provider, model, status, latency_ms=None, detail=''):
    trace = get_request_trace()
    trace['upstream_calls'].append({
        'provider': str(provider or ''),
        'model': str(model or ''),
        'status': str(status or ''),
        'latency_ms': int(latency_ms or 0),
        'detail': clean_text(detail, max_len=240) if detail else '',
    })


def build_request_trace_summary():
    trace = get_request_trace()
    calls = list(trace.get('upstream_calls') or [])
    by_provider = {}
    success_count = 0
    for call in calls:
        provider = call.get('provider') or 'unknown'
        by_provider[provider] = by_provider.get(provider, 0) + 1
        if str(call.get('status', '')).endswith(':ok') or str(call.get('status', '')) == 'ok':
            success_count += 1
    return {
        'action': trace.get('action') or '',
        'upstream_call_count': len(calls),
        'upstream_success_count': success_count,
        'upstream_calls_by_provider': by_provider,
        'upstream_calls': calls,
    }


def attach_request_trace(meta=None):
    payload = dict(meta or {})
    payload.update(build_request_trace_summary())
    return payload


def log_request_trace(prefix):
    summary = build_request_trace_summary()
    print(
        f"{prefix} upstream_call_count={summary['upstream_call_count']} "
        f"success_count={summary['upstream_success_count']} "
        f"calls={json.dumps(summary['upstream_calls'], ensure_ascii=False)}"
    )


def get_client_action_id():
    value = ''
    try:
        value = (
            request.headers.get('X-Client-Action-Id')
            or request.form.get('client_action_id')
            or (request.get_json(silent=True) or {}).get('client_action_id')
            or ''
        )
    except Exception:
        value = request.headers.get('X-Client-Action-Id') or request.form.get('client_action_id') or ''
    return clean_text(value, max_len=96)


def log_client_action_summary(route_name, client_action_id, meta=None):
    payload = dict(meta or {})
    payload.update(build_request_trace_summary())
    if client_action_id:
        payload['client_action_id'] = client_action_id
    print(f"{route_name} summary={json.dumps(payload, ensure_ascii=False)}")
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
SPEECH_TO_TEXT_MODEL = os.environ.get(
    'OPENROUTER_STT_MODEL',
    'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free'
).strip()
if SPEECH_TO_TEXT_MODEL.lower() in ('openai/whisper-large-v3', 'whisper-large-v3'):
    SPEECH_TO_TEXT_MODEL = 'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free'
elif SPEECH_TO_TEXT_MODEL and ':' not in SPEECH_TO_TEXT_MODEL:
    SPEECH_TO_TEXT_MODEL = f"{SPEECH_TO_TEXT_MODEL}:free"
elif SPEECH_TO_TEXT_MODEL and not SPEECH_TO_TEXT_MODEL.lower().endswith(':free'):
    SPEECH_TO_TEXT_MODEL = SPEECH_TO_TEXT_MODEL.rsplit(':', 1)[0] + ':free'
SPEECH_TO_TEXT_LANGUAGE = os.environ.get('SPEECH_TO_TEXT_LANGUAGE', 'zh')
USE_OPENROUTER_LLM = os.environ.get('USE_OPENROUTER_LLM', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
DISABLE_OPENROUTER_STT = os.environ.get('DISABLE_OPENROUTER_STT', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
ENABLE_OPENROUTER_STT = USE_OPENROUTER_LLM and not DISABLE_OPENROUTER_STT
OPENROUTER_ALLOW_PROVIDER_FALLBACKS = os.environ.get('OPENROUTER_ALLOW_PROVIDER_FALLBACKS', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
OPENROUTER_CHAT_TIMEOUT_SECONDS = float(os.environ.get('OPENROUTER_CHAT_TIMEOUT_SECONDS', '8'))
OPENROUTER_STT_TIMEOUT_SECONDS = float(os.environ.get('OPENROUTER_STT_TIMEOUT_SECONDS', '20'))
OPENROUTER_MAX_MODEL_ATTEMPTS = max(1, int(os.environ.get('OPENROUTER_MAX_MODEL_ATTEMPTS', '4')))
ENABLE_PACKAGED_NAME_REMOTE_REFINEMENT = os.environ.get('ENABLE_PACKAGED_NAME_REMOTE_REFINEMENT', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
OPENROUTER_PROVIDER_SORT = os.environ.get('OPENROUTER_PROVIDER_SORT', 'latency').strip().lower()
if OPENROUTER_PROVIDER_SORT not in ('latency', 'throughput', 'price'):
    OPENROUTER_PROVIDER_SORT = 'latency'

MSG_AUDIO_FILE_MISSING = "\u6ca1\u6709\u627e\u5230\u8bed\u97f3\u6587\u4ef6"
MSG_AUDIO_FILE_EMPTY_NAME = "\u8bed\u97f3\u6587\u4ef6\u540d\u4e3a\u7a7a"
MSG_AUDIO_FILE_TOO_SMALL = "\u97f3\u9891\u6587\u4ef6\u8fc7\u5c0f\u6216\u65e0\u6548"
MSG_STT_UNAVAILABLE = "\u5f53\u524d\u8bed\u97f3\u8f6c\u5199\u670d\u52a1\u6682\u65f6\u4e0d\u53ef\u7528\uff0c\u53ef\u76f4\u63a5\u6539\u7528\u6587\u5b57\u8bb0\u5f55\u3002"
MSG_VOICE_ANALYSIS_INCOMPLETE = "\u6ca1\u80fd\u7a33\u5b9a\u62c6\u5206\u51fa\u5177\u4f53\u98df\u7269\u6216\u8fd0\u52a8\uff0c\u8bf7\u6362\u4e00\u79cd\u66f4\u77ed\u3001\u66f4\u76f4\u63a5\u7684\u8bf4\u6cd5\u518d\u8bd5\u3002"
MSG_VOICE_AUDIO_FAILED = "\u8bed\u97f3\u5206\u6790\u6682\u65f6\u4e0d\u53ef\u7528\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5\u3002"

def parse_model_list(value, default_models):
    models = [m.strip() for m in (value or '').split(',') if m.strip()]
    return models or default_models

def parse_timeout_list(value, default_timeouts):
    if not value:
        return list(default_timeouts)
    parsed = []
    for part in str(value).split(','):
        part = part.strip()
        if not part:
            continue
        try:
            parsed.append(float(part))
        except ValueError:
            continue
    return parsed or list(default_timeouts)

def fit_timeouts_to_deadline(timeouts, deadline, minimum_slot=0.5):
    remaining = max(0.0, float(deadline or 0))
    fitted = []
    for raw_value in list(timeouts or []):
        if remaining <= 0:
            break
        value = max(float(raw_value), minimum_slot)
        slot = min(value, remaining)
        fitted.append(slot)
        remaining -= slot
    return fitted


def capped_attempt_count(models, timeouts, configured_cap=None):
    model_count = len(list(models or []))
    timeout_count = len(list(timeouts or []))
    available = model_count
    if timeout_count:
        available = min(available, timeout_count)
    cap = configured_cap if configured_cap is not None else OPENROUTER_MAX_MODEL_ATTEMPTS
    try:
        cap = int(cap)
    except Exception:
        cap = OPENROUTER_MAX_MODEL_ATTEMPTS
    return max(1, min(max(1, cap), max(1, available)))


class NonRetryableLLMError(Exception):
    """Raised when a request is malformed and should not fall through the retry chain."""


class LLMChainExhaustedError(Exception):
    """Raised when a provider/model chain is exhausted without a usable result."""

    def __init__(self, message, fallback_trace=None, last_error=None):
        super().__init__(message)
        self.fallback_trace = list(fallback_trace or [])
        self.last_error = last_error


def fallback_trace_indicates_upstream_unavailable(fallback_trace):
    trace = [str(item or '') for item in (fallback_trace or [])]
    if not trace:
        return False
    if any(item.endswith(':ok') for item in trace):
        return False
    unavailable_markers = (
        ':timeout',
        ':quota',
        ':http_429',
        ':http_500',
        ':http_502',
        ':http_503',
        ':http_504',
        ':error',
    )
    return any(marker in item for item in trace for marker in unavailable_markers)

def normalize_openrouter_free_model_name(model_name):
    model = str(model_name or '').strip()
    if not model:
        return ''
    if ':' in model:
        base, suffix = model.rsplit(':', 1)
        if suffix.lower() == 'free':
            return model
        model = base
    normalized = model + ':free'
    if normalized != model_name:
        print(f"Normalized OpenRouter free model alias: {model_name} -> {normalized}")
    return normalized

def enforce_openrouter_free_model_chain(models):
    normalized_models = []
    for raw_model in list(models or []):
        normalized = normalize_openrouter_free_model_name(raw_model)
        if normalized:
            normalized_models.append(normalized)
    return normalized_models

def openrouter_provider_config():
    return {
        "sort": OPENROUTER_PROVIDER_SORT,
        "allow_fallbacks": OPENROUTER_ALLOW_PROVIDER_FALLBACKS,
        "data_collection": "allow",
    }

OPENROUTER_NUTRITION_MODELS = enforce_openrouter_free_model_chain(parse_model_list(os.environ.get('OPENROUTER_NUTRITION_MODELS'), [
    'deepseek/deepseek-v4-flash:free',
    'qwen/qwen3-next-80b-a3b-instruct:free',
    'minimax/minimax-m2.5:free',
]))
OPENROUTER_NUTRITION_TIMEOUTS = parse_timeout_list(
    os.environ.get('OPENROUTER_NUTRITION_TIMEOUTS'),
    [5, 5, 5]
)
OPENROUTER_NUTRITION_HARD_DEADLINE_SECONDS = float(
    os.environ.get('OPENROUTER_NUTRITION_HARD_DEADLINE_SECONDS', '15')
)

OPENROUTER_TEXT_MODELS = enforce_openrouter_free_model_chain(parse_model_list(os.environ.get('OPENROUTER_TEXT_MODELS'), [
    'deepseek/deepseek-v4-flash:free',
    'qwen/qwen3-next-80b-a3b-instruct:free',
    'minimax/minimax-m2.5:free',
]))
OPENROUTER_TEXT_TIMEOUTS = parse_timeout_list(
    os.environ.get('OPENROUTER_TEXT_TIMEOUTS'),
    [5, 5, 5]
)
OPENROUTER_TEXT_HARD_DEADLINE_SECONDS = float(
    os.environ.get('OPENROUTER_TEXT_HARD_DEADLINE_SECONDS', '15')
)

OPENROUTER_VISION_MODELS = enforce_openrouter_free_model_chain(parse_model_list(os.environ.get('OPENROUTER_VISION_MODELS'), [
    'google/gemma-4-31b-it:free',
    'google/gemma-4-26b-a4b-it:free',
    'nvidia/nemotron-nano-12b-v2-vl:free',
    'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free',
]))
OPENROUTER_VISION_TIMEOUTS = parse_timeout_list(
    os.environ.get('OPENROUTER_VISION_TIMEOUTS'),
    [8, 6, 6, 6]
)
OPENROUTER_VISION_HARD_DEADLINE_SECONDS = float(
    os.environ.get('OPENROUTER_VISION_HARD_DEADLINE_SECONDS', '26')
)

def truthy_env(name, default='false'):
    return os.environ.get(name, default).strip().lower() in ('1', 'true', 'yes', 'on')

def normalize_openai_compat_base_url(base_url):
    base = (base_url or '').strip().rstrip('/')
    if not base:
        return ''
    parsed = urlparse(base)
    if not parsed.path or parsed.path == '/':
        return base + '/v1'
    return base

OPENAI_COMPAT_API_KEY = os.environ.get('OPENAI_COMPAT_API_KEY') or os.environ.get('OPENAI_API_KEY')
OPENAI_COMPAT_BASE_URL = normalize_openai_compat_base_url(
    os.environ.get('OPENAI_COMPAT_BASE_URL') or os.environ.get('OPENAI_BASE_URL')
)
PREMIUM_AI_ENABLED = truthy_env('PREMIUM_AI_ENABLED', 'true')
PREMIUM_AI_TIMEOUT_SECONDS = float(os.environ.get('PREMIUM_AI_TIMEOUT_SECONDS', '6'))
PREMIUM_AI_MAX_MODEL_ATTEMPTS = max(1, int(os.environ.get('PREMIUM_AI_MAX_MODEL_ATTEMPTS', '1')))
PREMIUM_ADMIN_TOKEN = os.environ.get('PREMIUM_ADMIN_TOKEN', '')
ACTIVATION_CODE_SECRET = os.environ.get('ACTIVATION_CODE_SECRET') or JWT_SECRET_KEY

PREMIUM_TEXT_MODELS = parse_model_list(os.environ.get('PREMIUM_TEXT_MODELS'), [
    'gpt-5.2',
])
PREMIUM_NUTRITION_MODELS = parse_model_list(os.environ.get('PREMIUM_NUTRITION_MODELS'), PREMIUM_TEXT_MODELS)
PREMIUM_VISION_MODELS = parse_model_list(os.environ.get('PREMIUM_VISION_MODELS'), [
    'gpt-5.2',
])
GOOGLE_FALLBACK_MODELS = parse_model_list(os.environ.get('GOOGLE_FALLBACK_MODELS'), [
    'gemini-2.5-flash-lite',
    'gemini-2.5-flash',
    'gemini-2.0-flash',
])
GOOGLE_FALLBACK_TIMEOUTS = parse_timeout_list(
    os.environ.get('GOOGLE_FALLBACK_TIMEOUTS'),
    [6, 6, 6]
)
PROVIDER_COOLDOWN_SECONDS = {
    'openrouter': max(30, int(os.environ.get('OPENROUTER_PROVIDER_COOLDOWN_SECONDS', '900'))),
    'google': max(30, int(os.environ.get('GOOGLE_PROVIDER_COOLDOWN_SECONDS', '900'))),
    'premium': max(15, int(os.environ.get('PREMIUM_PROVIDER_COOLDOWN_SECONDS', '180'))),
}
PROVIDER_FAILURE_STATE = {}

def premium_provider_available():
    return bool(PREMIUM_AI_ENABLED and OPENAI_COMPAT_API_KEY and OPENAI_COMPAT_BASE_URL)


def provider_cooldown_info(provider_name):
    info = PROVIDER_FAILURE_STATE.get(provider_name)
    if not info:
        return None
    if float(info.get('until') or 0) <= time.time():
        PROVIDER_FAILURE_STATE.pop(provider_name, None)
        return None
    return info


def mark_provider_cooldown(provider_name, seconds, reason=''):
    PROVIDER_FAILURE_STATE[provider_name] = {
        'until': time.time() + max(1, float(seconds or 1)),
        'reason': (reason or '').strip()[:300],
    }


def clear_provider_cooldown(provider_name):
    PROVIDER_FAILURE_STATE.pop(provider_name, None)


def parse_openrouter_reset_seconds(response, error_msg=''):
    header_candidates = [
        response.headers.get('X-RateLimit-Reset'),
        response.headers.get('x-ratelimit-reset'),
    ]
    for raw_value in header_candidates:
        if not raw_value:
            continue
        try:
            reset_value = float(str(raw_value).strip())
        except ValueError:
            continue
        now = time.time()
        # OpenRouter may return epoch milliseconds.
        if reset_value > 10_000_000_000:
            return max(30, int((reset_value / 1000.0) - now))
        # Or epoch seconds.
        if reset_value > now + 5:
            return max(30, int(reset_value - now))
        # Or relative seconds.
        if reset_value > 0:
            return max(30, int(reset_value))

    lowered = str(error_msg or '').lower()
    if 'free-models-per-day' in lowered:
        return 60 * 30
    return PROVIDER_COOLDOWN_SECONDS['openrouter']


def is_openrouter_free_tier_request_cap(response, error_msg=''):
    if not response or int(getattr(response, 'status_code', 0) or 0) != 429:
        return False
    lowered = str(error_msg or '').lower()
    return 'free-models-per-day' in lowered


def parse_google_retry_seconds(res_json=None, error_msg=''):
    error_info = (res_json or {}).get('error') if isinstance(res_json, dict) else None
    details = error_info.get('details') if isinstance(error_info, dict) else None
    for detail in details or []:
        if not isinstance(detail, dict):
            continue
        retry_delay = str(detail.get('retryDelay') or '').strip().lower()
        match = re.match(r'^([0-9.]+)s$', retry_delay)
        if match:
            return max(1, int(float(match.group(1))) + 1)

    match = re.search(r'retry\s+in\s+([0-9.]+)s', str(error_msg or ''), re.IGNORECASE)
    if match:
        return max(1, int(float(match.group(1))) + 1)
    return min(PROVIDER_COOLDOWN_SECONDS['google'], 60)


def is_google_quota_or_rate_limit(status_code, error_msg=''):
    lowered = str(error_msg or '').lower()
    return (
        int(status_code or 0) == 429
        or 'resource_exhausted' in lowered
        or 'quota exceeded' in lowered
        or 'rate limit' in lowered
        or 'please retry in' in lowered
    )

if not GEMINI_API_KEY and not OPENROUTER_API_KEY and not OPENAI_COMPAT_API_KEY:
    raise ValueError("GEMINI_API_KEY 或 OPENROUTER_API_KEY 环境变量未设置！请在本地 .env 中配置后重启应用。")

client = None
if GEMINI_API_KEY:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"初始化 Google GenAI client 失败: {e}")

def normalize_openai_input_audio_format(mime_type):
    raw = (mime_type or '').split(';', 1)[0].strip().lower()
    mapping = {
        'audio/mpeg': 'mp3',
        'audio/mp3': 'mp3',
        'audio/wav': 'wav',
        'audio/x-wav': 'wav',
        'audio/mp4': 'mp4',
        'audio/m4a': 'mp4',
        'audio/x-m4a': 'mp4',
        'audio/webm': 'webm',
        'audio/ogg': 'ogg',
    }
    if raw in mapping:
        return mapping[raw]
    if '/' in raw:
        return raw.rsplit('/', 1)[-1] or 'webm'
    return raw or 'webm'


def build_openai_chat_messages(prompt_text, image=None, audio=None, mime_type=None, history=None, system_instruction=None):
    messages = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})

    if history:
        for h in history:
            role = 'user' if h.get('role') == 'user' else 'assistant'
            content = h.get('content', '')
            if isinstance(content, list):
                text_parts = [
                    item.get('text', '')
                    for item in content
                    if isinstance(item, dict) and item.get('type') == 'text'
                ]
                content = ' '.join(text_parts) if text_parts else ''
            messages.append({"role": role, "content": content})

    user_content = []
    if prompt_text:
        user_content.append({"type": "text", "text": prompt_text})

    if image:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG")
        img_b64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
        })

    if audio:
        user_content.append({
            "type": "input_audio",
            "input_audio": {
                "data": base64.b64encode(audio).decode('utf-8'),
                "format": normalize_openai_input_audio_format(mime_type),
            }
        })

    if user_content:
        messages.append({"role": "user", "content": user_content})

    return messages


def normalize_google_inline_mime_type(mime_type, default='audio/webm'):
    raw = (mime_type or '').split(';', 1)[0].strip().lower()
    if not raw:
        return default
    mapping = {
        'audio/webm': 'audio/webm',
        'audio/mp4': 'audio/mp4',
        'audio/m4a': 'audio/mp4',
        'audio/x-m4a': 'audio/mp4',
        'audio/mpeg': 'audio/mpeg',
        'audio/mp3': 'audio/mpeg',
        'audio/wav': 'audio/wav',
        'audio/x-wav': 'audio/wav',
        'audio/ogg': 'audio/ogg',
        'audio/oga': 'audio/ogg',
        'audio/flac': 'audio/flac',
        'audio/aac': 'audio/aac',
        'audio/aiff': 'audio/aiff',
    }
    return mapping.get(raw, raw)


def build_google_contents(prompt_text, image=None, audio=None, mime_type=None, history=None):
    contents = []
    if history:
        for item in history:
            role = 'user' if item.get('role') == 'user' else 'model'
            content = item.get('content', '')
            if isinstance(content, list):
                content = ' '.join(
                    part.get('text', '')
                    for part in content
                    if isinstance(part, dict) and part.get('type') == 'text'
                )
            contents.append({
                'role': role,
                'parts': [{'text': str(content or '')}],
            })

    user_parts = []
    if prompt_text:
        user_parts.append({'text': prompt_text})
    if image is not None:
        image_buffer = io.BytesIO()
        image.save(image_buffer, format='JPEG')
        user_parts.append({
            'inline_data': {
                'mime_type': 'image/jpeg',
                'data': base64.b64encode(image_buffer.getvalue()).decode('utf-8'),
            }
        })
    if audio:
        user_parts.append({
            'inline_data': {
                'mime_type': normalize_google_inline_mime_type(mime_type, default='audio/webm'),
                'data': base64.b64encode(audio).decode('utf-8'),
            }
        })

    if user_parts:
        contents.append({'role': 'user', 'parts': user_parts})
    return contents


def extract_google_response_text(res_json):
    candidates = res_json.get('candidates') if isinstance(res_json, dict) else None
    if not candidates:
        return ''
    for candidate in candidates:
        content = candidate.get('content') or {}
        for part in content.get('parts') or []:
            text = (part.get('text') or '').strip()
            if text:
                return clean_ai_text(text)
    return ''


def call_google_generate_content(
    prompt_text,
    image=None,
    audio=None,
    mime_type=None,
    history=None,
    system_instruction=None,
    temperature=0.7,
    return_metadata=False,
    fallback_trace=None,
):
    if not GEMINI_API_KEY:
        raise RuntimeError("Google Gemini API key is not configured.")

    cooldown_info = provider_cooldown_info('google')
    if cooldown_info:
        reason = cooldown_info.get('reason') or 'cooldown active'
        raise RuntimeError(f"Google provider cooldown active: {reason}")

    payload = {
        'contents': build_google_contents(
            prompt_text=prompt_text,
            image=image,
            audio=audio,
            mime_type=mime_type,
            history=history,
        ),
        'generationConfig': {
            'temperature': temperature,
        },
    }
    if system_instruction:
        payload['system_instruction'] = {
            'parts': [{'text': system_instruction}]
        }

    timeout_schedule = list(GOOGLE_FALLBACK_TIMEOUTS or [])
    models_to_try = GOOGLE_FALLBACK_MODELS[:len(timeout_schedule) or len(GOOGLE_FALLBACK_MODELS)]
    google_trace = list(fallback_trace or [])
    last_error = None

    for index, model in enumerate(models_to_try):
        model_timeout = timeout_schedule[index] if index < len(timeout_schedule) else OPENROUTER_CHAT_TIMEOUT_SECONDS
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
        try:
            print(f"Calling Google REST model: {model} (timeout={model_timeout}s)")
            started_at = time.perf_counter()
            response = requests.post(url, json=payload, timeout=model_timeout)
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            try:
                res_json = response.json()
            except Exception:
                res_json = {}

            if response.status_code == 200:
                reply = extract_google_response_text(res_json)
                if not reply:
                    raise RuntimeError(f"Google model {model} returned empty content")
                clear_provider_cooldown('google')
                google_trace.append(f"google:{model}:ok")
                record_upstream_call('google', model, 'ok', latency_ms)
                if return_metadata:
                    return {
                        "text": reply,
                        "provider": "google",
                        "model": model,
                        "latency_ms": latency_ms,
                        "fallback_trace": google_trace,
                    }
                return reply

            error_info = res_json.get('error') if isinstance(res_json, dict) else None
            error_msg = error_info.get('message') if isinstance(error_info, dict) else ''
            error_msg = error_msg or (response.text or '').strip()[:500] or 'unknown'
            lower_error = error_msg.lower()
            if is_google_quota_or_rate_limit(response.status_code, error_msg):
                cooldown_seconds = parse_google_retry_seconds(res_json, error_msg)
                mark_provider_cooldown('google', cooldown_seconds, error_msg)
                google_trace.append(f"google:{model}:quota")
                record_upstream_call('google', model, 'quota', latency_ms, error_msg)
                last_error = RuntimeError(f"Google quota/auth issue on {model}: {error_msg}")
                continue
            if response.status_code in (401, 403):
                mark_provider_cooldown('google', PROVIDER_COOLDOWN_SECONDS['google'], error_msg)
                google_trace.append(f"google:{model}:auth")
                record_upstream_call('google', model, 'auth', latency_ms, error_msg)
                last_error = RuntimeError(f"Google auth issue on {model}: {error_msg}")
                break

            google_trace.append(f"google:{model}:http_{response.status_code}")
            record_upstream_call('google', model, f"http_{response.status_code}", latency_ms, error_msg)
            last_error = RuntimeError(f"Google error {response.status_code} on {model}: {error_msg}")
        except requests.exceptions.Timeout as timeout_err:
            google_trace.append(f"google:{model}:timeout")
            record_upstream_call('google', model, 'timeout', 0, timeout_err)
            last_error = timeout_err
        except Exception as api_err:
            google_trace.append(f"google:{model}:error")
            record_upstream_call('google', model, 'error', 0, api_err)
            last_error = api_err

    raise last_error or RuntimeError("Google REST request failed.")

def extract_chat_completion_text(res_json):
    choices = res_json.get('choices') if isinstance(res_json, dict) else None
    if not choices:
        return ''
    message = choices[0].get('message', {}) if isinstance(choices[0], dict) else {}
    reply = message.get('content', '')
    if isinstance(reply, list):
        reply = ''.join(
            part.get('text', '')
            for part in reply
            if isinstance(part, dict) and part.get('type') in ('text', 'output_text')
        )
    return clean_ai_text(reply)

def call_openai_compatible_llm(
    prompt_text,
    image=None,
    audio=None,
    mime_type=None,
    history=None,
    system_instruction=None,
    temperature=0.7,
    models=None,
    timeout=None,
    return_metadata=False,
):
    if not premium_provider_available():
        raise RuntimeError("Premium OpenAI-compatible provider is not configured.")
    cooldown_info = provider_cooldown_info('premium')
    if cooldown_info:
        reason = cooldown_info.get('reason') or 'cooldown active'
        raise RuntimeError(f"Premium provider cooldown active: {reason}")

    url = OPENAI_COMPAT_BASE_URL.rstrip('/') + '/chat/completions'
    headers = {
        "Authorization": f"Bearer {OPENAI_COMPAT_API_KEY}",
        "Content-Type": "application/json",
    }
    messages = build_openai_chat_messages(
        prompt_text=prompt_text,
        image=image,
        audio=audio,
        mime_type=mime_type,
        history=history,
        system_instruction=system_instruction,
    )
    default_models = PREMIUM_VISION_MODELS if (image is not None or audio is not None) else PREMIUM_TEXT_MODELS
    models_to_try = (models or default_models)[:PREMIUM_AI_MAX_MODEL_ATTEMPTS]
    timeout_seconds = timeout or PREMIUM_AI_TIMEOUT_SECONDS
    last_error = None

    for model in models_to_try:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        try:
            print(f"Calling premium OpenAI-compatible model: {model}")
            started_at = time.perf_counter()
            res = requests.post(url, headers=headers, json=payload, timeout=timeout_seconds)
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            try:
                res_json = res.json()
            except Exception:
                res_json = {}

            if res.status_code == 200:
                reply = extract_chat_completion_text(res_json)
                if not reply:
                    raise RuntimeError(f"Premium model {model} returned empty content")
                clear_provider_cooldown('premium')
                print(f"Success with premium OpenAI-compatible model: {model}")
                record_upstream_call('openai_compatible', model, 'ok', latency_ms)
                if return_metadata:
                    return {
                        "text": reply,
                        "provider": "openai_compatible",
                        "model": model,
                        "latency_ms": latency_ms,
                        "fallback_trace": [f"premium:{model}:ok"],
                    }
                return reply

            error_msg = res_json.get('error', {}).get('message') if isinstance(res_json.get('error'), dict) else ''
            error_msg = error_msg or res.text[:500]
            record_upstream_call('openai_compatible', model, f"http_{res.status_code}", latency_ms, error_msg)
            last_error = RuntimeError(f"Premium OpenAI-compatible error {res.status_code}: {error_msg}")
            if res.status_code >= 500:
                mark_provider_cooldown('premium', PROVIDER_COOLDOWN_SECONDS['premium'], error_msg)
            print(f"Premium model {model} failed: {error_msg}")
        except Exception as e:
            print(f"Premium OpenAI-compatible request error with model {model}: {e}")
            record_upstream_call('openai_compatible', model, 'error', 0, e)
            last_error = e

    raise last_error or RuntimeError("Premium OpenAI-compatible request failed.")

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
    openrouter_timeouts=None,
    premium=False,
    premium_models=None,
    premium_timeout=None,
    allow_google_fallback=True,
    openrouter_fallback_only_on_timeout=False,
    return_metadata=False,
):
    """
    Unified interface to call either OpenRouter API (if OPENROUTER_API_KEY is configured)
    or fall back to official Google Gemini API (using client.models.generate_content).
    """
    fallback_trace = []
    if preferred_provider == 'google':
        if GEMINI_API_KEY and not provider_cooldown_info('google'):
            return call_google_generate_content(
                prompt_text=prompt_text,
                image=image,
                audio=audio,
                mime_type=mime_type,
                history=history,
                system_instruction=system_instruction,
                temperature=temperature,
                return_metadata=return_metadata,
                fallback_trace=(fallback_trace + (["audio:google_direct"] if audio is not None else [])),
            )
        raise LLMChainExhaustedError(
            "Google provider explicitly requested but unavailable.",
            fallback_trace=(fallback_trace + ["google:unavailable"]),
            last_error=RuntimeError("Google provider unavailable or cooling down."),
        )

    if premium and preferred_provider != 'google' and premium_provider_available():
        try:
            return call_openai_compatible_llm(
                prompt_text=prompt_text,
                image=image,
                audio=audio,
                mime_type=mime_type,
                history=history,
                system_instruction=system_instruction,
                temperature=temperature,
                models=premium_models,
                timeout=premium_timeout,
                return_metadata=return_metadata,
            )
        except Exception as premium_err:
            print(f"Premium provider failed, falling back to free chain: {premium_err}")
            fallback_trace.append("premium:error")

    use_openrouter = (
        USE_OPENROUTER_LLM
        and bool(OPENROUTER_API_KEY)
        and preferred_provider != 'google'
        and audio is None
        and not provider_cooldown_info('openrouter')
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
        models_to_try = enforce_openrouter_free_model_chain(openrouter_models or default_models)[:attempt_limit]
        timeout_schedule = list(openrouter_timeouts or [])
        fallback_only_on_timeout = bool(openrouter_fallback_only_on_timeout)

        last_error = None
        for idx, model in enumerate(models_to_try):
            if not str(model).lower().endswith(':free'):
                error_msg = f"Blocked non-free OpenRouter model in free chain: {model}"
                print(error_msg)
                fallback_trace.append(f"{model}:blocked_non_free")
                last_error = NonRetryableLLMError(error_msg)
                break
            model_timeout = timeout_schedule[idx] if idx < len(timeout_schedule) else timeout_seconds
            payload = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "provider": openrouter_provider_config()
            }
            try:
                print(f"Calling OpenRouter model: {model} (timeout={model_timeout}s)")
                started_at = time.perf_counter()
                res = requests.post(url, headers=headers, json=payload, timeout=model_timeout)
                latency_ms = int((time.perf_counter() - started_at) * 1000)
                try:
                    res_json = res.json()
                except Exception:
                    res_json = {}
                if res.status_code == 200 and 'choices' in res_json:
                    message = res_json['choices'][0].get('message', {})
                    reply = message.get('content', '')
                    if isinstance(reply, list):
                        reply = ''.join(
                            part.get('text', '') for part in reply
                            if isinstance(part, dict) and part.get('type') in ('text', 'output_text')
                        )
                    reply = clean_ai_text(reply)
                    if not reply:
                        raise RuntimeError(f"OpenRouter model {model} returned empty content")
                    print(f"Success with OpenRouter model: {model}")
                    fallback_trace.append(f"{model}:ok")
                    record_upstream_call('openrouter', model, 'ok', latency_ms)
                    if return_metadata:
                        return {
                            "text": reply,
                            "provider": "openrouter",
                            "model": model,
                            "latency_ms": latency_ms,
                            "fallback_trace": fallback_trace,
                        }
                    return reply
                else:
                    error_info = res_json.get('error') if isinstance(res_json, dict) else None
                    if isinstance(error_info, dict):
                        error_msg = error_info.get('message') or str(error_info)
                    elif isinstance(error_info, str):
                        error_msg = error_info
                    else:
                        error_msg = (res.text or '').strip()[:500] or 'unknown'
                    status_label = f"http_{res.status_code}"
                    print(f"OpenRouter model {model} failed: status={res.status_code} error={error_msg}")
                    if res.status_code == 429:
                        fallback_trace.append(f"{model}:http_429")
                        record_upstream_call('openrouter', model, 'http_429', latency_ms, error_msg)
                        if is_openrouter_free_tier_request_cap(res, error_msg):
                            cooldown_seconds = parse_openrouter_reset_seconds(res, error_msg)
                            if cooldown_seconds >= 300:
                                mark_provider_cooldown('openrouter', cooldown_seconds, error_msg)
                            print(
                                f"OpenRouter free-tier request cap hit on {model}; "
                                f"cooldown={cooldown_seconds}s error={error_msg}"
                            )
                        last_error = RuntimeError(f"OpenRouter rate limit on {model}: {error_msg}")
                        continue
                    if res.status_code in (401, 402, 403) or 'user not found' in str(error_msg).lower():
                        mark_provider_cooldown('openrouter', PROVIDER_COOLDOWN_SECONDS['openrouter'], error_msg)
                        fallback_trace.append(f"{model}:{status_label}")
                        record_upstream_call('openrouter', model, status_label, latency_ms, error_msg)
                        last_error = RuntimeError(f"OpenRouter auth/billing error {res.status_code} on {model}: {error_msg}")
                        break
                    if res.status_code == 400:
                        fallback_trace.append(f"{model}:http_400")
                        record_upstream_call('openrouter', model, 'http_400', latency_ms, error_msg)
                        last_error = NonRetryableLLMError(f"OpenRouter bad request on {model}: {error_msg}")
                        continue
                    if is_openrouter_free_tier_request_cap(res, error_msg):
                        fallback_trace.append(f"{model}:{status_label}")
                        record_upstream_call('openrouter', model, status_label, latency_ms, error_msg)
                        cooldown_seconds = parse_openrouter_reset_seconds(res, error_msg)
                        if cooldown_seconds >= 300:
                            mark_provider_cooldown('openrouter', cooldown_seconds, error_msg)
                        print(
                            f"OpenRouter free-tier request cap hit on {model}; "
                            f"cooldown={cooldown_seconds}s error={error_msg}"
                        )
                        last_error = Exception(f"OpenRouter rate limit: {error_msg}")
                        continue
                    fallback_trace.append(f"{model}:{status_label}")
                    record_upstream_call('openrouter', model, status_label, latency_ms, error_msg)
                    last_error = Exception(f"OpenRouter error: {error_msg}")
                    if fallback_only_on_timeout:
                        break
            except requests.exceptions.Timeout as e:
                print(f"OpenRouter timeout with model {model}: {e}")
                fallback_trace.append(f"{model}:timeout")
                record_upstream_call('openrouter', model, 'timeout', 0, e)
                last_error = e
            except Exception as e:
                print(f"OpenRouter network/request error with model {model}: {e}")
                if not any(str(item).startswith(f"{model}:") for item in fallback_trace):
                    fallback_trace.append(f"{model}:error")
                record_upstream_call('openrouter', model, 'error', 0, e)
                last_error = e
                if fallback_only_on_timeout:
                    break
                
        if GEMINI_API_KEY and allow_google_fallback and not provider_cooldown_info('google'):
            print(f"OpenRouter models failed, falling back to Google REST: {last_error}")
            return call_google_generate_content(
                prompt_text=prompt_text,
                image=image,
                audio=audio,
                mime_type=mime_type,
                history=history,
                system_instruction=system_instruction,
                temperature=temperature,
                return_metadata=return_metadata,
                fallback_trace=fallback_trace,
            )
        else:
            raise LLMChainExhaustedError(
                "OpenRouter model chain exhausted.",
                fallback_trace=fallback_trace,
                last_error=last_error,
            )

    if GEMINI_API_KEY and allow_google_fallback and not provider_cooldown_info('google'):
        print("OpenRouter unavailable or disabled, using Google REST fallback.")
        return call_google_generate_content(
            prompt_text=prompt_text,
            image=image,
            audio=audio,
            mime_type=mime_type,
            history=history,
            system_instruction=system_instruction,
            temperature=temperature,
            return_metadata=return_metadata,
            fallback_trace=fallback_trace,
        )

    exhausted_trace = list(fallback_trace)
    if not OPENROUTER_API_KEY:
        exhausted_trace.append("openrouter:not_configured")
    elif provider_cooldown_info('openrouter'):
        exhausted_trace.append("openrouter:cooldown")
    if not GEMINI_API_KEY:
        exhausted_trace.append("google:not_configured")
    elif provider_cooldown_info('google'):
        exhausted_trace.append("google:cooldown")
    raise LLMChainExhaustedError(
        "LLM provider selection exhausted before producing a response.",
        fallback_trace=exhausted_trace,
        last_error=RuntimeError("No available LLM provider."),
    )


def get_text_chain_models():
    timeouts = fit_timeouts_to_deadline(
        OPENROUTER_TEXT_TIMEOUTS,
        OPENROUTER_TEXT_HARD_DEADLINE_SECONDS
    )
    models = OPENROUTER_TEXT_MODELS[:len(timeouts) or len(OPENROUTER_TEXT_MODELS)]
    return models, timeouts


def get_vision_chain_models():
    timeouts = fit_timeouts_to_deadline(
        OPENROUTER_VISION_TIMEOUTS,
        OPENROUTER_VISION_HARD_DEADLINE_SECONDS
    )
    models = OPENROUTER_VISION_MODELS[:len(timeouts) or len(OPENROUTER_VISION_MODELS)]
    return models, timeouts


def call_text_reasoning_llm(
    prompt_text,
    use_premium=False,
    history=None,
    system_instruction=None,
    temperature=0.2,
    return_metadata=False,
):
    text_models, text_timeouts = get_text_chain_models()
    attempt_cap = capped_attempt_count(text_models, text_timeouts)
    return call_llm(
        prompt_text=prompt_text,
        history=history,
        system_instruction=system_instruction,
        temperature=temperature,
        openrouter_models=text_models,
        openrouter_max_attempts=attempt_cap,
        openrouter_timeout=text_timeouts[0] if text_timeouts else OPENROUTER_CHAT_TIMEOUT_SECONDS,
        openrouter_timeouts=text_timeouts,
        premium=use_premium,
        premium_models=PREMIUM_TEXT_MODELS,
        premium_timeout=PREMIUM_AI_TIMEOUT_SECONDS,
        allow_google_fallback=True,
        openrouter_fallback_only_on_timeout=False,
        return_metadata=return_metadata,
    )


def build_voice_audio_direct_prompt():
    return """You are the direct audio understanding layer for NutriSnap, a Chinese food logging app.
Listen to the uploaded voice note and directly produce structured food and exercise records.
First understand what was actually said, then split only clearly audible consumed items and estimate nutrition.

Rules:
1. Return strict JSON only. No markdown. No explanation.
2. Include "transcription" with your best Chinese transcript for user confirmation.
3. Extract every clearly audible food or drink as a separate item.
4. Extract exercise separately only if exercise is clearly audible.
5. Estimate realistic calories, protein, carbs, fat, and edible grams for each food.
6. Respect modifiers such as 无糖, 去皮, 低脂, 大杯, 半个, 冰, 热, 奥尔良, 真空包装.
7. "无糖" means no added sugar. It does not mean zero calories for fruit juice, milk drinks, yogurt, latte, soy milk, or caloric beverages.
8. Only plain water, soda water, plain unsweetened tea, and black coffee should be near zero calories.
9. Never copy the whole sentence as a food_name.
10. If multiple foods are mentioned, do not merge them.
11. Never invent likely side dishes, exercises, quantities, or modifiers that were not audible.
12. Never copy or adapt foods, drinks, or exercises from these instructions into the output.
13. If the audio contains only one short food or drink phrase, output exactly one food item and no exercise.
14. If the audio is unclear or you are not confident which item was spoken, return the best transcription but empty foods/exercises arrays.
15. Do not use previous app state, nutrition expectations, or common meal patterns to fill missing items.

Return schema:
{
  "transcription": "用户语音转写文本",
  "type": "food|exercise|mixed",
  "foods": [
    {
      "food_name": "食物名称",
      "calories": 0,
      "protein": 0,
      "carbs": 0,
      "fat": 0,
      "weight": 0,
      "confidence": 0.8
    }
  ],
  "exercises": [
    {
      "exercise_name": "运动名称",
      "duration": 0,
      "calories": 0,
      "exercise_type": "aerobic|strength",
      "target_muscles": "",
      "confidence": 0.8
    }
  ]
}

Safety checks:
- A single short food name must not expand into a full meal.
- A drink modifier must not create unrelated foods or exercise.
- Long conversational wording must never appear as food_name.
"""


def call_voice_audio_direct_llm(audio_bytes, mime_type, use_premium=False, return_metadata=True):
    return call_llm(
        prompt_text=build_voice_audio_direct_prompt(),
        audio=audio_bytes,
        mime_type=mime_type,
        temperature=0.1,
        preferred_provider='auto' if use_premium else 'google',
        premium=use_premium,
        premium_models=PREMIUM_VISION_MODELS,
        premium_timeout=max(PREMIUM_AI_TIMEOUT_SECONDS, 12),
        allow_google_fallback=True,
        return_metadata=return_metadata,
    )


def normalize_direct_voice_audio_result(response):
    if isinstance(response, dict):
        raw_text = response.get('text', '')
    else:
        raw_text = response or ''
    payload = parse_json_payload(raw_text)
    parsed = parse_voice_input_result(json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else raw_text)
    transcription = ''
    if isinstance(payload, dict):
        transcription = clean_text(
            first_present(payload, 'transcription', 'transcript', 'text', default=''),
            default='',
            max_len=MAX_TEXT_INPUT_CHARS,
        )
    return parsed, transcription


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
    cooldown_info = provider_cooldown_info('openrouter')
    if cooldown_info:
        reason = cooldown_info.get('reason') or 'cooldown active'
        raise RuntimeError(f"OpenRouter provider cooldown active: {reason}")
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
            return clean_ai_text(text)
        raise RuntimeError("OpenRouter STT 返回空文本")

    error_msg = None
    if isinstance(res_json.get("error"), dict):
        error_msg = res_json["error"].get("message")
    elif isinstance(res_json.get("error"), str):
        error_msg = res_json.get("error")
    if not error_msg:
        error_msg = (res.text or "").strip()[:500] or 'unknown'
    if res.status_code in (401, 403) or 'user not found' in str(error_msg).lower():
        mark_provider_cooldown('openrouter', PROVIDER_COOLDOWN_SECONDS['openrouter'], error_msg)
    raise RuntimeError(f"OpenRouter STT failed ({res.status_code}): {error_msg}")


def transcribe_audio_with_google(audio_bytes, mime_type):
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured")
    prompt = """请把这段中文录音直接转写成用户原话，面向饮食/运动记录场景。

要求：
1. 只输出转写后的中文文本本身，不要加解释、前缀、总结或 markdown。
2. 保留食物名、品牌名、数量、单位、修饰词和运动时长，例如：无糖、去皮、半个、500毫升、快走40分钟。
3. 不要把“无糖”“去皮”“低脂”“大杯”“半个”“一份”等关键信息省掉。
4. 不要擅自把自然语言改写成营养结论，也不要补充没说过的内容。
5. 可以保留自然停顿后的口语顺序，例如“然后”“后来”“又”。
6. 如果听不清个别词，优先保留上下文最可能的日常中文表达；不要输出占位符。
7. 如果是静音、环境噪音或没有有效说话内容，直接返回空字符串。"""
    result_text = call_llm(
        prompt_text=prompt,
        audio=audio_bytes,
        mime_type=normalize_google_inline_mime_type(mime_type, default='audio/webm'),
        temperature=0,
        preferred_provider='google'
    )
    return (result_text or '').strip()


def transcribe_audio_with_openrouter(audio_bytes, mime_type, filename=''):
    cooldown_info = provider_cooldown_info('openrouter')
    if cooldown_info:
        reason = cooldown_info.get('reason') or 'cooldown active'
        raise RuntimeError(f"OpenRouter provider cooldown active: {reason}")
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY is not configured")

    audio_format = normalize_audio_format(mime_type, filename)
    audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')
    prompt = (
        "请把这段中文录音直接转写成用户原话，场景是饮食和运动记录。"
        "只输出转写文本，不要解释、总结、Markdown 或营养结论。"
        "保留食物名、品牌名、数量、单位、无糖、去皮、半个、大杯、运动时长等关键信息。"
        "如果没有有效说话内容，直接输出空字符串。"
    )
    payload = {
        "model": SPEECH_TO_TEXT_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "input_audio",
                    "input_audio": {
                        "data": audio_b64,
                        "format": audio_format,
                    },
                },
            ],
        }],
        "temperature": 0,
        "provider": openrouter_provider_config(),
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://nutrisnap.ai",
        "X-Title": "NutriSnap AI",
    }

    started_at = time.perf_counter()
    res = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=OPENROUTER_STT_TIMEOUT_SECONDS,
    )
    latency_ms = int((time.perf_counter() - started_at) * 1000)
    try:
        res_json = res.json()
    except Exception:
        res_json = {}

    if res.status_code == 200 and 'choices' in res_json:
        text = extract_chat_completion_text(res_json)
        if text:
            record_upstream_call('openrouter', SPEECH_TO_TEXT_MODEL, 'ok', latency_ms)
            return clean_ai_text(text)
        record_upstream_call('openrouter', SPEECH_TO_TEXT_MODEL, 'empty', latency_ms)
        raise RuntimeError("OpenRouter STT returned empty text")

    error_msg = None
    if isinstance(res_json.get("error"), dict):
        error_msg = res_json["error"].get("message")
    elif isinstance(res_json.get("error"), str):
        error_msg = res_json.get("error")
    if not error_msg:
        error_msg = (res.text or "").strip()[:500] or 'unknown'
    record_upstream_call('openrouter', SPEECH_TO_TEXT_MODEL, f"http_{res.status_code}", latency_ms, error_msg)
    if res.status_code in (401, 403) or 'user not found' in str(error_msg).lower():
        mark_provider_cooldown('openrouter', PROVIDER_COOLDOWN_SECONDS['openrouter'], error_msg)
    raise RuntimeError(f"OpenRouter STT failed ({res.status_code}): {error_msg}")


def run_speech_to_text_pipeline(audio_bytes, mime_type, filename='', client_action_id=''):
    transcription = ""
    errors = []
    stt_meta = {
        "mime_type": mime_type,
        "filename": clean_text(filename, max_len=120),
        "bytes": len(audio_bytes or b''),
        "attempts": [],
    }
    if client_action_id:
        stt_meta["client_action_id"] = client_action_id

    print(
        "Speech-to-text request: "
        f"user={get_current_username()} mime={mime_type} bytes={len(audio_bytes or b'')} filename={stt_meta['filename']}"
    )

    stt_attempts = []
    if GEMINI_API_KEY:
        stt_attempts.append(("google", lambda: transcribe_audio_with_google(audio_bytes, mime_type)))
    if USE_OPENROUTER_LLM and OPENROUTER_API_KEY and ENABLE_OPENROUTER_STT:
        stt_attempts.append(("openrouter", lambda: transcribe_audio_with_openrouter(audio_bytes, mime_type, filename)))

    for provider_name, runner in stt_attempts:
        if transcription:
            break
        try:
            transcription = runner()
            stt_meta["attempts"].append({
                "provider": provider_name,
                "ok": True,
                "text_length": len((transcription or '').strip()),
            })
        except Exception as stt_err:
            errors.append(f"{provider_name}: {stt_err}")
            stt_meta["attempts"].append({
                "provider": provider_name,
                "ok": False,
                "error": clean_text(stt_err, max_len=240),
            })
            print(f"{provider_name.title()} STT error: {stt_err}")

    transcription = re.sub(r'^["\'`]|["\'`]$', '', (transcription or '')).strip()
    if not transcription:
        if errors:
            print(f"Speech-to-text failed with errors: {' | '.join(errors)}")
        raise RuntimeError("stt_unavailable")

    return transcription, stt_meta


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
        ('nutrition_goal', 'TEXT'),
        ('premium_enabled', 'INTEGER DEFAULT 0'),
        ('premium_expires_at', 'TEXT'),
        ('premium_source', 'TEXT'),
        ('premium_model_opt_in', 'INTEGER DEFAULT 0')
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

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS activation_codes (
            code_hash TEXT PRIMARY KEY,
            plan TEXT DEFAULT 'premium',
            duration_days INTEGER DEFAULT 30,
            max_uses INTEGER DEFAULT 1,
            used_count INTEGER DEFAULT 0,
            expires_at TEXT,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS activation_redemptions (
            id {id_type},
            code_hash TEXT,
            username TEXT,
            redeemed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 6. Index
    cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_meals_username_client_id
        ON meals(username, client_id)
    ''')
    cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_activation_redemptions_code_user
        ON activation_redemptions(code_hash, username)
    ''')

    conn.commit()
    conn.close()

CORE_SCHEMA_READY = False

if os.environ.get('RUN_DB_MIGRATIONS') == 'true' or not os.environ.get('K_SERVICE'):
    init_db()
    CORE_SCHEMA_READY = True

def ensure_core_schema():
    global CORE_SCHEMA_READY
    if CORE_SCHEMA_READY:
        return
    init_db()
    CORE_SCHEMA_READY = True

PREMIUM_SCHEMA_READY = False

def ensure_premium_schema():
    global PREMIUM_SCHEMA_READY
    if PREMIUM_SCHEMA_READY:
        return
    ensure_core_schema()
    PREMIUM_SCHEMA_READY = True

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
    # Target regex for update_release.py: "version": "v5.6.46"
    fallback_version = "v5.6.61"
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
            
        lines = content.splitlines()
        changelog_lines = []
        first_heading_seen = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                if not first_heading_seen:
                    first_heading_seen = True
                    continue
                if changelog_lines:
                    break
                continue
            if first_heading_seen:
                changelog_lines.append(line)
        changelog = "\n".join(changelog_lines).strip()
        
        return version, changelog.strip() or "优化了系统性能和无障碍体验。"
    except Exception as e:
        print(f"Error reading release notes: {e}")
        return fallback_version, "本次更新包含性能优化与体验改进。"

@app.route('/api/health')
def health():
    version, _ = get_latest_release_info()
    openrouter_cooldown = provider_cooldown_info('openrouter') or {}
    google_cooldown = provider_cooldown_info('google') or {}
    openrouter_models_visible = bool(USE_OPENROUTER_LLM)
    return jsonify({
        "version": version,
        "architecture": "local-first + throttled-meal-sync + server-daily-summaries + Google AI Studio default + premium GPT",
        "models": GOOGLE_FALLBACK_MODELS,
        "openrouter_llm_enabled": bool(USE_OPENROUTER_LLM and OPENROUTER_API_KEY and not openrouter_cooldown),
        "openrouter_llm_preferred": bool(USE_OPENROUTER_LLM and OPENROUTER_API_KEY),
        "openrouter_api_key_present": bool(OPENROUTER_API_KEY),
        "use_openrouter_llm_flag": USE_OPENROUTER_LLM,
        "openrouter_allow_provider_fallbacks": OPENROUTER_ALLOW_PROVIDER_FALLBACKS,
        "enable_openrouter_stt": ENABLE_OPENROUTER_STT,
        "openrouter_stt_model": SPEECH_TO_TEXT_MODEL,
        "dotenv_path": ENV_PATH,
        "dotenv_exists": os.path.exists(ENV_PATH),
        "openrouter_cooldown_active": bool(openrouter_cooldown),
        "openrouter_cooldown_reason": openrouter_cooldown.get('reason', ''),
        "google_cooldown_active": bool(google_cooldown),
        "google_cooldown_reason": google_cooldown.get('reason', ''),
        "openrouter_text_models": OPENROUTER_TEXT_MODELS if openrouter_models_visible else [],
        "openrouter_text_timeouts": OPENROUTER_TEXT_TIMEOUTS if openrouter_models_visible else [],
        "openrouter_text_hard_deadline_seconds": OPENROUTER_TEXT_HARD_DEADLINE_SECONDS if openrouter_models_visible else 0,
        "openrouter_nutrition_models": OPENROUTER_NUTRITION_MODELS if openrouter_models_visible else [],
        "openrouter_nutrition_timeouts": OPENROUTER_NUTRITION_TIMEOUTS if openrouter_models_visible else [],
        "openrouter_nutrition_hard_deadline_seconds": OPENROUTER_NUTRITION_HARD_DEADLINE_SECONDS if openrouter_models_visible else 0,
        "openrouter_vision_models": OPENROUTER_VISION_MODELS if openrouter_models_visible else [],
        "openrouter_vision_timeouts": OPENROUTER_VISION_TIMEOUTS if openrouter_models_visible else [],
        "openrouter_vision_hard_deadline_seconds": OPENROUTER_VISION_HARD_DEADLINE_SECONDS if openrouter_models_visible else 0,
        "openrouter_chat_timeout_seconds": OPENROUTER_CHAT_TIMEOUT_SECONDS,
        "openrouter_max_model_attempts": OPENROUTER_MAX_MODEL_ATTEMPTS,
        "premium_ai_enabled": PREMIUM_AI_ENABLED,
        "premium_provider_configured": premium_provider_available(),
        "premium_text_models": PREMIUM_TEXT_MODELS,
        "premium_nutrition_models": PREMIUM_NUTRITION_MODELS,
        "premium_vision_models": PREMIUM_VISION_MODELS,
        "premium_timeout_seconds": PREMIUM_AI_TIMEOUT_SECONDS,
    })

@app.route('/api/update/info')
def update_info():
    version, changelog = get_latest_release_info()
    apk_path = os.path.join(APP_DIR, 'static', 'app-debug.apk')
    scheme = (request.headers.get('X-Forwarded-Proto') or request.scheme or 'https').split(',')[0].strip()
    if scheme not in ('http', 'https') or request.host.endswith('.run.app'):
        scheme = 'https'
    base_url = f"{scheme}://{request.host}/"
    return jsonify({
        "version": version,
        "changelog": changelog,
        "download_url": base_url + "api/update/download",
        "download_available": os.path.exists(apk_path),
    })

@app.route('/api/update/download')
def download_update():
    apk_path = os.path.join(APP_DIR, 'static', 'app-debug.apk')
    if not os.path.exists(apk_path):
        return jsonify({
            "error": "Update package is not bundled in this deployment",
            "path": "/api/update/download"
        }), 503
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
            'calories': clamp_number(first_present(item, 'calories', 'calories_kcal', 'kcal'), default=0, min_value=0, max_value=5000),
            'protein': clamp_number(first_present(item, 'protein', 'protein_g'), default=0, min_value=0, max_value=300),
            'carbs': clamp_number(first_present(item, 'carbs', 'carbs_g', 'carbohydrates', 'carbohydrates_g'), default=0, min_value=0, max_value=500),
            'fat': clamp_number(first_present(item, 'fat', 'fat_g'), default=0, min_value=0, max_value=300),
            'sodium_mg': clamp_number(first_present(item, 'sodium_mg', 'sodium'), default=0, min_value=0, max_value=100000),
            'sugar_g': clamp_number(first_present(item, 'sugar_g', 'sugar'), default=0, min_value=0, max_value=500),
            'fiber_g': clamp_number(first_present(item, 'fiber_g', 'fiber'), default=0, min_value=0, max_value=200),
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
    return """You are the single-pass vision analysis layer for a food photo logging app.
Inspect the image and directly return loggable edible components with realistic calories, macros, and grams.
If this is a packaged food, prioritize the printed package text and product identity over the generic visual category.
For packaged foods, read the visible front-of-pack brand/product text as carefully as possible.
Do not rename a branded packaged item into a generic category if the package text suggests a more specific product.
If the package shows a chocolate-coated ice cream / popsicle / frozen dessert on a wrapper, do not call it a plain chocolate bar.
Prefer Chinese consumer-facing names when the package text is Chinese.
If a plate / tray / lunch box contains physically separable foods, return them as separate items in the foods array.
Split separately plated components such as: main protein, staple/starch, vegetables/salad, fruit, soup, and sauce/dip in a separate compartment.
Do not split ingredients that are mixed into one combined dish or wrapped inside one food. Example: fried rice stays one item; a burger stays one item.
Ignore utensils and non-edible decoration. Ignore lime/lemon wedges or tiny herb garnish unless they are clearly intended to be eaten.
If a sauce or dip is clearly visible in its own compartment or obvious spoonable pool, include it as a separate food item.
If a printed nutrition label is visible, use it first. Otherwise estimate realistically from appearance and common Chinese-market nutrition profiles.
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
      "confidence": 0.0,
      "calories": 0,
      "protein": 0,
      "carbs": 0,
      "fat": 0,
      "sodium_mg": 0,
      "sugar_g": 0,
      "fiber_g": 0
    }
  ],
  "notes": "important uncertainty or missing context"
}
If no food is visible, return {"scene_type":"not_food","foods":[]}."""

GENERIC_PACKAGED_FOOD_NAMES = {
    'food',
    'snack',
    'dessert',
    'candy',
    'candy bar',
    'bar',
    'chocolate',
    'chocolate bar',
    'ice cream',
    'ice cream bar',
    'popsicle',
    'frozen dessert',
    'unknown food',
    '零食',
    '甜品',
    '食物',
    '巧克力',
    '巧克力棒',
    '冰淇淋',
    '雪糕',
    '冰棍',
    '冰棒',
}

CHINA_PACKAGED_BRAND_HINTS = {
    '巧乐兹': '巧乐兹',
    '梦龙': '梦龙',
    '可爱多': '可爱多',
    '甄稀': '甄稀',
    '随变': '随变',
    '冰工厂': '冰工厂',
    '绿色心情': '绿色心情',
    '东北大板': '东北大板',
    '伊利': '伊利',
    '和路雪': '和路雪',
    '蒙牛': '蒙牛',
}

ICE_CREAM_PACKAGE_HINTS = (
    '雪糕',
    '冰淇淋',
    '冰棍',
    '冰棒',
    '脆皮',
    '脆层',
    '牛乳',
    '奶脆',
    '冰品',
)


def contains_cjk(text):
    return bool(re.search(r'[\u4e00-\u9fff]', str(text or '')))


def normalize_name_key(text):
    return re.sub(r'[\s\-_]+', ' ', str(text or '').strip().lower())


def is_generic_packaged_food_name(name):
    normalized = normalize_name_key(name)
    return not normalized or normalized in GENERIC_PACKAGED_FOOD_NAMES


def guess_name_from_package_text(package_text):
    text = clean_text(package_text, max_len=400)
    if not text:
        return ''
    compact = re.sub(r'\s+', '', text)
    for token, label in CHINA_PACKAGED_BRAND_HINTS.items():
        if token in compact:
            if any(hint in compact for hint in ICE_CREAM_PACKAGE_HINTS):
                return f"{label}雪糕"
            return label
    if any(hint in compact for hint in ICE_CREAM_PACKAGE_HINTS):
        if '巧克力' in compact:
            return '巧克力脆皮雪糕'
        return '雪糕'
    return ''


def should_refine_packaged_food_name(visual_data):
    if not isinstance(visual_data, dict) or not visual_data.get('is_packaged_food'):
        return False
    foods = visual_data.get('foods') or []
    if not foods:
        return False
    name = clean_text((foods[0] or {}).get('name'), max_len=120)
    package_text = clean_text(visual_data.get('package_text'), max_len=400)
    if guess_name_from_package_text(package_text):
        return True
    if is_generic_packaged_food_name(name):
        return True
    if not contains_cjk(name) and (contains_cjk(package_text) or visual_data.get('scene_type') == 'packaged_food'):
        return True
    if normalize_name_key(name) == 'chocolate bar' and any(hint in package_text for hint in ICE_CREAM_PACKAGE_HINTS):
        return True
    return False


def build_packaged_name_refinement_prompt(visual_data):
    return f"""You are a packaging OCR and packaged-food identity resolver for a food logging app.
Your job is not to estimate calories. Your only job is to identify the exact or best-possible consumer-facing product name from the package.

Rules:
1. Read the visible package text carefully. Prioritize the front-of-pack product name over generic appearance.
2. If a branded Chinese product name is visible, return that exact Chinese name.
3. If the exact brand is unclear but the wrapper clearly shows an ice cream / popsicle / frozen dessert, return a Chinese frozen-dessert name such as "巧克力脆皮雪糕" or "冰淇淋棒".
4. Never call a wrapped ice cream bar a plain "巧克力棒" or "chocolate bar".
5. Prefer Chinese names whenever the package text is Chinese.
6. If uncertain, return the most specific safe Chinese generic name and lower the confidence.

Current observation JSON:
{json.dumps(visual_data, ensure_ascii=False)}

Return strict JSON only:
{{
  "product_name": "best consumer-facing product name in Chinese if possible",
  "generic_name": "generic food category",
  "packaging_type": "wrapper|box|bottle|cup|bag|other",
  "ocr_text": "visible package text",
  "confidence": 0.0,
  "reason": "short explanation"
}}"""


def refine_packaged_food_name(img, visual_data, use_premium=False):
    refinement_models, refinement_timeouts = get_vision_chain_models()
    refinement_attempt_cap = capped_attempt_count(refinement_models, refinement_timeouts)
    response = call_llm(
        prompt_text=build_packaged_name_refinement_prompt(visual_data),
        image=img,
        temperature=0,
        openrouter_models=refinement_models,
        openrouter_max_attempts=refinement_attempt_cap,
        openrouter_timeout=refinement_timeouts[0] if refinement_timeouts else OPENROUTER_CHAT_TIMEOUT_SECONDS,
        openrouter_timeouts=refinement_timeouts,
        premium=use_premium,
        premium_models=PREMIUM_VISION_MODELS,
        premium_timeout=PREMIUM_AI_TIMEOUT_SECONDS,
        allow_google_fallback=True,
        return_metadata=True,
    )
    payload = parse_json_payload(response.get('text'))
    if not isinstance(payload, dict):
        return None, response
    return {
        'product_name': clean_text(payload.get('product_name'), max_len=120),
        'generic_name': clean_text(payload.get('generic_name'), max_len=120),
        'ocr_text': clean_text(payload.get('ocr_text'), max_len=2000),
        'packaging_type': clean_text(payload.get('packaging_type'), max_len=40),
        'reason': clean_text(payload.get('reason'), max_len=240),
        'confidence': clamp_number(payload.get('confidence'), default=0.5, min_value=0, max_value=1, integer=False),
    }, response


def apply_packaged_food_name_refinement(visual_data, refinement):
    if not isinstance(visual_data, dict) or not isinstance(refinement, dict):
        return visual_data
    foods = list(visual_data.get('foods') or [])
    if not foods:
        return visual_data
    package_text = clean_text(refinement.get('ocr_text') or visual_data.get('package_text'), max_len=2000)
    product_name = clean_text(refinement.get('product_name'), max_len=120)
    generic_name = clean_text(refinement.get('generic_name'), max_len=120)
    guessed_name = guess_name_from_package_text(package_text)
    replacement = product_name or guessed_name or generic_name
    if not replacement:
        return visual_data
    current_name = clean_text((foods[0] or {}).get('name'), max_len=120)
    more_specific = (
        bool(replacement)
        and (
            is_generic_packaged_food_name(current_name)
            or (not contains_cjk(current_name) and contains_cjk(replacement))
            or (normalize_name_key(current_name) == 'chocolate bar' and replacement != current_name)
        )
    )
    if not more_specific:
        return visual_data
    foods[0] = dict(foods[0] or {})
    foods[0]['name'] = replacement
    visual_data = dict(visual_data)
    visual_data['foods'] = foods
    visual_data['package_text'] = package_text or visual_data.get('package_text', '')
    if refinement.get('reason'):
        visual_data['notes'] = clean_text(
            f"{visual_data.get('notes', '')} {refinement.get('reason')}".strip(),
            max_len=300
        )
    return visual_data


def build_nutrition_from_visual_prompt(visual_data):
    return f"""You are the nutrition reasoning layer for a food photo logging app.
Use the visual observation JSON below to estimate realistic nutrition. If a packaging nutrition label is present, prefer the printed label over visual estimation. Output strict JSON array only. No markdown.
If the visual observation already includes a packaged product name from package text, preserve that product name in food_name instead of replacing it with a generic category.
For wrapped frozen desserts or ice cream products, do not rename them to plain chocolate bars.
Return one array item per loggable edible component.
Do not merge separately plated foods into one item just because they are served together.
If the meal has a main protein plus raw vegetables plus a sauce/dip, return separate items for the meaningful components.
Ignore lime/lemon wedges and tiny garnish unless clearly eaten.
If a dipping sauce is in its own compartment or visible pool and looks more than about 10g, count it as a separate food item.
If a dish is mixed together (fried rice, sandwich, burrito, noodles in one bowl), keep it as one item.
Prefer concise Chinese food names when possible.
Make calorie and macro values internally consistent with weight. Do not output unrealistic zero-calorie fruit juice or milk drinks.

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

def build_manual_food_estimate_prompt(food_name, weight, provided_fields):
    provided = {
        'calories': provided_fields.get('calories'),
        'protein': provided_fields.get('protein'),
        'carbs': provided_fields.get('carbs'),
        'fat': provided_fields.get('fat'),
    }
    return f"""You are a nutrition estimation assistant for a Chinese food logging app.
Estimate realistic nutrition for one manually entered food item and output strict JSON only.

Food name: {food_name}
Weight (grams): {weight}
User provided fields (keep them exactly if present, fill only the missing ones):
{json.dumps(provided, ensure_ascii=False)}

Rules:
1. Preserve the food name in Chinese if possible.
2. If the user already provided a value, copy that exact value into the output.
3. Fill missing calories / protein / carbs / fat with realistic estimates for the stated weight.
4. Keep the output internally consistent. Calories should roughly match protein*4 + carbs*4 + fat*9 with normal rounding tolerance.
5. Use realistic nutrition references for common foods sold in China when relevant.
6. If the item is a caloric drink such as juice, latte, milk, soy milk, yogurt, or flavored coffee, do not return zero calories unless it is truly a plain zero-calorie drink.
7. If the name includes modifiers such as 无糖 / 去皮 / 低脂, reflect them realistically instead of treating the item as plain water.
8. Return only one JSON object. No markdown, no explanation.

Required schema:
{{
  "food_name": "食物名称",
  "calories": 0,
  "protein": 0,
  "carbs": 0,
  "fat": 0,
  "weight": 0,
  "confidence": 0.0,
  "notes": "short note"
}}"""


def normalize_manual_food_estimate(payload, fallback_name, fallback_weight, provided_fields):
    data = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(data, dict):
        return None

    result = {
        'food_name': clean_text(first_present(data, 'food_name', 'name', 'food'), default=fallback_name, max_len=120),
        'calories': clamp_number(first_present(data, 'calories', 'calories_kcal', 'kcal'), default=0, min_value=0, max_value=5000),
        'protein': clamp_number(first_present(data, 'protein', 'protein_g'), default=0, min_value=0, max_value=300),
        'carbs': clamp_number(first_present(data, 'carbs', 'carbs_g', 'carbohydrates', 'carbohydrates_g'), default=0, min_value=0, max_value=500),
        'fat': clamp_number(first_present(data, 'fat', 'fat_g'), default=0, min_value=0, max_value=300),
        'weight': clamp_number(first_present(data, 'weight', 'weight_g', 'estimated_grams', 'grams'), default=fallback_weight, min_value=1, max_value=2000),
        'confidence': clamp_number(data.get('confidence'), default=0.7, min_value=0, max_value=1, integer=False),
        'notes': clean_text(data.get('notes'), max_len=240),
    }

    field_limits = {
        'calories': 5000,
        'protein': 300,
        'carbs': 500,
        'fat': 300,
    }
    for field, max_value in field_limits.items():
        value = provided_fields.get(field)
        if value is not None:
            result[field] = clamp_number(value, default=result[field], min_value=0, max_value=max_value)

    return result


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


def attempted_statuses_for_models(fallback_trace, models):
    statuses = []
    for entry in list(fallback_trace or []):
        for model in list(models or []):
            prefix = f"{model}:"
            if entry.startswith(prefix):
                statuses.append(entry[len(prefix):])
                break
    return statuses


def all_model_attempts_timed_out(fallback_trace, models):
    expected = len(list(models or []))
    statuses = attempted_statuses_for_models(fallback_trace, models)
    return bool(statuses) and len(statuses) >= expected and all(status == 'timeout' for status in statuses[:expected])


def build_structured_nutrition_degraded_foods(visual_data):
    visual_items = list((visual_data or {}).get('foods') or [])
    if not visual_items:
        return [], False

    nutrition_label = (visual_data or {}).get('nutrition_label') or {}
    per_100g = {
        'calories': clamp_number(nutrition_label.get('calories'), default=0, min_value=0, max_value=5000),
        'protein': clamp_number(nutrition_label.get('protein'), default=0, min_value=0, max_value=300),
        'carbs': clamp_number(nutrition_label.get('carbs'), default=0, min_value=0, max_value=500),
        'fat': clamp_number(nutrition_label.get('fat'), default=0, min_value=0, max_value=300),
    }
    used_printed_label = any(value > 0 for value in per_100g.values())

    foods = []
    for item in visual_items:
        weight = clamp_number(item.get('estimated_weight_g'), default=100, min_value=1, max_value=2000)
        factor = weight / 100.0
        foods.append({
            'food_name': clean_text(item.get('name'), default='待确认食物', max_len=120),
            'calories': clamp_number(per_100g['calories'] * factor, default=0, min_value=0, max_value=5000),
            'protein': clamp_number(per_100g['protein'] * factor, default=0, min_value=0, max_value=300),
            'carbs': clamp_number(per_100g['carbs'] * factor, default=0, min_value=0, max_value=500),
            'fat': clamp_number(per_100g['fat'] * factor, default=0, min_value=0, max_value=300),
            'weight': weight,
            'sodium_mg': 0,
            'sugar_g': 0,
            'fiber_g': 0,
        })
    return foods, used_printed_label


def build_structured_nutrition_degraded_result(visual_data, visual_meta, nutrition_meta, pipeline_latency_ms, reason):
    foods, used_printed_label = build_structured_nutrition_degraded_foods(visual_data)
    if not foods:
        return None

    disclaimer = (
        '营养计算模型暂时不可用，当前结果为结构化降级估算；'
        '若包装营养标示不完整或未识别到，请保存前手动确认。不可用于医疗诊断。'
    )
    enriched, analysis_meta = enrich_foods_with_analysis_metadata(
        foods,
        visual_data,
        visual_meta,
        nutrition_meta,
        pipeline_latency_ms,
    )
    for item in enriched:
        item['needs_user_confirmation'] = True
        item['disclaimer'] = disclaimer
    analysis_meta['needs_user_confirmation'] = True
    analysis_meta['nutrition_degraded'] = True
    analysis_meta['nutrition_degraded_reason'] = reason
    analysis_meta['nutrition_degraded_used_label'] = used_printed_label
    analysis_meta['disclaimer'] = disclaimer
    return {
        'foods': enriched,
        'analysis_meta': analysis_meta,
    }


def foods_from_single_pass_visual(visual_data):
    foods = []
    for item in list((visual_data or {}).get('foods') or []):
        if not isinstance(item, dict):
            continue
        foods.append({
            'food_name': clean_text(item.get('name'), default='Unknown food', max_len=120),
            'calories': clamp_number(item.get('calories'), default=0, min_value=0, max_value=5000),
            'protein': clamp_number(item.get('protein'), default=0, min_value=0, max_value=300),
            'carbs': clamp_number(item.get('carbs'), default=0, min_value=0, max_value=500),
            'fat': clamp_number(item.get('fat'), default=0, min_value=0, max_value=300),
            'weight': clamp_number(item.get('estimated_weight_g') or item.get('weight'), default=100, min_value=1, max_value=2000),
            'sodium_mg': clamp_number(item.get('sodium_mg'), default=0, min_value=0, max_value=100000),
            'sugar_g': clamp_number(item.get('sugar_g'), default=0, min_value=0, max_value=500),
            'fiber_g': clamp_number(item.get('fiber_g'), default=0, min_value=0, max_value=200),
        })
    return foods or None

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
        'visual_fallback_trace': visual_meta.get('fallback_trace') if visual_meta else [],
        'nutrition_fallback_trace': nutrition_meta.get('fallback_trace') if nutrition_meta else [],
        'packaged_name_refined': bool(visual_meta.get('packaged_name_refined')) if visual_meta else False,
        'packaged_name_refinement': visual_meta.get('packaged_name_refinement') if visual_meta else None,
        'packaged_name_refinement_trace': visual_meta.get('packaged_name_refinement_trace') if visual_meta else [],
        'latency_ms': pipeline_latency_ms,
        'needs_user_confirmation': any(item.get('needs_user_confirmation') for item in enriched),
        'disclaimer': '营养数据为估算值，仅供参考，不可用于医疗诊断。',
    }

def analyze_food_with_two_stage_pipeline(img, use_premium=False):
    pipeline_started_at = time.perf_counter()
    vision_models, visual_timeouts = get_vision_chain_models()
    visual_attempt_cap = capped_attempt_count(
        vision_models,
        visual_timeouts,
        configured_cap=len(vision_models),
    )
    nutrition_timeouts = fit_timeouts_to_deadline(
        OPENROUTER_NUTRITION_TIMEOUTS,
        OPENROUTER_NUTRITION_HARD_DEADLINE_SECONDS
    )
    nutrition_models = OPENROUTER_NUTRITION_MODELS[:len(nutrition_timeouts) or len(OPENROUTER_NUTRITION_MODELS)]
    nutrition_attempt_cap = capped_attempt_count(nutrition_models, nutrition_timeouts)
    visual_prompt = build_visual_prompt()
    try:
        visual_response = call_llm(
            prompt_text=visual_prompt,
            image=img,
            temperature=0.1,
            openrouter_models=vision_models,
            openrouter_max_attempts=visual_attempt_cap,
            openrouter_timeout=visual_timeouts[0] if visual_timeouts else OPENROUTER_CHAT_TIMEOUT_SECONDS,
            openrouter_timeouts=visual_timeouts,
            premium=use_premium,
            premium_models=PREMIUM_VISION_MODELS,
            premium_timeout=PREMIUM_AI_TIMEOUT_SECONDS,
            allow_google_fallback=True,
            return_metadata=True,
        )
    except LLMChainExhaustedError as visual_err:
        fallback_trace = list(visual_err.fallback_trace or [])
        if (
            GEMINI_API_KEY
            and fallback_trace
            and all(status.startswith('http_429') for status in attempted_statuses_for_models(fallback_trace, vision_models))
            and not provider_cooldown_info('google')
        ):
            print("OpenRouter vision free chain hit 429; using Google vision fallback.")
            visual_response = call_google_generate_content(
                prompt_text=visual_prompt,
                image=img,
                temperature=0.1,
                return_metadata=True,
                fallback_trace=fallback_trace + ['google_vision_fallback:because_openrouter_429'],
            )
        else:
            raise
    visual_meta = {k: visual_response.get(k) for k in ('provider', 'model', 'latency_ms', 'fallback_trace')}
    visual_data = normalize_visual_observations(parse_json_payload(visual_response.get('text')))
    if not visual_data:
        return None
    single_pass_foods = foods_from_single_pass_visual(visual_data)
    has_inline_nutrition = bool(
        single_pass_foods and any(
            (item.get('calories') or item.get('protein') or item.get('carbs') or item.get('fat'))
            for item in single_pass_foods
        )
    )
    if has_inline_nutrition:
        pipeline_latency_ms = int((time.perf_counter() - pipeline_started_at) * 1000)
        enriched, analysis_meta = enrich_foods_with_analysis_metadata(
            single_pass_foods,
            visual_data,
            visual_meta,
            None,
            pipeline_latency_ms,
        )
        analysis_meta['premium_requested'] = bool(use_premium)
        analysis_meta['premium_used'] = visual_meta.get('provider') == 'openai_compatible'
        analysis_meta['single_pass_vision'] = True
        return {'foods': enriched, 'analysis_meta': analysis_meta}
    if ENABLE_PACKAGED_NAME_REMOTE_REFINEMENT and should_refine_packaged_food_name(visual_data):
        try:
            refined_name, refine_response = refine_packaged_food_name(img, visual_data, use_premium=use_premium)
            if refined_name:
                visual_data = apply_packaged_food_name_refinement(visual_data, refined_name)
                visual_meta = dict(visual_meta or {})
                visual_meta['packaged_name_refined'] = True
                visual_meta['packaged_name_refinement'] = refined_name
                visual_meta['packaged_name_refinement_trace'] = refine_response.get('fallback_trace')
        except Exception as refine_err:
            print(f"Packaged food name refinement failed: {refine_err}")

    nutrition_prompt = build_nutrition_from_visual_prompt(visual_data)
    try:
        nutrition_response = call_llm(
            prompt_text=nutrition_prompt,
            image=None,
            temperature=0.1,
            openrouter_models=nutrition_models,
            openrouter_max_attempts=nutrition_attempt_cap,
            openrouter_timeout=nutrition_timeouts[0] if nutrition_timeouts else OPENROUTER_CHAT_TIMEOUT_SECONDS,
            openrouter_timeouts=nutrition_timeouts,
            premium=use_premium,
            premium_models=PREMIUM_NUTRITION_MODELS,
            premium_timeout=PREMIUM_AI_TIMEOUT_SECONDS,
            allow_google_fallback=True,
            return_metadata=True,
        )
        nutrition_meta = {k: nutrition_response.get(k) for k in ('provider', 'model', 'latency_ms', 'fallback_trace')}
        foods = parse_ai_multi_result(nutrition_response.get('text'))
    except LLMChainExhaustedError as nutrition_err:
        fallback_trace = list(nutrition_err.fallback_trace or [])
        active_nutrition_models = nutrition_models
        reason = 'all_timeouts' if all_model_attempts_timed_out(fallback_trace, active_nutrition_models) else 'model_chain_exhausted'
        nutrition_meta = {
            'provider': 'degraded',
            'model': reason,
            'latency_ms': 0,
            'fallback_trace': fallback_trace,
        }
        pipeline_latency_ms = int((time.perf_counter() - pipeline_started_at) * 1000)
        degraded_result = build_structured_nutrition_degraded_result(
            visual_data,
            visual_meta,
            nutrition_meta,
            pipeline_latency_ms,
            reason,
        )
        if degraded_result:
            degraded_result['analysis_meta']['premium_requested'] = bool(use_premium)
            degraded_result['analysis_meta']['premium_used'] = visual_meta.get('provider') == 'openai_compatible'
            return degraded_result
        raise
    if foods is None:
        fallback_trace = list((nutrition_meta or {}).get('fallback_trace') or [])
        fallback_trace.append('parse:invalid_json')
        nutrition_meta = {
            'provider': 'degraded',
            'model': 'invalid_json_fallback',
            'latency_ms': nutrition_meta.get('latency_ms', 0),
            'fallback_trace': fallback_trace,
        }
        pipeline_latency_ms = int((time.perf_counter() - pipeline_started_at) * 1000)
        degraded_result = build_structured_nutrition_degraded_result(
            visual_data,
            visual_meta,
            nutrition_meta,
            pipeline_latency_ms,
            'invalid_json_fallback',
        )
        if degraded_result:
            degraded_result['analysis_meta']['premium_requested'] = bool(use_premium)
            degraded_result['analysis_meta']['premium_used'] = visual_meta.get('provider') == 'openai_compatible'
            return degraded_result
        return None
    if foods is None:
        return None
    pipeline_latency_ms = int((time.perf_counter() - pipeline_started_at) * 1000)
    enriched, analysis_meta = enrich_foods_with_analysis_metadata(foods, visual_data, visual_meta, nutrition_meta, pipeline_latency_ms)
    analysis_meta['premium_requested'] = bool(use_premium)
    analysis_meta['premium_used'] = (
        visual_meta.get('provider') == 'openai_compatible'
        or nutrition_meta.get('provider') == 'openai_compatible'
    )
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

def row_value(row, key, default=None):
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default

def parse_iso_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return value
    try:
        return datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00')).replace(tzinfo=None)
    except Exception:
        return None

def utcnow():
    return datetime.datetime.utcnow()

def iso_utc(dt):
    if not dt:
        return None
    return dt.replace(microsecond=0).isoformat() + 'Z'

def is_premium_active_values(enabled, expires_at):
    if not bool(int(enabled or 0)):
        return False
    expires = parse_iso_datetime(expires_at)
    return expires is None or expires > utcnow()

def hash_activation_code(code):
    normalized = re.sub(r'\s+', '', str(code or '').upper())
    return hmac.new(
        ACTIVATION_CODE_SECRET.encode('utf-8'),
        normalized.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

def generate_activation_code():
    raw = uuid.uuid4().hex[:16].upper()
    return 'NSP-' + '-'.join(raw[i:i + 4] for i in range(0, len(raw), 4))

def get_user_premium_status(username):
    status = {
        "is_premium": False,
        "premium_model_opt_in": False,
        "premium_expires_at": None,
        "premium_source": "",
        "premium_provider_available": premium_provider_available(),
        "payment_enabled": False,
    }
    if not username or username in ('local_user', 'anonymous', 'guest'):
        return status
    try:
        ensure_premium_schema()
        with get_db_connection() as conn:
            row = conn.execute('''
                SELECT premium_enabled, premium_expires_at, premium_source, premium_model_opt_in
                FROM users WHERE username = ?
            ''', (username,)).fetchone()
    except Exception as e:
        print(f"Premium status lookup failed: {e}")
        return status

    if not row:
        return status
    enabled = row_value(row, 'premium_enabled', 0)
    expires_at = row_value(row, 'premium_expires_at')
    is_active = is_premium_active_values(enabled, expires_at)
    status.update({
        "is_premium": is_active,
        "premium_model_opt_in": bool(int(row_value(row, 'premium_model_opt_in', 0) or 0)) and is_active,
        "premium_expires_at": expires_at,
        "premium_source": row_value(row, 'premium_source', '') or '',
    })
    return status

def request_premium_enabled(username, data=None):
    if not username or username in ('local_user', 'anonymous', 'guest'):
        return False
    requested = False
    if data is not None:
        requested = str(data.get('premium_ai', '')).lower() in ('1', 'true', 'yes', 'on') or data.get('premium_ai') is True
    if request.form:
        requested = requested or str(request.form.get('premium_ai', '')).lower() in ('1', 'true', 'yes', 'on')
    if not requested:
        return False
    status = get_user_premium_status(username)
    return bool(status.get('is_premium') and status.get('premium_model_opt_in') and status.get('premium_provider_available'))

def require_admin_token():
    token = request.headers.get('X-Admin-Token') or request.args.get('admin_token')
    if not PREMIUM_ADMIN_TOKEN or not token or not hmac.compare_digest(token, PREMIUM_ADMIN_TOKEN):
        return False
    return True

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

MOJIBAKE_MARKERS = (
    'Ã', 'Â', 'â', 'ã', 'å', 'æ', 'ç', 'è', 'é', 'ï',
    'ƒ', '„', '…', '†', '‡', 'ˆ', '‰', 'Š', '‹', 'Œ',
    'Ž', '‘', '’', '“', '”', '•', '–', '—', '˜', '™',
    'š', '›', 'œ', 'ž', 'Ÿ', '¼', '½', '¾'
)
MOJIBAKE_BOX_CHARS = ('□', '�', '\ufffd')


def count_cjk_chars(text):
    return len(re.findall(r'[\u4e00-\u9fff]', str(text or '')))


def mojibake_score(text):
    value = str(text or '')
    return (
        sum(value.count(marker) for marker in MOJIBAKE_MARKERS)
        + sum(value.count(marker) * 2 for marker in MOJIBAKE_BOX_CHARS)
    )


def repair_mojibake_text(value):
    text = str(value or '')
    if not text or not any(marker in text for marker in MOJIBAKE_MARKERS + MOJIBAKE_BOX_CHARS):
        return text

    base_cjk = count_cjk_chars(text)
    base_score = mojibake_score(text)
    best = text
    best_cjk = base_cjk
    best_score = base_score

    for encoding in ('cp1252', 'latin-1'):
        try:
            candidate = text.encode(encoding).decode('utf-8')
        except UnicodeError:
            continue
        candidate_cjk = count_cjk_chars(candidate)
        candidate_score = mojibake_score(candidate)
        if candidate_cjk > best_cjk and candidate_score <= best_score:
            best = candidate
            best_cjk = candidate_cjk
            best_score = candidate_score

    return best


def clean_ai_text(value, default=''):
    text = repair_mojibake_text(str(value or default).strip())
    return text


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
    text = clean_ai_text(value, default=default)
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
    ensure_core_schema()
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
    ensure_core_schema()
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

@app.route('/api/premium/status', methods=['GET'])
@token_required
def premium_status():
    return jsonify({
        "success": True,
        **get_user_premium_status(get_current_username())
    })

@app.route('/api/premium/toggle', methods=['POST'])
@token_required
def premium_toggle():
    username = get_current_username()
    data = request.get_json() or {}
    enabled = data.get('enabled') is True or str(data.get('enabled', '')).lower() in ('1', 'true', 'yes', 'on')
    status = get_user_premium_status(username)
    if enabled and not status.get('is_premium'):
        return jsonify({"error": "Premium is not active for this account."}), 403

    with get_db_connection() as conn:
        conn.execute(
            'UPDATE users SET premium_model_opt_in = ? WHERE username = ?',
            (1 if enabled else 0, username)
        )
        conn.commit()

    return jsonify({
        "success": True,
        **get_user_premium_status(username)
    })

@app.route('/api/premium/redeem', methods=['POST'])
@token_required
@limiter.limit("6 per hour", key_func=user_or_ip_limit_key)
def premium_redeem():
    username = get_current_username()
    data = request.get_json() or {}
    code = str(data.get('code', '')).strip()
    if len(code) < 8:
        return jsonify({"error": "Invalid activation code."}), 400

    code_hash = hash_activation_code(code)
    now = utcnow()
    ensure_premium_schema()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        row = cursor.execute('''
            SELECT code_hash, plan, duration_days, max_uses, used_count, expires_at
            FROM activation_codes WHERE code_hash = ?
        ''', (code_hash,)).fetchone()
        if not row:
            return jsonify({"error": "Activation code is invalid."}), 404

        code_expires = parse_iso_datetime(row_value(row, 'expires_at'))
        if code_expires and code_expires < now:
            return jsonify({"error": "Activation code has expired."}), 410
        if int(row_value(row, 'used_count', 0) or 0) >= int(row_value(row, 'max_uses', 1) or 1):
            return jsonify({"error": "Activation code has already been used."}), 409

        existing = cursor.execute('''
            SELECT 1 FROM activation_redemptions
            WHERE code_hash = ? AND username = ?
        ''', (code_hash, username)).fetchone()
        if existing:
            return jsonify({"error": "This account has already redeemed this code."}), 409

        user_row = cursor.execute('''
            SELECT premium_enabled, premium_expires_at FROM users WHERE username = ?
        ''', (username,)).fetchone()
        if not user_row:
            return jsonify({"error": "User does not exist."}), 404

        current_expiry = parse_iso_datetime(row_value(user_row, 'premium_expires_at'))
        base_time = current_expiry if current_expiry and current_expiry > now else now
        duration_days = max(1, int(row_value(row, 'duration_days', 30) or 30))
        new_expiry = base_time + datetime.timedelta(days=duration_days)

        cursor.execute('''
            UPDATE users
            SET premium_enabled = 1,
                premium_expires_at = ?,
                premium_source = ?,
                premium_model_opt_in = 1
            WHERE username = ?
        ''', (iso_utc(new_expiry), row_value(row, 'plan', 'premium') or 'premium', username))
        cursor.execute('''
            UPDATE activation_codes SET used_count = used_count + 1 WHERE code_hash = ?
        ''', (code_hash,))
        cursor.execute('''
            INSERT INTO activation_redemptions (code_hash, username) VALUES (?, ?)
        ''', (code_hash, username))
        conn.commit()

    return jsonify({
        "success": True,
        "message": "Premium activated.",
        **get_user_premium_status(username)
    })

@app.route('/api/admin/premium/codes', methods=['POST'])
@limiter.limit("20 per hour")
def admin_create_activation_code():
    if not require_admin_token():
        return jsonify({"error": "Admin token required."}), 401
    data = request.get_json() or {}
    ensure_premium_schema()
    code = str(data.get('code') or generate_activation_code()).strip().upper()
    duration_days = max(1, min(3650, int(data.get('duration_days') or 30)))
    max_uses = max(1, min(10000, int(data.get('max_uses') or 1)))
    plan = clean_text(data.get('plan'), default='premium', max_len=40)
    expires_at = data.get('expires_at')
    code_hash = hash_activation_code(code)

    with get_db_connection() as conn:
        try:
            conn.execute('''
                INSERT INTO activation_codes
                (code_hash, plan, duration_days, max_uses, used_count, expires_at, created_by)
                VALUES (?, ?, ?, ?, 0, ?, ?)
            ''', (code_hash, plan, duration_days, max_uses, expires_at, 'admin'))
            conn.commit()
        except INTEGRITY_ERRORS:
            return jsonify({"error": "Activation code already exists."}), 409

    return jsonify({
        "success": True,
        "code": code,
        "duration_days": duration_days,
        "max_uses": max_uses,
        "expires_at": expires_at
    })

@app.route('/api/admin/premium/grant', methods=['POST'])
@limiter.limit("30 per hour")
def admin_grant_premium():
    if not require_admin_token():
        return jsonify({"error": "Admin token required."}), 401
    data = request.get_json() or {}
    ensure_premium_schema()
    username = clean_text(data.get('username'), default='', max_len=120)
    if not username:
        return jsonify({"error": "username is required."}), 400
    duration_days = max(1, min(3650, int(data.get('duration_days') or 30)))
    source = clean_text(data.get('source'), default='admin_grant', max_len=40)
    now = utcnow()

    with get_db_connection() as conn:
        cursor = conn.cursor()
        row = cursor.execute('''
            SELECT premium_expires_at FROM users WHERE username = ?
        ''', (username,)).fetchone()
        if not row:
            return jsonify({"error": "User does not exist."}), 404
        current_expiry = parse_iso_datetime(row_value(row, 'premium_expires_at'))
        base_time = current_expiry if current_expiry and current_expiry > now else now
        new_expiry = base_time + datetime.timedelta(days=duration_days)
        cursor.execute('''
            UPDATE users
            SET premium_enabled = 1,
                premium_expires_at = ?,
                premium_source = ?,
                premium_model_opt_in = 1
            WHERE username = ?
        ''', (iso_utc(new_expiry), source, username))
        conn.commit()

    return jsonify({
        "success": True,
        "username": username,
        **get_user_premium_status(username)
    })

@app.route('/api/analyze', methods=['POST'])
@token_required
@limiter.limit("10 per minute", key_func=user_or_ip_limit_key)
def analyze_food():
    reset_request_trace('analyze_food')
    client_action_id = get_client_action_id()
    if 'image' not in request.files:
        return jsonify({"error": "没有找到图片"}), 400

    file = request.files['image']
    if file.filename == '':
        return jsonify({"error": "图片名为空"}), 400

    is_app = request.form.get('is_app') == 'true' or request.args.get('is_app') == 'true'
    username = get_current_username()
    use_premium = request_premium_enabled(username)

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

        analysis_result = analyze_food_with_two_stage_pipeline(img, use_premium=use_premium)
        if not analysis_result or not analysis_result.get('foods'):
            log_request_trace('analyze_food_empty')
            return jsonify({"error": "AI未检测到食物，请重新拍摄"}), 400
        foods = analysis_result.get('foods', [])
        analysis_meta = attach_request_trace(analysis_result.get('analysis_meta', {}))
        if client_action_id:
            analysis_meta['client_action_id'] = client_action_id

        # Return food data; frontend stores locally via MealStorage (localStorage).
        # Daily aggregated summaries are synced to server via /api/daily-summaries.
        session_id = str(uuid.uuid4())
        saved = []
        for i, food in enumerate(foods):
            food['id'] = uuid.uuid4().hex
            food['portion'] = 1.0
            saved.append(food)

        log_request_trace('analyze_food_success')
        log_client_action_summary('analyze_food', client_action_id, analysis_meta)
        return jsonify({
            "success": True,
            "session_id": session_id,
            "foods": saved,
            "image_url": "",
            "analysis_meta": analysis_meta
        })

    except UnidentifiedImageError:
        log_request_trace('analyze_food_invalid_image')
        return jsonify({"error": "Invalid image file"}), 400
    except LLMChainExhaustedError as llm_err:
        print(f"Analyze image chain exhausted: {llm_err}")
        log_request_trace('analyze_food_chain_exhausted')
        return jsonify({
            "error": "当前拍照识别服务暂时繁忙，请稍后重试，或先使用手动补录。",
            "fallback_trace": list(llm_err.fallback_trace or []),
            "analysis_meta": attach_request_trace({
                "client_action_id": client_action_id,
                "fallback_trace": list(llm_err.fallback_trace or []),
            }),
        }), 503
    except Exception as e:
        print(f"Error calling AI API: {e}")
        log_request_trace('analyze_food_exception')
        return jsonify({
            "error": "AI image analysis failed. Please try again later.",
            "analysis_meta": attach_request_trace({
                "client_action_id": client_action_id,
            }),
        }), 500
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


def build_voice_understanding_prompt(text):
    return f"""You are the understanding layer for a Chinese voice food logging app.
Your job is to understand messy real-life speech first, then extract structured food and exercise entities.
Do not estimate calories or macros in this step.

The user may speak casually and mix multiple events in one sentence:
- time words: 今天早上 / 中午 / 晚上 / 刚刚 / 后来
- connectors: 然后 / 又 / 还有 / 顺便 / 饭后
- vague spoken style: 我今天乱七八糟吃了点东西 / 喝了一大杯 / 吃了半个
- modifiers that must be kept: 无糖 / 去皮 / 低脂 / 大杯 / 冰 / 热 / 炸 / 烤 / 麻辣 / 真空包装 / 奥尔良
- brand or product-like names may appear

User input:
"{text}"

Hard rules:
1. Extract one item per actually consumed food or drink. If the user mentioned three foods, return three food items.
2. Extract exercise separately if present.
3. Never copy the whole sentence as a food name.
4. Preserve meaningful modifiers inside `modifiers`.
5. Use concise Chinese item names for `name`, but keep the semantic core:
   - "无糖西瓜汁" -> name can be "西瓜汁", modifiers should include ["无糖"]
   - "去皮大鸭腿" -> name can be "鸭腿", modifiers should include ["去皮"]
   - "冰美式" -> name can be "美式咖啡" or "黑咖啡", modifiers can include ["冰"]
6. For drinks, if the user said a quantity such as 500毫升 / 一大杯 / 一瓶, keep the amount/unit when possible.
7. If quantity is unknown, leave `amount` as null and `unit` as empty string.
8. If multiple foods are joined by connectors like "然后/又/还有", split them instead of merging.
9. If one phrase contains both a drink and a solid food, do not merge them into one item.
10. Return strict JSON only. No markdown. No explanation.

Normalization hints:
- Allowed unit examples: g, kg, ml, l, 杯, 碗, 个, 片, 块, 份, 瓶, 盒, 串, 只, 勺
- Spoken quantities like 半个 / 两颗 / 一大杯 should be normalized into `amount` + `unit` when possible.
- If confidence is low because the phrase is vague, still extract the most likely separate items instead of collapsing into one sentence-like name.

Few-shot examples:
Example 1
Input: "今天中午我吃了一个去皮大鸭腿、三个小翅根，喝了一杯无糖西瓜汁，大概500毫升。"
Output:
{{
  "type": "food",
  "foods": [
    {{"name": "鸭腿", "amount": 1, "unit": "个", "modifiers": ["去皮"], "confidence": 0.93}},
    {{"name": "小翅根", "amount": 3, "unit": "个", "modifiers": [], "confidence": 0.92}},
    {{"name": "西瓜汁", "amount": 500, "unit": "ml", "modifiers": ["无糖"], "confidence": 0.95}}
  ],
  "exercises": []
}}

Example 2
Input: "下午我喝了一大杯无糖拿铁，然后又吃了半个贝果。"
Output:
{{
  "type": "food",
  "foods": [
    {{"name": "拿铁", "amount": 1, "unit": "杯", "modifiers": ["无糖", "大杯"], "confidence": 0.9}},
    {{"name": "贝果", "amount": 0.5, "unit": "个", "modifiers": [], "confidence": 0.88}}
  ],
  "exercises": []
}}

Example 3
Input: "晚上吃了两颗鸡蛋一杯豆浆，饭后快走了40分钟。"
Output:
{{
  "type": "mixed",
  "foods": [
    {{"name": "鸡蛋", "amount": 2, "unit": "个", "modifiers": [], "confidence": 0.94}},
    {{"name": "豆浆", "amount": 1, "unit": "杯", "modifiers": [], "confidence": 0.91}}
  ],
  "exercises": [
    {{"exercise_name": "快走", "duration": 40, "exercise_type": "aerobic", "target_muscles": ["腿部"], "confidence": 0.9}}
  ]
}}

Schema:
{{
  "type": "food" | "exercise" | "mixed",
  "foods": [
    {{
      "name": "string",
      "amount": 0,
      "unit": "g|kg|ml|l|杯|碗|个|片|块|份|瓶|盒|串|只|勺|empty",
      "modifiers": ["string"],
      "confidence": 0.0
    }}
  ],
  "exercises": [
    {{
      "exercise_name": "string",
      "duration": 0,
      "exercise_type": "strength" | "aerobic",
      "target_muscles": ["string"],
      "confidence": 0.0
    }}
  ]
}}"""


def normalize_voice_understanding(payload):
    if not isinstance(payload, dict):
        return None

    foods_raw = payload.get('foods') or []
    exercises_raw = payload.get('exercises') or []
    foods = []
    exercises = []

    if isinstance(foods_raw, dict):
        foods_raw = [foods_raw]
    if isinstance(exercises_raw, dict):
        exercises_raw = [exercises_raw]

    for item in foods_raw:
        if not isinstance(item, dict):
            continue
        name = clean_text(first_present(item, 'name', 'food_name', 'food', 'label'), default='', max_len=120)
        if not name:
            continue
        modifiers = item.get('modifiers') or []
        if not isinstance(modifiers, list):
            modifiers = [modifiers] if modifiers else []
        normalized_modifiers = [
            clean_text(modifier, max_len=40)
            for modifier in modifiers
            if clean_text(modifier, max_len=40)
        ]
        foods.append({
            'name': name,
            'amount': clamp_number(item.get('amount'), default=0, min_value=0, max_value=5000, integer=False) if item.get('amount') not in (None, '') else None,
            'unit': clean_text(item.get('unit'), default='', max_len=16).lower(),
            'modifiers': normalized_modifiers,
            'confidence': clamp_number(item.get('confidence'), default=0.7, min_value=0, max_value=1, integer=False),
        })

    for item in exercises_raw:
        if not isinstance(item, dict):
            continue
        exercise_name = clean_text(first_present(item, 'exercise_name', 'name', 'exercise'), default='', max_len=120)
        if not exercise_name:
            continue
        muscles = item.get('target_muscles') or []
        if isinstance(muscles, str):
            muscles = [part.strip() for part in re.split(r'[,，/ ]+', muscles) if part.strip()]
        elif not isinstance(muscles, list):
            muscles = []
        exercises.append({
            'exercise_name': exercise_name,
            'duration': clamp_number(first_present(item, 'duration', 'duration_min', 'minutes'), default=0, min_value=0, max_value=600),
            'exercise_type': clean_text(item.get('exercise_type'), default='aerobic', max_len=16) or 'aerobic',
            'target_muscles': ','.join(clean_text(muscle, max_len=40) for muscle in muscles if clean_text(muscle, max_len=40)),
            'confidence': clamp_number(item.get('confidence'), default=0.7, min_value=0, max_value=1, integer=False),
        })

    result_type = clean_text(payload.get('type'), default='', max_len=16)
    if result_type not in ('food', 'exercise', 'mixed'):
        if foods and exercises:
            result_type = 'mixed'
        elif exercises:
            result_type = 'exercise'
        else:
            result_type = 'food'

    return {
        'type': result_type,
        'foods': foods,
        'exercises': exercises,
    }


def estimate_grams_from_voice_candidate(candidate):
    if not isinstance(candidate, dict):
        return 0
    amount = candidate.get('amount')
    if amount in (None, ''):
        return 0
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return 0
    unit = clean_text(candidate.get('unit'), default='', max_len=16).lower()
    if unit in ('g', '克'):
        grams = value
    elif unit in ('kg', '公斤'):
        grams = value * 1000
    elif unit in ('斤',):
        grams = value * 500
    elif unit in ('ml', '毫升', 'l', '升'):
        grams = value * (1000 if unit in ('l', '升') else 1)
    else:
        return 0
    return clamp_number(grams, default=0, min_value=0, max_value=2000)


def build_voice_food_estimation_prompt(food_candidates, source_text):
    return f"""You are the nutrition estimation layer for a Chinese food logging app.
The food items were already extracted from the user's original spoken sentence.
Estimate nutrition for each item separately and do not merge items together.

Original user sentence:
"{source_text}"

Extracted food items:
{json.dumps(food_candidates, ensure_ascii=False)}

Rules:
1. Return one output item for each input food item, in the same order.
2. Keep each output focused on that one item only. Never combine two foods into one result.
3. Use concise Chinese food names.
4. Respect modifiers from the original sentence. "无糖" means no added sugar, not automatically zero calories.
5. For drinks, estimate realistic grams from quantity, cup size, bottle size, or ml if provided.
6. For solids, estimate edible grams from count/unit and the food type.
7. Keep calories and macros internally consistent with the estimated weight.
8. If a drink is fruit juice, milk, soy milk, yogurt drink, latte, or other caloric beverage, it should not become zero calories just because it is "无糖".
9. Only plain water, soda water, plain unsweetened tea, and black coffee should be close to zero calories.
10. If the phrase sounds like a packaged or branded food, estimate using the most common Chinese-market nutrition profile for that product type.
11. If the input item name is still too vague, infer the most likely everyday Chinese food interpretation from the original sentence, but keep it as a single item.
12. Output strict JSON array only. No markdown. No explanation.

Reality checks before you answer:
- If the original sentence mentions multiple foods, you must still output separate entries item by item.
- A long conversational sentence must never appear as `food_name`.
- "大杯拿铁", "无糖西瓜汁", "半个贝果", "真空包装小翅根", "去皮鸭腿" should all produce realistic nonzero nutrition unless they are plain zero-calorie drinks.

Few-shot examples:
Example 1
Original sentence: "今天中午我吃了一个去皮大鸭腿、三个小翅根，喝了一杯无糖西瓜汁，大概500毫升。"
Extracted food items:
[{{"name":"鸭腿","amount":1,"unit":"个","modifiers":["去皮"],"confidence":0.93}},{{"name":"小翅根","amount":3,"unit":"个","modifiers":[],"confidence":0.92}},{{"name":"西瓜汁","amount":500,"unit":"ml","modifiers":["无糖"],"confidence":0.95}}]
Output:
[
  {{"food_name":"去皮鸭腿","calories":235,"protein":28,"carbs":0,"fat":13,"weight":160}},
  {{"food_name":"小翅根","calories":255,"protein":27,"carbs":0,"fat":17,"weight":180}},
  {{"food_name":"无糖西瓜汁","calories":150,"protein":2.5,"carbs":36,"fat":0.5,"weight":500}}
]

Example 2
Original sentence: "下午我喝了一大杯无糖拿铁，然后又吃了半个贝果。"
Extracted food items:
[{{"name":"拿铁","amount":1,"unit":"杯","modifiers":["无糖","大杯"],"confidence":0.9}},{{"name":"贝果","amount":0.5,"unit":"个","modifiers":[],"confidence":0.88}}]
Output:
[
  {{"food_name":"无糖拿铁","calories":160,"protein":8,"carbs":12,"fat":8,"weight":400}},
  {{"food_name":"贝果","calories":135,"protein":5,"carbs":27,"fat":1.5,"weight":55}}
]

Example 3
Original sentence: "晚上吃了两颗鸡蛋一杯豆浆，饭后快走了40分钟。"
Extracted food items:
[{{"name":"鸡蛋","amount":2,"unit":"个","modifiers":[],"confidence":0.94}},{{"name":"豆浆","amount":1,"unit":"杯","modifiers":[],"confidence":0.91}}]
Output:
[
  {{"food_name":"鸡蛋","calories":144,"protein":12.8,"carbs":1.5,"fat":9,"weight":100}},
  {{"food_name":"豆浆","calories":93,"protein":7.8,"carbs":3.6,"fat":4.8,"weight":300}}
]

Required schema:
[
  {{
    "food_name": "食物名称",
    "calories": 0,
    "protein": 0,
    "carbs": 0,
    "fat": 0,
    "weight": 0
  }}
]"""


def build_voice_single_pass_prompt(text):
    return f"""You are the single-pass voice analysis layer for a Chinese food logging app.
Read the user's original spoken sentence and directly return structured foods and exercises in one JSON object.
This is a real-world casual speech scenario, so the sentence may include multiple foods, drinks, and one exercise note.

User sentence:
"{text}"

Goals:
1. Extract each actually consumed food or drink as a separate item.
2. Extract exercise separately if present.
3. For each food, estimate realistic calories, protein, carbs, fat, and edible grams directly in this same step.
4. Respect modifiers such as 无糖, 去皮, 低脂, 大杯, 半个, 真空包装, 奥尔良.
5. "无糖" does not mean zero calories for fruit juice, milk drinks, yogurt drinks, or latte.
6. Only plain water, plain tea, soda water, and black coffee should be close to zero calories.
7. Never copy the whole sentence as food_name.
8. Return concise Chinese names.
9. If multiple foods are mentioned, do not merge them.
10. Output strict JSON only. No markdown. No explanation.

Return schema:
{{
  "type": "food|exercise|mixed",
  "foods": [
    {{
      "food_name": "食物名称",
      "calories": 0,
      "protein": 0,
      "carbs": 0,
      "fat": 0,
      "weight": 0
    }}
  ],
  "exercises": [
    {{
      "exercise_name": "运动名称",
      "duration": 0,
      "calories": 0,
      "exercise_type": "aerobic|strength",
      "target_muscles": ""
    }}
  ]
}}

Reality checks:
- "无糖西瓜汁 500毫升" should still have meaningful calories and carbs.
- "半个贝果" should not disappear.
- "两个茶叶蛋" should stay separate from drinks.
- Long conversational wording must never appear as food_name.
"""


def normalize_voice_food_estimates(payload, candidates):
    foods = parse_ai_multi_result(json.dumps(payload, ensure_ascii=False) if isinstance(payload, (dict, list)) else payload)
    if not foods:
        return None
    normalized = []
    for index, food in enumerate(foods):
        candidate = candidates[index] if index < len(candidates) else {}
        item = dict(food)
        fallback_name = clean_text(candidate.get('name'), default=item.get('food_name', ''), max_len=120)
        if fallback_name and food_name_looks_like_sentence(item.get('food_name')):
            item['food_name'] = fallback_name
        candidate_grams = estimate_grams_from_voice_candidate(candidate)
        if candidate_grams and not item.get('weight'):
            item['weight'] = candidate_grams
        item['weight'] = clamp_number(item.get('weight') or candidate_grams or 100, default=100, min_value=1, max_value=2000)
        normalized.append(item)
    return normalized or None


GENERIC_ZERO_CALORIE_NAME_HINTS = [
    '白水', '矿泉水', '纯净水', '气泡水', '苏打水', '无糖茶', '乌龙茶', '绿茶', '红茶', '黑咖啡', '美式', 'espresso', 'americano'
]

CALORIC_DRINK_NAME_HINTS = [
    '奶茶', '果茶', '果汁', '西瓜汁', '橙汁', '苹果汁', '豆浆', '牛奶', '酸奶', '拿铁', '摩卡', '卡布奇诺'
]


def is_expected_zero_calorie_item(food_name):
    lowered = normalize_name_key(food_name).lower()
    if any(keyword in lowered for keyword in CALORIC_DRINK_NAME_HINTS):
        return False
    return any(keyword.lower() in lowered for keyword in GENERIC_ZERO_CALORIE_NAME_HINTS)


def voice_food_estimate_needs_repair(food, candidate):
    if not isinstance(food, dict):
        return True
    weight = clamp_number(food.get('weight'), default=0, min_value=0, max_value=2000)
    calories = clamp_number(food.get('calories'), default=0, min_value=0, max_value=5000)
    protein = clamp_number(food.get('protein'), default=0, min_value=0, max_value=300, integer=False)
    carbs = clamp_number(food.get('carbs'), default=0, min_value=0, max_value=500, integer=False)
    fat = clamp_number(food.get('fat'), default=0, min_value=0, max_value=300, integer=False)
    name = clean_text(food.get('food_name'), default='', max_len=120)
    if not name or food_name_looks_like_sentence(name):
        return True
    if weight >= 50 and calories <= 0 and protein <= 0 and carbs <= 0 and fat <= 0 and not is_expected_zero_calorie_item(name):
        return True
    candidate_name = clean_text(candidate.get('name'), default='', max_len=120)
    if candidate_name and normalize_name_key(name) != normalize_name_key(candidate_name) and normalize_name_key(candidate_name) not in normalize_name_key(name):
        if food_name_looks_like_sentence(name):
            return True
    return False


def reestimate_voice_food_item(candidate, source_text, use_premium=False):
    if not isinstance(candidate, dict):
        return None
    modifiers = ', '.join(candidate.get('modifiers') or [])
    label = clean_text(candidate.get('name'), default='未知食物', max_len=120)
    if modifiers:
        label = f"{label} ({modifiers})"
    grams = estimate_grams_from_voice_candidate(candidate) or 100
    prompt = build_manual_food_estimate_prompt(label, grams, {})
    prompt += f'\n\nOriginal voice sentence: "{source_text}"\nRespect the original modifiers and quantity context from the sentence.'
    response = call_text_reasoning_llm(
        prompt_text=prompt,
        use_premium=use_premium,
        temperature=0.1,
        return_metadata=True,
    )
    parsed = normalize_manual_food_estimate(
        parse_json_payload(response.get('text')),
        label,
        grams,
        {},
    )
    return parsed, response


def fill_exercise_calories_with_rules(exercises, source_text):
    if not exercises:
        return []
    lowered = str(source_text or '').lower()
    explicit_calories = extract_calories_from_text(lowered)
    enriched = []
    for item in exercises:
        exercise = dict(item)
        duration = clamp_number(exercise.get('duration'), default=0, min_value=0, max_value=600)
        calories = clamp_number(exercise.get('calories'), default=0, min_value=0, max_value=5000)
        if calories <= 0:
            matched = None
            for entry in VOICE_EXERCISE_LIBRARY:
                if any(keyword in lowered for keyword in entry.get('keywords') or []):
                    if normalize_name_key(exercise.get('exercise_name')) == normalize_name_key(entry.get('exercise_name')):
                        matched = entry
                        break
                    if matched is None:
                        matched = entry
            if explicit_calories:
                calories = explicit_calories
            elif matched and duration > 0:
                calories = clamp_number(round(duration * matched['calories_per_min']), default=0, min_value=0, max_value=3000)
            elif duration > 0:
                calories = clamp_number(round(duration * 6), default=0, min_value=0, max_value=3000)
        exercise['calories'] = calories
        enriched.append(exercise)
    return enriched


VOICE_FOOD_LIBRARY = [
    {
        'keywords': ['鸡腿', '大鸡腿', '去皮鸡腿'],
        'food_name': '鸡腿',
        'default_grams': 140,
        'unit_grams': 140,
        'units': ['个', '只'],
        'nutrition_per_100g': {'calories': 195, 'protein': 19.0, 'carbs': 0.0, 'fat': 13.0},
    },
    {
        'keywords': ['鸭腿', '大鸭腿', '去皮鸭腿'],
        'food_name': '鸭腿',
        'default_grams': 160,
        'unit_grams': 160,
        'units': ['个', '只'],
        'nutrition_per_100g': {'calories': 220, 'protein': 18.0, 'carbs': 0.0, 'fat': 16.0},
    },
    {
        'keywords': ['翅根', '小翅根', '鸡翅根', '奥尔良翅根'],
        'food_name': '小翅根',
        'default_grams': 45,
        'unit_grams': 45,
        'units': ['个', '只'],
        'nutrition_per_100g': {'calories': 210, 'protein': 17.0, 'carbs': 4.0, 'fat': 14.0},
    },
    {
        'keywords': ['鸡翅', '翅中', '鸡中翅'],
        'food_name': '鸡翅',
        'default_grams': 55,
        'unit_grams': 55,
        'units': ['个', '只'],
        'nutrition_per_100g': {'calories': 240, 'protein': 17.0, 'carbs': 4.0, 'fat': 17.0},
    },
    {
        'keywords': ['茶叶蛋'],
        'food_name': '茶叶蛋',
        'default_grams': 55,
        'unit_grams': 55,
        'units': ['个', '颗', '枚'],
        'nutrition_per_100g': {'calories': 155, 'protein': 13.0, 'carbs': 1.8, 'fat': 10.0},
    },
    {
        'keywords': ['鸡蛋', '蛋'],
        'food_name': '鸡蛋',
        'default_grams': 50,
        'unit_grams': 50,
        'units': ['个', '颗', '枚'],
        'nutrition_per_100g': {'calories': 144, 'protein': 12.8, 'carbs': 1.5, 'fat': 8.8},
    },
    {
        'keywords': ['牛奶', 'milk'],
        'food_name': '牛奶',
        'default_grams': 250,
        'unit_grams': 250,
        'units': ['杯', '盒', '瓶'],
        'nutrition_per_100g': {'calories': 54, 'protein': 3.4, 'carbs': 5.0, 'fat': 3.2},
    },
    {
        'keywords': ['酸奶', 'yogurt', 'yoghurt'],
        'food_name': '酸奶',
        'default_grams': 180,
        'unit_grams': 180,
        'units': ['杯', '盒'],
        'nutrition_per_100g': {'calories': 72, 'protein': 3.6, 'carbs': 9.0, 'fat': 3.0},
    },
    {
        'keywords': ['米饭', '白饭'],
        'food_name': '米饭',
        'default_grams': 150,
        'unit_grams': 150,
        'units': ['碗'],
        'nutrition_per_100g': {'calories': 116, 'protein': 2.6, 'carbs': 25.9, 'fat': 0.3},
    },
    {
        'keywords': ['面条', '拌面', '汤面', '炒面', '拉面', '刀削面', '意面', '米粉', '河粉', '粉丝'],
        'food_name': '面条',
        'default_grams': 180,
        'unit_grams': 180,
        'units': ['碗'],
        'nutrition_per_100g': {'calories': 138, 'protein': 5.0, 'carbs': 25.0, 'fat': 2.0},
    },
    {
        'keywords': ['面包', '吐司', 'bread'],
        'food_name': '面包',
        'default_grams': 60,
        'unit_grams': 30,
        'units': ['片', '个'],
        'nutrition_per_100g': {'calories': 265, 'protein': 9.0, 'carbs': 49.0, 'fat': 3.2},
    },
    {
        'keywords': ['贝果', 'bagel'],
        'food_name': '贝果',
        'default_grams': 95,
        'unit_grams': 95,
        'units': ['个'],
        'nutrition_per_100g': {'calories': 270, 'protein': 10.0, 'carbs': 53.0, 'fat': 1.7},
    },
    {
        'keywords': ['鸡胸', '鸡胸肉'],
        'food_name': '鸡胸肉',
        'default_grams': 120,
        'unit_grams': 120,
        'units': ['块', '份'],
        'nutrition_per_100g': {'calories': 165, 'protein': 31.0, 'carbs': 0.0, 'fat': 3.6},
    },
    {
        'keywords': ['牛肉', 'beef'],
        'food_name': '牛肉',
        'default_grams': 120,
        'unit_grams': 120,
        'units': ['块', '份'],
        'nutrition_per_100g': {'calories': 250, 'protein': 26.0, 'carbs': 0.0, 'fat': 15.0},
    },
    {
        'keywords': ['猪肉', '排骨'],
        'food_name': '猪肉',
        'default_grams': 120,
        'unit_grams': 120,
        'units': ['块', '份'],
        'nutrition_per_100g': {'calories': 395, 'protein': 13.0, 'carbs': 2.0, 'fat': 37.0},
    },
    {
        'keywords': ['鱼', '三文鱼', '鳕鱼'],
        'food_name': '鱼肉',
        'default_grams': 120,
        'unit_grams': 120,
        'units': ['块', '份'],
        'nutrition_per_100g': {'calories': 120, 'protein': 20.0, 'carbs': 0.0, 'fat': 4.0},
    },
    {
        'keywords': ['虾'],
        'food_name': '虾',
        'default_grams': 100,
        'unit_grams': 100,
        'units': ['份'],
        'nutrition_per_100g': {'calories': 99, 'protein': 24.0, 'carbs': 0.2, 'fat': 0.3},
    },
    {
        'keywords': ['苹果', 'apple'],
        'food_name': '苹果',
        'default_grams': 180,
        'unit_grams': 180,
        'units': ['个'],
        'nutrition_per_100g': {'calories': 52, 'protein': 0.3, 'carbs': 14.0, 'fat': 0.2},
    },
    {
        'keywords': ['香蕉', 'banana'],
        'food_name': '香蕉',
        'default_grams': 120,
        'unit_grams': 120,
        'units': ['根'],
        'nutrition_per_100g': {'calories': 89, 'protein': 1.1, 'carbs': 23.0, 'fat': 0.3},
    },
    {
        'keywords': ['美式', '黑咖啡', '无糖咖啡', 'espresso', 'americano'],
        'food_name': '黑咖啡',
        'default_grams': 350,
        'unit_grams': 350,
        'units': ['杯'],
        'nutrition_per_100g': {'calories': 2, 'protein': 0.1, 'carbs': 0.3, 'fat': 0.0},
    },
    {
        'keywords': ['拿铁', '卡布奇诺', '摩卡', '咖啡'],
        'food_name': '咖啡饮品',
        'default_grams': 350,
        'unit_grams': 350,
        'units': ['杯'],
        'nutrition_per_100g': {'calories': 35, 'protein': 1.5, 'carbs': 4.5, 'fat': 1.2},
    },
    {
        'keywords': ['奶茶'],
        'food_name': '奶茶',
        'default_grams': 500,
        'unit_grams': 500,
        'units': ['杯'],
        'nutrition_per_100g': {'calories': 75, 'protein': 1.0, 'carbs': 15.0, 'fat': 1.2},
    },
    {
        'keywords': ['豆浆', '豆奶'],
        'food_name': '豆浆',
        'default_grams': 300,
        'unit_grams': 300,
        'units': ['杯', '瓶'],
        'nutrition_per_100g': {'calories': 31, 'protein': 2.6, 'carbs': 1.2, 'fat': 1.6},
    },
    {
        'keywords': ['西瓜汁'],
        'food_name': '西瓜汁',
        'default_grams': 300,
        'unit_grams': 300,
        'units': ['杯', '瓶', '份'],
        'nutrition_per_100g': {'calories': 30, 'protein': 0.5, 'carbs': 7.2, 'fat': 0.1},
    },
    {
        'keywords': ['橙汁', '橘汁'],
        'food_name': '橙汁',
        'default_grams': 300,
        'unit_grams': 300,
        'units': ['杯', '瓶', '份'],
        'nutrition_per_100g': {'calories': 45, 'protein': 0.7, 'carbs': 10.4, 'fat': 0.2},
    },
    {
        'keywords': ['苹果汁'],
        'food_name': '苹果汁',
        'default_grams': 300,
        'unit_grams': 300,
        'units': ['杯', '瓶', '份'],
        'nutrition_per_100g': {'calories': 46, 'protein': 0.1, 'carbs': 11.3, 'fat': 0.1},
    },
    {
        'keywords': ['果汁', '鲜榨汁', '果蔬汁'],
        'food_name': '果汁',
        'default_grams': 300,
        'unit_grams': 300,
        'units': ['杯', '瓶', '份'],
        'nutrition_per_100g': {'calories': 42, 'protein': 0.5, 'carbs': 10.0, 'fat': 0.1},
    },
    {
        'keywords': ['苏打水', '气泡水', '矿泉水', '纯净水', '白水', '乌龙茶', '绿茶', '红茶', '无糖茶'],
        'food_name': '无糖饮品',
        'default_grams': 350,
        'unit_grams': 350,
        'units': ['杯', '瓶'],
        'nutrition_per_100g': {'calories': 0, 'protein': 0.0, 'carbs': 0.0, 'fat': 0.0},
    },
]

VOICE_SENTENCE_FILLER_KEYWORDS = [
    '我', '今天', '早上', '早餐', '上午', '中午', '午餐', '下午', '晚上', '晚餐',
    '夜宵', '刚才', '喝了', '吃了', '大概', '大约', '左右', '的', '一下',
]

VOICE_ZERO_CALORIE_DRINK_HINTS = [
    '白水', '矿泉水', '纯净水', '苏打水', '气泡水', '无糖茶', '绿茶', '乌龙茶',
    '红茶', '黑咖啡', '美式', 'zero', '零度', '无糖可乐',
]

VOICE_SENTENCE_CONNECTOR_HINTS = [
    '，', ',', '。', '.', '；', ';', '、', '和', '跟', '还有', '然后', '再', '又', '以及', '并且', '外加', '顺便'
]

VOICE_ACTION_PATTERNS = [
    r'吃了?', r'喝了?', r'又吃了?', r'又喝了?', r'跑了?', r'快走了?', r'走了?', r'骑了?', r'游了?', r'练了?'
]

VOICE_ACTION_KEYWORDS = [
    '吃了', '喝了', '又吃了', '又喝了', '跑了', '快走了', '走了', '骑了', '游了', '练了', '运动了', '训练了'
]


def find_voice_food_library_entry(text):
    lowered = str(text or '').lower()
    best_entry = None
    best_keyword_len = -1
    for entry in VOICE_FOOD_LIBRARY:
        for keyword in entry.get('keywords') or []:
            keyword_text = str(keyword or '').lower()
            if keyword_text and keyword_text in lowered and len(keyword_text) > best_keyword_len:
                best_entry = entry
                best_keyword_len = len(keyword_text)
    return best_entry


def build_food_from_voice_library_entry(entry, grams):
    grams_value = clamp_number(grams or entry.get('default_grams') or 100, default=100, min_value=1, max_value=2000)
    nutrition = entry['nutrition_per_100g']
    scale = grams_value / 100.0
    return {
        'food_name': entry['food_name'],
        'calories': clamp_number(round(nutrition['calories'] * scale), default=0, min_value=0, max_value=5000),
        'protein': clamp_number(round(nutrition['protein'] * scale, 1), default=0, min_value=0, max_value=300, integer=False),
        'carbs': clamp_number(round(nutrition['carbs'] * scale, 1), default=0, min_value=0, max_value=500, integer=False),
        'fat': clamp_number(round(nutrition['fat'] * scale, 1), default=0, min_value=0, max_value=300, integer=False),
        'weight': grams_value,
    }


def food_name_looks_like_sentence(name):
    text = clean_text(name, default='', max_len=120)
    if not text:
        return True
    filler_hits = sum(1 for token in VOICE_SENTENCE_FILLER_KEYWORDS if token and token in text)
    connector_hits = sum(1 for token in VOICE_SENTENCE_CONNECTOR_HINTS if token and token in text)
    if connector_hits >= 1 and len(text) >= 10:
        return True
    if filler_hits >= 2:
        return True
    if filler_hits >= 1 and len(text) >= 16:
        return True
    return len(text) >= 28


def is_probably_zero_calorie_drink(text):
    sample = str(text or '').lower()
    return any(keyword.lower() in sample for keyword in VOICE_ZERO_CALORIE_DRINK_HINTS)


def food_item_has_nutrition_signal(food):
    if not isinstance(food, dict):
        return False
    calories = clamp_number(food.get('calories'), default=0, min_value=0, max_value=5000)
    protein = clamp_number(food.get('protein'), default=0, min_value=0, max_value=300, integer=False)
    carbs = clamp_number(food.get('carbs'), default=0, min_value=0, max_value=500, integer=False)
    fat = clamp_number(food.get('fat'), default=0, min_value=0, max_value=300, integer=False)
    return any(value > 0 for value in (calories, protein, carbs, fat))


def validate_food_result(food, source_text=''):
    if not isinstance(food, dict):
        return False, 'invalid_food_object'
    name = clean_text(food.get('food_name'), default='', max_len=120)
    if not name:
        return False, 'missing_food_name'
    if food_name_looks_like_sentence(name):
        return False, 'sentence_like_food_name'

    weight = clamp_number(food.get('weight'), default=0, min_value=0, max_value=2000)
    sample_text = f"{name} {clean_text(source_text, max_len=200)}".strip()
    if (
        weight >= 50
        and not food_item_has_nutrition_signal(food)
        and not is_expected_zero_calorie_item(name)
        and not is_probably_zero_calorie_drink(sample_text)
    ):
        return False, 'zero_nutrition_nonzero_food'

    return True, 'ok'


def split_valid_food_results(foods, source_text=''):
    valid_foods = []
    rejected_foods = []
    for item in list(foods or []):
        is_valid, reason = validate_food_result(item, source_text=source_text)
        if is_valid:
            valid_foods.append(item)
            continue
        rejected_foods.append({
            'food_name': clean_text((item or {}).get('food_name'), default='', max_len=120),
            'reason': reason,
        })
    return valid_foods, rejected_foods


def count_voice_quantity_mentions(text):
    sample = clean_text(text, default='', max_len=240)
    if not sample:
        return 0
    pattern = r'([0-9]+(?:\.\d+)?|半|一|二|两|三|四|五|六|七|八|九|十)\s*(?:大|小)?\s*(个|颗|枚|杯|瓶|份|块|根|只|碗|盒|片|串|勺|ml|毫升|g|克|公斤|kg)'
    return len(re.findall(pattern, sample, re.IGNORECASE))


def count_voice_action_mentions(text):
    sample = clean_text(text, default='', max_len=240)
    if not sample:
        return 0
    count = 0
    for segment in split_voice_segments(sample):
        lowered = segment.lower()
        if any(keyword in lowered for keyword in VOICE_ACTION_KEYWORDS):
            count += 1
    return count


def should_reject_rule_based_partial_result(parsed, rejected_foods, parse_meta, source_text=''):
    provider = str((parse_meta or {}).get('provider') or '')
    model = str((parse_meta or {}).get('model') or '')
    if provider != 'rule_based' and 'voice_rule_fallback' not in model:
        return False

    total_items = len((parsed or {}).get('foods') or []) + len((parsed or {}).get('exercises') or [])
    quantity_mentions = count_voice_quantity_mentions(source_text)
    action_mentions = count_voice_action_mentions(source_text)
    has_connector = any(token in str(source_text or '') for token in VOICE_SENTENCE_CONNECTOR_HINTS)
    looks_complex = has_connector or quantity_mentions >= 2 or action_mentions >= 2
    if not looks_complex:
        return False
    if rejected_foods:
        return True
    if total_items <= 0:
        return True
    if action_mentions >= 2 and total_items < action_mentions:
        return True
    if quantity_mentions >= 3 and total_items <= 1:
        return True
    return False


def repair_voice_foods_with_library(foods, source_text=''):
    repaired = []
    source = clean_text(source_text, default='', max_len=200)
    for item in list(foods or []):
        if not isinstance(item, dict):
            continue
        food = dict(item)
        food_name = clean_text(food.get('food_name'), default='', max_len=120)
        grams = clamp_number(food.get('weight'), default=0, min_value=0, max_value=2000)
        search_text = f"{food_name} {source}".strip()
        entry = find_voice_food_library_entry(search_text)
        if entry:
            if not grams:
                grams = extract_weight_from_text(search_text) or int(entry.get('default_grams') or 100)
            fallback_food = build_food_from_voice_library_entry(entry, grams)
            current_calories = clamp_number(food.get('calories'), default=0, min_value=0, max_value=5000)
            current_protein = clamp_number(food.get('protein'), default=0, min_value=0, max_value=300, integer=False)
            current_carbs = clamp_number(food.get('carbs'), default=0, min_value=0, max_value=500, integer=False)
            current_fat = clamp_number(food.get('fat'), default=0, min_value=0, max_value=300, integer=False)
            should_fill_from_library = (
                (current_calories <= 0 and current_protein <= 0 and current_carbs <= 0 and current_fat <= 0)
                or (
                    current_calories <= 5
                    and ('汁' in search_text or '奶' in search_text or '豆浆' in search_text or '酸奶' in search_text)
                    and not is_probably_zero_calorie_drink(search_text)
                )
            )
            if should_fill_from_library:
                food.update(fallback_food)
            else:
                food['weight'] = clamp_number(grams or fallback_food['weight'], default=fallback_food['weight'], min_value=1, max_value=2000)
            if food_name_looks_like_sentence(food_name):
                food['food_name'] = fallback_food['food_name']
        elif food_name_looks_like_sentence(food_name):
            simplified = re.sub(r'^(我|今天|早上|早餐|上午|中午|午餐|下午|晚上|晚餐|夜宵)', '', food_name)
            simplified = re.sub(r'(吃了|喝了)', '', simplified)
            simplified = re.sub(r'(大概|大约|左右|一下|的)$', '', simplified)
            simplified = clean_text(simplified, default=food_name, max_len=40)
            food['food_name'] = simplified or food_name
        repaired.append(food)
    return repaired

VOICE_EXERCISE_LIBRARY = [
    {'keywords': ['跑步', '慢跑', '快跑', 'run'], 'exercise_name': '跑步', 'exercise_type': 'aerobic', 'calories_per_min': 10, 'target_muscles': ''},
    {'keywords': ['跑了'], 'exercise_name': '跑步', 'exercise_type': 'aerobic', 'calories_per_min': 10, 'target_muscles': ''},
    {'keywords': ['快走', '散步', '走路', 'walk'], 'exercise_name': '步行', 'exercise_type': 'aerobic', 'calories_per_min': 4, 'target_muscles': ''},
    {'keywords': ['骑车', '骑行', '单车', '自行车', 'bike', 'cycling'], 'exercise_name': '骑行', 'exercise_type': 'aerobic', 'calories_per_min': 8, 'target_muscles': ''},
    {'keywords': ['游泳', 'swim'], 'exercise_name': '游泳', 'exercise_type': 'aerobic', 'calories_per_min': 9, 'target_muscles': ''},
    {'keywords': ['跳绳'], 'exercise_name': '跳绳', 'exercise_type': 'aerobic', 'calories_per_min': 12, 'target_muscles': ''},
    {'keywords': ['hiit', '间歇'], 'exercise_name': 'HIIT', 'exercise_type': 'aerobic', 'calories_per_min': 10, 'target_muscles': ''},
    {'keywords': ['瑜伽', 'yoga'], 'exercise_name': '瑜伽', 'exercise_type': 'aerobic', 'calories_per_min': 4, 'target_muscles': '核心'},
    {'keywords': ['深蹲', '腿举', '弓步'], 'exercise_name': '腿部力量训练', 'exercise_type': 'strength', 'calories_per_min': 6, 'target_muscles': '腿部,臀部'},
    {'keywords': ['卧推', '胸推', '俯卧撑'], 'exercise_name': '上肢推训练', 'exercise_type': 'strength', 'calories_per_min': 6, 'target_muscles': '胸部,肩部,肱三头肌'},
    {'keywords': ['引体向上', '划船', '硬拉'], 'exercise_name': '上肢拉训练', 'exercise_type': 'strength', 'calories_per_min': 6, 'target_muscles': '背部,肱二头肌'},
]

VOICE_COUNT_MAP = {
    '半': 0.5,
    '一': 1,
    '二': 2,
    '两': 2,
    '三': 3,
    '四': 4,
    '五': 5,
    '六': 6,
    '七': 7,
    '八': 8,
    '九': 9,
    '十': 10,
}


def parse_simple_number(raw_value):
    if raw_value is None:
        return None
    text = str(raw_value).strip()
    if not text:
        return None
    try:
        return float(text)
    except Exception:
        return VOICE_COUNT_MAP.get(text)


def extract_duration_minutes_from_text(text):
    total_minutes = 0.0
    hour_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:小时|h|hr|hrs)', text, re.IGNORECASE)
    if hour_match:
        total_minutes += float(hour_match.group(1)) * 60
    minute_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:分钟|分|mins?|minutes?)', text, re.IGNORECASE)
    if minute_match:
        total_minutes += float(minute_match.group(1))
    if total_minutes > 0:
        return int(round(total_minutes))
    return 0


def extract_calories_from_text(text):
    match = re.search(r'(\d+(?:\.\d+)?)\s*(?:kcal|千卡|大卡|卡路里)', text, re.IGNORECASE)
    if not match:
        return 0
    return clamp_number(match.group(1), default=0, min_value=0, max_value=5000)


def extract_weight_from_text(text):
    match = re.search(r'(\d+(?:\.\d+)?)\s*(?:g|克|kg|公斤|斤|ml|毫升|mL)', text, re.IGNORECASE)
    if not match:
        return 0
    value = float(match.group(1))
    unit_match = re.search(r'(\d+(?:\.\d+)?)\s*(g|克|kg|公斤|斤|ml|毫升|mL)', text, re.IGNORECASE)
    unit = (unit_match.group(2).lower() if unit_match else 'g')
    if unit in ('kg', '公斤'):
        value *= 1000
    elif unit == '斤':
        value *= 500
    return int(round(value))


def extract_count_for_keywords(text, keywords, units):
    if not keywords or not units:
        return None
    keyword_pattern = '(?:' + '|'.join(re.escape(keyword) for keyword in keywords) + ')'
    unit_pattern = '(?:' + '|'.join(re.escape(unit) for unit in units) + ')'
    patterns = [
        rf'([0-9]+(?:\.\d+)?|半|一|二|两|三|四|五|六|七|八|九|十)\s*(?:大|小)?\s*{unit_pattern}[^\n，。,;；]{{0,8}}{keyword_pattern}',
        rf'{keyword_pattern}[^\n，。,;；]{{0,8}}([0-9]+(?:\.\d+)?|半|一|二|两|三|四|五|六|七|八|九|十)\s*(?:大|小)?\s*{unit_pattern}',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            parsed = parse_simple_number(match.group(1))
            if parsed is not None:
                return parsed
    return None


def split_voice_segments(text):
    sample = clean_text(text, default='', max_len=240)
    if not sample:
        return []
    parts = re.split(r'[，。,；;、]|(?:然后|还有|并且|以及|顺便|外加|再|又|和|跟)', sample)
    cleaned = []
    for part in parts:
        piece = clean_text(part, default='', max_len=120).strip()
        if piece:
            quantity_like = re.search(
                r'([0-9]+(?:\.\d+)?|半|一|二|两|三|四|五|六|七|八|九|十)\s*(?:大|小)?\s*(个|颗|枚|杯|瓶|份|块|根|只|碗|盒|片|串|勺|ml|毫升|g|克|公斤|kg)',
                piece,
                re.IGNORECASE,
            )
            if cleaned and not find_foods_in_segment(piece) and quantity_like:
                cleaned[-1] = f"{cleaned[-1]} {piece}".strip()
            else:
                cleaned.append(piece)
    return cleaned or [sample]


def find_foods_in_segment(segment):
    lowered = str(segment or '').lower()
    matches = []
    for entry in VOICE_FOOD_LIBRARY:
        best_keyword = ''
        for keyword in entry.get('keywords') or []:
            if keyword and str(keyword).lower() in lowered:
                if len(str(keyword)) > len(best_keyword):
                    best_keyword = str(keyword)
        if best_keyword:
            matches.append((len(best_keyword), best_keyword, entry))
    matches.sort(key=lambda item: item[0], reverse=True)
    selected = []
    selected_keywords = []
    for _, keyword, entry in matches:
        lowered_keyword = keyword.lower()
        if any(lowered_keyword in chosen or chosen in lowered_keyword for chosen in selected_keywords):
            continue
        selected.append(entry)
        selected_keywords.append(lowered_keyword)
    return selected


def build_rule_based_voice_result(raw_text):
    text = clean_text(raw_text, default='', max_len=200).strip()
    lowered = text.lower()
    foods = []
    exercises = []
    used_food_names = set()
    used_exercise_names = set()

    explicit_calories = extract_calories_from_text(lowered)

    for entry in VOICE_EXERCISE_LIBRARY:
        if not any(keyword in lowered for keyword in entry['keywords']):
            continue
        exercise_name = entry['exercise_name']
        if exercise_name in used_exercise_names:
            continue
        duration = extract_duration_minutes_from_text(lowered) or 30
        calories = explicit_calories or clamp_number(
            round(duration * entry['calories_per_min']),
            default=0,
            min_value=0,
            max_value=3000
        )
        exercises.append({
            'exercise_name': exercise_name,
            'calories': calories,
            'duration': duration,
            'exercise_type': entry['exercise_type'],
            'target_muscles': entry['target_muscles'],
        })
        used_exercise_names.add(exercise_name)

    if text:
        for segment in split_voice_segments(text):
            segment_matches = find_foods_in_segment(segment)
            if not segment_matches:
                continue
            for entry in segment_matches:
                food_name = entry['food_name']
                if food_name in used_food_names:
                    continue
                count = extract_count_for_keywords(segment.lower(), entry['keywords'], entry.get('units') or [])
                grams = extract_weight_from_text(segment.lower()) or 0
                if not grams and count and entry.get('unit_grams'):
                    grams = int(round(float(count) * float(entry['unit_grams'])))
                if not grams:
                    grams = int(entry['default_grams'])
                nutrition = entry['nutrition_per_100g']
                scale = grams / 100.0
                foods.append({
                    'food_name': food_name,
                    'calories': clamp_number(round(nutrition['calories'] * scale), default=0, min_value=0, max_value=5000),
                    'protein': clamp_number(round(nutrition['protein'] * scale, 1), default=0, min_value=0, max_value=300, integer=False),
                    'carbs': clamp_number(round(nutrition['carbs'] * scale, 1), default=0, min_value=0, max_value=500, integer=False),
                    'fat': clamp_number(round(nutrition['fat'] * scale, 1), default=0, min_value=0, max_value=300, integer=False),
                    'weight': clamp_number(grams, default=100, min_value=1, max_value=2000),
                })
                used_food_names.add(food_name)

    if not foods and not exercises and text:
        inferred_type = 'exercise' if any(keyword in lowered for keyword in ['运动', '训练', '跑', '走', '骑', '游泳', '深蹲', '卧推']) else 'food'
        if inferred_type == 'exercise':
            duration = extract_duration_minutes_from_text(lowered) or 30
            exercises.append({
                'exercise_name': text[:40],
                'calories': explicit_calories or clamp_number(duration * 6, default=180, min_value=0, max_value=3000),
                'duration': duration,
                'exercise_type': 'aerobic',
                'target_muscles': '',
            })

    if foods and exercises:
        data_type = 'mixed'
    elif exercises:
        data_type = 'exercise'
    else:
        data_type = 'food'

    foods = repair_voice_foods_with_library(foods, source_text=text)

    return {
        'type': data_type,
        'foods': foods,
        'exercises': exercises,
    }

@app.route('/api/voice-input', methods=['POST'])
@token_or_local_app_required
@limiter.limit("10 per minute", key_func=user_or_ip_limit_key)
def voice_input():
    """Voice input: single-pass structured understanding + nutrition estimate."""
    data = request.json or {}
    text = clean_text(data.get('text', ''), default='', max_len=MAX_TEXT_INPUT_CHARS)
    client_action_id = clean_text(data.get('client_action_id') or get_client_action_id(), max_len=96)
    username = get_current_username()
    use_premium = request_premium_enabled(username, data)

    if not text:
        return jsonify({"error": "\u8bed\u97f3\u6587\u672c\u4e3a\u7a7a"}), 400
    if len(text) > MAX_TEXT_INPUT_CHARS:
        return jsonify({"error": "Input text is too long"}), 413

    try:
        reset_request_trace('voice_input')
        parse_meta = {"fallback_trace": []}
        rejected_foods = []
        try:
            response = call_text_reasoning_llm(
                prompt_text=build_voice_single_pass_prompt(text),
                use_premium=use_premium,
                temperature=0.1,
                return_metadata=True
            )
            parsed = parse_voice_input_result(response.get('text', ''))
            parse_meta = {
                "provider": response.get('provider') or 'unknown',
                "model": response.get('model') or '',
                "latency_ms": int(response.get('latency_ms') or 0),
                "fallback_trace": list(response.get('fallback_trace') or []),
            }
        except LLMChainExhaustedError as text_chain_err:
            print(f"Voice single-pass chain exhausted: {text_chain_err}")
            parsed = build_rule_based_voice_result(text)
            parse_meta = {
                "provider": "rule_based",
                "model": "voice_single_pass_fallback",
                "latency_ms": 0,
                "fallback_trace": list(text_chain_err.fallback_trace or []) + ["rule_based:ok"],
            }
        if parsed is not None:
            parsed_foods, rejected_foods = split_valid_food_results(parsed.get('foods') or [], source_text=text)
            parsed['foods'] = parsed_foods
            if parsed['foods'] and parsed['exercises']:
                parsed['type'] = 'mixed'
            elif parsed['exercises']:
                parsed['type'] = 'exercise'
            else:
                parsed['type'] = 'food'
            if should_reject_rule_based_partial_result(parsed, rejected_foods, parse_meta, source_text=text):
                parsed['foods'] = []
                parsed['exercises'] = []

        if parsed is None or (not parsed['foods'] and not parsed['exercises']):
            parsed = build_rule_based_voice_result(text)
            parsed_foods, fallback_rejected_foods = split_valid_food_results(parsed.get('foods') or [], source_text=text)
            rejected_foods.extend(fallback_rejected_foods)
            parsed['foods'] = parsed_foods
            if parsed['foods'] and parsed['exercises']:
                parsed['type'] = 'mixed'
            elif parsed['exercises']:
                parsed['type'] = 'exercise'
            else:
                parsed['type'] = 'food'
            parse_meta = {
                "provider": "rule_based",
                "model": "voice_rule_fallback",
                "latency_ms": parse_meta.get('latency_ms', 0),
                "fallback_trace": list(parse_meta.get('fallback_trace') or []) + ["rule_based:ok"],
            }
            if should_reject_rule_based_partial_result(parsed, rejected_foods, parse_meta, source_text=text):
                parsed['foods'] = []
                parsed['exercises'] = []
        if parsed is None or (not parsed['foods'] and not parsed['exercises']):
            analysis_meta = {
                **attach_request_trace(parse_meta),
                "premium_requested": bool(use_premium),
                "premium_used": False,
            }
            if client_action_id:
                analysis_meta['client_action_id'] = client_action_id
            if rejected_foods:
                analysis_meta['rejected_foods'] = rejected_foods
            log_request_trace('voice_input_incomplete')
            log_client_action_summary('voice_input', client_action_id, analysis_meta)
            return jsonify({
                "error": "没能稳定拆分出具体食物或运动，请换一种更短、更直接的说法再试。",
                "error_code": "voice_analysis_incomplete",
                "analysis_meta": analysis_meta,
            }), 422

        session_id = str(uuid.uuid4())
        saved_foods = []
        saved_exercises = []

        for food in parsed['foods']:
            food['id'] = uuid.uuid4().hex
            food['portion'] = 1.0
            saved_foods.append(food)

        for ex in parsed['exercises']:
            ex['id'] = uuid.uuid4().hex
            saved_exercises.append(ex)

        success_meta = attach_request_trace({
            **parse_meta,
            "premium_requested": bool(use_premium),
            "premium_used": 'openai_compatible' in str(parse_meta.get('provider') or ''),
            "rejected_foods": rejected_foods,
        })
        if client_action_id:
            success_meta['client_action_id'] = client_action_id
        log_request_trace('voice_input_success')
        log_client_action_summary('voice_input', client_action_id, success_meta)
        return jsonify({
            "success": True,
            "type": parsed['type'],
            "session_id": session_id,
            "foods": saved_foods,
            "exercises": saved_exercises,
            "image_url": "",
            "analysis_meta": success_meta
        })

    except Exception as e:
        print(f"Voice input error: {e}")
        parsed = build_rule_based_voice_result(text)
        parsed_foods, rejected_foods = split_valid_food_results(parsed.get('foods') or [], source_text=text)
        parsed['foods'] = parsed_foods
        if parsed['foods'] and parsed['exercises']:
            parsed['type'] = 'mixed'
        elif parsed['exercises']:
            parsed['type'] = 'exercise'
        else:
            parsed['type'] = 'food'
        exception_parse_meta = {
            "provider": "rule_based",
            "model": "voice_parser_exception_fallback",
            "latency_ms": 0,
            "fallback_trace": ["rule_based:exception_fallback"],
        }
        if should_reject_rule_based_partial_result(parsed, rejected_foods, exception_parse_meta, source_text=text):
            parsed['foods'] = []
            parsed['exercises'] = []
        if parsed and (parsed['foods'] or parsed['exercises']):
            session_id = str(uuid.uuid4())
            for food in parsed['foods']:
                food['id'] = uuid.uuid4().hex
                food['portion'] = 1.0
            for ex in parsed['exercises']:
                ex['id'] = uuid.uuid4().hex
            success_meta = attach_request_trace({
                "provider": "rule_based",
                "model": "voice_parser_exception_fallback",
                "latency_ms": 0,
                "fallback_trace": ["rule_based:exception_fallback"],
                "premium_requested": bool(use_premium),
                "premium_used": False,
                "rejected_foods": rejected_foods,
            })
            if client_action_id:
                success_meta['client_action_id'] = client_action_id
            log_request_trace('voice_input_exception_fallback_success')
            log_client_action_summary('voice_input', client_action_id, success_meta)
            return jsonify({
                "success": True,
                "type": parsed['type'],
                "session_id": session_id,
                "foods": parsed['foods'],
                "exercises": parsed['exercises'],
                "image_url": "",
                "analysis_meta": success_meta
            })
        return jsonify({
            "error": "这段语音内容没能稳定解析出具体食物，请换一种更短的说法再试。",
            "error_code": "voice_analysis_incomplete",
            "analysis_meta": attach_request_trace({
                "provider": "rule_based",
                "model": "voice_parser_exception_empty",
                "latency_ms": 0,
                "fallback_trace": ["rule_based:exception_fallback"],
                "premium_requested": bool(use_premium),
                "premium_used": False,
                "rejected_foods": rejected_foods,
                "client_action_id": client_action_id,
            })
        }), 422


@app.route('/api/speech-to-text', methods=['POST'])
@token_or_local_app_required
@limiter.limit("10 per minute", key_func=user_or_ip_limit_key)
def speech_to_text():
    reset_request_trace('speech_to_text')
    client_action_id = get_client_action_id()
    """Transcribe uploaded audio into text."""
    if 'audio' not in request.files:
        return jsonify({"error": MSG_AUDIO_FILE_MISSING}), 400

    audio_file = request.files['audio']
    if audio_file.filename == '':
        return jsonify({"error": MSG_AUDIO_FILE_EMPTY_NAME}), 400

    mime_type = audio_file.content_type
    if not mime_type or mime_type == 'application/octet-stream':
        if audio_file.filename.endswith('.mp4'):
            mime_type = 'audio/mp4'
        elif audio_file.filename.endswith('.webm'):
            mime_type = 'audio/webm'
        else:
            mime_type = 'audio/webm'

    try:
        audio_bytes = audio_file.read()
        if len(audio_bytes) < 100:
            return jsonify({"error": MSG_AUDIO_FILE_TOO_SMALL}), 400
        if len(audio_bytes) > MAX_AUDIO_UPLOAD_BYTES:
            return jsonify({"error": "Audio file is too large"}), 413

        try:
            transcription, stt_meta = run_speech_to_text_pipeline(
                audio_bytes,
                mime_type,
                filename=audio_file.filename,
                client_action_id=client_action_id,
            )
        except RuntimeError:
            stt_meta = {
                "client_action_id": client_action_id,
                "request_trace": {},
            }
            trace_meta = attach_request_trace({
                "client_action_id": client_action_id,
            })
            stt_meta["request_trace"] = trace_meta
            log_request_trace('speech_to_text_unavailable')
            log_client_action_summary('speech_to_text', client_action_id, trace_meta)
            return jsonify({
                "error": MSG_STT_UNAVAILABLE,
                "error_code": "stt_unavailable",
                "stt_meta": stt_meta,
            }), 503

        print(f"Speech transcription result: {transcription}")
        trace_meta = attach_request_trace({
            "client_action_id": client_action_id,
        })
        stt_meta["request_trace"] = trace_meta
        log_request_trace('speech_to_text_success')
        log_client_action_summary('speech_to_text', client_action_id, trace_meta)
        return jsonify({"text": transcription, "stt_meta": stt_meta})

    except Exception as e:
        print(f"Speech to text API error: {e}")
        log_request_trace('speech_to_text_exception')
        return jsonify({
            "error": MSG_STT_UNAVAILABLE,
            "error_code": "stt_unavailable",
            "stt_meta": {
                "client_action_id": client_action_id,
                "request_trace": attach_request_trace({
                    "client_action_id": client_action_id,
                }),
            },
        }), 503
@app.route('/api/voice-audio', methods=['POST'])
@token_or_local_app_required
@limiter.limit("10 per minute", key_func=user_or_ip_limit_key)
def voice_audio():
    reset_request_trace('voice_audio')
    client_action_id = get_client_action_id()
    if 'audio' not in request.files:
        return jsonify({"error": MSG_AUDIO_FILE_MISSING}), 400

    audio_file = request.files['audio']
    if audio_file.filename == '':
        return jsonify({"error": MSG_AUDIO_FILE_EMPTY_NAME}), 400

    mime_type = audio_file.content_type
    if not mime_type or mime_type == 'application/octet-stream':
        if audio_file.filename.endswith('.mp4'):
            mime_type = 'audio/mp4'
        elif audio_file.filename.endswith('.webm'):
            mime_type = 'audio/webm'
        else:
            mime_type = 'audio/webm'

    username = get_current_username()
    use_premium = request_premium_enabled(username)

    try:
        audio_bytes = audio_file.read()
        if len(audio_bytes) < 100:
            return jsonify({"error": MSG_AUDIO_FILE_TOO_SMALL}), 400
        if len(audio_bytes) > MAX_AUDIO_UPLOAD_BYTES:
            return jsonify({"error": "Audio file is too large"}), 413

        parse_meta = {"fallback_trace": []}
        rejected_foods = []
        try:
            response = call_voice_audio_direct_llm(
                audio_bytes,
                mime_type,
                use_premium=use_premium,
            )
            parsed, transcription = normalize_direct_voice_audio_result(response)
            parse_meta = {
                "provider": response.get('provider') or 'unknown',
                "model": response.get('model') or '',
                "latency_ms": int(response.get('latency_ms') or 0),
                "fallback_trace": list(response.get('fallback_trace') or []),
                "mode": "direct_audio_model",
            }
        except LLMChainExhaustedError as audio_chain_err:
            print(f"Voice audio direct model chain exhausted: {audio_chain_err}")
            parse_meta = {
                "provider": "unavailable",
                "model": "direct_audio_model",
                "latency_ms": 0,
                "fallback_trace": list(audio_chain_err.fallback_trace or []),
                "mode": "direct_audio_model",
            }
            analysis_trace = attach_request_trace({
                **parse_meta,
                "premium_requested": bool(use_premium),
                "premium_used": False,
                "client_action_id": client_action_id,
            })
            log_request_trace('voice_audio_model_unavailable')
            log_client_action_summary('voice_audio', client_action_id, analysis_trace)
            return jsonify({
                "error": MSG_VOICE_AUDIO_FAILED,
                "error_code": "voice_audio_model_unavailable",
                "transcription": "",
                "stt_meta": {
                    "mode": "not_used_direct_audio_model",
                    "client_action_id": client_action_id,
                    "mime_type": mime_type,
                    "filename": clean_text(audio_file.filename, max_len=120),
                    "bytes": len(audio_bytes or b''),
                    "request_trace": analysis_trace,
                },
                "analysis_meta": analysis_trace,
            }), 503

        transcription = clean_text(transcription, default='', max_len=MAX_TEXT_INPUT_CHARS)
        if parsed is not None:
            parsed['foods'] = repair_voice_foods_with_library(parsed.get('foods') or [], source_text=transcription)
            parsed_foods, rejected_foods = split_valid_food_results(parsed.get('foods') or [], source_text=transcription)
            parsed['foods'] = parsed_foods
            parsed['exercises'] = fill_exercise_calories_with_rules(parsed.get('exercises') or [], transcription)
            if parsed['foods'] and parsed['exercises']:
                parsed['type'] = 'mixed'
            elif parsed['exercises']:
                parsed['type'] = 'exercise'
            else:
                parsed['type'] = 'food'

        if (parsed is None or (not parsed['foods'] and not parsed['exercises'])) and transcription:
            parsed = build_rule_based_voice_result(transcription)
            parsed_foods, fallback_rejected_foods = split_valid_food_results(parsed.get('foods') or [], source_text=transcription)
            rejected_foods.extend(fallback_rejected_foods)
            parsed['foods'] = parsed_foods
            parsed['exercises'] = fill_exercise_calories_with_rules(parsed.get('exercises') or [], transcription)
            if parsed['foods'] and parsed['exercises']:
                parsed['type'] = 'mixed'
            elif parsed['exercises']:
                parsed['type'] = 'exercise'
            else:
                parsed['type'] = 'food'
            parse_meta = {
                "provider": "rule_based",
                "model": "direct_audio_transcript_rule_fallback",
                "latency_ms": parse_meta.get('latency_ms', 0),
                "mode": "direct_audio_model",
                "fallback_trace": list(parse_meta.get('fallback_trace') or []) + ["rule_based:ok"],
            }
            if should_reject_rule_based_partial_result(parsed, rejected_foods, parse_meta, source_text=transcription):
                parsed['foods'] = []
                parsed['exercises'] = []

        analysis_trace = attach_request_trace({
            **parse_meta,
            "premium_requested": bool(use_premium),
            "premium_used": 'openai_compatible' in str(parse_meta.get('provider') or ''),
            "rejected_foods": rejected_foods,
            "client_action_id": client_action_id,
        })
        stt_meta = {
            "mode": "not_used_direct_audio_model",
            "client_action_id": client_action_id,
            "mime_type": mime_type,
            "filename": clean_text(audio_file.filename, max_len=120),
            "bytes": len(audio_bytes or b''),
            "request_trace": analysis_trace,
        }

        if parsed is None or (not parsed['foods'] and not parsed['exercises']):
            log_request_trace('voice_audio_incomplete')
            log_client_action_summary('voice_audio', client_action_id, analysis_trace)
            return jsonify({
                "error": MSG_VOICE_ANALYSIS_INCOMPLETE,
                "error_code": "voice_analysis_incomplete",
                "transcription": transcription,
                "stt_meta": stt_meta,
                "analysis_meta": analysis_trace,
            }), 422

        session_id = str(uuid.uuid4())
        saved_foods = []
        saved_exercises = []
        for food in parsed['foods']:
            food['id'] = uuid.uuid4().hex
            food['portion'] = 1.0
            saved_foods.append(food)
        for ex in parsed['exercises']:
            ex['id'] = uuid.uuid4().hex
            saved_exercises.append(ex)

        log_request_trace('voice_audio_success')
        log_client_action_summary('voice_audio', client_action_id, analysis_trace)
        return jsonify({
            "success": True,
            "type": parsed['type'],
            "session_id": session_id,
            "foods": saved_foods,
            "exercises": saved_exercises,
            "image_url": "",
            "transcription": transcription,
            "stt_meta": stt_meta,
            "analysis_meta": analysis_trace,
        })

    except Exception as e:
        print(f"Voice audio API error: {e}")
        log_request_trace('voice_audio_exception')
        return jsonify({
            "error": MSG_VOICE_AUDIO_FAILED,
            "error_code": "voice_audio_failed",
            "stt_meta": {
                "client_action_id": client_action_id,
                "request_trace": attach_request_trace({
                    "client_action_id": client_action_id,
                }),
            },
        }), 503
@app.route('/api/meals', methods=['GET'])
@token_required
def get_meals():
    ensure_core_schema()
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
    ensure_core_schema()
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
    ensure_core_schema()
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
    ensure_core_schema()
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


def build_rule_based_coach_reply(
    user_message,
    total_cal,
    total_pro,
    total_carbs,
    total_fat,
    total_water,
    total_burn,
    total_duration,
    cal_target,
    pro_target,
    remaining_cal,
    protein_gap,
):
    message = clean_text(user_message, default='', max_len=160).lower()
    safe_cal_target = clamp_number(cal_target, default=2000, min_value=1, max_value=10000)
    safe_pro_target = clamp_number(pro_target, default=120, min_value=1, max_value=500)
    tips = []

    if total_cal <= 0:
        tips.append("\u4eca\u5929\u8fd8\u6ca1\u6709\u8bb0\u5f55\u996e\u98df\uff0c\u5148\u62cd\u4e00\u9910\u6216\u8865\u4e00\u6761\u6587\u5b57/\u8bed\u97f3\u8bb0\u5f55\uff0c\u6211\u624d\u80fd\u7ed9\u4f60\u66f4\u51c6\u786e\u7684\u5efa\u8bae\u3002")
    elif remaining_cal >= 350:
        tips.append(f"\u4eca\u5929\u8fd8\u5269\u7ea6 {remaining_cal} kcal\uff0c\u53ef\u628a\u4e0b\u4e00\u9910\u91cd\u70b9\u653e\u5728\u9ad8\u86cb\u767d\u548c\u852c\u83dc\u4e0a\uff0c\u4e3b\u98df\u6309\u62f3\u5934\u5927\u5c0f\u63a7\u5236\u3002")
    elif remaining_cal >= 0:
        tips.append(f"\u4eca\u5929\u8fd8\u5269\u7ea6 {remaining_cal} kcal\uff0c\u540e\u9762\u7684\u52a0\u9910\u5c3d\u91cf\u6e05\u6de1\uff0c\u4f18\u5148\u65e0\u7cd6\u996e\u54c1\u3001\u9178\u5976\u6216\u6c34\u679c\u3002")
    else:
        tips.append(f"\u4eca\u5929\u5df2\u8d85\u76ee\u6807\u7ea6 {abs(remaining_cal)} kcal\uff0c\u63a5\u4e0b\u6765\u4e00\u9910\u5c3d\u91cf\u4ee5\u852c\u83dc\u3001\u7626\u8089\u548c\u65e0\u7cd6\u996e\u54c1\u4e3a\u4e3b\u3002")

    if protein_gap >= 35:
        tips.append(f"\u86cb\u767d\u8d28\u8fd8\u5dee\u7ea6 {protein_gap}g\uff0c\u53ef\u4ee5\u8865\u9e21\u80f8\u8089\u3001\u9e21\u86cb\u3001\u65e0\u7cd6\u9178\u5976\u3001\u725b\u5976\u6216\u8c46\u8150\u3002")
    elif protein_gap > 0:
        tips.append(f"\u86cb\u767d\u8d28\u8fd8\u5dee\u7ea6 {protein_gap}g\uff0c\u665a\u4e9b\u53ef\u4ee5\u8865\u4e00\u4efd\u9ad8\u86cb\u767d\u5c0f\u98df\u3002")
    else:
        tips.append("\u4eca\u5929\u86cb\u767d\u8d28\u57fa\u672c\u8fbe\u6807\uff0c\u63a5\u4e0b\u6765\u66f4\u6ce8\u610f\u603b\u70ed\u91cf\u548c\u6cb9\u8102\u63a7\u5236\u5c31\u884c\u3002")

    if total_water < 1200:
        tips.append(f"\u5f53\u524d\u996e\u6c34\u7ea6 {total_water} ml\uff0c\u4eca\u5929\u5efa\u8bae\u518d\u8865 600-1000 ml\u3002")

    if total_burn > 0:
        tips.append(f"\u4eca\u5929\u8fd0\u52a8\u4e86 {total_duration} \u5206\u949f\uff0c\u6d88\u8017\u7ea6 {total_burn} kcal\uff0c\u6062\u590d\u671f\u4f18\u5148\u8865\u86cb\u767d\u8d28\u548c\u6c34\u5206\u3002")

    if any(keyword in message for keyword in ['\u65e9\u9910', '\u65e9\u4e0a']):
        tips.append("\u65e9\u9910\u5efa\u8bae\u4f18\u5148\u86cb\u767d\u8d28 + \u4e3b\u98df + \u6c34\u679c\uff0c\u522b\u53ea\u559d\u5496\u5561\u6216\u5403\u751c\u9762\u5305\u3002")
    elif any(keyword in message for keyword in ['\u5348\u9910', '\u4e2d\u5348']):
        tips.append("\u5348\u9910\u5c3d\u91cf\u505a\u5230\u4e00\u638c\u86cb\u767d\u3001\u4e00\u62f3\u4e3b\u98df\u3001\u4e24\u62f3\u852c\u83dc\uff0c\u9971\u8179\u611f\u548c\u7a33\u5b9a\u8840\u7cd6\u90fd\u4f1a\u66f4\u597d\u3002")
    elif any(keyword in message for keyword in ['\u665a\u9910', '\u591c\u5bb5', '\u665a\u4e0a']):
        tips.append("\u665a\u9910\u5c3d\u91cf\u6e05\u6de1\u4e00\u70b9\uff0c\u4e3b\u98df\u548c\u6cb9\u8102\u522b\u5806\u592a\u591a\uff0c\u7761\u524d\u907f\u514d\u9ad8\u7cd6\u96f6\u98df\u3002")
    elif any(keyword in message for keyword in ['\u51cf\u8102', '\u63a7\u5236', '\u7626']):
        tips.append(f"\u51cf\u8102\u9636\u6bb5\u5148\u7a33\u4f4f\u70ed\u91cf\u76ee\u6807 {safe_cal_target} kcal\uff0c\u540c\u65f6\u5c3d\u91cf\u628a\u86cb\u767d\u8d28\u5403\u5230 {safe_pro_target}g \u5de6\u53f3\u3002")
    elif any(keyword in message for keyword in ['\u589e\u808c', '\u8bad\u7ec3']):
        tips.append("\u8bad\u7ec3\u65e5\u53ef\u4ee5\u628a\u4e3b\u98df\u548c\u86cb\u767d\u8d28\u5b89\u6392\u5728\u8bad\u7ec3\u524d\u540e\uff0c\u5e2e\u52a9\u6062\u590d\u548c\u7ef4\u6301\u529b\u91cf\u8868\u73b0\u3002")

    closing = "\u4f60\u7ee7\u7eed\u8bb0\u5f55\u4e0b\u4e00\u9910\u6216\u4e00\u6bb5\u8fd0\u52a8\uff0c\u6211\u4f1a\u6309\u65b0\u6570\u636e\u7acb\u523b\u5e2e\u4f60\u8c03\u6574\u5efa\u8bae\u3002"
    reply = "\u5148\u7ed9\u4f60\u4e00\u4e2a\u53ef\u6267\u884c\u5efa\u8bae\uff1a\n" + "\n".join(f"{idx + 1}. {tip}" for idx, tip in enumerate(tips[:3]))
    if len(reply) < 240:
        reply = f"{reply}\n{closing}"
    return reply[:320]


@app.route('/api/coach/chat', methods=['POST'])
@token_required
@limiter.limit("10 per minute", key_func=user_or_ip_limit_key)
def coach_chat():
    username = get_current_username()
    data = request.get_json() or {}
    user_message = data.get('message', '')
    history = data.get('history', [])
    use_premium = request_premium_enabled(username, data)
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
        coach_response = call_text_reasoning_llm(
            prompt_text=user_message,
            use_premium=use_premium,
            history=history,
            system_instruction=system_instruction,
            temperature=0.7,
            return_metadata=True,
        )
        response_text = coach_response.get('text', '')
        if not response_text.strip():
            raise RuntimeError('Coach response is empty')
    except LLMChainExhaustedError as text_chain_err:
        print(f"Coach text-chain exhausted: {text_chain_err}")
        response_text = build_rule_based_coach_reply(
            user_message=user_message,
            total_cal=total_cal,
            total_pro=total_pro,
            total_carbs=total_carbs,
            total_fat=total_fat,
            total_water=total_water,
            total_burn=total_burn,
            total_duration=total_duration,
            cal_target=cal_target,
            pro_target=pro_target,
            remaining_cal=remaining_cal,
            protein_gap=protein_gap,
        )
        coach_response = {
            "provider": "rule_based",
            "model": "coach_fallback",
            "latency_ms": 0,
            "fallback_trace": list(text_chain_err.fallback_trace or []) + ["rule_based:ok"],
        }
    except Exception as api_err:
        print(f"Coach chat error: {api_err}")
        response_text = build_rule_based_coach_reply(
            user_message=user_message,
            total_cal=total_cal,
            total_pro=total_pro,
            total_carbs=total_carbs,
            total_fat=total_fat,
            total_water=total_water,
            total_burn=total_burn,
            total_duration=total_duration,
            cal_target=cal_target,
            pro_target=pro_target,
            remaining_cal=remaining_cal,
            protein_gap=protein_gap,
        )
        coach_response = {
            "provider": "rule_based",
            "model": "coach_exception_fallback",
            "latency_ms": 0,
            "fallback_trace": ["rule_based:exception_fallback"],
        }

    return jsonify({
        "success": True,
        "reply": clean_ai_text(response_text),
        "analysis_meta": {
            "provider": coach_response.get('provider'),
            "model": coach_response.get('model'),
            "latency_ms": coach_response.get('latency_ms'),
            "fallback_trace": coach_response.get('fallback_trace'),
            "premium_requested": bool(use_premium),
            "premium_used": coach_response.get('provider') == 'openai_compatible',
        }
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
    data = {}
    use_premium = False
    
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
        use_premium = request_premium_enabled(username, data)
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
        suggestion_response = call_text_reasoning_llm(
            prompt_text=prompt,
            use_premium=use_premium,
            temperature=0.5,
            return_metadata=True,
        )
        response_text = suggestion_response.get('text', '')
            
        return jsonify({
            "success": True,
            "suggestions": clean_ai_text(response_text),
            "analysis_meta": {
                "provider": suggestion_response.get('provider'),
                "model": suggestion_response.get('model'),
                "latency_ms": suggestion_response.get('latency_ms'),
                "fallback_trace": suggestion_response.get('fallback_trace'),
                "premium_requested": bool(use_premium),
                "premium_used": suggestion_response.get('provider') == 'openai_compatible',
            }
        })
    except LLMChainExhaustedError as text_chain_err:
        print(f"Suggestions text-chain exhausted: {text_chain_err}")
        return jsonify({
            "success": False,
            "error": "AI 洞察暂时不可用，请稍后再试",
            "analysis_meta": {
                "provider": "degraded",
                "model": "text_chain_exhausted",
                "fallback_trace": list(text_chain_err.fallback_trace or []),
                "premium_requested": bool(use_premium),
            }
        }), 503
    except Exception as e:
        print(f"Suggestions generation error: {e}")
        return jsonify({"success": False, "suggestions": "AI 营养教练服务繁忙，请稍后再试。"}), 500


@app.route('/api/manual-food/estimate', methods=['POST'])
@token_or_local_app_required
@limiter.limit("10 per minute", key_func=user_or_ip_limit_key)
def manual_food_estimate():
    data = request.get_json() or {}
    username = get_current_username()
    use_premium = request_premium_enabled(username, data)

    food_name = clean_text(data.get('food_name') or data.get('name'), max_len=120)
    if not food_name:
        return jsonify({"error": "请输入食物名称"}), 400

    weight = clamp_number(data.get('weight'), default=0, min_value=0, max_value=2000)
    if weight <= 0:
        return jsonify({"error": "请输入 1-2000g 的重量"}), 400

    provided_fields = {}
    field_limits = {
        'calories': 5000,
        'protein': 300,
        'carbs': 500,
        'fat': 300,
    }
    for field, max_value in field_limits.items():
        raw_value = data.get(field)
        if raw_value in (None, ''):
            provided_fields[field] = None
            continue
        provided_fields[field] = clamp_number(raw_value, default=0, min_value=0, max_value=max_value)

    try:
        estimate_response = call_text_reasoning_llm(
            prompt_text=build_manual_food_estimate_prompt(food_name, weight, provided_fields),
            use_premium=use_premium,
            temperature=0.2,
            return_metadata=True,
        )
        parsed = normalize_manual_food_estimate(
            parse_json_payload(estimate_response.get('text')),
            food_name,
            weight,
            provided_fields,
        )
        if not parsed:
            return jsonify({"error": "AI 未能返回有效的营养数据"}), 502
        is_valid, invalid_reason = validate_food_result(parsed, source_text=food_name)
        if not is_valid:
            return jsonify({
                "error": "AI 暂时没给出可信的营养估算，请补充更具体的食物名称或重量后重试。",
                "error_code": "manual_food_estimate_invalid",
                "analysis_meta": {
                    "provider": estimate_response.get('provider'),
                    "model": estimate_response.get('model'),
                    "latency_ms": estimate_response.get('latency_ms'),
                    "fallback_trace": estimate_response.get('fallback_trace'),
                    "invalid_reason": invalid_reason,
                    "premium_requested": bool(use_premium),
                    "premium_used": estimate_response.get('provider') == 'openai_compatible',
                    "manual_ai_estimate": True,
                }
            }), 422

        return jsonify({
            "success": True,
            "source": "manual",
            "session_id": f"manual_ai_{uuid.uuid4().hex[:12]}",
            "foods": [{
                "id": uuid.uuid4().hex,
                "food_name": parsed['food_name'],
                "calories": parsed['calories'],
                "protein": parsed['protein'],
                "carbs": parsed['carbs'],
                "fat": parsed['fat'],
                "weight": parsed['weight'],
                "portion": 1.0,
                "confidence": parsed['confidence'],
                "notes": parsed['notes'],
            }],
            "analysis_meta": {
                "provider": estimate_response.get('provider'),
                "model": estimate_response.get('model'),
                "latency_ms": estimate_response.get('latency_ms'),
                "fallback_trace": estimate_response.get('fallback_trace'),
                "premium_requested": bool(use_premium),
                "premium_used": estimate_response.get('provider') == 'openai_compatible',
                "manual_ai_estimate": True,
            }
        })
    except LLMChainExhaustedError as text_chain_err:
        print(f"Manual food estimate text-chain exhausted: {text_chain_err}")
        return jsonify({
            "error": "AI 估算暂时不可用，请稍后再试",
            "analysis_meta": {
                "provider": "degraded",
                "model": "text_chain_exhausted",
                "fallback_trace": list(text_chain_err.fallback_trace or []),
                "premium_requested": bool(use_premium),
                "manual_ai_estimate": True,
            }
        }), 503
    except Exception as e:
        print(f"Manual food estimate error: {e}")
        return jsonify({"error": "手动补录 AI 估算失败，请稍后再试"}), 500


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
    print(
        "Config status: "
        f"env_path={ENV_PATH} "
        f"env_exists={os.path.exists(ENV_PATH)} "
        f"gemini_configured={bool(GEMINI_API_KEY)} "
        f"openrouter_configured={bool(OPENROUTER_API_KEY)} "
        f"use_openrouter_llm={USE_OPENROUTER_LLM}"
    )
    app.run(host='127.0.0.1', debug=os.environ.get('FLASK_DEBUG') == '1', port=port)
