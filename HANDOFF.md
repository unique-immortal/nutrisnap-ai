# NutriSnap AI 项目交接文档

> **版本**：v5.0.0 (本地优先架构)
> **文档生成日期**：2026-05-20
> **GitHub 仓库**：[unique-immortal/nutrisnap-ai](https://github.com/unique-immortal/nutrisnap-ai)
> **线上地址**：[https://nutrisnap-ai-940406235442.us-central1.run.app](https://nutrisnap-ai-940406235442.us-central1.run.app)

---

## 一、项目概况

NutriSnap AI 是一款基于 AI 视觉识别的智能饮食记录应用。用户通过**拍照**或**语音输入**记录每日饮食与运动，系统使用 Google Gemini 多模态模型自动识别食物营养成分，并提供 AI 营养教练对话、每周营养报告、BMR/TDEE 计算等一站式健康管理功能。

**核心差异化优势**：AI 拍照识别 + 语音输入 + AI 营养教练，这些功能主流竞品（MyFitnessPal、Lose It、YAZIO 等）均未覆盖。

---

## 二、项目结构

```
ai-diet-tracker/
├── app.py                          # Flask 后端主文件 (1145 行)
├── templates/
│   └── index.html                  # 前端单页面 (2926 行, HTML+CSS+内联JS)
├── requirements.txt                # Python 依赖
├── Dockerfile                      # Cloud Run 生产镜像
├── cloudbuild.yaml                 # Cloud Build 构建配置
├── capacitor.config.json           # Capacitor Android 打包配置
├── package.json                    # Node.js 配置 (Capacitor 依赖)
├── patch_manifest.py               # Android Manifest 权限修补脚本
├── .env.example                    # 环境变量模板
├── .env                            # 本地环境变量 (gitignore)
├── .gitignore
├── .dockerignore
├── database.db                     # SQLite 数据库 (gitignore)
├── README.md                       # 项目说明
├── RELEASE_NOTES_v5.md             # v5 发布说明
├── www/                            # Capacitor Web 资源构建输出
│   └── index.html                  # 生产版 (API_BASE = Cloud Run URL)
└── .github/
    └── workflows/
        ├── deploy.yml              # Cloud Run 自动部署流水线
        └── android.yml             # Android APK 构建流水线

vibe coding/
└── .trae/documents/
    ├── nutrisnap-ai-13-fixes-plan.md      # 13 项修复计划
    ├── nutrisnap-ai-gap-analysis.md       # 竞品差距分析
    ├── nutrisnap-ai-bug-fix-plan.md       # 早期 Bug 修复计划
    └── nutrisnap-ai-roadmap.md            # 路线图
```

---

## 三、架构设计

### 3.1 总体架构

```
客户端 (Browser / APK)
  index.html (单页 SPA)
    ├── localStorage: 食物记录 / 运动记录 / 聊天记录
    ├── Tailwind CSS + Chart.js: UI / 图表
    └── Capacitor: Android 原生封装
         │ HTTPS
         ▼
Google Cloud Run (us-central1)
  app.py (Flask + gunicorn)
    ├── Gemini API 调用 (google-genai SDK)
    ├── SQLite 本地数据库
    └── 图片上传/临时存储
```

### 3.2 v5 本地优先架构 (Local-First)

| 数据 | 存储位置 | 说明 |
|------|----------|------|
| 🍔 食物记录 | 本地 localStorage | 单条记录不经服务器 |
| 🏃 运动记录 | 本地 localStorage | 单条记录不经服务器 |
| 💬 聊天记录 | 本地 localStorage | AI 教练对话上下文 |
| 📊 每日汇总 | 服务器 SQLite | 跨设备同步 |
| 🧬 BMR 身体数据 | 服务器 SQLite | 跨设备同步 |
| 🔐 账号密码 | 服务器 SQLite | SHA-256 哈希存储 |

### 3.3 AI 模型降级链

所有 AI 端点均使用同一降级策略：

```
gemini-3.5-flash → gemini-2.5-flash → gemini-2.5-flash-lite
→ gemini-2.0-flash → gemini-3.1-flash-lite
```

- 每个模型试 1 次，遇到 429 (RESOURCE_EXHAUSTED) 跳过
- 全部失败则返回 500

### 3.4 技术栈

| 层级 | 技术 |
|------|------|
| 后端框架 | Python Flask 3.1.3 |
| 生产服务器 | gunicorn 23.0.0 |
| AI 模型 | Google Gemini (google-genai SDK 2.4.0) |
| 数据库 | SQLite3 |
| 前端 | 原生 HTML/JS + Tailwind CSS + Chart.js (CDN) |
| 移动端 | Capacitor 6.x |
| 部署 | Google Cloud Run + Cloud Build |
| CI/CD | GitHub Actions + Workload Identity Federation |

---

## 四、API 端点

### 4.1 核心 AI 功能

| 端点 | 方法 | 输入 | 说明 |
|------|------|------|------|
| `/api/analyze` | POST | multipart `image` | 拍照识别食物 (多食物 JSON) |
| `/api/voice-input` | POST | JSON `{"text":"..."}` | 语音/文本 → 解析食物+运动 |
| `/api/speech-to-text` | POST | multipart `audio` | 语音文件转文字 |
| `/api/coach/chat` | POST | JSON `{"message":"...", "meals":[], "exercises":[]}` | AI 营养教练 |

### 4.2 用户与数据

| 端点 | 说明 |
|------|------|
| `/api/register` | 注册 (用户名>=3位, 密码>=4位) |
| `/api/login` | 登录 (客户端通过 X-User-Id 头传身份) |
| `/api/profile` GET/POST | 身体数据 + BMR/TDEE 计算 |
| `/api/meals` GET | 获取最近 50 条记录 |
| `/api/daily-summaries` GET/POST | 每日汇总同步 (upsert) |
| `/api/report/weekly` | 7 天周报 |
| `/api/report/suggestions` | AI 运动/营养建议 |
| `/api/health` | 版本检查 (v5-local-first) |

### 4.3 认证方式

客户端通过 `X-User-Id` 请求头传递用户名，无 token/session 管理。

---

## 五、数据库表结构

SQLite3 数据库，包含 4 张表：

- **users** — 账号 (username PK) + 身体数据 (gender, age, height, weight, activity_level)
- **meals** — 食物记录 (id PK, food_name, calories, protein, carbs, fat, portion, weight, session_id, username)
- **exercises** — 运动记录 (exercise_name, calories, duration, type, target_muscles)
- **daily_summaries** — 每日汇总 (username+date PK, total_calories, total_protein, total_carbs, total_fat, total_burn_calories, total_exercise_duration)

BMR 计算公式 (Mifflin-St Jeor)：
- 男性：`10*w + 6.25*h - 5*a + 5`
- 女性：`10*w + 6.25*h - 5*a - 161`
- TDEE = BMR * 活动量系数 (1.2 ~ 1.9)

---

## 六、关键代码位置

### app.py

| 行号 | 内容 |
|------|------|
| 1-30 | import, Flask 初始化, Gemini API Key |
| 36-118 | `init_db()` — 建表 + 列迁移 (幂等) |
| 129-141 | `/api/health` |
| 143-191 | `parse_ai_multi_result()` — 解析 AI JSON (数组/单对象/fallback) |
| 261-356 | `/api/analyze` — 拍照识别 (模型降级, 数据返回, 文件清理) |
| 414-511 | `/api/voice-input` — 语音输入 (食物+运动混合解析) |
| 514-588 | `/api/speech-to-text` — 语音转文字 |
| 651-697 | `/api/report/weekly` — 7 天周报 (空日补零) |
| 700-863 | `/api/coach/chat` — AI 营养教练 (多轮+上下文) |
| 870-943 | `/api/profile` — BMR/TDEE 计算 |
| 984-1120 | `/api/report/suggestions` — AI 运动/营养建议 |
| 1123-1164 | `/api/daily-summaries` — 每日汇总同步 |

### index.html

| 行号 | 内容 |
|------|------|
| 47-110 | CSS 变量 (MD3 深浅色板) |
| 260-344 | 首页仪表盘 (卡路里环 + 宏量 + 记录区) |
| 780-950 | 底部导航 (5 tabs) |
| 1000-1200 | `MealStorage` — localStorage 食物类 |
| 1200-1400 | `ExerciseStorage` — localStorage 运动类 |
| 1370-1550 | `showResult()` — 识别结果页 (多食物卡片+分量调节) |
| 1628-1770 | 语音输入 (MediaRecorder + 倒计时 + 转写) |
| 2105-2301 | `fetchTodayData()` — 首页数据聚合渲染 |
| 2303-2340 | `updateRings()` — 卡路里环 + 仪表盘更新 |
| 2420-2525 | `fetchWeeklyReport()` — 周报 (Chart.js 图表) |
| 2527-2570 | `fetchAISuggestions()` — AI 分析建议 (<3天门槛检查) |
| 2620-2670 | `generateCoachResponse()` — AI 教练聊天 |
| 2715-2844 | `updateProfileUI()` — 个人中心 + BMR 卡片 |
| 2862-2926 | 深色模式 + FAB 菜单 + 初始化 |

---

## 七、Bug 历史与已修复项 (v5.0.0)

本次交接前完成 13 项修复：

| # | 严重 | 问题 | 修复 |
|---|------|------|------|
| 1 | 🔴 | 卡路里仪表盘公式混淆 | `摄入-消耗=净摄入` + `目标→剩余` 双行 |
| 2 | 🔴 | 周报平均值除以7而非有记录天数 | 改除 nonZeroDays + 显示"基于 X 天" |
| 3 | 🔴 | 运动统计始终为0 | fetchWeeklyReport() 末尾赋值 |
| 4 | 🔴 | 语音弹窗错别字 | "吃了什么" |
| 5 | 🔴 | AI教练Markdown不渲染 | parseMarkdownToHTML(reply) |
| 6 | 🟡 | 数据不足时输出诊断性结论 | <3天显示引导卡片 |
| 7 | 🟡 | 删除确认无摘要 | 显示"汉堡包 250kcal" |
| 8 | 🟡 | FAB无菜单 | 弹出"记饮食/记运动"二选一 |
| 9 | 🟢 | 时间戳含秒 | formatTime() 今天/昨天/月日 |
| 10 | 🟢 | BMR空状态与目标矛盾 | "当前目标 2000 kcal（默认值）" |
| 11 | 🟢 | 深色模式两处入口 | 移除header图标 |
| 12 | 🟢 | 分量按钮无步进 | hover 提示 ±25g |

---

## 八、待做事项（接班后优先）

### P0：基础功能完整性

- [ ] **手动食物搜索** — 预置常见食物 JSON + 自定义输入 + AI 估算
- [ ] **新手引导** — 2-3 屏故事化引导页
- [ ] **每日推送提醒** — FCM / Capacitor Local Notifications
- [ ] **体重追踪** — 趋势折线图

### P1：用户参与度

- [ ] **连续打卡 (Streak)** — YAZIO 火焰图标
- [ ] **成就徽章** — 7天/30天打卡
- [ ] **饮水追踪**
- [ ] **"完美日"判定**

### P2：生态强化

- [ ] **条形码扫描** — Open Food Facts API + Capacitor MLKit
- [ ] **数据导出** — CSV
- [ ] **Android Health Connect 集成**
- [ ] **食物收藏夹**

### P3：差异化壁垒

- [ ] **间歇性断食计时器**
- [ ] **iOS App** — Capacitor 同构
- [ ] **API 降成本** — 切换到 OpenRouter 免费路由 (8+ 模型)
- [ ] **Android APK 正式发布** — 签名 + Google Play

### 基础设施

- [ ] **数据库升级** — SQLite → PostgreSQL
- [ ] **图片存储** — uploads → Cloud Storage
- [ ] **OAuth 登录** — Google/微信

---

## 九、部署

### 本地开发

```bash
cd ai-diet-tracker
cp .env.example .env   # 填入 GEMINI_API_KEY
pip install -r requirements.txt
python app.py           # → http://localhost:5000
```

### 生产部署 (Cloud Run)

```bash
gcloud config set project tensile-imprint-496808-v5

# 构建镜像
gcloud builds submit --config cloudbuild.yaml . --region us-central1

# 部署
gcloud run deploy nutrisnap-ai \
  --image gcr.io/tensile-imprint-496808-v5/nutrisnap-ai:v4 \
  --region us-central1 \
  --allow-unauthenticated
```

**GCP 项目**：`tensile-imprint-496808-v5` / `us-central1`

### CI/CD

推送到 `main` 分支 → GitHub Actions 自动构建镜像 + 部署 Cloud Run (`deploy.yml`)。Android APK 由 `android.yml` 构建。

### 环境变量

- `GEMINI_API_KEY` — 必填，Google Gemini API 密钥

---

## 十、已知限制

- **SQLite**：Cloud Run 缩容/重启数据丢失（单实例）
- **图片**：`uploads/` 容器重启丢失
- **Gemini 配额**：免费层 RPM 限制严格，高峰期可能 429
- **单实例**：不支持水平扩展
