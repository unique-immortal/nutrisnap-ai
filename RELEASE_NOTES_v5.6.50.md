# v5.6.50

## 修复
- Google quota/rate-limit 不再固定冷却 15 分钟，会读取上游建议的 retryDelay 并尽快自动恢复。
- Google 兜底链遇到 429 会继续尝试后续 Gemini 模型，而不是第一个模型限流就整条链停止。
- 视觉识别链 hard deadline 调整到 26 秒，确保四个免费视觉模型都能按顺序尝试。
- 保持 OpenRouter 免费链与高级 `gpt-5.4-mini` 链隔离，避免免费模式误用付费模型。

## 验证
- 后端语法检查通过，前端构建同步完成。
