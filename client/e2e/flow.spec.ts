import { expect, test, type Page } from '@playwright/test'
import {
  alertsPanel,
  API_URL,
  hasHorizontalOverflow,
  knownBug,
  localTimeIn,
  logDose,
  logPanel,
  medCard,
  openTab,
  pickProduct,
  scheduleProduct,
  shot,
  symptomPanel,
} from './helpers.ts'

const SKINCARE_WORDS = /retinol|niacinamide|glycerin|salicylic|hexanediol/i

const CURATED = [
  { query: 'Milk Skin Toner', brand: 'TIRTIR Milk Skin Toner' },
  { query: 'SOS Serum', brand: 'TIRTIR SOS Serum' },
  { query: 'Ceramic Cream', brand: 'TIRTIR Ceramic Cream' },
  {
    query: 'Toleriane Purifying',
    brand: 'La Roche-Posay Toleriane Purifying Foaming Facial Cleanser',
  },
  { query: 'Eva Naturals', brand: 'Eva Naturals Vitamin C+ Serum' },
]

const MY_PRODUCT_TEXT =
  'Water, Glycerin (Kosher, Vegetable), 1,2-Hexanediol, Retinol'

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Pharmakon' })).toBeVisible()
})

async function cardsOf(page: Page, selector: string) {
  return page.locator(selector).evaluateAll((nodes) =>
    nodes.map((node) => ({
      paras: Array.from(node.querySelectorAll('p')).map(
        (p) => p.textContent?.trim() ?? '',
      ),
      tags: Array.from(node.querySelectorAll('span.tag')).map(
        (t) => t.textContent?.trim() ?? '',
      ),
      links: Array.from(node.querySelectorAll('a')).map(
        (a) => a.getAttribute('href') ?? '',
      ),
      text: node.textContent?.trim() ?? '',
    })),
  )
}

test('a. NoDoz x1 shows a caffeine curve and no alert', async ({ page }) => {
  await shot(page, 'a-0-empty')
  await logDose(page, 'NoDoz', 'NoDoz', 1)
  await expect(page.locator('.chart h3', { hasText: 'Caffeine' })).toBeVisible()
  await expect(page.locator('.chart-plot svg').first()).toBeVisible()
  await expect(alertsPanel(page).getByText('No active alerts.')).toBeVisible()
  await expect(alertsPanel(page).locator('li.alert')).toHaveCount(0)
  await shot(page, 'a-1-nodoz-curve')
})

test('b. Tylenol x6 plus DayQuil x2 raises a caution acetaminophen alert, 4 more Tylenol makes it urgent', async ({
  page,
}) => {
  await logDose(page, 'Tylenol Extra Strength', 'Tylenol Extra Strength', 6)
  await logDose(page, 'DayQuil', 'DayQuil', 2)

  const alert = alertsPanel(page).locator('li.alert')
  await expect(alert).toHaveCount(1)
  await expect(alert.locator('.badge')).toHaveText('caution')
  await expect(alert.locator('.alert-substance')).toHaveText('Acetaminophen')
  const message = alert.locator(':scope > p').first()
  await expect(message).toContainText('Tylenol Extra Strength')
  await expect(message).toContainText('DayQuil')
  const counted = alert.locator('.dose-list').filter({ hasText: 'Counted doses' })
  await expect(counted.locator('li')).toHaveCount(2)
  await expect(counted.locator('li', { hasText: 'Tylenol Extra Strength' })).toHaveCount(1)
  await expect(counted.locator('li', { hasText: 'DayQuil' })).toHaveCount(1)
  await expect(alert.locator('.alert-why')).toContainText('Why this matters')
  await expect(alert.locator('.alert-action')).toContainText('What to do')
  await shot(page, 'b-1-caution')

  await logDose(page, 'Tylenol Extra Strength', 'Tylenol Extra Strength', 4)
  await expect(alert.locator('.badge')).toHaveText('urgent')
  await expect(alert.locator('.alert-action')).toContainText('What to do')
  await shot(page, 'b-2-urgent')
})

test('b. urgent alert "What to do" does not tell the user to stop taking the medication', async ({
  page,
}) => {
  await knownBug(
    'urgent WHAT_TO_DO in AlertList.tsx reads "Stop taking this and contact a pharmacist or prescriber now."',
    /Stop taking this/,
    async () => {
      const alert = alertsPanel(page).locator('li.alert')
      await expect(alert.locator('.badge')).toHaveText('urgent')
      await expect(alert.locator('.alert-action')).not.toContainText(/stop taking/i)
    },
  )
})

test('c. symptom headache finds a local-reference caffeine match, backache finds nothing', async ({
  page,
}) => {
  await logDose(page, 'NoDoz', 'NoDoz', 2)
  const panel = symptomPanel(page)
  await panel.getByRole('textbox', { name: 'Symptom' }).fill('headache')
  await panel.getByRole('button', { name: 'Check' }).click()
  const caffeine = panel
    .locator('li.alert')
    .filter({ has: page.locator('.alert-substance', { hasText: 'Caffeine' }) })
  await expect(caffeine).toHaveCount(1)
  await expect(caffeine.locator('.source-local_reference')).toContainText(
    /local reference/i,
  )
  await shot(page, 'c-1-headache')

  await panel.getByRole('textbox', { name: 'Symptom' }).fill('backache')
  await panel.getByRole('button', { name: 'Check' }).click()
  await expect(panel.locator('li.alert')).toHaveCount(0)
  await expect(
    panel.getByText(
      'None of your current substances at elevated levels are associated with this symptom.',
    ),
  ).toBeVisible()
  await shot(page, 'c-2-backache')
})

test('d. scheduling Advil and Aleve and taking Advil changes its status and shows ibuprofen', async ({
  page,
}) => {
  const soon = await localTimeIn(page, 20)
  await scheduleProduct(page, 'Advil', 'Advil', soon)
  await scheduleProduct(page, 'Aleve', 'Aleve')
  const advil = medCard(page, 'Advil')
  const before = (await advil.locator('.med-status').textContent())?.trim()
  expect(before).toBe('Due now')
  await shot(page, 'd-1-scheduled')

  await advil.getByRole('button', { name: 'Take now' }).click()
  await expect(advil.locator('.med-status')).toHaveText('Taken')
  await expect(advil.locator('.med-levels')).toContainText(
    /Ibuprofen:\s*\d+\.\d+\s*mg\/L/,
  )
  await shot(page, 'd-2-advil-taken')
})

test('e. Interactions tab shows the NSAID card with pharmacist questions and a copy button', async ({
  page,
  context,
}) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  await openTab(page, 'Interactions')
  const regimen = page
    .locator('section.panel')
    .filter({ has: page.getByRole('heading', { name: 'Your regimen' }) })
  const cards = regimen.locator('.skincare-flag')
  await expect(cards.first()).toBeVisible()
  const nsaid = cards.filter({ hasText: /NSAID|ibuprofen/i }).filter({
    hasText: /naproxen/i,
  })
  await expect(nsaid.first()).toBeVisible()
  await expect(nsaid.first().locator('.pharmacist-questions li').first()).toBeVisible()
  await expect(regimen.getByRole('button', { name: 'Copy for my pharmacist' })).toBeVisible()
  await shot(page, 'e-1-interactions')

  const all = await cardsOf(page, '.skincare-flag')
  const misleading = all.filter((card) => {
    const mentions = /mentions\s+naproxen/i.test(card.text)
    const quote = card.paras.find((p) => p.startsWith('"')) ?? ''
    return mentions && !/naproxen/i.test(quote)
  })
  expect(misleading.map((c) => c.text.slice(0, 200))).toEqual([])

  await regimen.getByRole('button', { name: 'Copy for my pharmacist' }).click()
  await expect(regimen.getByRole('button', { name: 'Copied!' })).toBeVisible()
  const clipboard = await page.evaluate(() => navigator.clipboard.readText())
  expect(clipboard).toContain('Questions for my pharmacist')
  await shot(page, 'e-2-copied')
})

async function foodCards(page: Page) {
  await openTab(page, 'Food & drink')
  await expect(page.locator('.skincare-flag').first()).toBeVisible()
  const cards = await cardsOf(page, '.skincare-flag')
  expect(cards.length).toBeGreaterThan(0)
  return {
    cards,
    quoted: cards
      .filter((c) => c.paras[1]?.startsWith('"'))
      .map((c) => ({
        ...c,
        quote: (c.paras[1] ?? '').replace(/^"/, '').replace(/"$/, ''),
        tag: c.tags[0] ?? '',
      })),
    pointers: cards.filter((c) => !c.paras[1]?.startsWith('"')),
  }
}

test('f. Food & drink quotes start properly, cards name a product, pointers link to DailyMed', async ({
  page,
}) => {
  const { cards, quoted, pointers } = await foodCards(page)
  await shot(page, 'f-1-food')

  expect(
    quoted.filter((c) => !/^[A-Z0-9]/.test(c.quote)).map((c) => c.quote),
    'every quote starts with a capital letter or digit',
  ).toEqual([])

  const withoutProduct = cards.filter((c) => {
    const middle = c.paras.slice(2, -1)
    return !middle.some((p) => !/per the label$/.test(p))
  })
  expect(
    withoutProduct.map((c) => c.paras[0]),
    'every card shows a product name',
  ).toEqual([])

  expect(
    pointers
      .filter((c) => !c.links.some((href) => /dailymed/i.test(href)))
      .map((c) => c.paras[0]),
    'every pointer card links to DailyMed',
  ).toEqual([])
  expect(
    pointers.filter((c) => !/DailyMed/.test(c.text)).map((c) => c.paras[0]),
    'every pointer card names DailyMed',
  ).toEqual([])
})

test('f. every Food & drink quote ends with terminal punctuation', async ({ page }) => {
  await knownBug(
    'FDA label text is quoted verbatim without a sentence end, e.g. "...if stomach upset occurs"',
    /quote ends with terminal punctuation/,
    async () => {
      const { quoted } = await foodCards(page)
      expect(
        quoted.filter((c) => !/[.!?]$/.test(c.quote)).map((c) => c.quote),
        'every quote ends with terminal punctuation',
      ).toEqual([])
    },
  )
})

test('f. no "Avoid" tag appears on a sentence about water', async ({ page }) => {
  await knownBug(
    'a dosage sentence ("take 2 tablets every 8 hours with water") is tagged Avoid',
    /no Avoid tag on a water sentence/,
    async () => {
      const { quoted } = await foodCards(page)
      expect(
        quoted
          .filter((c) => c.tag === 'Avoid' && /\bwater\b/i.test(c.quote))
          .map((c) => c.quote),
        'no Avoid tag on a water sentence',
      ).toEqual([])
    },
  )
})

test('f. a conditional take-with-food sentence is tagged as conditional', async ({
  page,
}) => {
  await knownBug(
    'FoodGuidance.tsx has no conditional tag; "take with food or milk if stomach upset occurs" is tagged plain "Take with food"',
    /tagged as conditional/,
    async () => {
      const { quoted } = await foodCards(page)
      const conditional = quoted.filter(
        (c) => c.tag === 'Take with food' && /\b(if|unless)\b/i.test(c.quote),
      )
      expect(conditional.length).toBeGreaterThan(0)
      expect(
        conditional.filter((c) => !/conditional/i.test(c.text)).map((c) => c.quote),
        'every conditional sentence is tagged as conditional',
      ).toEqual([])
    },
  )
})

test('g. five curated skincare products give the retinol and salicylic acid finding with sources', async ({
  page,
}) => {
  await logPanel(page).getByRole('combobox', { name: 'Product' }).fill('TIRTIR')
  const options = page.getByRole('option')
  await expect(options.first()).toBeVisible()
  await expect(
    options.filter({ hasText: 'From our curated skincare set' }),
  ).toHaveCount(3)
  await shot(page, 'g-1-tirtir-search')
  await logPanel(page).getByRole('button', { name: 'Clear search' }).click()

  for (const item of CURATED) {
    await logDose(page, item.query, item.brand, 1)
  }
  await openTab(page, 'Skincare')
  const auto = page.locator('.skincare-auto-results')
  await expect(auto).toBeVisible()
  const finding = auto
    .locator('.skincare-flag')
    .filter({ hasText: /retinol/i })
    .filter({ hasText: /salicylic/i })
  await expect(finding.first()).toBeVisible()
  await expect(finding.first()).toContainText('Matched on')
  await shot(page, 'g-2-skincare')
})

test('g. no ".." appears anywhere in the skincare text', async ({ page }) => {
  await knownBug(
    'the same-product note appends the rule text to "The source says: ... " and ends the sentence with a second period',
    /no double period in any text/,
    async () => {
      await openTab(page, 'Skincare')
      await expect(page.locator('.skincare-auto-results .skincare-flag').first()).toBeVisible()
      const text = await page.evaluate(() => document.body.innerText)
      expect(text, 'no double period in any text').not.toContain('..')
    },
  )
})

test('g. a niacinamide card lists every product containing niacinamide', async ({
  page,
}) => {
  await knownBug(
    'niacinamide appears only inside pairwise vitamin C findings; the cleanser containing niacinamide is not named by any finding',
    /niacinamide card lists/,
    async () => {
      await openTab(page, 'Skincare')
      await expect(page.locator('.skincare-auto-results .skincare-flag').first()).toBeVisible()
      const flags = await cardsOf(page, '.skincare-auto-results .skincare-flag')
      const niacinamide = flags.filter((c) => /niacinamide/i.test(c.paras[0] ?? ''))
      expect(niacinamide.length).toBeGreaterThan(0)
      const listed = niacinamide.map((c) => c.paras[0] ?? '').join(' | ')
      for (const product of [
        'La Roche-Posay Toleriane Purifying Foaming Facial Cleanser',
        'TIRTIR Milk Skin Toner',
        'Eva Naturals Vitamin C+ Serum',
      ]) {
        expect(listed, `niacinamide card lists ${product}`).toContain(product)
      }
    },
  )
})

test('g. every skincare finding carries an "Unverified reference" tag', async ({
  page,
}) => {
  await knownBug(
    'Skincare.tsx renders no Unverified reference tag on its findings (Interactions and Food & drink do)',
    /every finding carries Unverified reference/,
    async () => {
      await openTab(page, 'Skincare')
      await expect(page.locator('.skincare-auto-results .skincare-flag').first()).toBeVisible()
      const flags = await cardsOf(page, '.skincare-auto-results .skincare-flag')
      expect(flags.length).toBeGreaterThan(0)
      expect(
        flags
          .filter((c) => !c.tags.includes('Unverified reference'))
          .map((c) => c.paras[0]),
        'every finding carries Unverified reference',
      ).toEqual([])
    },
  )
})

test('g. a curated product is found by the full name shown in the results', async ({
  page,
}) => {
  await knownBug(
    'search_curated and my_products.search match brand or name separately, never "brand name" together',
    /full displayed name finds the product/,
    async () => {
      await logPanel(page)
        .getByRole('combobox', { name: 'Product' })
        .fill('TIRTIR Milk Skin Toner')
      await expect(
        page.getByRole('option').filter({ hasText: 'TIRTIR Milk Skin Toner' }),
        'full displayed name finds the product',
      ).toHaveCount(1, { timeout: 5000 })
    },
  )
})

test('h. an unknown product can be added from its ingredient list with a four-item preview', async ({
  page,
}) => {
  const panel = logPanel(page)
  await panel.getByRole('combobox', { name: 'Product' }).fill('zzqxv')
  await expect(page.getByText('No products found for "zzqxv"')).toBeVisible()
  await shot(page, 'h-1-no-results')
  await page
    .getByRole('button', { name: 'Add this product with its ingredient list' })
    .click()
  await page.getByLabel('Brand').fill('ZZ Labs')
  await page.getByLabel('Ingredients').fill(MY_PRODUCT_TEXT)
  await expect(page.getByText('Parsed 4 ingredients:')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Save to My products' })).toBeEnabled()
  await shot(page, 'h-2-preview')
})

test('h. saving an added product stores it, logs it, and shows it under My products and on the timeline', async ({
  page,
}) => {
  await knownBug(
    'Save to My products navigates the page to /? and posts nothing; separately, saveAndLogProduct re-searches "brand name", which search never matches',
    /added product is saved/,
    async () => {
      await logPanel(page).getByRole('combobox', { name: 'Product' }).fill('zzqxv')
      await page
        .getByRole('button', { name: 'Add this product with its ingredient list' })
        .click()
      await page.getByLabel('Brand').fill('ZZ Labs')
      await page.getByLabel('Ingredients').fill(MY_PRODUCT_TEXT)
      await page.getByRole('button', { name: 'Save to My products' }).click()
      await page.waitForLoadState('networkidle')
      await openTab(page, 'Skincare')
      const mine = page
        .locator('section.panel')
        .filter({ has: page.getByRole('heading', { name: 'My products' }) })
      await expect(mine, 'added product is saved').toContainText('ZZ Labs zzqxv', {
        timeout: 5000,
      })
      await expect(mine).toContainText(MY_PRODUCT_TEXT)
      await openTab(page, 'Timeline')
      await expect(
        page.locator('.timeline-item-dose').filter({ hasText: 'ZZ Labs zzqxv' }),
        'added product is on the timeline',
      ).toHaveCount(1)
    },
  )
})

test('i. timeline accepts a skin entry and a headache entry', async ({ page }) => {
  await openTab(page, 'Timeline')
  const form = page.locator('form.log-form')

  await form.getByLabel('Value').fill('dry patch on cheek')
  await form.getByRole('button', { name: 'Add to timeline' }).click()
  await expect(
    page.locator('.timeline-item-log').filter({ hasText: 'dry patch on cheek' }),
  ).toHaveCount(1)

  await form.getByLabel('Category').selectOption('headache')
  await form.getByLabel('Value').fill('mild, behind the eyes')
  await form.getByRole('button', { name: 'Add to timeline' }).click()
  await expect(
    page.locator('.timeline-item-log').filter({ hasText: 'mild, behind the eyes' }),
  ).toHaveCount(1)
  await shot(page, 'i-1-entries')
})

test('i. timeline entry can be deleted after a confirm step', async ({ page }) => {
  await knownBug(
    'Timeline.tsx has no delete control; deleteLog in api.ts is never called',
    /entry can be deleted/,
    async () => {
      await openTab(page, 'Timeline')
      const entry = page
        .locator('.timeline-item-log')
        .filter({ hasText: 'mild, behind the eyes' })
      await expect(entry).toHaveCount(1)
      page.once('dialog', (dialog) => void dialog.accept())
      const remove = entry.getByRole('button', { name: /delete|remove/i })
      await expect(remove, 'entry can be deleted').toHaveCount(1, { timeout: 3000 })
      await remove.click()
      await expect(entry).toHaveCount(0)
    },
  )
})

test('i. timeline has no nested scroll box', async ({ page }) => {
  await knownBug(
    '.timeline-scroll sets max-height 600px with overflow-y auto',
    /no nested scroll box/,
    async () => {
      await openTab(page, 'Timeline')
      await expect(page.locator('.timeline-item').first()).toBeVisible()
      const nested = await page.evaluate(() =>
        Array.from(document.querySelectorAll('.timeline, .timeline-scroll'))
          .map((el) => getComputedStyle(el).overflowY)
          .filter((value) => value === 'auto' || value === 'scroll'),
      )
      expect(nested, 'no nested scroll box').toEqual([])
    },
  )
})

test('j. neuro regimen gives a donepezil same-ingredient card and unmodeled cards, with no skincare lines', async ({
  page,
}) => {
  await scheduleProduct(page, 'Donepezil', 'Donepezil')
  await scheduleProduct(page, 'Namzaric', 'Namzaric')
  await scheduleProduct(page, 'Rivastigmine', 'Rivastigmine')
  for (const name of ['Donepezil', 'Namzaric', 'Rivastigmine']) {
    await expect(medCard(page, name).locator('.med-levels')).toContainText('Not modeled')
  }
  await shot(page, 'j-1-neuro-schedule')

  await openTab(page, 'Interactions')
  const regimen = page
    .locator('section.panel')
    .filter({ has: page.getByRole('heading', { name: 'Your regimen' }) })
  await expect(regimen.locator('.skincare-flag').first()).toBeVisible()
  const duplicate = regimen
    .locator('.skincare-flag')
    .filter({ hasText: /donepezil/i })
    .filter({ hasText: 'Namzaric' })
  await expect(duplicate.first()).toBeVisible()
  expect(await regimen.innerText()).not.toMatch(SKINCARE_WORDS)
  await shot(page, 'j-2-interactions')

  await openTab(page, 'Food & drink')
  const foodRegimen = page
    .locator('section.panel')
    .filter({ has: page.getByRole('heading', { name: 'Your regimen' }) })
  await expect(foodRegimen).not.toContainText('Loading…')
  expect(await foodRegimen.innerText()).not.toMatch(SKINCARE_WORDS)
  await shot(page, 'j-3-food')
})

test('k. bad input is handled: quantity 0 and -1, empty search, 5000 characters', async ({
  page,
}) => {
  const panel = logPanel(page)
  const logButton = panel.getByRole('button', { name: 'Log', exact: true })
  await expect(logButton).toBeDisabled()

  await pickProduct(panel, page, 'NoDoz', 'NoDoz')
  await expect(logButton).toBeEnabled()
  for (const value of ['0', '-1']) {
    await page.getByLabel('Quantity').fill(value)
    await expect(logButton).toBeDisabled()
  }
  await shot(page, 'k-1-quantity')

  await page.getByLabel('Quantity').fill('1')
  await panel.getByRole('button', { name: 'Clear search' }).click()
  await expect(logButton).toBeDisabled()

  const symptom = symptomPanel(page).getByRole('textbox', { name: 'Symptom' })
  await symptom.fill('a'.repeat(5000))
  await symptomPanel(page).getByRole('button', { name: 'Check' }).click()
  await expect(symptomPanel(page).locator('.symptom-result, .error').first()).toBeVisible()
  expect(await hasHorizontalOverflow(page)).toBe(false)
  const response = await page.request.post(`${API_URL}/symptom-check`, {
    data: { symptom: 'a'.repeat(5000) },
  })
  expect(response.status()).toBe(422)
  await shot(page, 'k-2-long-symptom')

  await openTab(page, 'Timeline')
  const note = 'felt unwell after lunch '.repeat(210)
  expect(note.length).toBeGreaterThan(5000)
  const form = page.locator('form.log-form')
  await form.getByLabel('Value').fill('long note check')
  await form.getByLabel('Notes').fill(note)
  await form.getByRole('button', { name: 'Add to timeline' }).click()
  await expect(
    page.locator('.timeline-item-log').filter({ hasText: 'long note check' }),
  ).toHaveCount(1)
  expect(await hasHorizontalOverflow(page)).toBe(false)
  await shot(page, 'k-3-long-note')
})

test('k. quantity 0 and -1 show a clear message', async ({ page }) => {
  await knownBug(
    'App.tsx disables Log for an invalid quantity but shows no message',
    /clear message for quantity/,
    async () => {
      const panel = logPanel(page)
      await pickProduct(panel, page, 'NoDoz', 'NoDoz')
      for (const value of ['0', '-1']) {
        await page.getByLabel('Quantity').fill(value)
        await expect(
          panel.locator('.error, .warning, [role="alert"], .muted').filter({
            hasText: /quantity|greater than|more than 0|at least|must be/i,
          }),
          'clear message for quantity',
        ).toHaveCount(1, { timeout: 2000 })
      }
    },
  )
})
