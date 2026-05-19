import os
import sqlite3
import datetime
import re
import time
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
from google import genai
from werkzeug.utils import secure_filename
import PIL.Image

# 加载 .env 文件（仅本地开发使用，Cloud Run 通过环境变量注入）
load_dotenv()

app = Flask(__name__)
CORS(app)  # 允许跨域请求 (让手机App能调用这个后端)

# ==========================================
# 1. 基础配置 (Configuration)
# ==========================================
# 上传图片的保存目录
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY 环境变量未设置！请在 Cloud Run 或本地 .env 中配置后重启应用。")
# 初始化 Gemini 客户端
client = genai.Client(api_key=GEMINI_API_KEY)

# ==========================================
# 2. 数据库配置 (Database)
# ==========================================
def init_db():
    """初始化数据库表：如果还没有表，就建一个用来存食物记录"""
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

# 启动程序时先初始化一下数据库
init_db()

def get_db_connection():
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row  # 返回字典格式的数据
    return conn

# ==========================================
# 3. 页面路由 (Serve Frontend)
# ==========================================

def parse_ai_result(result_text):
    """解析 AI 返回的文本，提取营养成分数据"""
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
    """当用户在浏览器访问根目录 '/' 时，把前端网页发给他们"""
    return render_template('index.html')

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ==========================================
# 4. 核心功能 API (Backend Logic)
# ==========================================
@app.route('/api/analyze', methods=['POST'])
def analyze_food():
    """接收前端发来的图片，调用AI，然后存入数据库"""
    # 1. 接收图片
    if 'image' not in request.files:
        return jsonify({"error": "没有找到图片"}), 400
    
    file = request.files['image']
    if file.filename == '':
        return jsonify({"error": "图片名为空"}), 400

    # 保存图片到服务器本地
    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    # 2. 调用 Gemini 识别图片（配额耗尽时自动降级到更弱的模型）
    try:
        img = PIL.Image.open(filepath)
        
        prompt = """
        请分析这张图片中的食物。
        严格以如下格式返回，不要包含其他废话，只需要这4行：
        食物名称: [名字]
        热量: [数字] (单位默认大卡)
        蛋白质: [数字] (单位默认克)
        碳水: [数字] (单位默认克)
        脂肪: [数字] (单位默认克)
        
        如果图片里没有食物，请返回：
        错误: 未检测到食物
        """
        
        models_to_try = [
            'gemini-2.5-flash',       # 最强
            'gemini-3-flash',
            'gemma-4-31b-it',
            'gemini-3.1-flash-lite',
            'gemini-2.5-flash-lite',
            'gemma-4-26b-a4b-it',     # 最弱兜底
        ]
        result_text = None
        last_error = None
        
        for model_name in models_to_try:
            for attempt in range(2):
                try:
                    print(f"Trying model: {model_name}, attempt {attempt+1}")
                    response = client.models.generate_content(
                        model=model_name,
                        contents=[prompt, img]
                    )
                    result_text = response.text
                    print(f"Success with model: {model_name}")
                    break
                except Exception as api_err:
                    last_error = api_err
                    err_str = str(api_err)
                    if '429' in err_str or 'RESOURCE_EXHAUSTED' in err_str or 'quota' in err_str.lower():
                        print(f"Model {model_name} quota exhausted, skipping...")
                        break
                    print(f"Model {model_name} attempt {attempt+1} failed: {api_err}")
                    time.sleep(1)
            if result_text:
                break
        
        if result_text is None:
            raise last_error
        
        data = parse_ai_result(result_text)
        if data is None:
            return jsonify({"error": "AI未检测到食物，请重新拍摄"}), 400

        # 3. 将分析结果保存到数据库
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO meals (image_path, food_name, calories, protein, carbs, fat)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (filepath, data['food_name'], data.get('calories',0), data.get('protein',0), data.get('carbs',0), data.get('fat',0)))
        conn.commit()
        conn.close()

        # 4. 返回结果
        return jsonify({
            "success": True,
            "data": data,
            "image_url": f"/{filepath}"
        })

    except Exception as e:
        print(f"Error calling AI API: {e}")
        return jsonify({"error": f"AI识别失败，详细错误: {str(e)}"}), 500


@app.route('/api/meals', methods=['GET'])
def get_meals():
    """获取历史记录"""
    conn = get_db_connection()
    # 按照时间倒序，获取最新的20条
    meals = conn.execute('SELECT * FROM meals ORDER BY created_at DESC LIMIT 20').fetchall()
    conn.close()
    
    # 将结果转换为列表字典发给前端
    meals_list = [dict(meal) for meal in meals]
    return jsonify({"data": meals_list})


@app.route('/api/meals/<int:meal_id>', methods=['DELETE'])
def delete_meal(meal_id):
    """删除一条饮食记录"""
    conn = get_db_connection()
    conn.execute('DELETE FROM meals WHERE id = ?', (meal_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


if __name__ == '__main__':
    # Cloud Run 通过环境变量 PORT 指定端口，本地开发默认用 5000
    port = int(os.environ.get('PORT', 5000))
    print(f"Backend server started! Running on port {port}")
    app.run(host='0.0.0.0', debug=True, port=port)
