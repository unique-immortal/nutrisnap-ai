# NutriSnap AI v5.6.35

- 语音文本解析改为 OpenRouter 低延迟文本模型优先，单次 5 秒预算，失败或无有效 JSON 再切 Gemini。
- 避免 Gemini 高峰/配额波动把语音记录主流程拖到 20 秒以上。
- 保留 v5.6.34 的字段兼容，支持 `name`、`calories_kcal`、`estimated_grams` 等结构化返回。
