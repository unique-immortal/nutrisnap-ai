import os
import uuid
import json
import sqlite3
import datetime
import re
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
from google import genai
from werkzeug.utils import secure_filename
import PIL.Image

# 加载 .env 文件（仅本地开发使用，Cloud Run 通过环境变量注入）
load_dotenv()

app = Flask(__name__)
CORS(app)

# ==========================================
# 1. 基础配置
# ==========================================
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY 环境变量未设置！请在 Cloud Run 或本地 .env 中配置后重启应用。")
client = genai.Client(api_key=GEMINI_API_KEY)

# ==========================================
# 2. 数据库配置
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
    # Migrate: add session_id and portion columns (idempotent)
    for col, col_def in [('session_id', 'TEXT'), ('portion', 'REAL DEFAULT 1.0')]:
        try:
            cursor.execute(f'ALTER TABLE meals ADD COLUMN {col} {col_def}')
        except sqlite3.OperationalError:
            pass  # column already exists
    # Backfill old records without session_id
    cursor.execute("UPDATE meals SET session_id = 'legacy_' || id WHERE session_id IS NULL")
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
        "version": "v4-multi",
        "models": [
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
        })
    return normalized


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ==========================================
# 4. 核心 API
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
        img = PIL.Image.open(filepath)

        prompt = """分析这张图片中的所有食物。对每种食物分别返回以下信息，用 JSON 数组格式：
[
  {
    "food_name": "食物名称",
    "calories": 热量数字(大卡),
    "protein": 蛋白质数字(克),
    "carbs": 碳水数字(克),
    "fat": 脂肪数字(克)
  }
]
如果图片里没有食物，返回：
{"error": "未检测到食物"}
严格只输出 JSON，不要加任何解释或 markdown 标记。"""

        models_to_try = [
            'gemini-2.5-flash',
            'gemini-2.5-flash-lite',
            'gemini-2.0-flash',
            'gemini-3.1-flash-lite',
        ]
        result_text = None
        last_error = None

        for model_name in models_to_try:
            try:
                print(f"Trying model: {model_name}")
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
                    continue
                print(f"Model {model_name} failed: {api_err}")
                continue

        if result_text is None:
            raise last_error

        foods = parse_ai_multi_result(result_text)
        if foods is None:
            return jsonify({"error": "AI未检测到食物，请重新拍摄"}), 400

        # Save to database
        session_id = str(uuid.uuid4())
        conn = get_db_connection()
        cursor = conn.cursor()
        saved = []
        for food in foods:
            cursor.execute('''
                INSERT INTO meals (image_path, food_name, calories, protein, carbs, fat, session_id, portion)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1.0)
            ''', (filepath, food['food_name'], food['calories'], food['protein'], food['carbs'], food['fat'], session_id))
            food['id'] = cursor.lastrowid
            food['portion'] = 1.0
            saved.append(food)
        conn.commit()
        conn.close()

        return jsonify({
            "success": True,
            "session_id": session_id,
            "foods": saved,
            "image_url": f"/{filepath}"
        })

    except Exception as e:
        print(f"Error calling AI API: {e}")
        return jsonify({"error": f"AI识别失败，详细错误: {str(e)}"}), 500


@app.route('/api/voice-input', methods=['POST'])
def voice_input():
    """语音输入：用 Gemini 解析自然语言食物描述"""
    text = request.json.get('text', '') if request.is_json else ''
    if not text:
        return jsonify({"error": "语音文本为空"}), 400

    prompt = f"""用户口述了以下食物描述，请分析并提取每种食物信息。
用户说："{text}"

请以 JSON 数组格式返回每种食物：
[
  {{
    "food_name": "食物名称",
    "calories": 估计热量(大卡),
    "protein": 估计蛋白质(克),
    "carbs": 估计碳水(克),
    "fat": 估计脂肪(克)
  }}
]
严格只输出 JSON，不要加任何解释。"""

    try:
        models_to_try = [
            'gemini-2.5-flash',
            'gemini-2.5-flash-lite',
            'gemini-2.0-flash',
            'gemini-3.1-flash-lite',
        ]
        result_text = None
        last_error = None

        for model_name in models_to_try:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                result_text = response.text
                break
            except Exception as api_err:
                last_error = api_err
                err_str = str(api_err)
                if '429' in err_str or 'RESOURCE_EXHAUSTED' in err_str or 'quota' in err_str.lower():
                    continue
                continue

        if result_text is None:
            raise last_error

        foods = parse_ai_multi_result(result_text)
        if foods is None:
            return jsonify({"error": "未能解析食物信息，请重新描述"}), 400

        session_id = str(uuid.uuid4())
        conn = get_db_connection()
        cursor = conn.cursor()
        saved = []
        for food in foods:
            cursor.execute('''
                INSERT INTO meals (image_path, food_name, calories, protein, carbs, fat, session_id, portion)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1.0)
            ''', ('', food['food_name'], food['calories'], food['protein'], food['carbs'], food['fat'], session_id))
            food['id'] = cursor.lastrowid
            food['portion'] = 1.0
            saved.append(food)
        conn.commit()
        conn.close()

        return jsonify({
            "success": True,
            "session_id": session_id,
            "foods": saved,
            "image_url": ""
        })

    except Exception as e:
        print(f"Voice input error: {e}")
        return jsonify({"error": f"语音识别失败: {str(e)}"}), 500


@app.route('/api/meals', methods=['GET'])
def get_meals():
    conn = get_db_connection()
    meals = conn.execute('''
        SELECT id, image_path, food_name,
               CAST(calories * COALESCE(portion, 1.0) AS INTEGER) as calories,
               CAST(protein * COALESCE(portion, 1.0) AS INTEGER) as protein,
               CAST(carbs * COALESCE(portion, 1.0) AS INTEGER) as carbs,
               CAST(fat * COALESCE(portion, 1.0) AS INTEGER) as fat,
               created_at, session_id, COALESCE(portion, 1.0) as portion
        FROM meals ORDER BY created_at DESC LIMIT 50
    ''').fetchall()
    conn.close()
    return jsonify({"data": [dict(m) for m in meals]})


@app.route('/api/meals/<int:meal_id>', methods=['PATCH', 'DELETE'])
def meal_action(meal_id):
    if request.method == 'DELETE':
        conn = get_db_connection()
        conn.execute('DELETE FROM meals WHERE id = ?', (meal_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": True})

    elif request.method == 'PATCH':
        data = request.get_json() or {}
        portion = data.get('portion')
        if portion is None:
            return jsonify({"error": "缺少 portion 参数"}), 400
        conn = get_db_connection()
        conn.execute('UPDATE meals SET portion = ? WHERE id = ?', (float(portion), meal_id))
        conn.commit()
        row = conn.execute('''
            SELECT id, food_name,
                   CAST(calories * COALESCE(portion, 1.0) AS INTEGER) as calories,
                   protein, carbs, fat, portion, session_id
            FROM meals WHERE id = ?
        ''', (meal_id,)).fetchone()
        conn.close()
        return jsonify({"success": True, "data": dict(row)})


@app.route('/api/meals/session/<session_id>', methods=['DELETE'])
def delete_session(session_id):
    conn = get_db_connection()
    conn.execute('DELETE FROM meals WHERE session_id = ?', (session_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route('/api/report/weekly', methods=['GET'])
def weekly_report():
    conn = get_db_connection()
    rows = conn.execute('''
        SELECT
            date(created_at) as day,
            SUM(calories * COALESCE(portion, 1.0)) as total_calories,
            SUM(protein * COALESCE(portion, 1.0)) as total_protein,
            SUM(carbs * COALESCE(portion, 1.0)) as total_carbs,
            SUM(fat * COALESCE(portion, 1.0)) as total_fat
        FROM meals
        WHERE created_at >= date('now', '-7 days')
        GROUP BY date(created_at)
        ORDER BY date(created_at) ASC
    ''').fetchall()
    conn.close()

    # Fill missing dates
    data_map = {row['day']: dict(row) for row in rows}
    report = []
    today = datetime.date.today()
    for i in range(6, -1, -1):
        d = (today - datetime.timedelta(days=i)).isoformat()
        if d in data_map:
            report.append(data_map[d])
        else:
            report.append({
                'day': d,
                'total_calories': 0,
                'total_protein': 0,
                'total_carbs': 0,
                'total_fat': 0,
            })
    return jsonify({"data": report})


@app.route('/api/coach/chat', methods=['POST'])
def coach_chat():
    data = request.get_json() or {}
    user_message = data.get('message', '')
    history = data.get('history', [])
    cal_target = data.get('calTarget', 2000)
    pro_target = data.get('proTarget', 120)

    if not user_message and not history:
        return jsonify({"error": "消息内容为空"}), 400

    # Fetch today's meals from SQLite
    conn = get_db_connection()
    rows = conn.execute('''
        SELECT food_name, 
               CAST(calories * COALESCE(portion, 1.0) AS INTEGER) as calories,
               CAST(protein * COALESCE(portion, 1.0) AS INTEGER) as protein,
               CAST(carbs * COALESCE(portion, 1.0) AS INTEGER) as carbs,
               CAST(fat * COALESCE(portion, 1.0) AS INTEGER) as fat,
               COALESCE(portion, 1.0) as portion
        FROM meals
        WHERE date(created_at, 'localtime') = date('now', 'localtime')
    ''').fetchall()
    conn.close()

    meals_summary = []
    total_cal = 0
    total_pro = 0
    total_carbs = 0
    total_fat = 0

    for row in rows:
        meals_summary.append(
            f"- {row['food_name']}: {row['calories']} kcal (蛋白质 {row['protein']}g, 碳水 {row['carbs']}g, 脂肪 {row['fat']}g, 分量 {row['portion']}x)"
        )
        total_cal += row['calories'] or 0
        total_pro += row['protein'] or 0
        total_carbs += row['carbs'] or 0
        total_fat += row['fat'] or 0

    if meals_summary:
        meals_context = "用户今日已吃食物如下：\n" + "\n".join(meals_summary) + f"\n今日累计摄入：热量 {total_cal} kcal，蛋白质 {total_pro}g，碳水 {total_carbs}g，脂肪 {total_fat}g。"
    else:
        meals_context = "用户今日尚未记录任何饮食。"

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

    # Format history for GenAI SDK
    contents = []
    for msg in history:
        role = 'user' if msg.get('role') == 'user' else 'model'
        content_text = msg.get('content', '')
        if content_text:
            contents.append({
                'role': role,
                'parts': [{'text': content_text}]
            })
            
    if user_message:
        contents.append({
            'role': 'user',
            'parts': [{'text': user_message}]
        })

    models_to_try = [
        'gemini-2.5-flash',
        'gemini-2.5-flash-lite',
        'gemini-2.0-flash',
        'gemini-3.1-flash-lite',
    ]

    response_text = None
    last_error = None

    for model_name in models_to_try:
        try:
            print(f"Coach calling model: {model_name}")
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config={
                    'system_instruction': system_instruction,
                    'temperature': 0.7,
                }
            )
            response_text = response.text
            print(f"Coach success with model: {model_name}")
            break
        except Exception as api_err:
            last_error = api_err
            err_str = str(api_err)
            if '429' in err_str or 'RESOURCE_EXHAUSTED' in err_str or 'quota' in err_str.lower():
                print(f"Coach model {model_name} quota exhausted, skipping...")
                continue
            print(f"Coach model {model_name} failed: {api_err}")
            continue

    if response_text is None:
        return jsonify({"error": f"AI 营养教练服务繁忙，请稍后再试。详细错误: {str(last_error)}"}), 500

    return jsonify({
        "success": True,
        "reply": response_text
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"Backend server started! Running on port {port}")
    app.run(host='0.0.0.0', debug=True, port=port)
