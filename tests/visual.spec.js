import { test, expect } from '@playwright/test';

// 稳定性辅助函数：在截图前禁用所有动画和过渡，等待字体加载完毕以防止误报
async function stabilize(page) {
  await page.waitForLoadState('networkidle');
  await page.evaluate(async () => {
    // 等待所有 Web 字体加载就绪
    if (document.fonts?.ready) {
      await document.fonts.ready;
    }
    // 强制全局关闭 CSS Transition、Animation 和文本光标闪烁
    const s = document.createElement('style');
    s.innerHTML = `
      *, *::before, *::after {
        transition: none !important;
        animation: none !important;
        transition-duration: 0s !important;
        animation-duration: 0s !important;
      }
      input, textarea {
        caret-color: transparent !important;
      }
    `;
    document.head.appendChild(s);
  });
}

// 初始化状态，绕过 onboarding 界面和登录界面，直接进入主界面
async function setupApp(page) {
  await page.goto('/');
  await page.evaluate(() => {
    localStorage.setItem('onboarding_completed', 'true');
    localStorage.setItem('currentUser', 'local_user');
    localStorage.setItem('storageMode', 'local');
  });
  // 重新加载页面以应用状态
  await page.goto('/?testMode=1');
  await stabilize(page);
}

test('Dashboard Bento 布局视觉回归', async ({ page }) => {
  await setupApp(page);

  // 截图整个 Dashboard 布局并与基准对比
  await expect(page).toHaveScreenshot('dashboard-bento.png', {
    fullPage: true,
    // 遮罩图表 canvas 以避免随机图表数据变动或渲染抖动导致报错
    mask: [
      page.locator('#calorieChart'),
      page.locator('#macroChart'),
      page.locator('#weightChart')
    ]
  });
});

test('关于弹窗（About Modal）交互与视觉验证', async ({ page }) => {
  await setupApp(page);

  // 切换到“个人”标签页
  await page.locator('[data-tab="profile"]').click();
  await expect(page.locator('#page-profile')).toBeVisible();

  // 模拟用户点击“关于”按钮
  await page.locator('text=关于 NutriSnap AI').click();

  // 验证弹窗可见
  const aboutModal = page.locator('#aboutModal');
  await expect(aboutModal).toBeVisible();

  // 对弹窗本身进行截图对比，确保关于界面布局和版本渲染正确
  await expect(page).toHaveScreenshot('about-modal.png');
});

test('深色模式切换稳定性与视觉对比', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));

  await setupApp(page);

  // 切换到“个人”标签页
  await page.locator('[data-tab="profile"]').click();
  await expect(page.locator('#page-profile')).toBeVisible();

  // 模拟点击切换深色模式
  const modeBtn = page.locator('text=深色模式').or(page.locator('text=Theme')).first();
  if (await modeBtn.isVisible()) {
    await modeBtn.click();
    await page.waitForTimeout(300); // 等待主题应用
  }

  // 切换回“首页”标签页以进行深色模式 Bento 截图
  await page.locator('[data-tab="home"]').click();
  await expect(page.locator('#page-home')).toBeVisible();

  // 截图记录深色模式首页
  await expect(page).toHaveScreenshot('home-dark.png', {
    fullPage: true,
    mask: [
      page.locator('#calorieChart'),
      page.locator('#macroChart'),
      page.locator('#weightChart')
    ]
  });

  // 确保没有发生运行时 JS 崩溃错误
  expect(errors).toEqual([]);
});

test('BMR 设置弹窗 (BMR Setup Modal) 视觉验证', async ({ page }) => {
  await setupApp(page);

  // 打开 BMR 设置弹窗
  await page.evaluate(() => {
    showBMRSetupModal(false);
  });

  // 验证弹窗可见
  const bmrModal = page.locator('#bmrSetupModal');
  await expect(bmrModal).toBeVisible();

  // 稳定页面并截图对比
  await stabilize(page);
  await expect(page).toHaveScreenshot('bmr-modal.png');
});

test('记录饮水弹窗 (Custom Water Modal) 视觉验证', async ({ page }) => {
  await setupApp(page);

  // 打开记录饮水弹窗
  await page.evaluate(() => {
    showCustomWaterModal();
  });

  // 验证弹窗可见
  const waterModal = page.locator('#waterRecordModal');
  await expect(waterModal).toBeVisible();

  // 稳定页面并截图对比
  await stabilize(page);
  await expect(page).toHaveScreenshot('water-record-modal.png');
});

test('教练标签页 (Coach Tab) 视觉验证', async ({ page }) => {
  await setupApp(page);

  // 切换到“教练”标签页
  await page.locator('[data-tab="coach"]').click();
  await expect(page.locator('#page-coach')).toBeVisible();

  // 稳定页面并截图对比
  await stabilize(page);
  await expect(page).toHaveScreenshot('coach-tab.png');
});

test('营养周报标签页 (Report Tab) 视觉验证', async ({ page }) => {
  await setupApp(page);

  // 切换到“报告”标签页
  await page.locator('[data-tab="report"]').click();
  await expect(page.locator('#page-report')).toBeVisible();

  // 稳定页面并截图对比
  await stabilize(page);
  await expect(page).toHaveScreenshot('report-tab.png', {
    mask: [
      page.locator('#calorieChart'),
      page.locator('#macroChart'),
      page.locator('#weightChart')
    ]
  });
});

test('个人中心标签页 (Profile Tab) 视觉验证', async ({ page }) => {
  await setupApp(page);

  // 切换到“个人”标签页
  await page.locator('[data-tab="profile"]').click();
  await expect(page.locator('#page-profile')).toBeVisible();

  // 稳定页面并截图对比
  await stabilize(page);
  await expect(page).toHaveScreenshot('profile-tab.png');
});


