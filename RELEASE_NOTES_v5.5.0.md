# v5.5.0 — iOS Adaptation & OpenRouter Free Model Fallback Tuning / iOS 适配与 OpenRouter 免费模型调优

> **线上地址 / Live URL**: https://nutrisnap-ai-940406235442.us-central1.run.app  
> **发布时间 / Release Date**: 2026-05-22  

---

## 🌟 新特性 / New Features

### 📱 iOS 适配与安全区优化 / iOS Adaptation & Safe Area Refinements
- **中**: 在 `.glass-header` 样式中加入了顶部安全区填充 padding-top `calc(env(safe-area-inset-top, 0px) + 0.75rem)`，防止头部标题栏与 iOS 刘海或状态标发生重叠。
- **EN**: Refined header positioning by adding safe area inset padding `calc(env(safe-area-inset-top, 0px) + 0.75rem)` to `.glass-header`, preventing layout overlaps with the iOS status bar or notch.

### 🤖 OpenRouter 免费模型智能调优 / OpenRouter Free Model Tuning & Fallback Chain
- **中**: 针对 AI 教练与食物分析接口重新梳理了 OpenRouter 调优降级链，根据请求类型（多模态/文本）分别自动路由并优先使用高品质免费模型（如 Llama 3.3 70B 免费版、Gemma 4 31B 免费视觉版），显著降低 API 运行成本。
- **EN**: Redesigned the OpenRouter fallback chain to intelligently differentiate between multimodal and text-only requests, prioritizing top-performing free models (e.g. `meta-llama/llama-3.3-70b-instruct:free` and `google/gemma-4-31b-it:free`) to significantly lower API operating costs.

---

## 🔧 技术变更与系统优化 / Technical Updates & System Optimizations
- **中**: 更新 `capacitor.config.json` 添加 `"iosScheme": "https"` 以支持 iOS 本地安全资源加载与跨域请求；将前端页面打包重新部署于 Capacitor `www/` 目录。
- **EN**: Configured `capacitor.config.json` with `"iosScheme": "https"` to support secure local resource loading and prevent CORS issues on iOS. Compiled and synchronized front-end bundle assets inside the local Capacitor build directory.
