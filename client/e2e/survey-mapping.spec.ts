import { expect, test } from '@playwright/test'
import { scrollsToSchedule, startingView } from '../src/survey.ts'

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

test.describe('schedule scroll', () => {
  test('reminders without avoiding interactions scrolls to the schedule', () => {
    expect(scrollsToSchedule({ tracks: ['medications'], goals: ['reminders'] })).toBe(true)
  })

  test('avoiding interactions wins, so no scroll', () => {
    expect(
      scrollsToSchedule({ tracks: [], goals: ['reminders', 'avoid_interactions'] }),
    ).toBe(false)
  })

  test('no reminders goal and no survey never scroll', () => {
    expect(scrollsToSchedule({ tracks: ['skincare'], goals: [] })).toBe(false)
    expect(scrollsToSchedule(null)).toBe(false)
  })
})
