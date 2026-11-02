import { diffLines } from 'diff'
import { describe, expect, it, vi } from 'vitest'

import { computeProxyDiff, MAX_DIFF_CHARS, MAX_DIFF_LINES } from '../diff'

vi.mock('diff', async (importOriginal) => {
  const actual = await importOriginal<typeof import('diff')>()
  return { ...actual, diffLines: vi.fn(actual.diffLines) }
})

describe('computeProxyDiff', () => {
  it('marks a removed span inside a changed line at word granularity', () => {
    const segs = computeProxyDiff('keep this <reminder> tail\nnext\n', 'keep this tail\nnext\n')!
    expect(segs.filter((s) => s.kind === 'removed').map((s) => s.text).join('')).toContain('<reminder>')
    expect(segs.filter((s) => s.kind === 'same').map((s) => s.text).join('')).toContain('keep')
    expect(segs.filter((s) => s.kind === 'same').map((s) => s.text).join('')).toContain('next')
  })

  it('shows an edit as removed old text plus added new text', () => {
    const segs = computeProxyDiff('Be verbose.\n', 'Be terse.\n')!
    expect(segs).toContainEqual({ kind: 'removed', text: 'verbose' })
    expect(segs).toContainEqual({ kind: 'added', text: 'terse' })
  })

  it('keeps a whole dropped line as one removed segment', () => {
    const segs = computeProxyDiff('a\nvolatile\nb\n', 'a\nb\n')!
    expect(segs).toContainEqual({ kind: 'removed', text: 'volatile\n' })
  })

  it('reconstructs both texts from the segments', () => {
    const a = 'one two\nthree\nfour five six\n'
    const b = 'one\nthree\nfour SIX five\nseven\n'
    const segs = computeProxyDiff(a, b)!
    expect(segs.filter((s) => s.kind !== 'added').map((s) => s.text).join('')).toBe(a)
    expect(segs.filter((s) => s.kind !== 'removed').map((s) => s.text).join('')).toBe(b)
  })

  it('skips the diff above the line limit in either text', () => {
    const big = 'x\n'.repeat(MAX_DIFF_LINES + 1)
    expect(computeProxyDiff(big, 'x')).toBeNull()
    expect(computeProxyDiff('x', big)).toBeNull()
  })

  it('diffs a text of exactly the line limit, with or without a trailing newline', () => {
    expect(computeProxyDiff('x\n'.repeat(MAX_DIFF_LINES), 'y')).not.toBeNull()
    expect(computeProxyDiff(Array(MAX_DIFF_LINES).fill('x').join('\n'), 'y')).not.toBeNull()
  })

  it('skips the diff above the character limit even on a single line', () => {
    const wide = 'x'.repeat(MAX_DIFF_CHARS + 1)
    expect(computeProxyDiff(wide, 'x')).toBeNull()
    expect(computeProxyDiff('x', wide)).toBeNull()
  })

  it('returns null when the diff throws', () => {
    vi.mocked(diffLines).mockImplementationOnce(() => {
      throw new Error('boom')
    })
    expect(computeProxyDiff('a\n', 'b\n')).toBeNull()
  })
})
