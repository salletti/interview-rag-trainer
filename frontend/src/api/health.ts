// Client minimal de GET /health/ready (relayé par le proxy Vite ou nginx).

export type ComponentStatus = 'ok' | 'unavailable'
export type ReadinessStatus = 'ready' | 'degraded' | 'not_ready'

export interface ComponentCheck {
  status: ComponentStatus
  required: boolean
}

export interface ReadyResponse {
  status: ReadinessStatus
  checks: Record<string, ComponentCheck>
}

export type BackendState = 'checking' | ReadinessStatus | 'unavailable'

export const READY_URL = '/health/ready'

export async function fetchBackendState(
  fetchImpl: typeof fetch = fetch,
  signal?: AbortSignal,
): Promise<BackendState> {
  try {
    const response = await fetchImpl(READY_URL, signal === undefined ? {} : { signal })
    // 200 (ready / degraded) et 503 (not_ready) portent tous deux un corps JSON.
    if (response.status !== 200 && response.status !== 503) {
      return 'unavailable'
    }
    const body = (await response.json()) as Partial<ReadyResponse>
    if (body.status === 'ready' || body.status === 'degraded' || body.status === 'not_ready') {
      return body.status
    }
    return 'unavailable'
  } catch {
    return 'unavailable'
  }
}
