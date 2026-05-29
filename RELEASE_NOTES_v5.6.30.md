# NutriSnap AI v5.6.30

- 修复本地/离线 App 用户语音接口未携带 JWT 时无法进入后端转写链路的问题。
- Android App 内新增系统原生语音识别优先通道，失败后再退回浏览器录音上传。
- 语音上传在本地模式下显式携带 `is_app=true`，确保 OpenRouter/Gemini 转写链路能被调用。
- OpenRouter 默认只用于语音转写 fallback，不再因为配置 key 就接管普通文字解析/聊天请求，避免额外消耗。
- 不新增任何付费语音服务；保持文字记录兜底。
