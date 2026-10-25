import { expect, test } from './fixtures.ts'
import { API_URL, LOGGED_OUT, newCredentials } from './auth-state.ts'
import { openTab, shot } from './helpers.ts'
import type { BrowserContext, Page } from '@playwright/test'

test.use({ storageState: LOGGED_OUT, viewport: { width: 390, height: 844 } })

const CLOSING = 'A pharmacist can tell you what this means for you.'

let userId = ''
let savedCookies: Parameters<BrowserContext['addCookies']>[0] = []

test.describe.configure({ mode: 'serial' })

test.beforeAll(async ({ playwright }) => {
  const api = await playwright.request.newContext({ baseURL: API_URL })
  const created = await api.post('/auth/signup', { data: newCredentials() })
  expect(created.status()).toBe(201)
  userId = ((await created.json()) as { id: string }).id
  const skipped = await api.put(`/users/${userId}/survey`, {
    data: { tracks: [], goals: [], experience: null, guidance: null, status: 'skipped' },
  })
  expect(skipped.status()).toBe(200)
  savedCookies = (await api.storageState()).cookies
  await api.dispose()
})

test.beforeEach(async ({ context }) => {
  await context.addCookies(savedCookies)
})

async function logItem(page: Page, id: string, productId: string, hoursAgo: number) {
  const response = await page.request.post(`${API_URL}/users/${id}/doses`, {
    data: {
      product_id: productId,
      quantity: 1,
      taken_at: new Date(Date.now() - hoursAgo * 3600_000).toISOString(),
    },
  })
  expect(response.status()).toBe(201)
}

async function openInteractions(page: Page) {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Pharmakon' })).toBeVisible()
  await openTab(page, 'Interactions')
  await expect(page.getByText('Loading…')).toHaveCount(0, { timeout: 60_000 })
}

test('the demo scenario: duplicate acetaminophen, then coffee, beer and chicken', async ({
  page,
}) => {
  const id = userId
  await logItem(page, id, 'prod_tylenol_es_500', 8)
  await logItem(page, id, 'prod_dayquil_liquicap', 6)

  await openInteractions(page)
  const duplicate = page.locator('.skincare-flag').filter({ hasText: /acetaminophen/i })
  await expect(duplicate.first()).toBeVisible()
  await expect(page.locator('.tag-food')).toHaveCount(0)
  await shot(page, 'food-1-duplicate')

  await logItem(page, id, 'prod_food_coffee', 4)
  await page.reload()
  await openTab(page, 'Interactions')
  await expect(page.getByText('Loading…')).toHaveCount(0, { timeout: 60_000 })
  await expect(page.locator('.tag-food')).toHaveCount(0)

  await logItem(page, id, 'prod_example_beer', 2)
  await page.reload()
  await openTab(page, 'Interactions')
  await expect(page.getByText('Loading…')).toHaveCount(0, { timeout: 60_000 })
  const foodCards = page.locator('.skincare-flag').filter({ has: page.locator('.tag-food') })
  await expect(foodCards).toHaveCount(1)
  await expect(foodCards.first()).toContainText(
    '3 or more alcoholic drinks every day while using this product',
  )
  await expect(foodCards.first()).toContainText('Quoted from FDA label')
  await shot(page, 'food-2-beer')

  await logItem(page, id, 'prod_food_chicken', 1)
  await page.reload()
  await openTab(page, 'Interactions')
  await expect(page.getByText('Loading…')).toHaveCount(0, { timeout: 60_000 })
  await expect(page.locator('.tag-food')).toHaveCount(1)
})

test('Why this matters is collapsed on a phone, opens with a button and keeps its sources', async ({
  page,
}) => {
  const id = userId
  await logItem(page, id, 'prod_tylenol_es_500', 8)
  await logItem(page, id, 'prod_dayquil_liquicap', 6)
  await openInteractions(page)

  const toggle = page.getByRole('button', { name: 'Why this matters' }).first()
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  await expect(page.getByText(CLOSING)).toHaveCount(0)
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByText(CLOSING).first()).toBeVisible()
  await expect(page.getByText('What this means').first()).toBeVisible()
  await expect(page.getByText('What could happen').first()).toBeVisible()
  await expect(page.getByText('Why it matters').first()).toBeVisible()
  await expect(
    page.getByRole('link', { name: 'Call Poison Control' }).first(),
  ).toHaveAttribute('href', 'tel:18002221222')
  await expect(page.getByRole('link', { name: 'Open on DailyMed' }).first()).toHaveAttribute(
    'href',
    /dailymed\.nlm\.nih\.gov/,
  )
  await expect(page.getByRole('link', { name: 'Open source' }).first()).toHaveAttribute(
    'href',
    'https://www.govinfo.gov/content/pkg/FR-2011-01-14/html/2011-709.htm',
  )
  await shot(page, 'food-3-why-open')
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
})

test('the Why this matters toggle starts open on a desktop screen', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 })
  const id = userId
  await logItem(page, id, 'prod_tylenol_es_500', 8)
  await logItem(page, id, 'prod_dayquil_liquicap', 6)
  await openInteractions(page)
  await expect(
    page.getByRole('button', { name: 'Why this matters' }).first(),
  ).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByText(CLOSING).first()).toBeVisible()
})

test('the log intake search returns foods typed as Food & drink beside medications', async ({
  page,
}) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Log intake' })).toBeVisible()
  await page.getByRole('combobox', { name: 'Product' }).fill('coffee')
  const coffee = page.getByRole('option').filter({ hasText: 'Coffee' }).first()
  await expect(coffee).toContainText('Food & drink')
  await expect(coffee).toContainText('Source: Pharmakon food list')
  await expect(coffee).toContainText('Typical caffeine; varies by product')
  await expect(coffee).not.toContainText('Concentration estimate')
  await page.getByRole('combobox', { name: 'Product' }).fill('nodoz')
  const nodoz = page.getByRole('option').filter({ hasText: 'NoDoz' }).first()
  await expect(nodoz).toContainText('Medication')
  await expect(nodoz).toContainText('Concentration estimate')
})
