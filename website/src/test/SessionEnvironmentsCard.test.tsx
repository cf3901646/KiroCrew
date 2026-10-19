import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import { SessionEnvironmentsCard } from '../pages/settings/SessionEnvironmentsCard'
import { FeaturePreviewsSection } from '../pages/settings/FeaturePreviewsSection'
import { PREVIEW_SESSION_ENVIRONMENTS } from '../utils/previewFlags'
import { settingEntryOffered } from '../components/commandPalette/settingsSearchCore'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'

vi.mock('../pages/settings/DecisionsCard', () => ({ DecisionsCard: () => null }))

const profile = { id: 'dev', name: 'Development', template: 'python-dev', workdir: '/workspace/repo' }
interface Section {
  coder_url: string
  profiles: typeof profile[]
  default_profile?: string
}
let stored: { session_environments?: Section }
let reads: number
let writes: unknown[]
let qc: QueryClient

function mount() {
  localStorage.setItem(PREVIEW_SESSION_ENVIRONMENTS, '1')
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={qc}><MemoryRouter><SessionEnvironmentsCard /></MemoryRouter></QueryClientProvider>)
}

beforeEach(() => {
  localStorage.clear()
  reads = 0
  writes = []
  stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [{ ...profile }] } }
  server.use(
    http.get('*/api/config/kirocrew', () => { reads++; return HttpResponse.json(stored) }),
    http.patch('*/api/config/kirocrew', async ({ request }) => {
      const body = await request.json() as { path: string; value: Section }
      writes.push(body)
      stored = { session_environments: body.value }
      return HttpResponse.json(stored)
    }),
  )
})
afterEach(() => { cleanup(); qc?.clear(); localStorage.clear(); vi.useRealTimers() })

const url = () => screen.getByLabelText('Coder URL')
const typeUrl = (value: string) => fireEvent.change(url(), { target: { value } })
const save = () => screen.getByRole('button', { name: 'Save settings' })
const toggle = () => screen.getByRole('switch', { name: 'Session Environments' })
/** Saved profiles start collapsed; open one by its header (name, then subtitle). */
const expand = (title: string) => fireEvent.click(screen.getByRole('button', { name: new RegExp(`^${title}`) }))
const expandAll = () => screen.getAllByRole('button', { expanded: false }).forEach(header => fireEvent.click(header))
const SAVED = 'Settings saved.'
const LOAD_ERROR = 'Could not load environment settings.'
const check = () => screen.getByRole('button', { name: 'Check Coder connection' })
const savedSection = (value: object) => expect(writes).toEqual([{ path: 'session_environments', value }])
const configReads = (fail: boolean) => server.use(http.get('*/api/config/kirocrew', () =>
  fail ? HttpResponse.json({ error: 'offline' }, { status: 500 }) : HttpResponse.json(stored)))
const checkAnswers = (body: object, status = 200) =>
  server.use(http.post('*/api/session-environments/check', () => HttpResponse.json(body, { status })))
async function mounted() {
  const view = mount()
  await screen.findByLabelText('Coder URL')
  return view
}

describe('Session Environments configuration preview', () => {
  it('indexes the opt-in but hides the URL search result until enabled', () => {
    const governance = { decisionsEnabled: true, tipsEnabled: true }
    const gate = SETTINGS_REGISTRY.find(entry => entry.id === 'developer.session-environments')!
    const connection = SETTINGS_REGISTRY.find(entry => entry.configKey === 'session_environments.coder_url')!
    expect(gate).toBeDefined()
    expect(connection).toBeDefined()
    expect(connection.tab).toBe('session-environments')
    expect(settingEntryOffered(gate, governance)).toBe(true)
    expect(settingEntryOffered(connection, governance)).toBe(false)
    localStorage.setItem(PREVIEW_SESSION_ENVIRONMENTS, '1')
    expect(settingEntryOffered(connection, governance)).toBe(true)
  })

  it('withholds every row of the preview tab, including one with no configKey', () => {
    const governance = { decisionsEnabled: true, tipsEnabled: true, updateSwitches: { app: true, gateway: true } }
    const picker = SETTINGS_REGISTRY.find(entry => entry.id === 'session-environments.default-for-new-chats')!
    expect(picker.configKey).toBeUndefined()
    const rows = SETTINGS_REGISTRY.filter(entry => entry.tab === 'session-environments')
    expect(rows.length).toBeGreaterThan(1)
    for (const row of rows) expect(settingEntryOffered(row, governance)).toBe(false)
    localStorage.setItem(PREVIEW_SESSION_ENVIRONMENTS, '1')
    for (const row of rows) expect(settingEntryOffered(row, governance)).toBe(true)
  })

  it('keeps only the opt-in in Developer, without reading or writing configuration', () => {
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><MemoryRouter><FeaturePreviewsSection /></MemoryRouter></QueryClientProvider>)
    expect(toggle()).not.toBeChecked()
    expect(screen.queryByRole('textbox', { name: 'Coder URL' })).not.toBeInTheDocument()
    fireEvent.click(toggle())
    expect(toggle()).toBeChecked()
    expect(screen.queryByRole('textbox', { name: 'Coder URL' })).not.toBeInTheDocument()
    expect(reads).toBe(0)
    expect(writes).toEqual([])
    expect(localStorage.getItem(PREVIEW_SESSION_ENVIRONMENTS)).toBe('1')
  })

  it('writes the complete section only on Save and reloads saved profiles', async () => {
    const first = await mounted()
    typeUrl('https://new.example.test')
    expand('Development')
    fireEvent.change(screen.getByLabelText('Coder template'), { target: { value: 'node-dev' } })
    expect(writes).toEqual([])
    fireEvent.click(save())
    expect(await screen.findByText(SAVED)).toBeVisible()
    savedSection({ coder_url: 'https://new.example.test', profiles: [{ ...profile, template: 'node-dev' }] })
    expect(save()).toBeDisabled()
    first.unmount(); qc.clear()
    mount()
    expect(await screen.findByLabelText('Coder URL')).toHaveValue('https://new.example.test')
    expand('Development')
    expect(screen.getByLabelText('Coder template')).toHaveValue('node-dev')
  })

  it('round-trips a per-profile "files survive a stop" switch, on by default', async () => {
    const first = await mounted()
    expand('Development')
    const survives = () => screen.getByRole('switch', { name: "Let Kiro Crew stop this profile's workspaces" })
    expect(survives()).toBeChecked()
    expect(screen.getByText(/Turn off for a template that loses its files on a stop/)).toBeInTheDocument()
    fireEvent.click(survives())
    expect(survives()).not.toBeChecked()
    fireEvent.click(save())
    expect(await screen.findByText(SAVED)).toBeVisible()
    savedSection({ coder_url: 'https://coder.example.test', profiles: [{ ...profile, compute_survives_stop: false }] })
    first.unmount(); qc.clear()
    await mounted()
    expand('Development')
    expect(survives()).not.toBeChecked()
  })

  it('offers no retention control: archived workspaces are kept a fixed week', async () => {
    stored.session_environments = { ...stored.session_environments!, archived_retention_days: 6 } as Section
    await mounted()
    expect(screen.queryByRole('button', { name: 'Increase' })).not.toBeInTheDocument()
    expect(screen.queryByText(/archived workspaces/i)).not.toBeInTheDocument()
  })

  it('keeps edits across refetch and discards explicitly', async () => {
    await mounted()
    typeUrl('https://draft.example.test')
    act(() => qc.setQueryData(['kirocrewConfig'], { session_environments: { coder_url: 'https://other.example.test', profiles: [] } }))
    expect(url()).toHaveValue('https://draft.example.test')
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes' }))
    expect(url()).toHaveValue('https://other.example.test')
    expect(writes).toEqual([])
  })

  it('adds unique profile drafts and removes them without provisioning', async () => {
    stored.session_environments!.profiles.push({ ...profile, id: 'profile-1' })
    await mounted()
    fireEvent.click(screen.getByRole('button', { name: 'Add profile' }))
    expandAll()
    expect(screen.getAllByLabelText('Profile ID').map(input => (input as HTMLInputElement).value)).toEqual(['dev', 'profile-1', 'profile-2'])
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove profile' })[2])
    expect(screen.getAllByLabelText('Profile ID')).toHaveLength(2)
    expect(writes).toEqual([])
  })

  it('says a removal is undone by Discard changes, until it is saved', async () => {
    const UNDO = 'Removed. Discard changes to undo.'
    await mounted()
    expandAll()
    fireEvent.click(screen.getByRole('button', { name: 'Remove profile' }))
    expect(screen.getByRole('status')).toHaveTextContent(UNDO)
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes' }))
    expect(screen.queryByText(UNDO)).toBeNull()
    expect(screen.getByRole('button', { name: /^Development/ })).toBeInTheDocument()
    expandAll()
    fireEvent.click(screen.getByRole('button', { name: 'Remove profile' }))
    fireEvent.click(save())
    expect(await screen.findByText(SAVED)).toBeVisible()
    expect(screen.queryByText(UNDO)).toBeNull()
    savedSection({ coder_url: 'https://coder.example.test', profiles: [] })
  })

  it('retains a rejected draft and shows the backend validation detail', async () => {
    server.use(http.patch('*/api/config/kirocrew', () => HttpResponse.json({ error: 'profiles[0].id is a duplicate' }, { status: 400 })))
    await mounted()
    typeUrl('https://draft.example.test')
    fireEvent.click(save())
    expect(await screen.findByText('Could not save the settings: profiles[0].id is a duplicate')).toBeVisible()
    expect(url()).toHaveValue('https://draft.example.test')
    expect(save()).toBeEnabled()
    expect(screen.queryByText(SAVED)).not.toBeInTheDocument()
  })

  it('marks the Coder URL box when the gateway refuses the URL, and only then', async () => {
    server.use(http.patch('*/api/config/kirocrew', () => HttpResponse.json({ error: 'coder_url must include a hostname' }, { status: 400 })))
    await mounted()
    expect(url()).not.toHaveAttribute('aria-invalid')
    typeUrl('https://')
    fireEvent.click(save())
    await screen.findByText('Could not save the settings: coder_url must include a hostname')
    expect(url()).toHaveAttribute('aria-invalid', 'true')
    // The reason sits under the box too, and the box points at it.
    const describedBy = url().getAttribute('aria-describedby') ?? ''
    expect(document.getElementById(describedBy.split(' ').pop()!)).toHaveTextContent('coder_url must include a hostname')
    // Editing clears the refusal and the mark with it.
    typeUrl('https://coder.example.test')
    expect(url()).not.toHaveAttribute('aria-invalid')
  })

  it('does not fabricate settings support on an older gateway', async () => {
    stored = {}
    mount()
    expect(await screen.findByText(/This version of Kiro Crew does not support Session Environments settings/)).toBeVisible()
    expect(screen.queryByLabelText('Coder URL')).not.toBeInTheDocument()
    expect(writes).toEqual([])
  })

  it('retries loading after a failed configuration read', async () => {
    configReads(true)
    mount()
    expect(await screen.findByText(LOAD_ERROR)).toBeVisible()
    configReads(false)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByLabelText('Coder URL')
    expect(save()).toBeDisabled()
  })

  it.each([false, true])('reports failed refreshes without losing cached settings or drafts (draft=%s)', async draft => {
    await mounted()
    const value = draft ? 'https://draft.example.test' : stored.session_environments!.coder_url
    if (draft) fireEvent.change(url(), { target: { value } })
    configReads(true)
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    const notice = await screen.findByText(LOAD_ERROR)
    expect(notice).toBeVisible()
    expect(url()).toHaveValue(value)
    expect(save().hasAttribute('disabled')).toBe(!draft)
    expect(within(notice.closest('[role="alert"]') as HTMLElement).queryByRole('button', { name: /Ask the agent/i })).not.toBeInTheDocument()
    configReads(false)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.queryByText(LOAD_ERROR)).not.toBeInTheDocument())
    expect(url()).toHaveValue(value)
    expect(writes).toEqual([])
    if (draft) {
      fireEvent.click(save())
      await screen.findByText(SAVED)
      savedSection({ ...stored.session_environments!, coder_url: value })
    }
  })

  it('freezes editing during a save and does not submit twice', async () => {
    let finish!: () => void
    const pending = new Promise<void>(resolve => { finish = resolve })
    server.use(http.patch('*/api/config/kirocrew', async ({ request }) => {
      writes.push(await request.json())
      await pending
      return HttpResponse.json(stored)
    }))
    await mounted()
    typeUrl('https://draft.example.test')
    fireEvent.click(save())
    await waitFor(() => expect(url()).toBeDisabled())
    expect(save()).toBeDisabled()
    await act(async () => finish())
    await screen.findByText(SAVED)
    expect(writes).toHaveLength(1)
  })
})

describe('Coder setup and saved profile management', () => {
  it('shows Coder setup without claiming a live connection', async () => {
    await mounted()
    // jsdom keeps closed <details> content in the tree, so assert the
    // disclosure state itself: instructions start collapsed.
    const instructions = screen.getByText('Setup instructions', { exact: true }).closest('details')!
    expect(instructions.open).toBe(false)
    instructions.open = true
    expect(screen.getByRole('link', { name: 'Open provider setup documentation' })).toHaveAttribute('href', 'https://coder.com/docs/admin/setup')
    // One provider, so there is no provider choice to make.
    expect(screen.queryByRole('radio')).not.toBeInTheDocument()
    expect(screen.getByText(/A chat runs on a profile once you choose it in the environment menu next to the message box/)).toBeVisible()
    expect(writes).toEqual([])
  })

  it('collapses saved profiles, opens new ones, and keeps edits across collapse', async () => {
    await mounted()
    const header = screen.getByRole('button', { name: /^Development/ })
    expect(header).toHaveAttribute('aria-expanded', 'false')
    expect(header).toHaveTextContent('python-dev')
    expect(screen.queryByLabelText('Coder template')).not.toBeInTheDocument()
    fireEvent.click(header)
    expect(header).toHaveAttribute('aria-expanded', 'true')
    for (const hint of ['A short unique code: letters, digits, _ and - only.', "Shown in the chat's environment menu.", 'Name of a template in your Coder deployment (run coder templates list to see them).', "Folder where the chat's files and commands run."]) expect(screen.getByText(hint)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Coder template'), { target: { value: 'node-dev' } })
    fireEvent.click(header)
    await waitFor(() => expect(screen.queryByLabelText('Coder template')).not.toBeInTheDocument())
    expect(header).toHaveTextContent('node-dev')
    fireEvent.click(header)
    expect(screen.getByLabelText('Coder template')).toHaveValue('node-dev')
    fireEvent.click(screen.getByRole('button', { name: 'Add profile' }))
    expect(screen.getByRole('button', { name: /^profile-1/ })).toHaveAttribute('aria-expanded', 'true')
    expect(writes).toEqual([])
  })

  it('keeps configure, verify and manage on one page without treating a save as readiness', async () => {
    await mounted()
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument()
    for (const name of ['1. Connect and configure', '2. Verify readiness', '3. Manage']) {
      expect(screen.getByRole('region', { name })).toBeVisible()
    }
    typeUrl('https://changed.example.test')
    // An unsaved draft cannot be checked: the check reads what the gateway saved.
    expect(check()).toBeDisabled()
    expect(screen.getByText('Save first: the check uses the saved settings.')).toBeVisible()
    fireEvent.click(save())
    await screen.findByText(SAVED)
    expect(check()).toBeEnabled()
    expect(check()).toHaveAttribute('aria-describedby')
    // The controls that did nothing are gone.
    for (const name of ['Start session', 'Manage resources']) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
    }
    expect(writes).toHaveLength(1)
  })

  it('cannot check before a Coder URL is saved', async () => {
    stored = { session_environments: { coder_url: '', profiles: [] } }
    await mounted()
    expect(check()).toBeDisabled()
    expect(screen.queryByRole('link', { name: 'Open Coder' })).toBeNull()
  })

  it('picks a default environment, and drops it with the profile', async () => {
    const second = { ...profile, id: 'gpu', name: 'GPU box' }
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [{ ...profile }, second], default_profile: '' } }
    await mounted()
    // Global, after the numbered steps: off by default, no picker until it is on.
    const defaults = screen.getByRole('region', { name: 'Global defaults' })
    const manage = screen.getByRole('region', { name: '3. Manage' })
    expect(manage.compareDocumentPosition(defaults) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    const toggle = within(defaults).getByRole('switch', { name: 'Start new chats on a profile' })
    expect(toggle).not.toBeChecked()
    expect(within(defaults).queryByRole('combobox', { name: 'Default for new chats' })).toBeNull()
    fireEvent.click(toggle)
    const select = within(defaults).getByRole('combobox', { name: 'Default for new chats' })
    expect(select).toHaveTextContent('Development')
    fireEvent.click(select)
    fireEvent.click(await screen.findByRole('option', { name: 'GPU box' }))
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect((writes[0] as { value: Section }).value.default_profile).toBe('gpu')
    // Removing the default profile falls back to this machine in the same draft.
    expand('GPU box')
    const header = screen.getByRole('button', { name: /^GPU box/ })
    const panel = header.closest('div')!.parentElement!
    fireEvent.click(within(panel).getByRole('button', { name: 'Remove profile' }))
    // The switch that turned itself off says why, beside it.
    expect(within(defaults).getByRole('switch', { name: 'Start new chats on a profile' })).not.toBeChecked()
    expect(defaults).toHaveTextContent('Turned off: its profile was removed. Discard changes to undo.')
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(2))
    expect((writes[1] as { value: Section }).value).toMatchObject({ default_profile: '', profiles: [profile] })
    expect(defaults).not.toHaveTextContent('Turned off: its profile was removed.')
  })

  it('carries the default along when its profile id is renamed', async () => {
    const second = { ...profile, id: 'gpu', name: 'GPU box' }
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [{ ...profile }, second], default_profile: 'gpu' } }
    await mounted()
    expand('GPU box')
    const panel = screen.getByRole('button', { name: /^GPU box/ }).closest('div')!.parentElement!
    fireEvent.change(within(panel).getByLabelText('Profile ID'), { target: { value: 'gpu-2' } })
    expect(screen.getByRole('combobox', { name: 'Default for new chats' })).toHaveTextContent('GPU box')
    // Clearing the id to retype it keeps the default for the retyped id.
    fireEvent.change(within(panel).getByLabelText('Profile ID'), { target: { value: '' } })
    fireEvent.change(within(panel).getByLabelText('Profile ID'), { target: { value: 'gpu-3' } })
    // Renaming another profile leaves the default where it is.
    expand('Development')
    const first = screen.getByRole('button', { name: /^Development/ }).closest('div')!.parentElement!
    fireEvent.change(within(first).getByLabelText('Profile ID'), { target: { value: 'dev-2' } })
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect((writes[0] as { value: Section }).value).toMatchObject({
      default_profile: 'gpu-3', profiles: [{ ...profile, id: 'dev-2' }, { ...second, id: 'gpu-3' }],
    })
  })

  it('disables the shutdown switch until a profile exists', async () => {
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [], default_profile: '', stop_on_shutdown: true } as Section }
    await mounted()
    const stop = () => screen.getByRole('switch', { name: 'Stop workspaces when Kiro Crew shuts down' })
    // Shown off while it cannot apply, so it never reads as "on and locked".
    expect(stop()).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(stop())
    expect(stop()).not.toBeChecked()
    fireEvent.click(screen.getByRole('button', { name: 'Add profile' }))
    expect(stop()).not.toHaveAttribute('aria-disabled', 'true')
    expect(stop()).toBeChecked()
    expect(writes).toEqual([])
  })
})

describe('Session Environments readiness and workspaces', () => {
  it('runs the read-only check and reports Coder and each template', async () => {
    let checks = 0
    server.use(http.post('*/api/session-environments/check', () => {
      checks++
      return HttpResponse.json({
        coder: { ok: true, owner: 'devuser' },
        profiles: [{ id: 'dev', ok: false, error: 'Coder template is unavailable' }],
      })
    }))
    await mounted()
    fireEvent.click(check())
    const results = await screen.findByRole('list', { name: 'Check results' })
    expect(checks).toBe(1)
    expect(results).toHaveTextContent('Signed in to Coder as devuser')
    expect(results).toHaveTextContent('Development: Coder template is unavailable')
    const failure = within(results).getByRole('alert')
    expect(failure).toHaveTextContent('Development: Coder template is unavailable')
    expect(within(failure).getByRole('button', { name: /Ask the agent/ })).toBeVisible()
    expect(writes).toEqual([])
  })

  it('surfaces failed Coder checks with a safe hand-off and drops them while editing', async () => {
    checkAnswers({ coder: { ok: false, error: 'Coder login expired' }, profiles: [] })
    await mounted()
    fireEvent.click(check())
    const results = await screen.findByRole('list', { name: 'Check results' })
    const failure = within(results).getByRole('alert')
    // What to do leads; the raw detail follows.
    expect(failure).toHaveTextContent(/^The Coder check failed\. Make sure the Coder CLI is installed and signed in on this machine, then check again\./)
    // The CLI's raw words are folded under Details, not repeated in red.
    expect(failure).not.toHaveTextContent('Coder login expired')
    const details = within(results).getByText('Details').closest('details')!
    expect(details).not.toHaveAttribute('open')
    expect(details).toHaveTextContent('Coder login expired')
    const command = within(results).getByText('coder login')
    expect(command.tagName).toBe('CODE')
    expect(command.closest('p')).toHaveTextContent('To sign in, run coder login in a terminal on this machine.')
    expect(within(failure).getByRole('button', { name: /Ask the agent/ })).toBeVisible()
    typeUrl('https://draft.example.test')
    expect(screen.queryByRole('list', { name: 'Check results' })).toBeNull()
  })

  it.each([[[{ id: 'dev', ok: true }]], [[]]])('marks templates not checked when Coder sign-in fails (%j)', async rows => {
    checkAnswers({ coder: { ok: false, error: 'Coder login expired' }, profiles: rows })
    await mounted()
    fireEvent.click(check())
    const results = await screen.findByRole('list', { name: 'Check results' })
    expect(results).toHaveTextContent('Development: not checked until Coder sign-in works')
    expect(results).not.toHaveTextContent(/is available/)
  })

  it('reports a check that could not run through the error notice', async () => {
    checkAnswers({ error: 'x' }, 500)
    await mounted()
    fireEvent.click(check())
    expect(await screen.findByText('The check could not run.')).toBeVisible()
    // An edit makes the answer stale and starts an unsaved draft: the notice,
    // and its hand-off that would leave the page, go away.
    typeUrl('https://draft.example.test')
    expect(screen.queryByText('The check could not run.')).toBeNull()
  })

  it('offers no hand-off on a failed workspace list while a draft is unsaved', async () => {
    server.use(http.get('*/api/session-environments/workspaces', () => HttpResponse.json({ error: 'x' }, { status: 500 })))
    await mounted()
    const notice = (await screen.findByText('The workspace list could not be read.')).closest('[role="alert"]') as HTMLElement
    expect(within(notice).getByRole('button', { name: /Ask the agent/ })).toBeVisible()
    typeUrl('https://draft.example.test')
    const after = screen.getByText('The workspace list could not be read.').closest('[role="alert"]') as HTMLElement
    expect(within(after).queryByRole('button', { name: /Ask the agent/ })).toBeNull()
  })

  it('lists the workspaces Kiro Crew owns, with when an archived one is deleted', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-10-12T10:00:00Z'))
    const gone = { slot: 'chat-4', state: 'stopped', template: 't', created_at: '2026-10-01T12:00:00Z' }
    server.use(http.get('*/api/session-environments/workspaces', () => HttpResponse.json({
      workspaces: [
        { workspace: 'crewenv-devuser-aaaa', slot: 'chat-1', state: 'running', template: 't', created_at: '2026-10-09T10:00:00Z', deletion_due_at: '' },
        { workspace: 'crewenv-devuser-bbbb', slot: 'chat-2', state: 'running', template: 't', created_at: '2026-10-09T11:00:00Z', deletion_due_at: '2026-10-16T11:00:00Z' },
        { workspace: 'crewenv-devuser-cccc', slot: 'chat-3', state: 'deleting', template: 't', created_at: '2026-10-09T12:00:00Z', deletion_due_at: '' },
        { ...gone, workspace: 'later-today', deletion_due_at: '2026-10-12T22:00:00Z' },
        { ...gone, workspace: 'overdue', deletion_due_at: '2026-10-08T12:00:00Z' },
      ],
    })))
    mount()
    const list = await screen.findByRole('list', { name: 'Workspaces' })
    vi.useRealTimers()
    const rows = list.querySelectorAll('li')
    expect(rows).toHaveLength(5)
    expect(rows[0]).toHaveTextContent('crewenv-devuser-aaaa')
    expect(rows[0]).toHaveTextContent('In use by a chat')
    expect(rows[0]).not.toHaveTextContent('Archived')
    // An archived row is not "in use": it says when it goes instead.
    expect(rows[1]).toHaveTextContent(/Archived\. Will be deleted on/)
    expect(rows[1]).not.toHaveTextContent('In use by a chat')
    expect(rows[2]).toHaveTextContent('Being deleted')
    expect(rows[2]).not.toHaveTextContent('In use by a chat')
    // A due row still gives its date, so the two archived wordings read as one stage.
    for (const row of [rows[3], rows[4]]) expect(row).toHaveTextContent(/^.*Archived\. Deletion is due .*2026.*; reopen the chat and continue it to keep the workspace\.$/)
    expect(within(rows[0]).getByRole('link', { name: 'crewenv-devuser-aaaa' })).toHaveAttribute('href', '/chat?sid=chat-1')
    expect(within(rows[1]).queryByRole('link')).toBeNull()
    // The Coder deployment is one click away, opened safely in a new tab.
    const link = screen.getByRole('link', { name: 'Open Coder' })
    expect(link).toHaveAttribute('href', 'https://coder.example.test')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })
})
