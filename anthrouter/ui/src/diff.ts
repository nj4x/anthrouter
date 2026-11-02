import { diffLines, diffWordsWithSpace } from 'diff'

export type DiffSegment = { kind: 'same' | 'removed' | 'added'; text: string }

/** Above this many lines in either text the drawer skips the diff (ADR-0024 Decision 6). */
export const MAX_DIFF_LINES = 2000

/**
 * Character cap: a JSON block-list system prompt keeps each block on one
 * physical line, so the line limit alone does not bound word-diff cost.
 */
export const MAX_DIFF_CHARS = 200_000

function lineCount(text: string): number {
  let n = 1
  for (let i = 0; i < text.length; i++) if (text.charCodeAt(i) === 10) n++
  return text.endsWith('\n') ? n - 1 : n
}

function tooLarge(text: string): boolean {
  return text.length > MAX_DIFF_CHARS || lineCount(text) > MAX_DIFF_LINES
}

function wordSegments(removed: string, added: string): DiffSegment[] {
  return diffWordsWithSpace(removed, added).map((c) => ({
    kind: c.removed ? 'removed' : c.added ? 'added' : 'same',
    text: c.value,
  }))
}

/**
 * Line diff of original against dispatched, refined to a word diff inside each
 * removed/added line pair.  Returns null when either text is over
 * MAX_DIFF_LINES lines or MAX_DIFF_CHARS characters, or the diff throws; the
 * caller then shows the plain original.  The size check runs before diffing,
 * so an over-limit prompt never reaches jsdiff.
 */
export function computeProxyDiff(original: string, dispatched: string): DiffSegment[] | null {
  if (tooLarge(original) || tooLarge(dispatched)) return null
  try {
    const changes = diffLines(original, dispatched)
    const out: DiffSegment[] = []
    for (let i = 0; i < changes.length; i++) {
      const c = changes[i]
      const next = changes[i + 1]
      if (c.removed && next?.added) {
        out.push(...wordSegments(c.value, next.value))
        i++
      } else {
        out.push({ kind: c.removed ? 'removed' : c.added ? 'added' : 'same', text: c.value })
      }
    }
    return out
  } catch {
    return null
  }
}
