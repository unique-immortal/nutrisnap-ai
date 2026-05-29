# NutriSnap AI v5.6.31

- OpenRouter 免费模型按功能拆分：拍照只使用支持 image 输入的视觉/多模态模型池。
- 默认模型顺序改为“质量与速度平衡”：先用稳定的免费模型，失败或返回异常时快速切换到更强兜底模型。
- 语音保持先转写，再把转写文本交给免费纯文本模型池做食物和运动解析。
- 线上可通过 `OPENROUTER_TEXT_MODELS` 与 `OPENROUTER_VISION_MODELS` 分别调整模型顺序。
