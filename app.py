import os
import base64
import sqlite3
import datetime
import re
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
from openai import OpenAI
from werkzeug.utils import secure_filename

# 加载 .env 文件（仅本地开发使用，Cloud Run 通过环境变量注入）
load_dotenv()

app = Flask(__name__)
CORS(app)  # 允许跨域请求 (让手机App能调用这个后端)

# ==========================================
# 1. 基础配置 (Configuration)
# ==========================================
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY')
if not OPENROUTER_API_KEY:
    raise ValueError("OPENROUTER_API_KEY 环境变量未设置！")

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

MODELS = [
    "google/gemma-4-31b-it:free",         # Gemma 4 大杯
    "google/gemma-4-26b-a4b-it:free",     # Gemma 4 中杯
    "nvidia/nemotron-nano-12b-v2-vl:free", # NVIDIA 视觉兜底
]

# ==========================================
# 2. 数据库配置 (Database)
# ==========================================
def init_db():
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
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
    conn.commit()
    conn.close()

init_db()

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
        "version": "v3-openrouter",
        "models": MODELS,
    })

def parse_ai_result(result_text):
    if "错误" in result_text or "未检测到食物" in result_text:
        return None
    data = {}
    for line in result_text.strip().split('\n'):
        if ':' in line:
            key, val = line.split(':', 1)
        elif '：' in line:
            key, val = line.split('：', 1)
        else:
            continue
        key = key.strip()
        num_match = re.search(r'\d+', val)
        num_val = int(num_match.group()) if num_match else 0
        if "食物名称" in key:
            data['food_name'] = val.strip()
        elif "热量" in key:
            data['calories'] = num_val
        elif "蛋白" in key:
            data['protein'] = num_val
        elif "碳水" in key:
            data['carbs'] = num_val
        elif "脂肪" in key:
            data['fat'] = num_val
    if 'food_name' not in data:
        data['food_name'] = "未知食物"
    return data


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ==========================================
# 4. OpenRouter Gemma 4 图片分析
# ==========================================
@app.route('/api/analyze', methods=['POST'])
def analyze_food():
    if 'image' not in request.files:
        return jsonify({"error": "没有找到图片"}), 400

    file = request.files['image']
    if file.filename == '':
        return jsonify({"error": "图片名为空"}), 400

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    try:
        # 图片转 base64
        with open(filepath, 'rb') as f:
            img_b64 = base64.b64encode(f.read()).decode('utf-8')
        ext = os.path.splitext(filepath)[1].lower()
        mime = 'image/jpeg' if ext in ('.jpg', '.jpeg') else 'image/png'

        prompt = """请分析这张图片中的食物。
严格以如下格式返回，不要包含其他废话，只需要这4行：
食物名称: [名字]
热量: [数字] (单位默认大卡)
蛋白质: [数字] (单位默认克)
碳水: [数字] (单位默认克)
脂肪: [数字] (单位默认克)

如果图片里没有食物，请返回：
错误: 未检测到食物"""

        print("Calling OpenRouter...")
        result_text = None
        last_error = None
        
        for model_name in MODELS:
            try:
                print(f"Trying OpenRouter model: {model_name}")
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}}
                        ]
                    }],
                    max_tokens=300,
                )
                result_text = response.choices[0].message.content
                print(f"Success with OpenRouter: {model_name}")
                break
            except Exception as api_err:
                last_error = api_err
                print(f"OpenRouter {model_name} failed: {api_err}")
                continue
        
        if result_text is None:
            raise last_error
        
        print(f"OpenRouter response: {result_text[:100]}...")

        data = parse_ai_result(result_text)
        if data is None:
            return jsonify({"error": "AI未检测到食物，请重新拍摄"}), 400

        # 存入数据库
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO meals (image_path, food_name, calories, protein, carbs, fat)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (filepath, data['food_name'], data.get('calories',0), data.get('protein',0), data.get('carbs',0), data.get('fat',0)))
        conn.commit()
        conn.close()

        return jsonify({
            "success": True,
            "data": data,
            "image_url": f"/{filepath}"
        })

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"error": f"AI识别失败，详细错误: {str(e)}"}), 500


@app.route('/api/meals', methods=['GET'])
def get_meals():
    conn = get_db_connection()
    meals = conn.execute('SELECT * FROM meals ORDER BY created_at DESC LIMIT 20').fetchall()
    conn.close()
    return jsonify({"data": [dict(m) for m in meals]})


@app.route('/api/meals/<int:meal_id>', methods=['DELETE'])
def delete_meal(meal_id):
    conn = get_db_connection()
    conn.execute('DELETE FROM meals WHERE id = ?', (meal_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"Backend server started! Running on port {port}")
    app.run(host='0.0.0.0', debug=True, port=port)
