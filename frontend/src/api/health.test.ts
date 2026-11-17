import { describe, expect, it, vi } from 'vitest'

import { fetchBackendState } from './health.ts'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('fetchBackendState', () => {
  it.each([
    [200, 'ready'],
    [200, 'degraded'],
    [503, 'not_ready'],
  ] as const)('relaie le statut %i/%s du backend', async (status, body) => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(status, { status: body }))

    expect(await fetchBackendState(fetchMock)).toBe(body)
  })

  it('traite un statut HTTP inattendu comme indisponible', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('Bad Gateway', { status: 502 }))

    expect(await fetchBackendState(fetchMock)).toBe('unavailable')
  })

  it('traite une erreur réseau comme indisponible', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'))

    expect(await fetchBackendState(fetchMock)).toBe('unavailable')
  })
})
