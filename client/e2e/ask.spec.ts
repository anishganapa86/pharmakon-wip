import { expect, test } from './fixtures.ts'
import { shot } from './helpers.ts'
import type { Page } from '@playwright/test'

const NOTICE =
  "Demo assistant using Google Gemini. Don't enter real medical or personal information."

async function openSheet(page: Page) {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Pharmakon' })).toBeVisible()
  await page.getByRole('button', { name: 'Ask' }).click()
  await expect(page.getByRole('dialog', { name: 'Ask Pharmakon' })).toBeVisible()
}

async function ask(page: Page, text: string) {
  await page.getByLabel('Your question').fill(text)
  await page.getByRole('button', { name: 'Send' }).click()
}

test('the sheet shows the notice and suggestion chips, and Esc closes it', async ({
  page,
}) => {
  await openSheet(page)
  await expect(page.getByText(NOTICE)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Where do I log intake?' })).toBeVisible()
  await shot(page, 'ask-1-sheet')
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Ask' })).toBeVisible()
})

test('a reply with a suggested tab offers an Open button that switches tabs', async ({
  page,
}) => {
  await page.route('**/users/*/chat', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        reply: 'Interactions are checked on the Interactions tab.',
        suggested_tab: 'interactions',
      }),
    }),
  )
  await openSheet(page)
  await ask(page, 'where are interactions checked?')
  await expect(page.getByText('Interactions are checked on the Interactions tab.')).toBeVisible()
  await page.getByRole('button', { name: 'Open Interactions' }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByRole('tab', { name: 'Interactions' })).toHaveAttribute(
    'aria-selected',
    'true',
  )
})

test('the sent body carries only the chat messages', async ({ page }) => {
  let body = ''
  await page.route('**/users/*/chat', async (route) => {
    body = route.request().postData() ?? ''
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ reply: 'Hello.', suggested_tab: null }),
    })
  })
  await openSheet(page)
  await ask(page, 'hi there')
  await expect(page.getByText('Hello.')).toBeVisible()
  expect(JSON.parse(body)).toEqual({
    messages: [{ role: 'user', content: 'hi there' }],
  })
})

for (const [status, message, shown] of [
  [503, 'The demo assistant isn\'t set up on this server.', "The assistant isn't available on this server yet."],
  [429, 'The demo assistant is busy, try again later.', 'The demo assistant is busy, try again later.'],
  [502, 'The demo assistant couldn\'t answer. Try again.', "The assistant couldn't answer. Try again."],
] as const) {
  test(`a ${status} shows a friendly message and a retry that works`, async ({
    page,
  }) => {
    let calls = 0
    await page.route('**/users/*/chat', (route) => {
      calls += 1
      if (calls === 1) {
        return route.fulfill({
          status,
          contentType: 'application/json',
          body: JSON.stringify({ error: { code: 'x', message } }),
        })
      }
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ reply: 'Back again.', suggested_tab: null }),
      })
    })
    await openSheet(page)
    await ask(page, 'hello')
    await expect(page.getByRole('alert')).toContainText(shown)
    await page.getByRole('button', { name: 'Try again' }).click()
    await expect(page.getByText('Back again.')).toBeVisible()
    await expect(page.getByRole('alert')).toHaveCount(0)
    expect(calls).toBe(2)
  })
}

test('a network failure shows the connection message and can retry', async ({
  page,
}) => {
  let calls = 0
  await page.route('**/users/*/chat', (route) => {
    calls += 1
    if (calls === 1) return route.abort('failed')
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ reply: 'Connected.', suggested_tab: null }),
    })
  })
  await openSheet(page)
  await ask(page, 'hello')
  await expect(page.getByRole('alert')).toContainText("Couldn't reach the Pharmakon server")
  await page.getByRole('button', { name: 'Try again' }).click()
  await expect(page.getByText('Connected.')).toBeVisible()
})

test('chips follow the current tab', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('tab', { name: 'Interactions' }).click()
  await page.getByRole('button', { name: 'Ask' }).click()
  await expect(page.getByRole('button', { name: 'What does this finding mean?' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'How do I add a food?' })).toBeVisible()
})

test('a suggested action renders a second button that opens that flow', async ({
  page,
}) => {
  await page.route('**/users/*/chat', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        reply: '1. Open Scan.\n2. Take a photo of the label.',
        suggested_tab: null,
        suggested_action: 'open_scan',
      }),
    }),
  )
  await openSheet(page)
  await ask(page, 'how do I scan a label?')
  await expect(page.getByText('Take a photo of the label.')).toBeVisible()
  await page.getByRole('button', { name: 'Open Scan' }).click()
  await expect(page.getByRole('heading', { name: 'Scan a product' })).toBeVisible()
})

test('open_survey opens the preferences editor', async ({ page }) => {
  await page.route('**/users/*/chat', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        reply: 'You can change your answers in Settings.',
        suggested_tab: null,
        suggested_action: 'open_survey',
      }),
    }),
  )
  await openSheet(page)
  await ask(page, 'change my preferences')
  await page.getByRole('button', { name: 'Open my preferences' }).click()
  await expect(page.getByText('Step 1 of 4')).toBeVisible()
})
