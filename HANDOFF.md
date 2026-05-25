# NutriSnap AI 项目交接文档 (Handoff Document)

---

## 1. 项目概述 / Project Overview

**NutriSnap AI** 是一个基于 Flask + Capacitor 的混合应用，核心功能包括：

- AI 饮食追踪：通过自然语言描述或拍照记录每日饮食
- 食物识别与营养分析：对接 Open Food Facts API，获取食物营养数据
- 体重追踪：记录和可视化体重变化趋势
- AI 教练对话：基于大语言模型提供个性化饮食建议

**技术栈 / Tech Stack：**

| 层面 | 技术 |
|------|------|
| 后端 | Python Flask |
| 前端 | 原生 JavaScript / HTML / CSS |
| 移动端打包 | Capacitor (Android APK) |
| 部署 | Google Cloud Run |
| CI/CD | GitHub Actions |

---

## 2. 项目目录结构 / Directory Structure

```
ai-diet-tracker/
├── app.py                      # Flask 主应用（后端 API + 路由）
├── requirements.txt            # Python 依赖
├── Dockerfile                  # Cloud Run 容器化配置
├── .dockerignore
├── Procfile                    # （遗留，Cloud Run 不使用）
├── HANDOFF.md                  # 本文档
├── README.md
│
├── templates/                  # 前端 HTML（开发版）
│   └── index.html              # 开发版前端，API_BASE = ""
│
├── www/                        # 前端 HTML（生产版，CI 自动生成）
│   ├── index.html              # 生产版前端，API_BASE 由 CI 注入
│   └── static/                 # 静态资源（CI 构建时从根 static/ 复制）
│
├── static/                     # 静态资源根目录
│   └── (图标、样式、JS 等)
│
├── android-icons/              # Android 原生图标素材
│   ├── mipmap-mdpi/
│   │   ├── ic_launcher.png
│   │   └── ic_launcher_round.png
│   ├── mipmap-hdpi/
│   ├── mipmap-xhdpi/
│   ├── mipmap-xxhdpi/
│   └── mipmap-xxxhdpi/
│
├── .github/workflows/
│   ├── android.yml             # Android APK 构建流水线
│   └── deploy.yml              # Cloud Run 自动部署流水线
│
└── android/                    # Capacitor Android 项目（由 CI 生成）
```

---

## 3. 双文件前端架构（关键） / Dual Frontend Architecture (Critical!)

项目使用**双文件前端架构**，这是最容易出错的地方：

| 文件 | 用途 | API_BASE |
|------|------|----------|
| `templates/index.html` | **开发版**，本地开发时使用 | `""` (空字符串) |
| `www/index.html` | **生产版**，由 CI 自动生成 | Cloud Run URL（CI 注入） |

### 工作流 / Workflow：

1. **开发修改**：只改 `templates/index.html`
2. **提交代码**：`git commit + push`
3. **CI 自动处理**：
   - 复制 `templates/index.html` → `www/index.html`
   - 用 `sed` 将 `www/index.html` 中的 `API_BASE` 替换为 Cloud Run URL
   - 复制 `static/` → `www/static/`
4. **Cloud Run 部署**：使用 `www/index.html` 作为前端入口

### 重要规则 / Important Rules：

- 修改前端时，**两个文件必须保持同步**（实际上只改 templates，CI 会自动同步到 www）
- 不要手动编辑 `www/index.html`，它会被 CI 覆盖
- `static/` 目录是静态资源的唯一来源，CI 构建时会复制到 `www/static/`

---

## 4. 后端 API 端点 / Backend API Endpoints

所有 API 端点定义在 `app.py` 中，前缀为 `/api`。

### 食物搜索 / Food Search

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/food/search` | GET | 食物关键词搜索（Open Food Facts API） |
| `/api/food/barcode/<barcode>` | GET | 条形码查询食物信息 |

### 体重追踪 / Weight Tracking

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/weight` | GET | 获取体重历史记录 |
| `/api/weight` | POST | 添加体重记录 |

### 报告 / Reports

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/report/daily` | GET | 生成日报（当日饮食汇总） |
| `/api/report/weekly` | GET | 生成周报（含体重趋势数据） |

### AI 对话 / AI Chat

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/chat` | POST | AI 教练对话 |
| `/api/analyze` | POST | 食物图片分析 |

### 认证 / Auth

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/auth/login` | POST | 用户登录 |
| `/api/auth/register` | POST | 用户注册 |

---

## 5. CI/CD 流水线 / CI/CD Pipelines

### 5.1 Android 构建 (`.github/workflows/android.yml`)

**触发条件**：push 到 `main` 或 `master` 分支

**步骤**：
1. Checkout 代码
2. 安装 Node.js + Java
3. `npm install` 安装依赖
4. **Build Web Assets**：
   - 复制 `templates/index.html` → `www/index.html`
   - 用 `sed` 注入 Cloud Run URL 到 `www/index.html`
   - 复制 `static/` → `www/static/`
5. `npx cap add android`（如果 android 目录不存在）
6. **Replace App Icon**：从 `android-icons/` 复制各分辨率的图标文件到对应的 Android mipmap 目录
7. Patch AndroidManifest.xml
8. `gradle assembleDebug` 构建 APK
9. Upload APK as artifact

**图标文件**：`android-icons/` 下按密度分目录存放：
- `mipmap-mdpi/ic_launcher.png` + `ic_launcher_round.png`
- `mipmap-hdpi/`
- `mipmap-xhdpi/`
- `mipmap-xxhdpi/`
- `mipmap-xxxhdpi/`

### 5.2 Cloud Run 部署 (`.github/workflows/deploy.yml`)

**线上地址**：`https://nutrisnap-ai-940406235442.us-central1.run.app`

自动部署流程：push 后自动构建 Docker 镜像并部署到 Cloud Run。

---

## 6. 最近变更记录 / Recent Changes

### v5.5.11 (最新发布)
- **CORS 跨域修复 / CORS Config for WebViews**: 允许 `http://localhost`, `https://localhost` 以及 `capacitor://localhost` 等移动端 WebView 源方案，解决手机客户端请求被拦截报“网络连接异常”的问题。
- **401 自动登出与自愈 / 401 Expiration Auto-Logout**: 在 `safeFetchJSON` 中拦截 401 错误，检测到 Token 过期或无效时，自动清理本地 `authToken` 缓存并跳转回登录页面重新登录，解决客户端因会话失效导致的死锁状态。

### v5.5.10 (最新发布)
- **弹窗样式修复 / Modal Action Style Fix**: 移除 `.modal-actions` 底部按钮区的冗余粘性（sticky）、高斯模糊与背景设置，修复了“基础代谢 (BMR)”与“自定义饮水”等弹窗在轻量模式下显示白色硬色块的视觉 Bug。
- **安全密钥清理与配置 / Keystore Safety Hardening**: 从 Git 跟踪中移除敏感的 `debug.keystore` 和日志文件；GitHub Actions 编译时改为从 GitHub Repository Secrets 获取 Base64 编码的密钥，确保签名和 overlay 升级的一致性。
- **无鉴权升级下载 / Public Update Download**: 移除 `/api/update/download` 的 `@token_required` 限制，使旧版客户端无需 Token 也能下载更新包。
- **Cloud Run 启动修复 / Cloud Run 503 Startup Fix**: 修正了 `gcloud` 环境变量格式将 `TEST_MODE=true` 错误拼入 `JWT_SECRET_KEY` 的问题，确保生产环境下限流器回退机制正常运作。

### v5.5.0 (当前开发)
- **数据库迁移至 PostgreSQL / Database Migration to PostgreSQL**: 支持通过 `DATABASE_URL` 环境变量配置连接外部持久化 PostgreSQL 数据库，防止 Cloud Run 重启后数据丢失；同时保留本地 SQLite (`database.db`) 自动回退以保障本地离线开发的便利性。
- **iOS 适配**: 调整 Capacitor iOS 配置，在 `capacitor.config.json` 中配置 `"iosScheme": "https"`，并优化了 `.glass-header` 的 `padding-top` 样式，加入顶部安全区以适配有刘海或状态栏 of iOS 设备。
- **OpenRouter 免费模型调优**: 在 `call_llm` 中优化了 OpenRouter 降级链，将多模态请求与纯文本请求进行分流，分别优先调用最佳的高性能免费模型（如 Llama 3.3 70B 及 Gemma 4 31B 视觉版），显著节省 API 运行成本。

### v5.4.0 (最新发布)
- **饮水追踪**: 首页和报告页高颜值饮水打卡与水量追踪组件，采用双层动态 SVG 波浪动画圈。
- **连续打卡 (Streak)**: 火焰图标记录用户连续记录天数，当用户当天或昨天有记录餐食、运动或饮水时，Streak 会自动递增或保持，逾期未记录重置。
- **成就系统 (Achievements)**: 新增成就徽章系统，提供初步补水、补水达人、初显成效、自律达人、膳食管家 5 个精美徽章及解锁进度。
- **全局解锁通知 (Achievements Toast)**: 实时解锁成就时，会在页面顶部滑出悬浮提示。
- **GitHub Release 自动更新**: 增加了 `update_release.py` 脚本，在部署时可同时更新 GitHub 发布页。

### v5.3.0 (最新发布)
- **离线支持 (Offline Support)**: 采用 Service Worker 离线缓存，无网络时仍可查看历史记录和报告。
- **本地推送通知 (Push Notifications)**: 本地推送通知，用餐时间提醒（无需服务器）。
- **UI 全面重设计**: Material Design 3 设计令牌系统重构，药丸式现代底部导航，Chart.js 深色模式适配。
- **降级链机制**: AI 接口适配多模型自动降级链。

### v5.2.0
- Open Food Facts API 集成，实现食物搜索和条形码扫描。
- 体重追踪功能完整实现，数据集成到报告页面，底部导航从 6 项简化为 5 项。
- GitHub Release 内容改为中英双语格式。

---

## 7. GitHub 信息 / GitHub Repository Info

| 项目 | 值 |
|------|-----|
| 仓库 | `unique-immortal/nutrisnap-ai` |
| Token | `ghp_************************************` |
| 最新 Release | v5.5.11 (当前发布中) |
| 线上地址 | https://nutrisnap-ai-940406235442.us-central1.run.app |

---

## 8. 当前状态与待办 / Current Status & TODO

- Android APK 图标已通过 CI 替换为新 AS + 绿叶设计，需手动下载 artifact 安装
- 体重功能已完整集成在报告页（周报）
- 底部导航已简化为 5 项（首页 / 教练 / + / 报告 / 个人）
- 待优化：离线支持、推送通知、iOS 适配

---

## 9. 常见操作指南 / Common Operations

### 修改前端 / Modify Frontend
```
改 templates/index.html → git commit + push → 等 CI 自动部署
```
> 不要直接改 www/index.html，它会被 CI 覆盖。

### 修改后端 / Modify Backend
```
改 app.py → git commit + push → 等 Cloud Run 自动部署
```

### 修改 Android 图标 / Modify Android Icon
```
替换 android-icons/ 下对应分辨率文件 → git push → CI 自动构建 APK
```

### 更新 GitHub Release / Update GitHub Release
推荐直接运行 `update_release.py` 自动化脚本，该脚本会自动读取 `app.py` 中的当前版本及对应的 `RELEASE_NOTES_v<version>.md` 日志文件，并在 GitHub API 检索并创建或 PATCH 对应的 Release 发布页。
```bash
# 需在环境变量或 .env 中设置 GITHUB_TOKEN
python update_release.py
```

---

## 10. 注意事项 / Notes

1. **Python + urllib.request 处理中文**：调用 GitHub API 时，`json.dumps` 必须设置 `ensure_ascii=False`，然后 `.encode("utf-8")`
2. **用户偏好**：所有文档和 Release 内容均为中英双语格式（中文在上，英文在下）
3. **用户倾向于直接执行**，不需要频繁确认，减少不必要的交互
4. **前端静态资源**：`www/` 下的文件由 CI 自动生成，不要手动编辑
5. **API_BASE 注入**：CI 使用 `sed` 命令替换，确保 `www/index.html` 中的 `API_BASE` 占位符格式与 `sed` 模式匹配

---

## 11. 数据库配置与迁移 / Database Configuration & Migration

应用采用动态数据库连接层，能自动适配本地开发环境和生产 Cloud Run 环境的持久化需求：

### 11.1 自动路由逻辑 / Automatic Routing Logic
1. 当检测到环境变量中有 `DATABASE_URL` 且以 `postgres://` 或 `postgresql://` 开头时：
   - 自动加载 `psycopg2` 驱动连接 PostgreSQL 数据库（如 Supabase 或 Neon）。
   - 在首次启动时自动执行 PostgreSQL 兼容的建表与更新逻辑（`init_db()`）。
2. 当未配置 `DATABASE_URL`，或连接异常时：
   - 自动回退（Fallback）至本地 SQLite 数据库 `database.db`，确保本地开发零配置且能完全离线运行。

### 11.2 SQL 语法兼容翻译 / SQL Compatibility Translation
由于 PostgreSQL 和 SQLite 语法差异，代码内置了 SQL 翻译适配层（`translate_sql`）：
- 占位符转换：运行时将 `?` 转换为 `%s`。
- 时间函数转换：
  - `date('now', '-7 days')` 转换为 `CURRENT_DATE - INTERVAL '7 days'`。
  - `date(recorded_at)` 转换为 `CAST(recorded_at AS DATE)`。
- UPSERT 翻译：将 `INSERT OR REPLACE` 转换为 PostgreSQL 的 `ON CONFLICT (username, date) DO UPDATE SET...`。
- Row 结构体伪造：实现了 `PgRowWrapper` 模拟 `sqlite3.Row` 行为，支持通过列名访问字段与 `dict(row)` 操作，完全避免修改业务代码。

---

*最后更新：2026-05-26*