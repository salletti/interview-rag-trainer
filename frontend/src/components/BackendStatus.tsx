import { useEffect, useState } from 'react'

import { fetchBackendState, type BackendState } from '../api/health.ts'

const LABELS: Record<BackendState, string> = {
  checking: 'Vérification du backend…',
  ready: 'Backend disponible',
  degraded: 'Backend disponible (mode dégradé)',
  not_ready: 'Backend joignable mais pas prêt',
  unavailable: 'Backend indisponible',
}

interface Props {
  /** Intervalle de rafraîchissement en millisecondes. 0 désactive le polling. */
  intervalMs?: number
}

export function BackendStatus({ intervalMs = 10_000 }: Props) {
  const [state, setState] = useState<BackendState>('checking')

  useEffect(() => {
    const controller = new AbortController()
    const refresh = () => {
      void fetchBackendState(fetch, controller.signal).then((next) => {
        if (!controller.signal.aborted) {
          setState(next)
        }
      })
    }
    refresh()
    const timer = intervalMs > 0 ? window.setInterval(refresh, intervalMs) : undefined
    return () => {
      controller.abort()
      if (timer !== undefined) {
        window.clearInterval(timer)
      }
    }
  }, [intervalMs])

  return (
    <p className="status" data-state={state} role="status" aria-live="polite">
      <span className="dot" aria-hidden="true" />
      {LABELS[state]}
    </p>
  )
}
