import { test, expect } from '@playwright/test';
import { loadFixtures, login } from './helpers.js';

async function layout(page) {
  return page.evaluate(() => {
    const app = document.querySelector('.app');
    const aside = document.querySelector('aside');
    const main = document.querySelector('main');
    const cards = document.querySelector('.cards');
    const links = document.querySelector('#appLinks');
    const appStyle = getComputedStyle(app);
    return {
      innerWidth: window.innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
      appWidth: app.getBoundingClientRect().width,
      asideWidth: aside.getBoundingClientRect().width,
      asideBottom: aside.getBoundingClientRect().bottom,
      mainTop: main.getBoundingClientRect().top,
      appGridTracks: appStyle.gridTemplateColumns.split(' ').filter(Boolean).length,
      overflowY: appStyle.overflowY,
      cardsTracks: getComputedStyle(cards).gridTemplateColumns.split(' ').filter(Boolean).length,
      linksDisplay: getComputedStyle(links).display,
    };
  });
}

async function desktopGeometry(page) {
  return page.evaluate(() => {
    const rect = selector => {
      const box = document.querySelector(selector).getBoundingClientRect();
      return { top: box.top, bottom: box.bottom, left: box.left, right: box.right, width: box.width, height: box.height };
    };
    const chat = document.querySelector('#chat');
    return {
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
      scrollWidth: document.documentElement.scrollWidth,
      scrollHeight: document.documentElement.scrollHeight,
      app: rect('.app'),
      header: rect('#mainHeader'),
      ops: rect('#opsSection'),
      chat: { ...rect('#chat'), clientHeight: chat.clientHeight, scrollHeight: chat.scrollHeight, overflowY: getComputedStyle(chat).overflowY },
      composer: rect('#composer'),
      inputValue: document.querySelector('#input').value,
      sendDisabled: document.querySelector('#send').disabled,
    };
  });
}

test.describe('responsive layout', () => {
  test('desktop workspace fills the viewport and keeps the composer visible', async ({ page }) => {
    const fixtures = loadFixtures();
    await page.setViewportSize({ width: 1070, height: 792 });
    await login(page, fixtures.users.employee);
    const box = await desktopGeometry(page);
    expect(box.app.left).toBeCloseTo(0, 1);
    expect(box.app.top).toBeCloseTo(0, 1);
    expect(box.app.width).toBeCloseTo(box.innerWidth, 1);
    expect(box.app.height).toBeCloseTo(box.innerHeight, 1);
    expect(box.scrollWidth).toBeLessThanOrEqual(box.innerWidth + 1);
    expect(box.scrollHeight).toBeLessThanOrEqual(box.innerHeight + 1);
    expect(box.header.top).toBeGreaterThanOrEqual(-1);
    expect(box.ops.bottom).toBeLessThanOrEqual(box.innerHeight + 1);
    expect(box.composer.bottom).toBeLessThanOrEqual(box.innerHeight + 1);
  });

  test('long chat history scrolls inside the desktop workspace and can still send', async ({ page }) => {
    const fixtures = loadFixtures();
    await page.setViewportSize({ width: 1070, height: 792 });
    await login(page, fixtures.users.employee);

    await page.locator('#input').fill('分析最近7天美团和抖音数据');
    await page.getByRole('button', { name: '发送', exact: true }).click();
    await expect(page.locator('.message')).toHaveCount(2);

    await page.locator('#input').fill('我今天有什么任务');
    await page.locator('#input').press('Enter');
    await expect(page.locator('.message')).toHaveCount(4);

    const box = await desktopGeometry(page);
    expect(box.chat.overflowY).toBe('auto');
    expect(box.chat.scrollHeight).toBeGreaterThan(box.chat.clientHeight + 20);
    expect(box.chat.bottom).toBeLessThanOrEqual(box.composer.top + 1);
    expect(box.composer.bottom).toBeLessThanOrEqual(box.innerHeight + 1);
    expect(box.inputValue).toBe('');
    expect(box.sendDisabled).toBe(false);

    await page.locator('#chat').evaluate(element => { element.scrollTop = 0; });
    await expect(page.locator('.message').first()).toBeInViewport();
  });

  test('split-pane width stacks the sidebar above the workspace', async ({ page }) => {
    const fixtures = loadFixtures();
    await page.setViewportSize({ width: 820, height: 900 });
    await login(page, fixtures.users.employee);
    const box = await layout(page);
    expect(box.scrollWidth).toBeLessThanOrEqual(box.innerWidth + 1);
    expect(box.appGridTracks).toBe(1);
    expect(box.asideWidth).toBeGreaterThan(box.appWidth - 3);
    expect(box.mainTop).toBeGreaterThanOrEqual(box.asideBottom - 1);
    expect(box.linksDisplay).not.toBe('none');
  });

  test('phone width has one card column and no hidden vertical overflow', async ({ page }) => {
    const fixtures = loadFixtures();
    await page.setViewportSize({ width: 390, height: 844 });
    await login(page, fixtures.users.employee);
    const box = await layout(page);
    expect(box.scrollWidth).toBeLessThanOrEqual(box.innerWidth + 1);
    expect(box.appGridTracks).toBe(1);
    expect(box.cardsTracks).toBe(1);
    expect(box.overflowY).not.toBe('hidden');
    expect(box.mainTop).toBeGreaterThanOrEqual(box.asideBottom - 1);
  });
});