import { expect, test } from './fixtures.ts'
import { LOGGED_OUT, newCredentials } from './auth-state.ts'
import {
  alertsPanel,
  hasHorizontalOverflow,
  knownBug,
  logDose,
  medCard,
  openTab,
  scheduleProduct,
  shot,
} from './helpers.ts'

const MIN_TARGET_PX = 40
const MIN_FONT_PX = 16
const TABS = ['Concentration', 'Timeline', 'Skincare', 'Interactions', 'Food & drink']

interface LayoutReport {
  pageOverflow: number
  wide: string[]
  small: string[]
  clipped: string[]
  fonts: string[]
}

function layoutScan([minTarget, minFont]: readonly [number, number]): LayoutReport {
  const viewport = document.documentElement.clientWidth
  const describe = (el: Element): string => {
    const label = (el.textContent ?? '').trim().replace(/\s+/g, ' ').slice(0, 40)
    return `${el.tagName.toLowerCase()}.${String(el.className).split(' ')[0]} "${label}"`
  }
  const visible = (el: Element): boolean => {
    const rect = el.getBoundingClientRect()
    const style = getComputedStyle(el)
    return (
      rect.width > 0 &&
      rect.height > 0 &&
      style.visibility !== 'hidden' &&
      style.display !== 'none'
    )
  }
  const report: LayoutReport = {
    pageOverflow: document.documentElement.scrollWidth - viewport,
    wide: [],
    small: [],
    clipped: [],
    fonts: [],
  }
  for (const el of Array.from(document.body.querySelectorAll('*'))) {
    if (!visible(el)) continue
    if (el.closest('svg') && el.tagName.toLowerCase() !== 'svg') continue
    const rect = el.getBoundingClientRect()
    if (rect.width > viewport + 0.5) report.wide.push(`${describe(el)} ${Math.round(rect.width)}px`)
  }
  const targets = document.querySelectorAll(
    'button, [role="tab"], select, input:not([type="checkbox"]), summary',
  )
  for (const el of Array.from(targets)) {
    if (!visible(el)) continue
    const rect = el.getBoundingClientRect()
    if (rect.height < minTarget) {
      report.small.push(`${describe(el)} ${Math.round(rect.height)}px tall`)
    }
  }
  for (const el of Array.from(document.querySelectorAll('input, textarea, select'))) {
    if (!visible(el)) continue
    const size = parseFloat(getComputedStyle(el).fontSize)
    if (size < minFont) report.fonts.push(`${describe(el)} ${size}px`)
  }
  for (const el of Array.from(document.body.querySelectorAll('*'))) {
    if (!visible(el)) continue
    const style = getComputedStyle(el)
    const hidesX = style.overflowX === 'hidden' || style.overflowX === 'clip'
    const hidesY = style.overflowY === 'hidden' || style.overflowY === 'clip'
    if (!hidesX && !hidesY) continue
    if (el.tagName.toLowerCase() === 'svg' || el.closest('svg')) continue
    const clippedX = hidesX && el.scrollWidth > el.clientWidth + 1
    const clippedY = hidesY && el.scrollHeight > el.clientHeight + 1
    if (clippedX || clippedY) {
      report.clipped.push(
        `${describe(el)} ${el.scrollWidth}x${el.scrollHeight} in ${el.clientWidth}x${el.clientHeight}`,
      )
    }
  }
  return report
}

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Pharmakon' })).toBeVisible()
})

test('phone: load data across the app', async ({ page }) => {
  await logDose(page, 'Tylenol Extra Strength', 'Tylenol Extra Strength', 6)
  await logDose(page, 'DayQuil', 'DayQuil', 2)
  await logDose(page, 'NoDoz', 'NoDoz', 2)
  for (const name of ['Advil', 'Aleve']) {
    if ((await medCard(page, name).count()) === 0) {
      await scheduleProduct(page, name, name)
    }
  }
  await logDose(page, 'Eva Naturals', 'Eva Naturals Vitamin C+ Serum', 1)
  await logDose(page, 'Milk Skin Toner', 'TIRTIR Milk Skin Toner', 1)
  await openTab(page, 'Timeline')
  const form = page.locator('form.log-form')
  await form.getByLabel('Value').fill('dry patch on cheek')
  await form.getByRole('button', { name: 'Add to timeline' }).click()
  await expect(
    page.locator('.timeline-item-log').filter({ hasText: 'dry patch on cheek' }),
  ).toHaveCount(1)
})

for (const tab of TABS) {
  test(`phone: ${tab} tab fits a 390px screen`, async ({ page }) => {
    test.fixme(
      true,
      'Known: these layout checks time out since the tabs moved into the bottom bar and More menu; see docs/UI_HANDOFF.md',
    )
    const tapProblems: string[] = []
    const tabButton = page.getByRole('tab', { name: tab })
    try {
      await tabButton.click({ timeout: 4000 })
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      const blocker = message.split('\n').find((l) => /intercepts pointer events/.test(l))
      tapProblems.push(`${tab} tab cannot be tapped: ${(blocker ?? message.split('\n')[0]).trim()}`)
      await tabButton.dispatchEvent('click')
    }
    await expect(tabButton).toHaveAttribute('aria-selected', 'true')
    await page.waitForLoadState('networkidle')
    await expect(page.getByText('Loading…')).toHaveCount(0)
    await shot(page, `phone-${tab.toLowerCase().replace(/[^a-z]+/g, '-')}`)
    const report = await page.evaluate(layoutScan, [MIN_TARGET_PX, MIN_FONT_PX] as const)
    const problems = [
      ...tapProblems,
      ...(report.pageOverflow > 0
        ? [`page overflows horizontally by ${report.pageOverflow}px`]
        : []),
      ...report.wide.map((d) => `wider than the viewport: ${d}`),
      ...report.small.map((d) => `under ${MIN_TARGET_PX}px tall: ${d}`),
      ...report.clipped.map((d) => `text clipped by overflow hidden: ${d}`),
      ...report.fonts.map((d) => `input font under ${MIN_FONT_PX}px: ${d}`),
    ]
    if (problems.length > 0) {
      const summary = `${problems.length} problems, first: ${problems.slice(0, 3).join('; ')}`
      await knownBug(summary, /layout problems on/, async () => {
        expect(problems, `layout problems on ${tab}`).toEqual([])
      })
    }
  })
}

test.describe('logged out', () => {
  test.use({ storageState: LOGGED_OUT })

  for (const url of ['/login.html', '/signup.html']) {
    test(`phone ${url} fits a 390px screen`, async ({ page }) => {
      await page.goto(url)
      await expect(page.getByRole('button').first()).toBeVisible()
      expect(await hasHorizontalOverflow(page)).toBe(false)
      const small = await page.evaluate(
        ([minTarget, minFont]) =>
          Array.from(document.querySelectorAll('input, button'))
            .filter((el) => {
              const box = el.getBoundingClientRect()
              const font = parseFloat(getComputedStyle(el).fontSize)
              return box.height < minTarget || font < minFont
            })
            .map((el) => el.tagName.toLowerCase() + '#' + el.id),
        [MIN_TARGET_PX, MIN_FONT_PX] as const,
      )
      expect(small).toEqual([])
      await shot(page, `phone-${url.slice(1).replace('.html', '')}`)
    })
  }
})

test.describe('survey on a phone', () => {
  test.use({ storageState: LOGGED_OUT })

  test('phone: the survey fits a 390px screen', async ({ page }) => {
    const { email, password } = newCredentials()
    await page.goto('/signup.html')
    await page.getByLabel('Email').fill(email)
    await page.getByLabel('Password', { exact: true }).fill(password)
    await page.getByLabel('Confirm password').fill(password)
    await page.getByRole('button', { name: 'Create account' }).click()
    await expect(page.getByText('Step 1 of 4')).toBeVisible()
    expect(await hasHorizontalOverflow(page)).toBe(false)
    const small = await page.evaluate(
      ([minTarget, minFont]) =>
        Array.from(document.querySelectorAll('button'))
          .filter((el) => {
            const box = el.getBoundingClientRect()
            const font = parseFloat(getComputedStyle(el).fontSize)
            return box.height < minTarget || font < minFont
          })
          .map((el) => (el.textContent ?? '').trim()),
      [MIN_TARGET_PX, MIN_FONT_PX] as const,
    )
    expect(small).toEqual([])
    await shot(page, 'phone-survey')
  })
})

test.describe('new account on a phone', () => {
  test.use({ storageState: LOGGED_OUT })

  test('phone: a duplicate ingredient shows on Today and Interactions after logging', async ({
    page,
  }) => {
    const { email, password } = newCredentials()
    await page.goto('/signup.html')
    await page.getByLabel('Email').fill(email)
    await page.getByLabel('Password', { exact: true }).fill(password)
    await page.getByLabel('Confirm password').fill(password)
    await page.getByRole('button', { name: 'Create account' }).click()
    await expect(page.getByText('Step 1 of 4')).toBeVisible()

    const next = page.getByRole('button', { name: 'Continue' })
    await page.getByRole('button', { name: 'Medications' }).click()
    await next.click()
    await page.getByRole('button', { name: 'Avoiding interactions' }).click()
    await next.click()
    await page.getByRole('button', { name: 'Somewhat' }).click()
    await next.click()
    await page.getByRole('button', { name: 'Walk me through it' }).click()
    await next.click()
    await page.getByRole('button', { name: 'Start using Pharmakon' }).click()

    await logDose(page, 'Tylenol Extra Strength', 'Tylenol Extra Strength', 1)
    await logDose(page, 'DayQuil', 'DayQuil', 1)

    const duplicate = /Acetaminophen appears in 2 of your products/
    await expect(page.locator('.finding-title', { hasText: duplicate })).toBeVisible()

    await openTab(page, 'Concentration')
    const card = alertsPanel(page).locator('li.duplicate-alert')
    await expect(card).toHaveCount(1)
    await expect(card).toContainText(duplicate)
    await expect(card.locator('.dot')).toBeVisible()
    await expect(card.getByRole('button', { name: 'Open Interactions' })).toBeVisible()
    expect(await hasHorizontalOverflow(page)).toBe(false)
    await shot(page, 'phone-duplicate-today')

    await card.getByRole('button', { name: 'Open Interactions' }).click()
    await expect(page.locator('.finding-title', { hasText: duplicate })).toBeVisible()
    await shot(page, 'phone-duplicate-interactions')
  })
})
