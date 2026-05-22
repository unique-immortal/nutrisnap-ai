# v5.4.0 — Hydration Tracking, Consecutive Streak & Achievements / 饮水追踪、连续打卡与成就徽章

> **线上地址 / Live URL**: https://nutrisnap-ai-940406235442.us-central1.run.app  
> **发布时间 / Release Date**: 2026-05-22  

---

## 🌟 新特性 / New Features

### 💧 饮水追踪 / Hydration Tracking
- **中**: 首页和报告页全新加入高颜值饮水打卡与水量追踪组件，采用双层动态 SVG 波浪动画圈，随每日 2000ml 目标进度动态起伏。
- **EN**: A premium hydration tracking card added to Home and Report tabs, featuring a dual-layered animated wave SVG progress circle based on a daily target of 2000ml.

### 🔥 连续打卡与火焰图标 / Consecutive Streak & Streak Pill
- **中**: 顶部导航栏新增 YAZIO 风格的橙色火焰连续记录天数 (Streak Pill)。只要当天或昨天记录了饮食、运动或饮水，Streak 就会递增或保持，逾期未记录自动重置。
- **EN**: Added a YAZIO-style orange flame Streak Pill in the header. Consecutive logging days count increments or persists when logging meals, exercise, or water today or yesterday, and resets to 0 if both days are missed.

### 🏆 成就系统与悬浮通知 / Achievements & Floating Toast
- **中**: 新增成就系统，包含「初步补水」、「补水达人」、「初显成效」、「自律达人」及「膳食管家」等 5 项核心徽章及进度进度展示。解锁时顶部滑动显示高颜值 Toast 浮窗提示。
- **EN**: Introduced a gamified Achievements system featuring 5 milestone badges: "First Water", "Daily Hydration Master", "3-Day Streak", "7-Day Streak", and "First Meal". An elegant floating toast triggers at the top of the page when any badge is unlocked.

---

## 🔧 技术变更与数据库升级 / Technical Updates & Database Upgrades
- **中**: 升级 SQLite 数据库，动态为 `daily_summaries` 表添加 `total_water` 字段；更新后端 `/api/daily-summaries`、`/api/report/weekly` 等接口支持水分统计及跨设备本地同步逻辑。
- **EN**: Migrated the SQLite schema to dynamically add `total_water` to the `daily_summaries` table. Upgraded Flask API endpoints (`/api/daily-summaries`, `/api/report/weekly`) to support hydration stats and local-first cross-device sync.
