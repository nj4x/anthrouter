import { useEffect, useState } from 'react'

interface ConfigField {
  restart_required: boolean
  value: string
  description: string
  type: 'str' | 'int' | 'float' | 'bool'
  enum?: string[]
  min?: number
  max?: number
  group: string
}

interface ConfigResponse {
  admin_token_configured: boolean
  fields: Record<string, ConfigField>
  field_order: string[]
}

export function ConfigModal({ isOpen, onClose }: { isOpen: boolean; onClose: () => void }) {
  const [config, setConfig] = useState<ConfigResponse | null>(null)
  const [values, setValues] = useState<Record<string, string>>({})
  const [token, setToken] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (!isOpen) return

    fetch('/admin/config')
      .then(r => r.json() as Promise<ConfigResponse>)
      .then(data => {
        setConfig(data)
        setValues(Object.fromEntries(Object.entries(data.fields).map(([name, field]) => [name, field.value])))
        setError(null)
        setSaved(false)
      })
      .catch(() => {
        // silently fail
      })
  }, [isOpen])

  const handleSave = () => {
    setSaving(true)
    setError(null)
    fetch('/admin/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Admin-Token': token },
      body: JSON.stringify(values),
    })
      .then(async r => {
        if (!r.ok) {
          const data = await r.json().catch(() => null) as { error?: { message?: string } } | null
          throw new Error(data?.error?.message || `Save failed (${r.status})`)
        }
        setSaved(true)
      })
      .catch((err: Error) => {
        setError(err.message || 'Network error while saving configuration')
      })
      .finally(() => {
        setSaving(false)
      })
  }

  useEffect(() => {
    if (!isOpen) return

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [isOpen, onClose])

  const renderField = (name: string, field: ConfigField) => {
    const currentValue = values[name] ?? field.value

    const renderInput = () => {
      // Enum field -> select
      if (field.enum) {
        return (
          <select
            value={currentValue}
            onChange={e => setValues(prev => ({ ...prev, [name]: e.target.value }))}
            className="mt-2 w-full rounded border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200"
          >
            {field.enum.map(opt => (
              <option key={opt} value={opt}>{opt}</option>
            ))}
          </select>
        )
      }

      // Bool field -> checkbox
      if (field.type === 'bool') {
        const isChecked = currentValue === 'true'
        return (
          <div className="mt-2 flex items-center gap-2">
            <input
              type="checkbox"
              checked={isChecked}
              onChange={e => setValues(prev => ({ ...prev, [name]: e.target.checked ? 'true' : 'false' }))}
              className="h-4 w-4 rounded border-slate-300 text-slate-600 focus:ring-slate-500 dark:border-slate-600 dark:bg-slate-900"
            />
            <span className="text-sm text-slate-600 dark:text-slate-400">{isChecked ? 'Yes' : 'No'}</span>
          </div>
        )
      }

      // Numeric field -> input type="number"
      if (field.type === 'int' || field.type === 'float') {
        return (
          <input
            type="number"
            value={currentValue}
            onChange={e => setValues(prev => ({ ...prev, [name]: e.target.value }))}
            min={field.min}
            max={field.max}
            step={field.type === 'int' ? '1' : 'any'}
            className="mt-2 w-full rounded border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200"
          />
        )
      }

      // String field -> input type="text"
      return (
        <input
          type="text"
          value={currentValue}
          onChange={e => setValues(prev => ({ ...prev, [name]: e.target.value }))}
          className="mt-2 w-full rounded border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200"
        />
      )
    }

    return (
      <div key={name} className="rounded border border-slate-200 p-3 dark:border-slate-700">
        <div className="flex items-center justify-between">
          <label className="text-sm font-mono text-slate-700 dark:text-slate-300">{name}</label>
          {field.restart_required && (
            <span className="inline-block rounded bg-red-100 px-2 py-1 text-xs font-semibold text-red-800 dark:bg-red-900 dark:text-red-200">
              Restart required
            </span>
          )}
        </div>
        {renderInput()}
        <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{field.description}</p>
        {saved && field.restart_required && (
          <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">
            Takes effect on next restart via the installed launcher.
          </p>
        )}
      </div>
    )
  }

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
              {config.field_order.map((name, index) => {
                const field = config.fields[name]
                const prevField = index > 0 ? config.fields[config.field_order[index - 1]] : null
                const showGroupHeading = !prevField || prevField.group !== field.group

                return (
                  <div key={name}>
                    {showGroupHeading && (
                      <h3 className="mb-2 mt-4 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                        {field.group}
                      </h3>
                    )}
                    {renderField(name, field)}
                  </div>
                )
              })}
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

            {error && (
              <div className="mb-4 rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-900/20 dark:text-red-200">
                {error}
              </div>
            )}

            {saved && !error && (
              <div className="mb-4 rounded border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800 dark:border-emerald-900 dark:bg-emerald-900/20 dark:text-emerald-200">
                Configuration saved.
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
                onClick={handleSave}
                disabled={!config.admin_token_configured || saving}
                className="rounded bg-slate-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-slate-700"
              >
                {saving ? 'Saving…' : 'Save'}
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
