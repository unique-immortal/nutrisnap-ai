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
- **每次部署都必须更新版本号**：手机端更新提示依赖服务端版本号高于客户端 `CLIENT_VERSION`，如果只部署代码但版本号不变，已安装的手机端不会收到更新推送。通常需要同步更新 `templates/index.html` 中的 `CLIENT_VERSION`、新增对应 `RELEASE_NOTES_vX.Y.Z.md`，必要时更新 `app.py` 中的 `fallback_version`。

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

### v5.6.16 (Secondary and Tertiary Modals Bottom Sheet Consolidation)
- **底部弹窗统一 / Bottom Sheet Pass**: 将 `exerciseLogModal` (运动记录), `waterRecordModal` (饮水记录), `weightRecordModal` (体重记录), `editMealModal` (编辑饮食), `deleteConfirmModal` (删除确认), `bmrSetupModal` (BMR设置), 以及 `loginOverlay` (登录面板) 全局改造为符合 Stitch 设计语言的底部弹窗 (Bottom Sheet)。引入了毛玻璃遮罩、拖拽手柄、底部安全区避让、统一圆角和进场动画。
- **页面级别结构梳理 / Page Structure Rework**: `#page-history` (记录历史) 改进为带筛选条件的紧凑图文卡片列表；`#manualFoodModal` (手动补录) 改造为更直观的双列/三列营养输入网格；`#page-weight` 改造为更精美的信息图表风格。
- **版本更新规则已执行 / Version Bump Applied**: 同步更新了 `CLIENT_VERSION`、`fallback_version`、`sw.js` 缓存版本为 `v5.6.16`，并生成了对应的 RELEASE NOTES，重新构建了 www/。

### v5.6.19 (Voice STT reliability pass)
- **语音听写修复 / Voice STT Fix**: `/api/speech-to-text` 现在优先走 OpenRouter 专用 STT 接口，失败后回退到 Google Gemini 音频转写，避免把录音硬塞进通用聊天模型导致的失败。
- **前端鉴权补强 / Auth Header Fix**: 语音上传请求补齐显式 `Authorization` 头，减少手机端 token 状态不一致导致的失败。
- **版本更新 / Version Bump**: 本轮手机端改动已经升到 `v5.6.19`，并同步更新 `templates/index.html` 的 `CLIENT_VERSION`、`app.py` 的 `fallback_version`、`static/sw.js` 的 `CACHE_NAME`、以及 `RELEASE_NOTES_v5.6.19.md`。

### v5.6.17 (Weight page and mobile polish pass)
- **体重页重排 / Weight Page Rework**: `#page-weight` 已改为更接近 Stitch 记录页的三段式布局，加入摘要主卡、7/30/90 天紧凑切换、短日期轴和轻量历史行，减少旧式大面板感。
- **手动补录和图标修复 / Manual Entry and Icon Fix**: `#manualFoodModal` 的搜索区、关闭按钮和搜索结果层级继续压紧；本地图标映射补齐 `search` 与 `monitoring`，避免移动端 fallback 成 info/异常图形。
- **版本更新 / Version Bump**: 本轮手机端 UI 改动已经升到 `v5.6.17`，并同步更新 `templates/index.html` 的 `CLIENT_VERSION`、`app.py` 的 `fallback_version`、`static/sw.js` 的 `CACHE_NAME`、以及 `RELEASE_NOTES_v5.6.17.md`。
- **后续任务 / Remaining Work**: 继续收敛 `#page-history` 的空状态/时间线层次、`#exerciseLogModal`、`#bmrSetupModal`、`#weightRecordModal` 与设置页的细节一致性。

### v5.6.13 (Record/Report page fidelity pass)
- **记录/报告页重构 / Record Report Alignment**: 将 `#page-report` 向 Stitch `_9` 参考稿靠齐，改为记录历史 header、横向日期条、热量收支卡、宏量营养卡、7 天趋势、今日饮食/运动时间线和 AI 洞察结构，减少旧版周报页的空白与割裂感。
- **新卡片数据绑定 / Data Binding**: `fetchWeeklyReport()` 已补齐 `reportTargetCal`、`reportIntakeTotal`、`reportBurnTotal`、`reportNetTotal`、`reportProteinProgress`、`reportAvgCarbs`、`reportAvgFat`、`reportActiveDaysLabel` 等新 UI 字段；趋势图不再因记录不足 3 天被整块隐藏。
- **窄屏修复 / Narrow Screen Fixes**: 修复 320px 首页环形热量/蛋白卡横向溢出、个人页 `2,000 kcal` 数字内部溢出，以及扫描页/报告页少数 40-42px 按钮触摸高度不足的问题。
- **图标修复 / Icon Mapping**: 补齐 `photo_library`、`center_focus_strong`、`barcode_scanner`、`edit`、`bookmark`、`save` 等本地 SVG 图标，扫描页和结果页按钮不再回退成默认 info 图标。
- **验证 / Verification**: 本地 `http://127.0.0.1:5000/api/health` 返回 `version: "v5.6.13"`；`python ast.parse(app.py)` 通过；`node build.js` 已同步 `www/index.html` 与 `www/sw.js`；`scratch/cdp-mobile-capture.mjs` 在 390px、360px、320px 截图均为 `overflowCount=0` 且无运行时错误；`scratch/mobile-audit.cjs` 在 iPhone 13、360 Android、320 窄屏审计结果均为 `{}`。
- **版本更新 / Version Bump Applied**: 同步更新 `CLIENT_VERSION`、`fallback_version`、`static/sw.js`、`www/index.html`、`www/sw.js` 和 `RELEASE_NOTES_v5.6.13.md`。注意：每次面向手机端的 UI/前端/部署改动仍必须继续递增版本号，否则已安装手机端不会收到更新提示。

### v5.6.12 (AI coach data consistency)
- **AI 建议同源数据 / Same-Source AI Context**: 修复周报/AI 运动训练建议把最近记录固定除以 7 的问题；现在按“有记录天数”计算日均，并同时向后端传入与首页同源的 `todaySummary`，避免今天已超目标时仍提示“严重摄入不足”。
- **教练聊天分量修复 / Portion-Aware Coach Chat**: `/api/coach/chat` 现在按食物 `portion` 计算热量、蛋白质、碳水和脂肪，并在提示词中加入首页同源的今日剩余/超出结论。
- **验证 / Verification**: Flask test client 已模拟“今日 2,832 kcal / 111g 蛋白、目标 2,687 kcal”的场景，确认报告建议 prompt 显示今日超出 145 kcal，不再出现错误的 `962 kcal` 结论；教练聊天接口也验证了 2 倍分量不会被算成原始单份热量。
- **版本更新 / Version Bump Applied**: 同步更新 `CLIENT_VERSION`、`fallback_version`、`static/sw.js`、`www/index.html`、`www/sw.js` 和 `RELEASE_NOTES_v5.6.12.md`。

### v5.6.11 (Result detail alignment)
- **识别结果页对齐 / Result Detail Alignment**: 结果页进一步向 Stitch `_10` 食物详情结构靠拢，改为圆形餐盘图、居中热量、AI 洞察卡、三列宏量营养卡，并保留多食材、份量调整、编辑、删除和保存流程。
- **截图覆盖 / Screenshot Coverage**: `scratch/cdp-mobile-capture.mjs` 增加结果页样例截图，手机端审计现在覆盖首页、记录入口、扫描页、结果页、记录页、教练页、个人页和 BMR 弹窗。
- **版本更新 / Version Bump Applied**: 同步更新 `CLIENT_VERSION`、`fallback_version`、`static/sw.js`、`www/index.html`、`www/sw.js` 和 `RELEASE_NOTES_v5.6.11.md`。

### v5.6.10 (Profile UI refinement + verification fix)
- **个人页再压紧 / Profile Density Pass**: 继续按 Stitch / image2 参考压紧个人页首屏，降低头像、标题、目标卡片、身体数据列表的视觉重量，修复热量与蛋白质单位在窄屏下贴边的问题。
- **AI 教练页收紧 / Coach Density Pass**: 把快捷问题改为两列网格并取消空白过大的聊天高度，让教练页首屏更像可直接操作的工具界面。
- **截图验证更可靠 / Screenshot Verification Fix**: `scratch/cdp-mobile-capture.mjs` 改为每次使用独立 Edge profile 和 cache-bust URL，支持 `UI_WIDTH` / `UI_HEIGHT` / `UI_OUT_DIR` 指定手机视口，并在截图审计中记录 `CLIENT_VERSION`、Profile DOM 状态和运行时错误，避免旧 Service Worker 或 Flask 模板缓存误判。
- **版本更新 / Version Bump Applied**: 同步更新 `CLIENT_VERSION`、`fallback_version`、`static/sw.js`、`www/index.html`、`www/sw.js` 和 `RELEASE_NOTES_v5.6.10.md`，确保手机端能收到新版本提示。

### v5.6.8 (UI Redesign Completion)
- **首页与底栏重构 / Home & Bottom Nav Redesign**: 完全移除了旧版的渲染脚本，直接从底层重构了 `#page-home` 静态 HTML 和底部导航栏的 DOM 与 Tailwind CSS 类。
- **高保真还原 / High-fidelity alignment**: 实现绿色胶囊状的导航激活状态，取消了旧版不一致的红点，并且完成了圆环热量/蛋白质进度表设计，完全对齐设计稿 (Stitch UI)。
- **版本更新 / Version Bump Applied**: 同步更新 `CLIENT_VERSION`、`fallback_version` 和 `RELEASE_NOTES_v5.6.8.md`，升级 service worker 缓存 `v5.6.8` 触发强制刷新。

### v5.6.6 (Stitch UI consolidation)
- **首页渲染一致性修复 / Home Rendering Consolidation**: 移除旧版三次重写 `#page-home` 的不稳定逻辑，将最新的 Stitch 双圆环卡片布局（v5.6.4/v5.6.5）直接固化到静态 HTML 结构中，解决了 `applyStitchV564Alignment` 在某些加载时序下被跳过或覆盖导致的大块旧版 TODAY BALANCE 布局残留问题。
- **动态注水 / Hydration Script Simplified**: 将原先大段替换 HTML 的代码简化为仅通过 ID 赋值用户名与打招呼语。
- **版本更新规则已执行 / Version Bump Applied**: 同步更新 `CLIENT_VERSION`、`fallback_version` 和 `RELEASE_NOTES_v5.6.6.md`，确保后续手机端部署可以收到更新提示。

### v5.6.5 (nutrition goal calculator)
- **目标选择 / Goal Selection**: BMR 设置流程新增“减脂 / 维持 / 增肌”三种目的，保存到本地资料与云端 `nutrition_goal`。
- **权威估算链路 / Evidence-Based Estimates**: 热量采用 Mifflin-St Jeor 静息代谢公式 + 活动系数，按目标做 -15% / 维持 / +10% TDEE 调整。
- **宏量营养分配 / Macro Targets**: 蛋白按体重 g/kg 计算，脂肪按热量比例计算，剩余热量分配给碳水；首页、个人页和 AI 建议 payload 共用同一套 P/C/F 目标。
- **版本更新规则已执行 / Version Bump Applied**: 同步更新 `CLIENT_VERSION`、`fallback_version` 和 `RELEASE_NOTES_v5.6.5.md`，确保后续手机端部署可以收到更新提示。

### v5.6.4 (Stitch fidelity整改)
- **首页二次收敛 / Home Fidelity Pass**: 再次压缩顶部栏、双圆环卡片、宏量营养、饮水区和空状态，减轻粗边框与大字号，让首屏更接近 Stitch 渲染图。
- **扫描页真实相机感 / Camera Fidelity Pass**: 扫描页改用 Stitch 参考的真实餐盘照片背景，保留 AI 扫描框、食材标签和显眼语音入口。
- **个人页重排 / Profile Rework**: 个人页改为头像头部、目标卡、身体数据、偏好设置和系统设置列表结构，并修复预览状态下用户名 JSON 外露。
- **版本更新规则已执行 / Version Bump Applied**: 同步更新 `CLIENT_VERSION`、`fallback_version` 和 `RELEASE_NOTES_v5.6.4.md`，确保后续手机端部署可以收到更新提示。

### v5.6.3 (Stitch visual alignment)
- **首页重做 / Home Rework**: 按 Stitch 参考稿把首页从深色大 Hero 改为浅底双圆环热量/蛋白卡片，压缩宏量营养、快捷饮水和今日记录间距。
- **沉浸式扫描页 / Immersive Camera Screen**: 拍照页改为全屏相机感布局，底部固定相册、扫描、手动补录三点式操作，并隐藏常规底栏避免视觉冲突。
- **语音入口保留 / Voice Entry Visibility**: 在扫描页顶部和底部都保留语音记录入口，同时放入搜索/扫码入口，保证记录功能优先级不被 UI 弱化。
- **版本更新规则已执行 / Version Bump Applied**: 同步更新 `CLIENT_VERSION`、`fallback_version` 和 `RELEASE_NOTES_v5.6.3.md`，确保后续手机端部署可以收到更新提示。

### v5.6.2 (mobile UI cleanup)
- **固定栏遮挡修复 / Fixed Bar Cleanup**: 顶部 header、底部导航和结果页固定操作栏改为实底背景，减少滚动时内容透出造成的残影和遮挡感。
- **移动端密度优化 / Mobile Density Pass**: 收紧首页、扫描页、结果页、AI 教练和个人页的字号、卡片圆角、模块高度和底部按钮高度，让首屏更像日常工具而不是展示页。
- **记录页进度修正 / Weekly Progress Fix**: 记录页周均进度从固定 87% 改为按实际周均摄入和目标动态计算，并把日期横条放入带淡出遮罩的容器，避免像裁切错误。
- **扫描与补录控件优化 / Scan and Manual Entry Controls**: 扫描区去掉黑棕重色块，改为绿色相机预览风格；扫描按钮区分“选择照片后扫描 / 开始扫描”；手动补录弹窗新增明确搜索按钮。
- **版本更新规则已执行 / Version Bump Applied**: 同步更新 `CLIENT_VERSION`、`fallback_version` 和 `RELEASE_NOTES_v5.6.2.md`，确保后续手机端部署可以收到更新提示。

### v5.6.1 (record entry IA fix)
- **主记录入口修正 / Primary Record Entry Fix**: 底部中间主按钮不再直接进入拍照页，改为打开“选择记录方式”面板，拍照识别、语音记录、搜索/扫码/手动补录和记录运动都从同一个入口进入。
- **语音记录可发现性 / Voice Discoverability**: 语音记录在记录方式面板中与拍照同级展示，并在拍照页增加可见的“语音记录”卡片和文字化麦克风按钮，避免核心功能被藏在右上角。
- **版本更新规则已执行 / Version Bump Applied**: 同步更新 `CLIENT_VERSION`、`fallback_version` 和 `RELEASE_NOTES_v5.6.1.md`，确保后续手机端部署可以收到更新提示。

### v5.6.0 (UI redesign in progress)
- **移动端 UI 总体重构 / Mobile UI Redesign**: 按 Stitch 参考方向重做首页、记录、拍照、识别结果、AI 教练、个人中心和关键弹窗层级，底部导航调整为“首页 / 记录 / 拍照 / 教练 / 个人”。
- **版本更新规则已执行 / Version Bump Applied**: 同步更新 `CLIENT_VERSION`、`fallback_version` 和 `RELEASE_NOTES_v5.6.0.md`，确保后续手机端部署可以收到更新提示。

### v5.5.11 (最新发布)
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

- **当前本地版本 / Current Local Version**: v5.6.19，`/api/health` 需在重新构建/重启后返回 `version: "v5.6.19"`。
- **本地预览地址 / Local Preview URL**: http://127.0.0.1:5000/ 。当前服务因根目录 `database.db` 只读，使用可写预览库启动；如果重启失败，优先检查是否又落回根目录只读 SQLite。
- **已完成 / Completed**: 首页 (`#page-home`) 及底部导航栏保持 Stitch 风格；扫描页真实餐盘背景已恢复；中间记录入口弹窗中“拍照识别”和“语音记录”为同级主入口；结果页已向 Stitch `_10` 食物详情结构靠拢；记录/报告页已向 Stitch `_9` 记录历史结构靠拢；个人页已进一步向 Stitch `_8` 参考稿压紧；AI 教练页已减少首屏空白并改为紧凑快捷问题网格；AI 周报/教练接口已修复与真实记录不同源的问题；体重页和手动补录弹窗已开始向 Stitch 记录页与底部 Sheet 语言靠拢；语音听写链路已改为 OpenRouter STT 优先、Gemini 兜底。
- **已验证 / Verified**: 旧版本验证链路可参考 `v5.6.13`，但 `v5.6.19` 仍需重新执行 `python ast.parse(app.py)`、`node build.js` 与手机端截图审计来确认语音修复与二/三级页面改动。
- **截图证据 / Screenshot Evidence**: `scratch/current-mobile-ui/01-home.png`、`03-scan.png`、`04-result.png`、`05-report.png`、`06-coach.png`、`07-profile.png`、`08-bmr-goal-modal.png`，审计结果在 `scratch/current-mobile-ui/audit.json`。
- **已完成重点 / UI Redesign Completed**: 已完成首页、扫描页、记录入口、结果页、个人页和 AI 教练页的主要手机端 UI 整改；当前截图已不再出现旧版大 Profile 布局、扫描页版本弹窗遮挡或教练页大面积空白；AI 建议不再把周均摊平值当成今日真实摄入。
- **下一步建议 / Next Step**: 如继续追求更接近渲染图，可继续对历史详情、运动/体重/BMR 弹窗、设置页等二/三级页面做同一套密度和视觉语言统一；部署前再次确认版本号递增规则，并用生产 URL 做一次手机真机刷新验证；语音链路如仍异常，优先检查 OpenRouter STT 返回码与模型可用性。
- Android APK 图标已通过 CI 替换为新 AS + 绿叶设计，需手动下载 artifact 安装
- 体重功能已完整集成在报告页（周报）
- 底部导航已简化为 5 项（首页 / 教练 / + / 报告 / 个人）
- 待优化：离线支持、推送通知、iOS 适配

---

## 8A. Antigravity 接手说明 / Antigravity Continuation Brief

### 当前目标 / Objective

继续把 NutriSnap AI 手机端 UI 整改到更接近 Stitch / image2 渲染图，重点从一级页面转向二级、三级界面一致性。当前不要重新推翻已完成的首页、扫描页、结果页、记录/报告页、教练页、个人页主结构；下一步应补齐历史详情、手动补录、体重页、运动/体重/BMR 弹窗、设置页等深层界面的同一套视觉语言。

### 重要约束 / Non-negotiable Rules

1. **每次面向手机端的 UI/前端/部署改动都必须递增版本号**。当前已是 `v5.6.19`，下一次 UI 改动应从 `v5.6.20` 开始。
2. 版本号必须同步更新：
   - `templates/index.html` 中的 `CLIENT_VERSION`
   - `app.py` 中 `fallback_version` 和附近 target regex 注释
   - `static/sw.js` 中 `CACHE_NAME`
   - 新增 `RELEASE_NOTES_vX.Y.Z.md`
   - 运行 `node build.js` 同步 `www/index.html`、`www/sw.js`、`www/static/sw.js`
3. `templates/index.html` 是前端源文件；不要手工长期维护 `www/index.html`，它由 `node build.js` 生成。
4. 用户倾向“直接执行”，不要反复确认；但部署、删除、重置等高风险操作仍需谨慎。
5. 不要回滚工作树里已有修改。当前工作树已有多轮 UI/version 文件处于 modified/untracked 状态，视为已有成果。

### 当前已完成 / Already Done

- `v5.6.13` 已完成记录/报告页向 Stitch `_9` 靠齐：记录历史 header、日期条、热量收支卡、宏量营养卡、7 天趋势、今日记录列表、AI 洞察。
- AI 教练/报告建议已修复为和真实记录同源，不再把周均摊平值误当今日摄入。
- 底部中间记录入口已改为“选择记录方式”，拍照识别和语音记录同级，不再把语音藏在右上角。
- 390px、360px、320px 一级页面截图审计曾通过：`overflowCount=0`，`scratch/mobile-audit.cjs` 在 iPhone 13、360 Android、320 窄屏结果为 `{}`。
- 扫描页/结果页缺失图标已补齐，不再回退成默认 info 图标。

### 当前未完成 / Remaining Work

按优先级继续：

1. **整体视觉密度与对齐微调 / Final Polish Pass**
   - 当前一级、二级、三级弹窗的 Stitch 风格重构已基本铺设完毕。
   - 建议在真实移动设备或不同分辨率的模拟器中复查横向/纵向溢出、留白比例、阴影质感、动画流畅度。
   - 检查组件复用的 CSS class 是否可以在 `static/index.css` 或 `<style>` 标签中进一步精简提取，降低 HTML 的类名堆叠。

2. **iOS 特定样式适配 / iOS specific tweaks**
   - 检查刘海屏（Notch）与 Home Bar 附近的内边距（env(safe-area-inset-bottom) 和 env(safe-area-inset-top)）是否在所有的绝对定位层级中均起到了保护作用。

3. **辅助界面的补齐**
   - 应用设置、个人信息修改、通知设置等较深层级的界面，依然存在部分旧版样式，可以作为下一步统一处理的对象。

### 当前截图/验证脚本状态 / Verification Script State

- 主流程截图脚本：`scratch/cdp-mobile-capture.mjs`
  - 覆盖首页、记录入口、扫描、结果、记录/报告、教练、个人、BMR 弹窗。
  - 常用命令：
    ```powershell
    node scratch\cdp-mobile-capture.mjs
    $env:UI_WIDTH='360'; $env:UI_HEIGHT='800'; $env:UI_OUT_DIR='scratch/current-mobile-ui-360'; node scratch\cdp-mobile-capture.mjs
    $env:UI_WIDTH='320'; $env:UI_HEIGHT='740'; $env:UI_OUT_DIR='scratch/current-mobile-ui-320'; node scratch\cdp-mobile-capture.mjs
    ```
- 布局审计脚本：`scratch/mobile-audit.cjs`
  - 常用命令：
    ```powershell
    $env:AUDIT_URL='http://127.0.0.1:5000/'; node scratch\mobile-audit.cjs
    ```
- 本轮曾尝试新增 `scratch/cdp-secondary-capture.mjs` 用于历史页/手动补录/体重页/运动弹窗截图，但尚未跑通。
  - 已修过 ESM import 问题。
  - 当前已知问题：Playwright `page.evaluate()` 不能序列化传入函数 `isoFor`，需要改成传入预生成的时间字符串对象，或把 `isoFor` 函数定义在浏览器上下文内部。
  - 这个脚本位于 `scratch/`，可能被 `.gitignore` 忽略；Antigravity 若看不到 git status 变更，仍可直接打开本地文件检查。

### 建议 Antigravity 的第一步 / First Action

1. 先确认本地服务：
   ```powershell
   try { (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5000/api/health -TimeoutSec 8).Content } catch { $_.Exception.Message }
   ```
2. 修好或重写 `scratch/cdp-secondary-capture.mjs`，生成以下截图：
   - `01-history.png`
   - `02-manual-food-modal.png`
   - `03-weight.png`
   - `04-weight-modal.png`
   - `05-exercise-modal.png`
3. 先不要大改全部页面。建议第一轮只做 `#page-history` + `#manualFoodModal`，版本升到 `v5.6.14`，因为这两处和“记录功能服务于 UI”的关系最强。
4. 完成后运行：
   ```powershell
   python -c "import ast, pathlib; ast.parse(pathlib.Path('app.py').read_text(encoding='utf-8')); print('app.py syntax ok')"
   node build.js
   node scratch\cdp-mobile-capture.mjs
   $env:AUDIT_URL='http://127.0.0.1:5000/'; node scratch\mobile-audit.cjs
   ```
5. 若改了二级截图脚本，也跑 `node scratch\cdp-secondary-capture.mjs` 并查看 320px 截图是否无横向溢出。

### 给 Antigravity 的可复制提示词 / Copyable Prompt

```text
请在 G:\我的云端硬盘\vibe coding\ai-diet-tracker 继续 NutriSnap AI 手机端 UI 整改。先阅读 HANDOFF.md，当前版本是 v5.6.19。不要推翻已有首页、扫描页、结果页、记录/报告页、教练页、个人页主结构，以及刚完成的二级/三级底部弹窗 (Bottom Sheet)。本轮优先继续收敛二级页面与设置细节，尤其是记录历史、体重页、运动/体重/BMR 弹窗的视觉一致性。

重要规则：任何手机端 UI/前端改动必须 bump 到下一个版本，并同步 templates/index.html CLIENT_VERSION、app.py fallback_version/注释、static/sw.js CACHE_NAME、新增对应 RELEASE_NOTES_*.md，然后运行 node build.js。templates/index.html 是源文件，www/index.html 由 build 生成。

完成后跑 app.py 语法检查、node build.js，并更新 HANDOFF.md。
```

---

## 9. 常见操作指南 / Common Operations

### 修改前端 / Modify Frontend
```
改 templates/index.html → 更新 CLIENT_VERSION → 新增 RELEASE_NOTES_vX.Y.Z.md → git commit + push → 等 CI 自动部署
```
> 不要直接改 www/index.html，它会被 CI 覆盖。

### 修改后端 / Modify Backend
```
改 app.py → 更新 fallback_version / RELEASE_NOTES_vX.Y.Z.md（如影响客户端）→ git commit + push → 等 Cloud Run 自动部署
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
6. **版本号与手机更新推送**：每次面向手机端的部署必须递增版本号，并准备同版本 Release Notes；否则 `/api/update/info` 返回的版本不会高于旧客户端 `CLIENT_VERSION`，手机端不会弹出更新提示。

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

---

## 12. v5.6.28 本轮完成状态 / Current v5.6.28 Status

### 本轮目标

继续修复 NutriSnap AI 手机端：个人页身体数据不显示、AI 洞察刷新过频、语音听写失败、识别速度慢、中国大陆可用性，以及部署前 UI 布局风险。

### 已完成

- 当前版本已递增到 `v5.6.28`，并同步：
  - `templates/index.html` 的 `CLIENT_VERSION`
  - `app.py` 的 `fallback_version`
  - `static/sw.js` 的 `CACHE_NAME`
  - `package.json` / `package-lock.json`
  - `RELEASE_NOTES_v5.6.28.md`
  - `node build.js` 已同步 `www/index.html` 与 `www/sw.js`
- 个人页身体数据已修复：云端 profile 为空时，优先用 `ProfileStorage.getProfile()` 的本地档案兜底，体重/身高/BMR 能正常显示。
- AI 洞察刷新逻辑已改为：数据变化只标记 dirty，不立即请求；用户进入记录页且签名变化时才刷新。
- 语音功能已加原生 `SpeechRecognition/webkitSpeechRecognition` 优先路径；服务端 STT 失败时保留弹窗并显示文字兜底，不再直接关闭。
- 图片识别速度已优化：前端上传前压缩图片，后端视觉输入限制到最大边 1280，并优先调用 Flash Lite/Flash。
- 中国大陆可用性已改善：
  - 移除前端 Tailwind/Chart/html5-qrcode/Google Fonts CDN。
  - 改用 `/static/vendor/` 本地资源。
  - 移除扫描页残留的 `googleusercontent.com` 外链背景，改为本地 CSS 食物盘视觉。
- 修复一个关键结构性 UI bug：`body` 顶部“跳过导航”链接的结束标签历史乱码为 `?/a>`，导致整个 App 被浏览器包进隐藏 `sr-only` 链接，手机端 active page 宽度变成 `0px`。已改为合法 `</a>`，页面重新成为 `body` 直接子元素。
- BMR/体重/运动/水/成就等弹窗关闭按钮增加 `min-w-11 min-h-11`，避免 320/360px 视口下触控目标四舍五入低于 44px。

### 已验证

本地服务：`http://127.0.0.1:5000/`

已运行并通过：

```powershell
python -c "import ast, pathlib; ast.parse(pathlib.Path('app.py').read_text(encoding='utf-8')); print('app.py syntax ok')"
python check_js_syntax.py
node build.js
$env:AUDIT_URL='http://127.0.0.1:5000/'; node scratch\mobile-audit.cjs
```

`scratch/mobile-audit.cjs` 最新结果：

```text
iphone-13: {}
android-360x800: {}
narrow-320x720: {}
```

额外 Playwright 冒烟验证：

- `CLIENT_VERSION` 为 `v5.6.28`
- 页面不再含 `v5.6.27`
- 页面不再含 `cdn.tailwindcss.com`、`fonts.googleapis.com`、`googleusercontent.com`
- `page-home` 是 `body` 直接子元素，宽度为视口宽度，不再是 `0px`
- 个人页本地 profile 兜底显示：体重 `72.5 kg`、身高 `178 cm`、BMR 正常显示、状态为 `已设置`
- AI 洞察：标脏后进入记录页才请求 `/api/report/suggestions`，进入前请求数为 0，进入后请求数为 1
- 语音听写失败时显示 `voiceTextFallback`，并提供文字输入兜底

### 部署前注意

- `www/` 是构建产物，源文件仍是 `templates/index.html`。
- `static/vendor/` 三个本地 vendor 文件必须随提交进入仓库，否则生产/手机端会缺资源：
  - `static/vendor/tailwindcss-forms-container-queries.js`
  - `static/vendor/chart.umd.min.js`
  - `static/vendor/html5-qrcode.min.js`
- 当前工作区仍有大量历史 untracked 调试脚本和旧 release notes，提交时只加入本轮相关文件，避免把 scratch/临时脚本打包进去。

### 下一步

提交并推送 `main`，触发 GitHub Actions 部署 Cloud Run 和 Android APK 构建。部署后检查：

```powershell
Invoke-WebRequest -UseBasicParsing https://nutrisnap-ai-940406235442.us-central1.run.app/api/health
```

期望 `version` 返回 `v5.6.28`。

---

*最后更新：2026-05-29*
