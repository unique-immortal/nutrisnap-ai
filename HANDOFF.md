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

### v5.5.0 (当前开发)
- **iOS 适配**: 调整 Capacitor iOS 配置，在 `capacitor.config.json` 中配置 `"iosScheme": "https"`，并优化了 `.glass-header` 的 `padding-top` 样式，加入顶部安全区以适配有刘海或状态栏的 iOS 设备。
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
| 最新 Release | v5.5.0 (当前发布中) |
| 线上地址 | https://nutrisnap-ai-940406235442.us-central1.run.app |

---

## 8. 当前状态与待办 / Current Status & TODO

- Android APK 图标已通过 CI 替换为新 AS + 绿叶设计，需手动下载 artifact 安装
- 体重功能已完整集成在报告页（周报）
- 底部导航已简化为 5 项（首页 / 教练 / + / 报告 / 个人）
- iOS 适配与 OpenRouter 免费模型调优已于 v5.5.0 顺利完成
- 待优化：离线模式本地数据同步细节、iOS 推送通知适配

### 🌟 UI/UX 优化规划 / UI/UX Optimization Roadmap

为了进一步提升产品的视觉质感与用户体验，已在全局 Agent 部署了专用的高级 UI 优化技能 `ui-optimization`，位于 `C:\Users\14615\.gemini\antigravity\skills\ui-optimization\SKILL.md`。
To further enhance visual aesthetics and user experience, a custom `ui-optimization` agent skill has been deployed globally at `C:\Users\14615\.gemini\antigravity\skills\ui-optimization\SKILL.md`.

规划以下 UI 专项优化路线图：
The following UI optimization roadmap is planned:

1. **毛玻璃与卡片视觉微调 (Glassmorphism & Surface Refinement)**:
   - **目标 / Target**：优化 [templates/index.html](file:///G:/%E6%88%91%E7%9A%84%E4%BA%91%E7%AB%AF%E7%A1%AC%E7%9B%98/vibe%20coding/ai-diet-tracker/templates/index.html) 中的 `.surface-panel`、`.card`、`.modal-content` 等组件。
   - **细节 / Details**：统一全局毛玻璃 `backdrop-filter` 模糊度为 `16px`，背景混色 `color-mix` 比例调优至 `88%` 增强弱光下易读性。
   - **立体感 / Depth**：为深色模式下的卡片外边框增设 `border: 1px solid rgba(255, 255, 255, 0.05)` 及内阴影 `inset 0 1px 0 rgba(255, 255, 255, 0.08)` 增强卡片层次感。

2. **自适应流式字体排版 (Fluid Typography & Clamp Integration)**:
   - **目标 / Target**：优化 `#calories-summary` 核心卡路里看盘、各卡片标题 `.card-title` 及栏目头 `.section-header`。
   - **细节 / Details**：全面使用 `clamp()` 自适应大小，例如大卡路里看板设置 `font-size: clamp(2rem, 6vw, 3.5rem)`，彻底解决 iPhone SE 等窄屏或折叠屏下的字体折行和溢出问题。

3. **微交互与弹性点击动效 (Micro-Animations & Physics Click Feedback)**:
   - **目标 / Target**：主功能点击按钮、饮水打卡、以及底部导航项切换。
   - **细节 / Details**：为核心按钮应用 `transition: transform 0.2s cubic-bezier(0.34, 1.56, 0.64, 1)`。并在 `:active` 状态下注入 `transform: scale(0.95)` 带来真实的物理点按回弹手感。

4. **渐变骨架屏占位动画 (Skeleton Shimmer Loaders for AI & Load States)**:
   - **目标 / Target**：AI 饮食教练聊天面板与图片分析上传状态。
   - **细节 / Details**：使用自定义动画 `skeleton-shimmer` 与流畅循环的闪烁骨架块，替换原有单调的“正在分析…”或“AI 正在思考中…”纯文字提示，极大平滑等待体验。

5. **图表暗黑模式与色彩动态映射 (Chart.js Theme Sync via CSS Variables)**:
   - **目标 / Target**：报告页周报及趋势分析图表。
   - **细节 / Details**：重构 JavaScript 中 Chart.js 的初始颜色读取逻辑，每次初始化前使用 `getComputedStyle(document.documentElement)` 动态拉取当前主题的 CSS 变量（如 `--text-muted` 和 `--border-color`），确保图表格线与整体主题背景过渡完全同步。

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
6. **UI/UX 优化标准**：所有 UI 的进一步优化开发都应遵循 `ui-optimization` 技能定义的毛玻璃与弹性排版规范。
   (All subsequent UI/UX optimization steps should follow the guidelines defined in the `ui-optimization` agent skill.)

---

*最后更新：2026-05-22 (Last Updated: May 22, 2026)*