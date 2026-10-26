import { expect, test } from './fixtures.ts'
import { LOGGED_OUT, newCredentials } from './auth-state.ts'
import { shot } from './helpers.ts'
import type { Page } from '@playwright/test'

test.describe('survey flow', () => {
  test.use({ storageState: LOGGED_OUT })

  async function signUp(page: Page) {
    const { email, password } = newCredentials()
    await page.goto('/signup.html')
    await page.getByLabel('Email').fill(email)
    await page.getByLabel('Password', { exact: true }).fill(password)
    await page.getByLabel('Confirm password').fill(password)
    await page.getByRole('button', { name: 'Create account' }).click()
    await expect(page.getByText('Step 1 of 4')).toBeVisible()
  }

  test('sign up, answer the survey, land on the mapped tab', async ({ page }) => {
    await signUp(page)
    await expect(
      page.getByText('Educational and demonstrative use only. Not a medical device.'),
    ).toBeVisible()

    const next = page.getByRole('button', { name: 'Continue' })
    await expect(next).toBeDisabled()
    await page.getByRole('button', { name: 'Medications' }).click()
    await page.getByRole('button', { name: 'Skincare' }).click()
    await expect(page.getByRole('button', { name: 'Skincare' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    await next.click()

    await expect(page.getByText('Step 2 of 4')).toBeVisible()
    await expect(next).toBeDisabled()
    await page.getByRole('button', { name: 'Avoiding interactions' }).click()
    await next.click()

    await expect(page.getByText('Step 3 of 4')).toBeVisible()
    await page.getByRole('button', { name: 'Somewhat' }).click()
    await next.click()

    await expect(page.getByText('Step 4 of 4')).toBeVisible()
    await page.getByRole('button', { name: 'Back' }).click()
    await expect(page.getByText('Step 3 of 4')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Somewhat' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    await next.click()
    await page.getByRole('button', { name: 'Walk me through it' }).click()
    await next.click()

    await expect(page.getByRole('heading', { name: 'All set' })).toBeVisible()
    await shot(page, 'survey-1-all-set')
    await page.getByRole('button', { name: 'Start using Pharmakon' }).click()

    await expect(page.getByRole('tab', { name: 'Interactions' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    await expect(
      page.getByText('Looks for interactions between the medications you logged.'),
    ).toBeVisible()
    await shot(page, 'survey-2-landed')

    await page.reload()
    await expect(page.getByRole('tab', { name: 'Interactions' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
  })

  test('skipping goes to the default tab and the survey does not return', async ({
    page,
  }) => {
    await signUp(page)
    await page.getByRole('button', { name: 'Skip' }).click()
    await expect(page.getByRole('tab', { name: 'Concentration' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    await expect(page.getByText('Step 1 of 4')).toHaveCount(0)
    await page.reload()
    await expect(page.getByRole('heading', { name: 'Pharmakon' })).toBeVisible()
    await expect(page.getByText('Step 1 of 4')).toHaveCount(0)
  })

  test('settings reopens the survey prefilled and hints follow the guidance choice', async ({
    page,
  }) => {
    await signUp(page)
    await page.getByRole('button', { name: 'Skincare' }).click()
    await page.getByRole('button', { name: 'Continue' }).click()
    await page.getByRole('button', { name: 'Reminders' }).click()
    await page.getByRole('button', { name: 'Continue' }).click()
    await page.getByRole('button', { name: 'Very' }).click()
    await page.getByRole('button', { name: 'Continue' }).click()
    await page.getByRole('button', { name: 'Just the essentials' }).click()
    await page.getByRole('button', { name: 'Continue' }).click()
    await page.getByRole('button', { name: 'Start using Pharmakon' }).click()

    await expect(page.getByRole('tab', { name: 'Skincare' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    await expect(page.locator('.hint')).toHaveCount(0)

    await page.getByRole('button', { name: 'Settings' }).click()
    await page.getByRole('button', { name: 'Retake the survey' }).click()
    await expect(page.getByRole('button', { name: 'Skincare' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    await page.getByRole('button', { name: 'Continue' }).click()
    await page.getByRole('button', { name: 'Continue' }).click()
    await page.getByRole('button', { name: 'Continue' }).click()
    await page.getByRole('button', { name: 'Walk me through it' }).click()
    await page.getByRole('button', { name: 'Continue' }).click()
    await page.getByRole('button', { name: 'Save preferences' }).click()
    await page.getByRole('button', { name: 'Back to app' }).click()

    await expect(page.locator('.hint').first()).toBeVisible()
  })

  test('skipping with a partial answer still lands on the default tab', async ({
    page,
  }) => {
    await signUp(page)
    await page.getByRole('button', { name: 'Medications' }).click()
    await page.getByRole('button', { name: 'Continue' }).click()
    await page.getByRole('button', { name: 'Avoiding interactions' }).click()
    await page.getByRole('button', { name: 'Skip' }).click()
    await expect(page.getByRole('tab', { name: 'Concentration' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
  })

  test('opening Settings and going back keeps the tab you were on', async ({
    page,
  }) => {
    await signUp(page)
    await page.getByRole('button', { name: 'Skip' }).click()
    await page.getByRole('tab', { name: 'Timeline' }).click()
    await page.getByRole('button', { name: 'Settings' }).click()
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
    await page.getByRole('button', { name: 'Back to app' }).click()
    await expect(page.getByRole('tab', { name: 'Timeline' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
  })
})
