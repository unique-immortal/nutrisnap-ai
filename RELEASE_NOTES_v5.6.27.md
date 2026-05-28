# NutriSnap AI v5.6.27

## 中文

- 修复登录/云端同步模式下个人页身体数据被空云端档案覆盖，体重、身高、基础代谢会优先使用已保存的本地档案兜底显示。
- AI 洞察改为“数据变化后，用户进入记录页时才刷新”，保存、删除、修改目标不再立刻重复请求 AI。
- 语音记录增加浏览器原生听写优先路径；服务器听写失败时保留弹窗并提供文字记录兜底，不再直接关闭。
- 图片识别提速：前端上传前压缩大图，后端限制视觉模型输入尺寸，并优先调用更快的 Flash Lite/Flash 模型。
- 继续遵守移动端版本递增规则，更新 Service Worker 缓存到 v5.6.27，确保手机端可以收到更新。

## English

- Fixed profile body metrics being overwritten by an empty cloud profile; saved local body data now remains visible as a fallback.
- AI insights now refresh only after data changes and when the user opens the report tab, avoiding repeated background refreshes.
- Added native browser speech recognition first, plus a text fallback when server transcription fails.
- Improved image recognition speed with client-side image compression, backend image resizing, and faster model priority.
- Bumped mobile-facing version and Service Worker cache to v5.6.27 so installed clients can receive the update.
