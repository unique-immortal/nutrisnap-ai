# NutriSnap AI v5.6.33

- 调整模型路由为用户体验优先：语音文本解析先走稳定快链路，失败后才尝试一个 OpenRouter 免费营养模型。
- 拍照两阶段流水线保留 OpenRouter 免费模型，但视觉层最多 1 次 6 秒，营养层最多 1 次 5 秒，超时或格式异常立即切 Gemini 兜底。
- 保持视觉识别、包装标签 OCR、营养推理三类职责分离，避免免费模型排队拖慢主流程。
- 拍照结果新增 `confidence`、`weight_range`、`needs_user_confirmation`、`data_source`、`model_used` 与 `latency_ms` 元数据，便于前端提示低置信度份量确认。
