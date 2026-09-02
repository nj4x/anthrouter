import { useEffect, useState } from 'react'

interface ConfigField {
  restart_required: boolean
  value: string
}

interface ConfigResponse {
  admin_token_configured: boolean
  fields: Record<string, ConfigField>
}

export function ConfigModal({ isOpen, onClose }: { isOpen: boolean; onClose: () => void }) {
  const [config, setConfig] = useState<ConfigResponse | null>(null)
  const [token, setToken] = useState('')

  useEffect(() => {
    if (!isOpen) return

    fetch('/admin/config')
      .then(r => r.json() as Promise<ConfigResponse>)
      .then(data => {
        setConfig(data)
      })
      .catch(() => {
        // silently fail
      })
  }, [isOpen])

  useEffect(() => {
    if (!isOpen) return

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [isOpen, onClose])

  if (!isOpen) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="w-full max-w-2xl rounded-lg bg-white p-6 dark:bg-slate-800"
        onClick={e => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Edit Configuration"
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-slate-900 dark:text-white">Edit Configuration</h2>
          <button
            onClick={onClose}
            aria-label="Close"
            className="text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200"
          >
            ✕
          </button>
        </div>

        {config ? (
          <>
            <div className="mb-6 max-h-96 space-y-3 overflow-y-auto">
              {Object.entries(config.fields)
                .sort(([a], [b]) => a.localeCompare(b))
                .map(([name, field]) => (
                  <div key={name} className="rounded border border-slate-200 p-3 dark:border-slate-700">
                    <div className="flex items-center justify-between">
                      <label className="text-sm font-mono text-slate-700 dark:text-slate-300">{name}</label>
                      {field.restart_required && (
                        <span className="inline-block rounded bg-red-100 px-2 py-1 text-xs font-semibold text-red-800 dark:bg-red-900 dark:text-red-200">
                          Restart required
                        </span>
                      )}
                    </div>
                    <input
                      type="text"
                      readOnly
                      value={field.value}
                      className="mt-2 w-full rounded border border-slate-300 bg-slate-50 px-3 py-2 text-sm text-slate-800 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200"
                    />
                  </div>
                ))}
            </div>

            <div className="mb-6 rounded border border-slate-200 bg-slate-50 p-3 dark:border-slate-700 dark:bg-slate-900">
              <label className="block text-sm font-mono text-slate-700 dark:text-slate-300">Admin Token</label>
              <input
                type="password"
                value={token}
                onChange={e => setToken(e.target.value)}
                placeholder="Enter admin token to enable configuration edits"
                className="mt-2 w-full rounded border border-slate-300 px-3 py-2 text-sm text-slate-800 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-200"
              />
            </div>

            {!config.admin_token_configured && (
              <div className="mb-4 rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800 dark:border-amber-900 dark:bg-amber-900/20 dark:text-amber-200">
                Set ANTHROUTER_ADMIN_TOKEN in config.env and restart to enable configuration edits.
              </div>
            )}

            <div className="flex justify-end gap-2">
              <button
                onClick={onClose}
                className="rounded border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 dark:border-slate-600 dark:text-slate-300 dark:hover:bg-slate-700"
              >
                Cancel
              </button>
              <button
                disabled={!config.admin_token_configured}
                className="rounded bg-slate-400 px-4 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-slate-700"
              >
                Save
              </button>
            </div>
          </>
        ) : (
          <div className="py-8 text-center text-slate-600 dark:text-slate-400">Failed to load configuration</div>
        )}
      </div>
    </div>
  )
}
