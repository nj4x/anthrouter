import { useState } from 'react'

import { Requests } from './views/Requests'
import { Routing } from './views/Routing'
import { Sanitizer } from './views/Sanitizer'
import { Usage } from './views/Usage'

const VIEWS = {
  usage: { label: 'Usage', render: () => <Usage /> },
  requests: { label: 'Requests', render: () => <Requests /> },
  routing: { label: 'Routing', render: () => <Routing /> },
  sanitizer: { label: 'Sanitizer', render: () => <Sanitizer /> },
} as const

type ViewName = keyof typeof VIEWS

export function App() {
  const [view, setView] = useState<ViewName>('usage')

  return (
    <div className="min-h-screen bg-white dark:bg-slate-950 text-slate-900 dark:text-slate-100">
      <header className="border-b border-slate-200 dark:border-slate-800">
        <div className="mx-auto flex max-w-7xl items-center gap-6 px-6 py-3">
          <h1 className="text-sm font-semibold tracking-wide text-slate-700 dark:text-slate-300">anthrouter</h1>
          <nav className="flex gap-1">
            {(Object.keys(VIEWS) as ViewName[]).map((name) => (
              <button
                key={name}
                type="button"
                onClick={() => setView(name)}
                className={`rounded px-3 py-1.5 text-sm ${
                  view === name
                    ? 'bg-slate-200 dark:bg-slate-800 text-slate-900 dark:text-slate-100'
                    : 'text-slate-600 dark:text-slate-400 hover:text-slate-800 dark:hover:text-slate-200'
                }`}
              >
                {VIEWS[name].label}
              </button>
            ))}
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-6 py-6">{VIEWS[view].render()}</main>
    </div>
  )
}
