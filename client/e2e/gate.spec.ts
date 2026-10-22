import { expect, test } from './fixtures.ts'
import {
  alertsPanel,
  logDose,
  openTab,
  scheduleProduct,
  shot,
} from './helpers.ts'

const NOTICE = /\d+ rules? (is|are) awaiting verification and (is|are) not being applied/

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Pharmakon' })).toBeVisible()
})

test('@gate setup: the acetaminophen alert remains while curated rules are off', async ({
  page,
}) => {
  await logDose(page, 'Tylenol Extra Strength', 'Tylenol Extra Strength', 6)
  await logDose(page, 'DayQuil', 'DayQuil', 2)
  await scheduleProduct(page, 'Advil', 'Advil')
  await scheduleProduct(page, 'Aleve', 'Aleve')
  await logDose(page, 'Eva Naturals', 'Eva Naturals Vitamin C+ Serum', 1)
  await logDose(page, 'Milk Skin Toner', 'TIRTIR Milk Skin Toner', 1)

  const alert = alertsPanel(page).locator('li.alert')
  await expect(alert).toHaveCount(1)
  await expect(alert.locator('.alert-substance')).toHaveText('Acetaminophen')
  await expect(alert.locator('.badge')).toHaveText('caution')
  await shot(page, 'gate-1-alert')
})

test('@gate Interactions shows the awaiting-verification notice and no curated NSAID card', async ({
  page,
}) => {
  await openTab(page, 'Interactions')
  await expect(page.getByText(NOTICE).first()).toBeVisible()
  const regimen = page
    .locator('section.panel')
    .filter({ has: page.getByRole('heading', { name: 'Your regimen' }) })
  await expect(regimen).not.toContainText('Loading…')
  const cards = regimen.locator('.skincare-flag')
  await expect(cards.filter({ hasText: /curated reference/i })).toHaveCount(0)
  await expect(cards.filter({ hasText: /FDA label/i }).first()).toBeVisible()
  await shot(page, 'gate-2-interactions')
})

test('@gate Skincare shows the awaiting-verification notice and no findings', async ({
  page,
}) => {
  await openTab(page, 'Skincare')
  await expect(page.getByText(NOTICE).first()).toBeVisible()
  const auto = page.locator('.skincare-auto-results')
  await expect(auto).toBeVisible()
  await expect(auto.locator('.skincare-flag')).toHaveCount(0)
  await shot(page, 'gate-3-skincare')
})

test('@gate Food & drink shows the notice, keeps live FDA quotes and drops curated entries', async ({
  page,
}) => {
  await openTab(page, 'Food & drink')
  await expect(page.getByText(NOTICE).first()).toBeVisible()
  const cards = page.locator('.skincare-flag')
  await expect(cards.first()).toBeVisible()
  const quoted = cards.filter({ hasText: 'FDA label' })
  expect(await quoted.count()).toBeGreaterThan(0)
  await expect(cards.filter({ hasText: 'Curated reference' })).toHaveCount(0)
  await shot(page, 'gate-4-food')
})
