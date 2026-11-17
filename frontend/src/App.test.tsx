import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App } from './App.tsx'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('App', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('affiche le titre et le message de construction', () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(200, { status: 'ready' })))

    render(<App />)

    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Interview RAG Trainer')
    expect(screen.getByText('Application en construction.')).toBeInTheDocument()
  })

  it('indique un backend disponible quand /health/ready répond ready', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, { status: 'ready' }))
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    expect(await screen.findByText('Backend disponible')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith('/health/ready', expect.anything())
  })

  it('indique un backend indisponible quand la requête échoue', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))

    render(<App />)

    expect(await screen.findByText('Backend indisponible')).toBeInTheDocument()
  })
})
