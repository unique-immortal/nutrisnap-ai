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

```text
ai-diet-tracker/
├── app.py                      # Flask 主应用（后端 API + 路由）
├── requirements.txt            # Python 依赖
├── Dockerfile                  # Cloud Run 容器化配置
├── .dockerignore
├── Procfile                    # 遗留文件，Cloud Run 不使用
├── HANDOFF.md                  # 本文档
├── README.md
│
├── templates/
│   └── index.html              # 开发版前端，API_BASE = ""
│
├── www/
│   ├── index.html              # 生产版前端，由 build/CI 生成
│   └── static/                 # build 时从 static/ 复制
│
├── static/                     # 静态资源源目录
├── android-icons/              # Android 原生图标素材
├── .github/workflows/
│   ├── android.yml             # Android APK 构建流水线
│   └── deploy.yml              # Cloud Run 自动部署流水线
└── android/                    # Capacitor Android 项目（CI 生成）
```

---

## 3. 双文件前端架构 / Dual Frontend Architecture

项目使用双文件前端架构，这是最容易出错的地方：

| 文件 | 用途 | API_BASE |
|------|------|----------|
| `templates/index.html` | 开发版，本地开发时使用 | `""` |
| `www/index.html` | 生产版，由 `npm run build` 或 CI 生成 | CI 注入 Cloud Run URL |

### 工作流 / Workflow

1. 开发修改：优先改 `templates/index.html`。
2. 本地同步：运行 `npm run build` 生成 `www/index.html` 与 `www/static/`。
3. 提交代码：`git commit + push`。
4. CI 自动处理：
   - 复制 `templates/index.html` 到 `www/index.html`
   - 用 `sed` 将 `const API_BASE = "";` 替换为 Cloud Run URL
   - 复制 `static/` 到 `www/static/`
5. Cloud Run 部署：后端使用 Flask 路由，Android 使用 `www/` 静态产物。

### 重要规则 / Important Rules

- 前端源码以 `templates/index.html` 为准。
- `www/index.html` 应由 `npm run build` 或 CI 生成，不建议手动编辑。
- `static/` 是静态资源的唯一来源。
- CI 的 API_BASE 注入依赖精确文本 `const API_BASE = "";`，不要随意改这个占位格式。

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
| `/api/weekly-report` | GET | 周报兼容别名 |
| `/api/daily-summaries` | GET/POST | 读取或同步每日汇总 |
| `/api/report/suggestions` | GET/POST | 报告页 AI 建议 |

### AI 与语音 / AI and Voice

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/analyze` | POST | 食物图片分析 |
| `/api/coach/chat` | POST | AI 教练对话 |
| `/api/voice-input` | POST | 语音文本饮食记录 |
| `/api/speech-to-text` | POST | 语音转文字 |

### 餐食、用户和运动 / Meals, User, Exercise

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/register` | POST | 用户注册 |
| `/api/login` | POST | 用户登录 |
| `/api/profile` | GET/POST | 用户身体数据与目标 |
| `/api/exercises` | GET/POST | 运动记录 |
| `/api/meals` | GET | 餐食记录 |
| `/api/meals/sync` | GET/POST | 节流批量餐食同步 |

---

## 5. CI/CD 流水线 / CI/CD Pipelines

### 5.1 Android 构建 (`.github/workflows/android.yml`)

**触发条件**：push 到 `main` 或 `master` 分支，也可手动触发 `workflow_dispatch`。

**步骤：**

1. Checkout 代码。
2. 安装 Node.js 20 与 Java 17。
3. `npm install` 安装依赖。
4. `npm run build` 复制 `templates/index.html` 到 `www/index.html`，复制 `static/` 到 `www/static/`，并复制 `static/sw.js`。
5. 用 `sed` 将 `www/index.html` 中的 `const API_BASE = "";` 注入为 Cloud Run URL。
6. `npx cap add android` 生成 Android 平台。
7. 从 `android-icons/` 替换各分辨率启动图标。
8. 运行 `python patch_manifest.py` 修补 Android 权限。
9. `./gradlew assembleDebug` 构建 APK 并上传 artifact。

### 5.2 Cloud Run 部署 (`.github/workflows/deploy.yml`)

**线上地址**：`https://nutrisnap-ai-940406235442.us-central1.run.app`

push 到 `main` 后会通过 GitHub Actions 触发 Cloud Build，再部署到 Cloud Run 服务 `nutrisnap-ai`。

---

## 6. 最近变更记录 / Recent Changes

### v5.5.0 (当前开发)

- **高级 UI 优化 (Premium UI/UX Optimization)**：遵循 `ui-optimization` 规范对前端样式进行全面重构。
  - **毛玻璃与卡片立体感**：统一全局毛玻璃模糊度为 `16px`，卡片及弹窗采用半透明混合背景、极细边框与 inset 投影，增强弱光可读性和层次感。
  - **自适应流式排版**：关键大尺寸数值与标题采用 `clamp()` 缩放，减少窄屏和折叠屏溢出。
  - **物理弹性动效**：为核心按钮、指标卡和记录卡加入弹性 `cubic-bezier(0.34, 1.56, 0.64, 1)` 点击反馈。
  - **AI 渐变骨架屏**：将图片分析 loading 改为闪烁渐变骨架屏。
  - **Chart.js 主题同步**：周报与体重趋势图从 CSS 变量动态读取颜色，改善深浅色切换一致性。
- **iOS 适配**：`capacitor.config.json` 配置 `"iosScheme": "https"`，并为 `.glass-header` 加入顶部安全区 padding。
- **OpenRouter 免费模型调优**：`call_llm` 将多模态与纯文本请求分流，优先调用免费模型降级链以降低运行成本。

### v5.4.0

- **饮水追踪**：首页和报告页新增饮水打卡与水量追踪组件。
- **连续打卡 (Streak)**：根据餐食、运动或饮水记录维护连续记录天数。
- **成就系统 (Achievements)**：新增 5 个成就徽章及解锁进度。
- **全局解锁通知**：实时解锁成就时显示顶部悬浮提示。
- **GitHub Release 自动更新**：新增 `update_release.py`。

### v5.3.0

- **离线支持 (Offline Support)**：采用 Service Worker 离线缓存。
- **本地推送通知 (Push Notifications)**：新增本地用餐提醒。
- **UI 全面重设计**：Material Design 3 设计令牌、现代底部导航、Chart.js 深色模式适配。
- **降级链机制**：AI 接口适配多模型自动降级。

### v5.2.0

- Open Food Facts API 集成，实现食物搜索和条形码扫描。
- 体重追踪功能完整实现，数据集成到报告页面。
- 底部导航从 6 项简化为 5 项。
- GitHub Release 内容改为中英双语格式。

---

## 7. GitHub 信息 / GitHub Repository Info

| 项目 | 值 |
|------|-----|
| 仓库 | `unique-immortal/nutrisnap-ai` |
| 线上地址 | `https://nutrisnap-ai-940406235442.us-central1.run.app` |
| 凭据规则 | 不在文档中保存 token；发布脚本从环境变量或 `.env` 读取 `GITHUB_TOKEN` |

---

## 8. 当前状态与待办 / Current Status & TODO

- Android APK 图标已通过 CI 替换为新 AS + 绿叶设计，需从 GitHub Actions artifact 手动下载并安装。
- 体重功能已集成在报告页（周报）和独立体重页。
- 底部导航已简化为 5 项：首页 / 教练 / + / 报告 / 个人。
- iOS 适配与 OpenRouter 免费模型调优已于 v5.5.0 完成。
- 高级 UI/UX 专项优化已完成：毛玻璃卡片、流式排版、弹性动效、骨架屏、图表主题同步。
- 待优化：离线模式本地数据同步细节、iOS 推送通知兼容。

### UI/UX 优化结果 / UI/UX Optimization Result

为了进一步提升产品的视觉质感与用户体验，已在全局 Agent 部署了专用的高级 UI 优化技能 `ui-optimization`，位于 `C:\Users\14615\.gemini\antigravity\skills\ui-optimization\SKILL.md`。

以下五大 UI 专项优化已于 v5.5.0 中落实：

1. **毛玻璃与卡片视觉微调**：统一 `backdrop-filter: blur(16px)`，调优背景混色比例至 `88%` / `82%`，并增强深色模式卡片边框与内阴影。
2. **自适应流式字体排版**：使用 `clamp()` 适配窄屏和折叠屏。
3. **微交互与弹性点击动效**：核心按钮和卡片使用弹性 transition 与 `:active` scale。
4. **渐变骨架屏占位动画**：图片分析 loading 改为 `skeleton-shimmer` 骨架块。
5. **图表暗黑模式与色彩动态映射**：Chart.js 初始化前读取 CSS 变量，同步主题颜色。

---

## 9. 常见操作指南 / Common Operations

### 修改前端 / Modify Frontend

```bash
改 templates/index.html -> npm run build -> git commit + push -> 等 CI 自动部署
```

> 日常开发以 `templates/index.html` 为源；`www/index.html` 应由 `npm run build` 或 CI 生成。

### 修改后端 / Modify Backend

```bash
改 app.py -> 做语法/接口验证 -> git commit + push -> 等 Cloud Run 自动部署
```

### 修改 Android 图标 / Modify Android Icon

```bash
替换 android-icons/ 下对应分辨率文件 -> git push -> CI 自动构建 APK
```

### 更新 GitHub Release / Update GitHub Release

推荐运行 `update_release.py` 自动化脚本。脚本会读取 `app.py` 中当前版本及对应 `RELEASE_NOTES_v<version>.md`，并通过 GitHub API 创建或 PATCH 对应 Release。

```bash
# 需在环境变量或 .env 中设置 GITHUB_TOKEN
python update_release.py
```

---

## 10. 注意事项 / Notes

1. 调用 GitHub API 处理中文时，`json.dumps` 必须设置 `ensure_ascii=False`，然后 `.encode("utf-8")`。
2. 文档和 Release 内容保持中英双语格式，中文在上、英文在下。
3. 用户偏好是直接执行，减少不必要确认。
4. `static/` 是静态资源来源；`www/` 是构建产物。
5. CI 使用 `sed` 替换 `www/index.html` 中的 `const API_BASE = "";`，不要随意改动该占位格式。
6. 后续 UI/UX 优化继续遵循 `ui-optimization` 的毛玻璃、流式排版、骨架屏和动效规范。

---

## 11. 交接说明 / Handoff Notes

- 本次交接任务：将 `ai-diet-tracker` 项目状态、关键注意点和当前开发进展交接给下一位负责人。
- 关键点：
  1. 开发环境以 `templates/index.html` 为主，`www/index.html` 由 `npm run build` 或 CI 生成。
  2. 后端入口在 `app.py`，前端静态资源在 `static/`；部署以 `Dockerfile` + Cloud Run 为主。
  3. 发布新版本前先更新 `RELEASE_NOTES_v<version>.md`，然后运行 `python update_release.py`。
  4. 当前本地最新版本为 v5.5.0，UI/UX 优化已完成，主要剩余项是离线数据同步细节和 iOS 推送兼容。

### 2026-05-23 接手核对 / Takeover Check

- 当前分支：`main`，跟踪 `origin/main`。
- 当前未提交改动：`HANDOFF.md`、`templates/index.html`、`www/index.html`，另有未跟踪目录 `scratch/`。
- 已验证：`templates/index.html` 与 `www/index.html` 当前一致。
- 已验证：HTML 内联脚本语法通过 Node 解析。
- 已验证：`app.py` 通过 Python AST 语法检查。
- 已验证：`npm run build` 可成功复制前端生产资源到 `www/`。
- 已处理：体重页相关函数已安全挂到 `window`，暗黑模式切换通过 `refreshWeightChartTheme()` 刷新体重图，避免局部作用域变量导致运行时报错。
- 接手后的第一建议动作：对当前 UI/UX 改动做一次移动端视觉回归检查；通过后再提交并推送，触发 Cloud Run 与 Android 构建。

---

*最后更新：2026-05-23 (Last Updated: May 23, 2026)*
