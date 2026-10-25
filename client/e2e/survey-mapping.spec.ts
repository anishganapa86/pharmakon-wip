import { expect, test } from '@playwright/test'
import { startingView } from '../src/survey.ts'

test.describe('starting tab mapping', () => {
  test('avoiding interactions opens Interactions', () => {
    expect(
      startingView({ tracks: ['medications'], goals: ['avoid_interactions'] }),
    ).toBe('interactions')
  })

  test('avoiding interactions wins over a skincare-only list', () => {
    expect(
      startingView({ tracks: ['skincare'], goals: ['avoid_interactions'] }),
    ).toBe('interactions')
  })

  test('only skincare tracked opens Skincare', () => {
    expect(startingView({ tracks: ['skincare'], goals: [] })).toBe('skincare')
    expect(
      startingView({ tracks: ['skincare'], goals: ['stay_consistent'] }),
    ).toBe('skincare')
  })

  test('skincare plus something else keeps the default', () => {
    expect(
      startingView({ tracks: ['skincare', 'medications'], goals: [] }),
    ).toBe('concentration')
  })

  test('reminders opens the Concentration tab that holds the schedule', () => {
    expect(startingView({ tracks: ['medications'], goals: ['reminders'] })).toBe(
      'concentration',
    )
  })

  test('no answers and no survey keep the default', () => {
    expect(startingView({ tracks: [], goals: [] })).toBe('concentration')
    expect(startingView(null)).toBe('concentration')
  })
})
