# NutriSnap AI v5.6.16

## UI
- 对所有二级 Modal 和面板进行底部弹窗（Bottom Sheet）改造，使其交互语言向 Stitch 稿看齐。
- 将原本居中显示的各个功能性弹窗统一迁移为屏幕底部拉出的交互模式，包含以下组件：
  - `exerciseLogModal` (运动记录弹窗)
  - `waterRecordModal` (饮水记录弹窗)
  - `weightRecordModal` (体重记录弹窗)
  - `editMealModal` (编辑餐饮弹窗)
  - `deleteConfirmModal` (删除确认弹窗)
  - `bmrSetupModal` (基础代谢设置弹窗)
  - `loginOverlay` (登录面板)
- 这些底部弹窗均引入了拖拽手柄条（Handle Bar）、`rounded-t-3xl` 顶部圆角、更协调的边距以及平滑拉起动画，加强了移动端用户的沉浸感与一致性。

## Mobile Update
- 运行 `node build.js` 将改动打包至 `www/` 目录。
- 更新了 `CLIENT_VERSION`、`fallback_version` 以及 `sw.js` 的缓存版本为 `v5.6.16`，以确保老版本客户端正确触发自动更新。
