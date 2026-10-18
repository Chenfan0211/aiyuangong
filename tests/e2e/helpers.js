import { createHmac } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect } from '@playwright/test';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..', '..');
const fixturesPath = path.join(root, 'data', 'e2e', 'fixtures.json');

export function loadFixtures() {
  return JSON.parse(fs.readFileSync(fixturesPath, 'utf-8'));
}

function decodeBase32(value) {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  let bits = '';
  for (const char of String(value).replace(/=+$/g, '').toUpperCase()) {
    const index = alphabet.indexOf(char);
    if (index < 0) throw new Error(`Invalid base32 character: ${char}`);
    bits += index.toString(2).padStart(5, '0');
  }
  const bytes = [];
  for (let offset = 0; offset + 8 <= bits.length; offset += 8) {
    bytes.push(Number.parseInt(bits.slice(offset, offset + 8), 2));
  }
  return Buffer.from(bytes);
}

export function totp(secret, now = Date.now()) {
  const counter = Math.floor(now / 30_000);
  const message = Buffer.alloc(8);
  message.writeBigUInt64BE(BigInt(counter));
  const digest = createHmac('sha1', decodeBase32(secret)).update(message).digest();
  const offset = digest[digest.length - 1] & 0x0f;
  const code = ((digest[offset] & 0x7f) << 24)
    | ((digest[offset + 1] & 0xff) << 16)
    | ((digest[offset + 2] & 0xff) << 8)
    | (digest[offset + 3] & 0xff);
  return String(code % 1_000_000).padStart(6, '0');
}

export async function login(page, user) {
  await page.goto('/agent.html');
  await page.getByLabel('门店账号').fill(user.username);
  await page.getByLabel('口令').fill(user.password);
  const meReady = page.waitForResponse(
    (response) => response.url().includes('/api/auth/me') && response.status() === 200,
    { timeout: 15_000 }
  );
  await page.getByRole('button', { name: '登录', exact: true }).click();
  if (user.totp_secret) {
    await expect(page.locator('#mfaBox')).toBeVisible();
    await page.getByLabel('动态验证码或恢复码').fill(totp(user.totp_secret));
    await page.getByRole('button', { name: '验证', exact: true }).click();
  }
  await meReady;
  await expect(page.locator('#userCard')).toBeVisible();
  await expect(page.locator('#currentUser')).not.toHaveText('');
}

export async function expectNoBearerToken(page) {
  const suspicious = await page.evaluate(() => {
    const entries = [];
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index);
      const value = key ? localStorage.getItem(key) : '';
      if (/token|bearer|authorization/i.test(String(key) + ' ' + String(value))) entries.push([key, value]);
    }
    return entries;
  });
  expect(suspicious).toEqual([]);
}

export async function logout(page) {
  await page.getByRole('button', { name: '退出登录' }).click();
  await expect(page.locator('#loginBox')).toBeVisible();
}

export function onePixelPng() {
  return Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=',
    'base64'
  );
}
