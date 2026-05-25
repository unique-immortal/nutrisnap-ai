import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  retries: process.env.CI ? 2 : 0,
  reporter: [['html'], ['list']],
  use: {
    baseURL: 'http://127.0.0.1:5000',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  expect: {
    toHaveScreenshot: {
      maxDiffPixelRatio: 0.02,   // 容忍 2% 像素差异以减少跨操作系统渲染差异导致的误报
      animations: 'disabled',     // 自动禁用 CSS 动画和过渡
    },
  },
  projects: [
    { name: 'mobile-narrow-320',   use: { viewport: { width: 320, height: 720 }, isMobile: true, hasTouch: true } },
    { name: 'android-pixel-7',     use: { ...devices['Pixel 7'] } },
    { name: 'fold-inner',          use: { viewport: { width: 1768, height: 2208 }, deviceScaleFactor: 2 } },
    { name: 'fold-outer',          use: { viewport: { width: 904,  height: 2316 }, deviceScaleFactor: 2.5 } },
    { name: 'tablet-768',          use: { viewport: { width: 768,  height: 1024 }, isMobile: true, hasTouch: true } },
    { name: 'desktop-1440',        use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 1000 } } },
  ],
  webServer: {
    command: 'python app.py',
    url: 'http://127.0.0.1:5000',
    reuseExistingServer: !process.env.CI,
    stdout: 'ignore',
    stderr: 'pipe',
    timeout: 30000,
    env: {
      TEST_MODE: 'true'
    }
  },
});
