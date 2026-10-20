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

test.describe('responsive layout', () => {
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