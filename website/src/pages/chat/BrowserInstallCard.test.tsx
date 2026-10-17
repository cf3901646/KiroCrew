import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      getBrowserInstall: vi.fn(),
      installBrowserCli: vi.fn(),
    },
  }
})

import { api, type BrowserInstallData, type BrowserInstallJob } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { hasBrowserInstallMarker } from '../../utils/detectBrowserInstallMarker'
import BrowserInstallCard from './BrowserInstallCard'
import { browserInstallCardState } from './browserInstallCardState'

const MARKER = '<!-- kirocrew:browser-install -->'
const REPLY = `Browser support needs a one-time install.\n\n${MARKER}\n`

function data(overrides: Partial<BrowserInstallData> = {}): BrowserInstallData {
  return {
    installed: false,
    cli_path: null,
    cli_version: null,
    node_ok: true,
    node_version: '22.0.0',
    browser_ok: false,
    installing: false,
    last_error: null,
    token: false,
    install_job: null,
    ...overrides,
  }
}

function job(overrides: Partial<BrowserInstallJob> = {}): BrowserInstallJob {
  return {
    id: 'j1',
    kind: 'cli_setup',
    engine: null,
    status: 'running',
    stage: 'downloading_browser',
    started_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:30Z',
    finished_at: null,
    elapsed_s: 30,
    error_code: null,
    error_detail: null,
    ...overrides,
  }
}

const card = () => screen.getByTestId('browser-install-card')

describe('hasBrowserInstallMarker', () => {
  it('finds the hidden marker', () => {
    expect(hasBrowserInstallMarker(REPLY)).toBe(true)
    expect(hasBrowserInstallMarker('<!--kirocrew:browser-install-->')).toBe(true)
  })

  it('ignores a marker the reader sees as code, and other markers', () => {
    expect(hasBrowserInstallMarker('```\n' + MARKER + '\n```')).toBe(false)
    expect(hasBrowserInstallMarker('Write `' + MARKER + '` to offer it.')).toBe(false)
    expect(hasBrowserInstallMarker('<!-- kirocrew:open-file path="/a/b.md" -->')).toBe(false)
    expect(hasBrowserInstallMarker('')).toBe(false)
  })
})

describe('browserInstallCardState', () => {
  it('offers the install when nothing is installed or running', () => {
    expect(browserInstallCardState(data(), null, 0)).toEqual({ kind: 'needed' })
  })

  it('reports a running job with its stage', () => {
    expect(browserInstallCardState(data({ installing: true, install_job: job() }), null, 0))
      .toMatchObject({ kind: 'running', stage: 'downloading_browser' })
  })

  it('treats this tab’s unanswered click as running', () => {
    expect(browserInstallCardState(data(), { kind: 'cli_setup' }, 1)).toMatchObject({ kind: 'running', stage: 'preparing' })
  })

  it('is installed only once the CLI and its browser are both present', () => {
    expect(browserInstallCardState(data({ installed: true, browser_ok: true }), null, 0)).toEqual({ kind: 'installed' })
    expect(browserInstallCardState(data({ installed: true, browser_ok: false }), null, 0)).toEqual({ kind: 'needed' })
  })

  it('surfaces a failed CLI setup with its detail', () => {
    const failed = job({ status: 'failed', error_detail: 'npm not found; install Node.js 20 or newer first' })
    expect(browserInstallCardState(data({ install_job: failed }), null, 0))
      .toEqual({ kind: 'failed', detail: 'npm not found; install Node.js 20 or newer first' })
  })
})

describe('BrowserInstallCard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders nothing, and reads no status, without the marker', () => {
    renderWithProviders(<BrowserInstallCard content="Here is the page summary." />)
    expect(screen.queryByTestId('browser-install-card')).toBeNull()
    expect(api.getBrowserInstall).not.toHaveBeenCalled()
  })

  it('installs on click and shows progress, without installing on render', async () => {
    vi.mocked(api.getBrowserInstall).mockResolvedValue(data())
    vi.mocked(api.installBrowserCli).mockResolvedValue(data({ installing: true, install_job: job() }))
    renderWithProviders(<BrowserInstallCard content={REPLY} />)
    const install = await screen.findByTestId('browser-install-card-install')
    expect(api.installBrowserCli).not.toHaveBeenCalled()
    expect(card().dataset.state).toBe('needed')
    // From here the gateway reports the job it started.
    vi.mocked(api.getBrowserInstall).mockResolvedValue(data({ installing: true, install_job: job() }))
    fireEvent.click(install)
    await waitFor(() => expect(api.installBrowserCli).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(card().dataset.state).toBe('running'))
  })

  it('offers to continue once installed, pre-filling the composer rather than sending', async () => {
    vi.mocked(api.getBrowserInstall).mockResolvedValue(data({ installed: true, browser_ok: true }))
    const prefill = vi.fn()
    window.addEventListener('mc:prefill-composer', prefill)
    try {
      renderWithProviders(<BrowserInstallCard content={REPLY} />)
      fireEvent.click(await screen.findByTestId('browser-install-card-continue'))
      expect(prefill).toHaveBeenCalledTimes(1)
      const text = (prefill.mock.calls[0][0] as CustomEvent).detail.text
      expect(typeof text).toBe('string')
      expect(text.length).toBeGreaterThan(0)
    } finally {
      window.removeEventListener('mc:prefill-composer', prefill)
    }
  })

  it('shows a refused install and keeps the Settings link', async () => {
    vi.mocked(api.getBrowserInstall).mockResolvedValue(data())
    vi.mocked(api.installBrowserCli).mockRejectedValue(new ApiError(403, 'owner only', '{}'))
    renderWithProviders(<BrowserInstallCard content={REPLY} />)
    fireEvent.click(await screen.findByTestId('browser-install-card-install'))
    expect(await screen.findByTestId('browser-install-card-error')).toBeTruthy()
    expect(screen.getByTestId('browser-install-card-settings').getAttribute('href')).toBe('/settings/browser')
  })
})
