# NutriSnap AI v5.6.28

## 中文

- 修复本地重新启动后手机端页面容器宽度为 0 的潜在布局问题，避免布局审计和窄屏渲染出现异常宽度。
- 移除扫描页残留的 Google 外链背景图，改为本地 CSS 食物盘视觉，保证中国大陆网络环境下也能稳定显示。
- 继续遵守移动端更新规则，版本号和 Service Worker 缓存已递增到 v5.6.28。

## English

- Fixed a mobile page container width issue that could produce zero-width active pages in runtime audits and narrow viewport rendering.
- Removed the remaining Google-hosted scan background image and replaced it with a local CSS food-plate visual for mainland China compatibility.
- Bumped the mobile-facing app version and Service Worker cache to v5.6.28.
