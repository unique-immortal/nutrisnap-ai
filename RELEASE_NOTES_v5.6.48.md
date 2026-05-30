# Release v5.6.48

## Fixes

* 修复显式 Google 语音转写路径被免费模型路由短路的问题，避免语音录制后直接进入“转写服务繁忙”。
* 语音转写优先接入 OpenRouter 免费音频多模态模型 `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`，失败后再兜底 Google 转写。
* 修复拍照识别链只尝试前两个视觉模型的问题，现在会完整尝试三段免费视觉降级链。
* OpenRouter 免费额度短暂 429 不再立即把整个 OpenRouter 提供商长时间冷却，降低拍照/语音误报“服务繁忙”的概率。
* Service Worker 与前端版本号同步更新到 v5.6.48，确保手机端能正确拉到新缓存。
