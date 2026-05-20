# NutriSnap AI 🥗

AI 智能饮食记录应用 — 拍照识别食物营养成分，支持多食物识别、分量调节、语音输入、每周营养报告。

**GitHub**: https://github.com/unique-immortal/nutrisnap-ai  
**线上地址**: https://nutrisnap-ai-940406235442.us-central1.run.app  
**技术栈**: Python Flask + SQLite + Gemini API + Tailwind CSS + Chart.js  
**部署**: Google Cloud Run (us-central1)

---

## 架构总览

```
app.py                      ← Flask 后端（所有 API + 数据库 + AI 调用）
templates/index.html        ← 单页前端（HTML + CSS + 内联 JS）
database.db                 ← SQLite（Cloud Run 实例内，单实例部署暂不丢失）
uploads/                    ← 用户上传的食物图片
Dockerfile                  ← 生产部署镜像
cloudbuild.yaml             ← Cloud Build 配置
requirements.txt            ← Python 依赖
```

### API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 前端页面 |
| `/api/health` | GET | 版本检查（当前 v4-multi） |
| `/api/analyze` | POST | 拍照识别（multipart image → 多食物 JSON） |
| `/api/voice-input` | POST | 语音输入（text → 食物 JSON） |
| `/api/meals` | GET | 获取饮食记录（支持 portion 倍率） |
| `/api/meals/<id>` | PATCH | 更新分量（`{"portion": 1.5}`） |
| `/api/meals/<id>` | DELETE | 删除单条记录 |
| `/api/meals/session/<id>` | DELETE | 删除同一 session 所有记录 |
| `/api/report/weekly` | GET | 本周营养数据（7 天，含空日补零） |

### 数据库 Schema (`meals` 表)

```sql
id          INTEGER PRIMARY KEY AUTOINCREMENT
image_path  TEXT          -- 上传图片路径（语音输入为空字符串）
food_name   TEXT
calories    INTEGER       -- 原始热量（前端 × portion 展示调整后值）
protein     INTEGER
carbs       INTEGER
fat         INTEGER
created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
session_id  TEXT          -- 同一张照片/语音的所有食物共享
portion     REAL DEFAULT 1.0  -- 分量倍率
```

---

## 核心功能

### 1. 多食物拍照识别 + 分量调节
- Gemini 返回 JSON 数组，每种食物独立记录
- 前端结果页每个食物有滑块（0.25x ~ 5.0x），拖动实时计算营养值
- 滑块 `onchange` 调用 PATCH `/api/meals/<id>` 持久化

### 2. 每周营养报告
- Chart.js 折线图（热量趋势 + 目标线）+ 堆叠柱状图（蛋白质/碳水/脂肪）
- 本周平均热量/蛋白质统计卡片

### 3. 语音输入
- 浏览器 Web Speech API（需要 Chrome）
- 转写文本 POST 到 `/api/voice-input`，Gemini 解析自然语言为食物列表
- 不支持 Web Speech API 时隐藏入口

### AI 模型降级链

```
gemini-2.5-flash → gemini-3-flash → gemini-3.1-flash-lite 
→ gemini-2.5-flash-lite → gemma-4-31b-it → gemma-4-26b-a4b-it
```

每个模型试 1 次，429 跳过，全部失败则返回错误。

---

## 本地开发

```bash
cd ai-diet-tracker

# 1. 创建 .env（从 .env.example 复制）
cp .env.example .env
# 编辑 .env，填入你的 Gemini API Key：
# GEMINI_API_KEY=your-key-here

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动
python app.py
# → http://localhost:5000
```

---

## 部署到 Cloud Run

### 方式 1：通过 gcloud 命令行（推荐）

```bash
gcloud config set project tensile-imprint-496808-v5

# 如果 Cloud Build 报错缺少日志桶，先构建镜像再部署：
gcloud builds submit --config cloudbuild.yaml . --region us-central1
gcloud run deploy nutrisnap-ai \
  --image gcr.io/tensile-imprint-496808-v5/nutrisnap-ai:v4 \
  --region us-central1 \
  --allow-unauthenticated \
  --update-env-vars GEMINI_API_KEY=your-key
```

### 方式 2：Cloud Run 控制台

1. https://console.cloud.google.com/run
2. 点 `nutrisnap-ai` → 编辑并部署新修订版本
3. 来源选 GitHub 仓库，确认环境变量 `GEMINI_API_KEY`
4. 点部署

### 环境变量

| 变量 | 值 | 说明 |
|------|-----|------|
| `GEMINI_API_KEY` | `AIzaSy...` | Google Gemini API Key |

---

## 已知限制与注意事项

- **配额**：Gemini 免费层有每分钟请求限制（RPM），429 错误时等待配额重置
- **SQLite**：Cloud Run 单实例部署，数据在容器内；建议后续迁移到 Cloud SQL
- **图片存储**：上传图片存在容器本地，容器重启丢失；建议后续迁移到 Cloud Storage
- **语音识别**：仅 Chrome/Edge 支持 Web Speech API

---

## 项目文件说明

| 文件 | 用途 |
|------|------|
| `app.py` | 后端全部逻辑 |
| `templates/index.html` | 前端单页面（Material Design 3 + Tailwind） |
| `Dockerfile` | Cloud Run 生产镜像 |
| `cloudbuild.yaml` | Cloud Build 构建配置 |
| `requirements.txt` | Python 依赖 |
| `capacitor.config.json` | Capacitor Android 配置（预留） |
| `.github/workflows/android.yml` | Android APK 构建流水线（预留） |
