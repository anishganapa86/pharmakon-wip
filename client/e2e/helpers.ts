import fs from 'node:fs'
import path from 'node:path'
import { expect, test, type Locator, type Page } from '@playwright/test'

export const API_URL = process.env.DEMO_API_URL ?? 'http://localhost:8000'

const runDir =
  process.env.DEMO_RUN_DIR ??
  path.resolve(import.meta.dirname, '../../reports/runs/local')

export async function shot(page: Page, name: string): Promise<void> {
  const dir = path.join(runDir, 'screenshots')
  fs.mkdirSync(dir, { recursive: true })
  await page.screenshot({ path: path.join(dir, `${name}.png`), fullPage: true })
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

export function exactBrand(brand: string): RegExp {
  return new RegExp(`^${escapeRegExp(brand)}(Modeled|Logged only)`, 'i')
}

export function logPanel(page: Page): Locator {
  return page
    .locator('section.panel')
    .filter({ has: page.getByRole('heading', { name: 'Log a dose' }) })
}

export function alertsPanel(page: Page): Locator {
  return page
    .locator('section.panel')
    .filter({ has: page.getByRole('heading', { name: 'Alerts', exact: true }) })
}

export function symptomPanel(page: Page): Locator {
  return page
    .locator('section.panel')
    .filter({ has: page.getByRole('heading', { name: 'How are you feeling?' }) })
}

export function medCard(page: Page, name: string): Locator {
  return page.locator('.med-card').filter({
    has: page.locator('.med-name', {
      hasText: new RegExp(`^${escapeRegExp(name)}$`, 'i'),
    }),
  })
}

export async function openTab(page: Page, name: string): Promise<void> {
  await page.getByRole('tab', { name }).click()
  await expect(page.getByRole('tab', { name })).toHaveAttribute(
    'aria-selected',
    'true',
  )
}

export async function pickProduct(
  scope: Locator,
  page: Page,
  query: string,
  brand: string,
): Promise<void> {
  await scope.getByRole('combobox', { name: 'Product' }).fill(query)
  const option = page
    .getByRole('option')
    .filter({
      has: page.locator('.search-brand').filter({ hasText: exactBrand(brand) }),
    })
    .first()
  await option.click()
}

export async function logDose(
  page: Page,
  query: string,
  brand: string,
  quantity: number,
): Promise<void> {
  await pickProduct(logPanel(page), page, query, brand)
  await page.getByLabel('Quantity').fill(String(quantity))
  await logPanel(page).getByRole('button', { name: 'Log', exact: true }).click()
  await expect(page.getByText(`Logged ${quantity} × ${brand}.`)).toBeVisible()
  await page.waitForLoadState('networkidle')
}

export async function localTimeIn(page: Page, minutes: number): Promise<string> {
  return page.evaluate((offset) => {
    const at = new Date(Date.now() + offset * 60_000)
    const pad = (n: number) => String(n).padStart(2, '0')
    return `${pad(at.getHours())}:${pad(at.getMinutes())}`
  }, minutes)
}

export async function scheduleProduct(
  page: Page,
  query: string,
  brand: string,
  time?: string,
): Promise<void> {
  await page.getByRole('button', { name: 'Add medication' }).click()
  const form = page.locator('form.med-form')
  await pickProduct(form, page, query, brand)
  const timeInput = form.locator('input[type="time"]')
  await expect(timeInput).toHaveValue('08:00')
  if (time) await timeInput.fill(time)
  await form.getByRole('button', { name: 'Save medication' }).click()
  await expect(form).toHaveCount(0)
  await expect(medCard(page, brand)).toBeVisible()
  await page.waitForLoadState('networkidle')
}

export async function hasHorizontalOverflow(page: Page): Promise<boolean> {
  return page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth,
  )
}

export async function knownBug(
  note: string,
  signature: RegExp,
  run: () => Promise<void>,
): Promise<void> {
  test.fail(true, note)
  test.info().annotations.push({ type: 'bug', description: note })
  try {
    await run()
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    if (signature.test(message)) throw error
  }
}
