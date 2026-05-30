# v5.6.49

## 修复
- 修复 OpenRouter 配置后 `call_llm()` 不再进入 Google 兜底、部分功能直接显示服务繁忙的问题。
- 按 OpenRouter 官方模型接口核对免费模型 slug，补齐视觉链 `nvidia/nemotron-nano-12b-v2-vl:free`，并把默认尝试数提高到 4。
- OpenRouter 现在会打印状态码和 error message；429 会继续尝试下一个免费模型，401/402/403 会跳出 OpenRouter 并尝试 Google 兜底。
- 语音转写优先走 Google inline audio，OpenRouter STT 只作为后备，避免 webm 音频兼容问题。
- 文本推理链不再只在 timeout 时降级，AI 教练、洞察、手动补录等文本功能会按免费文本模型链逐级尝试。

## 验证
- 版本号与 service worker 缓存更新到 v5.6.49，确保移动端能收到更新提示。
