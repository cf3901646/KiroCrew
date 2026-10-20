import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import PrivacyChapter from './PrivacyChapter'
import { LocalOverlayRoot } from './overlayRoot'
import { api } from '../api/client'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      patchConfig: vi.fn().mockResolvedValue({}),
      themeBoot: vi.fn().mockResolvedValue({ mode: '', color: '', onboarded: false }),
      beaconStatus: vi.fn(),
      collectionStatus: vi.fn(),
    },
  }
})

const patchConfig = vi.mocked(api.patchConfig)
const beaconStatus = vi.mocked(api.beaconStatus)
const collectionStatus = vi.mocked(api.collectionStatus)

const BEACON_ON = {
  enabled: true,
  would_send: true,
  reason: 'ready',
  reason_code: 'ready',
  endpoint_configured: true,
  env_override: false,
  env_var: 'KIROCREW_TELEMETRY_DISABLED',
  governance_override: false,
}

// The gateway as the config writes leave it: Turn off flips both switches, so
// the status re-read after the writes reports what was written.
let stored = { recording: true, beacon: true }
function statusFromStore() {
  beaconStatus.mockImplementation(async () => ({ ...BEACON_ON, enabled: stored.beacon }))
  collectionStatus.mockImplementation(async () => ({ enabled: stored.recording, env_pinned: false, env_var: 'KIROCREW_TELEMETRY' }))
  patchConfig.mockImplementation(async (path: string, value: unknown) => {
    if (path === 'telemetry.enabled') stored.recording = value as boolean
    if (path === 'telemetry.beacon_enabled') stored.beacon = value as boolean
    return {}
  })
}
function statusOn() {
  stored = { recording: true, beacon: true }
  statusFromStore()
}

describe('PrivacyChapter', () => {
  beforeEach(() => {
    patchConfig.mockReset()
    statusOn()
  })

  it('says usage data is on and lists what is and is never collected', async () => {
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    expect(await screen.findByRole('heading', { name: 'Usage data is on' })).toBeInTheDocument()
    expect(screen.getByText(/records how the app is used/)).toBeInTheDocument()
    expect(screen.getByText('What is collected')).toBeInTheDocument()
    expect(screen.getByText('A random install ID that is not linked to you')).toBeInTheDocument()
    expect(screen.getByText('What is never collected')).toBeInTheDocument()
    expect(screen.getByText("Your prompts or the model's replies")).toBeInTheDocument()
    expect(screen.getByText('Where it goes')).toBeInTheDocument()
    expect(screen.getByText(/You can change this anytime in Settings > Privacy, with the Send anonymous usage heartbeat and Record metrics switches/)).toBeInTheDocument()
    // The shared detail sections stay below the notice.
    expect(screen.getByText('Anonymous daily heartbeat')).toBeInTheDocument()
    expect(screen.getByText('Never sent')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Keep on' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Turn off' })).toBeEnabled()
  })

  it('shows the chapter name with no step counter', () => {
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    // A single-screen chapter: the eyebrow is the name alone, never "1 of 1".
    const eyebrow = screen.getByText('Privacy', { selector: 'p' })
    expect(eyebrow).toBeInTheDocument()
    expect(eyebrow.textContent).not.toMatch(/\bof\b|·/)
  })

  it('is mandatory: no skip affordance and Escape does not dismiss', () => {
    const onContinue = vi.fn()
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    expect(screen.queryByRole('button', { name: /skip/i })).not.toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onContinue).not.toHaveBeenCalled()
  })

  it('traps Tab while showing, and leaves it to the Crew in front while hidden', () => {
    const shown = renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)
    ;(document.activeElement as HTMLElement | null)?.blur()
    // From outside the dialog the trap claims Tab and wraps into it.
    expect(fireEvent.keyDown(document, { key: 'Tab' })).toBe(false)
    shown.unmount()

    renderWithProviders(
      <LocalOverlayRoot hidden><PrivacyChapter open onContinue={vi.fn()} /></LocalOverlayRoot>,
    )
    ;(document.activeElement as HTMLElement | null)?.blur()
    expect(fireEvent.keyDown(document, { key: 'Tab' })).toBe(true)
  })

  it('Keep on records the ack and the notice version, and writes no telemetry switch', async () => {
    const onContinue = vi.fn().mockResolvedValue(undefined)
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    await screen.findByText('What is collected')
    fireEvent.click(screen.getByRole('button', { name: 'Keep on' }))
    await waitFor(() => expect(onContinue).toHaveBeenCalledWith({ noticeVersion: true }))
    expect(patchConfig).not.toHaveBeenCalled()
  })

  it('with only the heartbeat on it says so, lists no recording, and records no notice version', async () => {
    stored = { recording: false, beacon: true }
    const onContinue = vi.fn().mockResolvedValue(undefined)
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    expect(await screen.findByRole('heading', { name: 'A daily heartbeat is on' })).toBeInTheDocument()
    expect(screen.getByText(/Recording how the app is used on this machine is off/)).toBeInTheDocument()
    expect(screen.getByText(/Nothing is recorded on this machine. Once a day/)).toBeInTheDocument()
    expect(screen.queryByText('What is collected')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Keep on' }))
    // The version means "told that recording is on"; this text did not say so.
    await waitFor(() => expect(onContinue).toHaveBeenCalledWith({ noticeVersion: false }))
  })

  it('Turn off keeps the chapter open when something outside config.json holds a switch on', async () => {
    // An env var or config.local.json: the writes succeed, the effective state does not move.
    collectionStatus.mockImplementation(async () => ({ enabled: true, env_pinned: false, overlay_override: true }))
    const onContinue = vi.fn()
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    await screen.findByText('What is collected')
    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }))
    expect(await screen.findByText(/config.local.json in the .* data folder keeps it on/)).toBeInTheDocument()
    expect(onContinue).not.toHaveBeenCalled()
  })

  it('Turn off names the env var that keeps recording on', async () => {
    collectionStatus.mockImplementation(async () => ({ enabled: true, env_pinned: true, env_var: 'KIROCREW_TELEMETRY' }))
    const onContinue = vi.fn()
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    await screen.findByText('What is collected')
    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }))
    expect(await screen.findByText(/the KIROCREW_TELEMETRY environment variable keeps it on/)).toBeInTheDocument()
    expect(onContinue).not.toHaveBeenCalled()
  })

  it('says so when the status cannot be read, and still offers both buttons', async () => {
    beaconStatus.mockRejectedValue(new Error('500'))
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    expect(await screen.findByText(/Could not check what is on right now/)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Usage data is on' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Turn off' })).toBeEnabled()
  })

  it('the off variant says nothing is sent and lists no collection', async () => {
    stored = { recording: false, beacon: false }
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    expect(await screen.findByRole('heading', { name: 'Usage data is off on this machine' })).toBeInTheDocument()
    expect(screen.getByText('Nothing is recorded on this machine and nothing is sent.')).toBeInTheDocument()
    expect(screen.queryByText('What is collected')).not.toBeInTheDocument()
    expect(screen.getByTestId('privacy-off-reason')).toHaveTextContent('It was turned off in Settings')
    expect(screen.getByTestId('privacy-details-off')).toHaveTextContent('Off on this machine')
    expect(screen.queryByTestId('privacy-turn-off-scope')).not.toBeInTheDocument()
  })

  it('says what Turn off stops, next to the buttons', async () => {
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    expect(await screen.findByTestId('privacy-turn-off-scope')).toHaveTextContent(
      'Turn off stops local recording, the daily heartbeat and app install receipts.',
    )
  })

  it('Turn off writes both switches off BEFORE the ack', async () => {
    const order: string[] = []
    patchConfig.mockImplementation(async (path: string, value: unknown) => {
      order.push(`${path}=${String(value)}`)
      if (path === 'telemetry.enabled') stored.recording = value as boolean
      if (path === 'telemetry.beacon_enabled') stored.beacon = value as boolean
      return {}
    })
    const onContinue = vi.fn(async () => { order.push('ack') })
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }))
    await waitFor(() => expect(onContinue).toHaveBeenCalledTimes(1))
    expect(order).toEqual(['telemetry.enabled=false', 'telemetry.beacon_enabled=false', 'ack'])
    expect(onContinue).toHaveBeenCalledWith({ noticeVersion: true })
  })

  it('a failed off write keeps the chapter open with an error and records no ack', async () => {
    patchConfig.mockRejectedValueOnce(new Error('500'))
    const onContinue = vi.fn()
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }))
    expect(await screen.findByText(/Your choice could not be saved/)).toBeInTheDocument()
    expect(onContinue).not.toHaveBeenCalled()
    // Still on screen, and the buttons are usable again for a retry.
    expect(screen.getByRole('button', { name: 'Turn off' })).toBeEnabled()
  })

  it('a failed ack write keeps the chapter open with an error', async () => {
    const onContinue = vi.fn().mockRejectedValue(new Error('500'))
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    fireEvent.click(screen.getByRole('button', { name: 'Keep on' }))
    expect(await screen.findByText(/Your choice could not be saved/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Keep on' })).toBeEnabled()
  })

  it('says it is off, with the reason and one Continue, when DO_NOT_TRACK pins it', async () => {
    beaconStatus.mockImplementation(async () => ({ ...BEACON_ON, env_override: true, env_var: 'DO_NOT_TRACK', would_send: false }))
    collectionStatus.mockImplementation(async () => ({ enabled: false, env_pinned: true, env_var: 'DO_NOT_TRACK' }))
    const onContinue = vi.fn().mockResolvedValue(undefined)
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    expect(
      await screen.findByRole('heading', { name: 'Usage data is off on this machine' }),
    ).toBeInTheDocument()
    expect(screen.getByTestId('privacy-off-reason')).toHaveTextContent('DO_NOT_TRACK')
    expect(screen.queryByRole('button', { name: 'Turn off' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    await waitFor(() => expect(onContinue).toHaveBeenCalledWith({ noticeVersion: false }))
    expect(patchConfig).not.toHaveBeenCalled()
  })

  it('names the administrator policy when governance pins it off', async () => {
    stored = { recording: false, beacon: true }
    beaconStatus.mockImplementation(async () => ({ ...BEACON_ON, governance_override: true, would_send: false }))
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    expect(await screen.findByTestId('privacy-off-reason')).toHaveTextContent(
      "Your administrator's security policy turns it off.",
    )
  })

  it('uses the same left panel copy as the Import setup chapter', () => {
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    // Identical aside copy is what keeps the shared shell's mascots from
    // re-animating across the hand-off.
    expect(screen.getByText('Bring your crew with you.')).toBeInTheDocument()
    expect(
      screen.getByText('Merge-only setup · credentials stay where they are'),
    ).toBeInTheDocument()
  })

  it('renders nothing while closed', () => {
    renderWithProviders(<PrivacyChapter open={false} onContinue={vi.fn()} />)

    expect(screen.queryByRole('heading', { name: 'Usage data is on' })).not.toBeInTheDocument()
  })
})
