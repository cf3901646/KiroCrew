import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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
const fargate = {
  id: 'aws-dev', name: 'AWS development', aws_profile: 'sandbox', region: 'us-east-1', cluster: 'dev',
  task_definition: 'session-dev:1', subnets: ['subnet-12345678'], security_groups: ['sg-12345678'],
  assign_public_ip: false, workdir: '/workspace',
}
interface Section {
  coder_url: string
  profiles: typeof profile[]
  fargate_profiles?: typeof fargate[]
  default_profile?: string
  stop_on_shutdown?: boolean
}
let stored: { session_environments?: Section }
let reads: number
let writes: unknown[]
let qc: QueryClient

function mount(enabled = true) {
  if (enabled) localStorage.setItem(PREVIEW_SESSION_ENVIRONMENTS, '1')
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={qc}><MemoryRouter><SessionEnvironmentsCard enabled={enabled} /></MemoryRouter></QueryClientProvider>)
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
const check = () => screen.getByRole('button', { name: 'Check settings' })
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
/** The status glyph of the check line holding `node`. */
const glyph = (node: HTMLElement) => node.closest('li')!.querySelector('svg')!
const SANDBOX_LINK = 'Change the agent sandbox (Developer → Config)'

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

  it('does not load the configuration when disabled', () => {
    mount(false)
    expect(reads).toBe(0)
    expect(screen.queryByLabelText('Coder URL')).not.toBeInTheDocument()
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
    const survives = () => screen.getByRole('switch', { name: 'Files survive a workspace stop' })
    expect(survives()).toBeChecked()
    expect(screen.getByText(/deletes its files when the workspace stops/)).toBeInTheDocument()
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
    const UNDO = 'Removed “Development”. Discard changes restores it and undoes any other unsaved edits.'
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
    for (const hint of ['A short unique code: letters, digits, _ and - only.', "Shown in the chat's environment menu.", 'Name of a template in your Coder deployment (run coder templates list to see them).', "Folder in the workspace where the chat's files and commands run."]) expect(screen.getByText(hint)).toBeInTheDocument()
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
    expect(screen.getByText('Save your changes to check them.')).toBeVisible()
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
    const toggle = within(defaults).getByRole('switch', { name: 'Start new chats on an environment profile' })
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
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(2))
    expect((writes[1] as { value: Section }).value).toMatchObject({ default_profile: '', profiles: [profile] })
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
  it('runs the read-only check and reports Coder, each template and each git host', async () => {
    let checks = 0
    server.use(http.post('*/api/session-environments/check', () => {
      checks++
      return HttpResponse.json({
        coder: { ok: true, owner: 'devuser' },
        profiles: [{ id: 'dev', ok: false, error: 'Coder template is unavailable' }],
        git: { 'github.com': 'signed_in', 'dev.azure.com': 'not_signed_in', 'bitbucket.org': 'unchecked' },
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
    expect(results).toHaveTextContent('GitHub: signed in on this machine')
    expect(results).toHaveTextContent(
      'Azure DevOps: not signed in on this machine. To sign in, run az login in a terminal on this machine.',
    )
    expect(results).toHaveTextContent(
      "Bitbucket: can't be checked from here; git will use this machine's saved Bitbucket login if there is one. If git can't sign in, clone a private Bitbucket repository over HTTPS in a terminal on this machine; git's credential helper saves the login it asks for.",
    )
    // No bare `git credential approve`: typed alone it waits on stdin.
    expect(results).not.toHaveTextContent('git credential approve')
    // Bitbucket has a display name of its own, like the other hosts.
    expect(results).not.toHaveTextContent('bitbucket.org')
    // The sign-in command reads as a command the reader copies, untranslated.
    const command = within(results).getByText('az login')
    expect(command.tagName).toBe('CODE')
    expect(command).toHaveAttribute('translate', 'no')
    // A git host is optional: signed out is a muted state ring, never the
    // danger cross the blocking Coder and template failures use, and never an
    // arrow that reads as a sign-in button.
    const signedOut = glyph(command)
    expect(signedOut).toHaveClass('lucide-circle-dashed', 'text-muted')
    expect(signedOut).not.toHaveClass('lucide-log-in')
    expect(signedOut).not.toHaveClass('text-danger')
    expect(results.querySelector('svg.text-danger, svg.lucide-x, svg.lucide-log-in')).toBeNull()
    expect(within(results).queryAllByRole('button').filter(b => !b.closest('[role="alert"]'))).toEqual([])
    const unchecked = glyph(within(results).getByText(/^Bitbucket: can't be checked/))
    expect(unchecked).toHaveClass('lucide-minus', 'text-muted')
    expect(glyph(within(results).getByText('GitHub: signed in on this machine'))).toHaveClass('lucide-check', 'text-ok')
    // Nothing was withheld, so there is no sandbox to point at.
    expect(screen.queryByRole('link', { name: SANDBOX_LINK })).toBeNull()
    expect(writes).toEqual([])
  })

  it('says once that a strict sandbox shares no git login and links to where the sandbox is set', async () => {
    server.use(http.post('*/api/session-environments/check', () => HttpResponse.json({
      coder: { ok: true, owner: 'devuser' }, profiles: [],
      git: { 'github.com': 'withheld', 'gitlab.com': 'withheld', 'bitbucket.org': 'withheld', 'dev.azure.com': 'withheld' },
    })))
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(check())
    const results = await screen.findByRole('list', { name: 'Check results' })
    const withheld = within(results).getByText(
      "Git logins aren't shared while the agent sandbox is set to strict. Each workspace uses its own git login.",
    )
    // One line for every host, not four identical ones.
    expect(within(results).getAllByRole('listitem')).toHaveLength(2)
    expect(results).not.toHaveTextContent(/GitHub|GitLab|Bitbucket|Azure DevOps|signed in on this machine/)
    // Withheld has a glyph of its own, distinct from the unchecked dash.
    expect(glyph(withheld)).toHaveClass('lucide-lock', 'text-muted')
    expect(glyph(withheld)).toHaveAttribute('aria-hidden', 'true')
    const link = screen.getByRole('link', { name: SANDBOX_LINK })
    expect(link).toHaveAttribute('href', '/developer?tab=config&highlight=key%3Aagent.sandbox')
    expect(results).not.toContainElement(link)
  })

  it('keeps per-host rows when only some hosts are withheld', async () => {
    server.use(http.post('*/api/session-environments/check', () => HttpResponse.json({
      coder: { ok: true, owner: 'devuser' }, profiles: [], git: { 'github.com': 'withheld', 'gitlab.com': 'unchecked' },
    })))
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(check())
    const results = await screen.findByRole('list', { name: 'Check results' })
    expect(glyph(within(results).getByText("GitHub: not shared with remote sessions under the 'strict' sandbox"))).toHaveClass('lucide-lock')
    expect(glyph(within(results).getByText(/^GitLab: can't be checked/))).toHaveClass('lucide-minus')
    expect(results).not.toHaveTextContent(/Git logins aren't shared/)
    expect(screen.getByRole('link', { name: SANDBOX_LINK })).toBeVisible()
  })

  it('renders the Coder and profile rows from a gateway that sends no git field', async () => {
    server.use(http.post('*/api/session-environments/check', () => HttpResponse.json({
      coder: { ok: true, owner: 'devuser' }, profiles: [{ id: 'dev', ok: true }],
    })))
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(check())
    const results = await screen.findByRole('list', { name: 'Check results' })
    expect(results).toHaveTextContent('Signed in to Coder as devuser')
    expect(results).toHaveTextContent('Development: template python-dev is available')
    expect(within(results).getAllByRole('listitem')).toHaveLength(2)
    expect(results).not.toHaveTextContent(/GitHub|Git logins|signed in on this machine/)
    expect(screen.queryByRole('link', { name: SANDBOX_LINK })).toBeNull()
  })

  it('says the readiness check also reads git sign-ins, and leaves the Manage hint to p1', async () => {
    mount()
    await screen.findByLabelText('Coder URL')
    // The readiness rows say how git signs in; the Manage hint does not repeat it.
    expect(screen.queryByText(/Git to GitHub, GitLab, Bitbucket and Azure DevOps/)).toBeNull()
    // The readiness check says it also reads which git hosts this machine is signed in to.
    expect(screen.getByText(
      "Checks that the Coder CLI on this machine is signed in, that each profile's template exists, and which git hosts this machine is signed in to. Nothing is started and no credential is read.",
    )).toBeVisible()
    expect(screen.queryByText(/which keeps the credentials/)).toBeNull()
  })

  it('surfaces failed Coder checks with a safe hand-off and drops them while editing', async () => {
    checkAnswers({ coder: { ok: false, error: 'Coder login expired' }, profiles: [], git: {} })
    await mounted()
    fireEvent.click(check())
    const results = await screen.findByRole('list', { name: 'Check results' })
    const failure = within(results).getByRole('alert')
    // What to do leads; the raw detail follows.
    expect(failure).toHaveTextContent(/^The Coder check failed\. Make sure the Coder CLI is installed on this machine and signed in \(run coder login in a terminal on this machine\).*Details: Coder login expired/)
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
    for (const row of [rows[3], rows[4]]) expect(row).toHaveTextContent('Archived. Workspace deleting soon')
    // The Coder deployment is one click away, opened safely in a new tab.
    const link = screen.getByRole('link', { name: 'Open Coder' })
    expect(link).toHaveAttribute('href', 'https://coder.example.test')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })
})

describe('Session Environments AWS Fargate profiles', () => {
  it('offers no provider choice on a gateway without Fargate profiles', async () => {
    mount()
    await screen.findByLabelText('Coder URL')
    expect(screen.queryByRole('radio', { name: 'AWS Fargate' })).toBeNull()
  })

  it('opens on Fargate when only Fargate is saved, and checks it without a Coder URL', async () => {
    stored = { session_environments: { coder_url: '', profiles: [], fargate_profiles: [{ ...fargate }] } }
    server.use(http.post('*/api/session-environments/check', () => HttpResponse.json({
      coder: { ok: false, error: 'no Coder URL is saved' },
      profiles: [],
      fargate: [{ id: 'aws-dev', ok: true }],
      git: {},
    })))
    mount()
    expect(await screen.findByRole('radio', { name: 'AWS Fargate' })).toBeChecked()
    expect(screen.getByText(/different from Deploy my crew/)).toBeVisible()
    fireEvent.click(check())
    const results = await screen.findByRole('list', { name: 'Check results' })
    expect(results).toHaveTextContent('AWS development: task definition session-dev:1 is ready')
    // A Fargate-only setup reports no Coder line.
    expect(results).not.toHaveTextContent('Coder')
  })

  it('adds a private-network Fargate profile and saves it with the section', async () => {
    stored = { session_environments: { coder_url: '', profiles: [], fargate_profiles: [] } }
    mount()
    fireEvent.click(await screen.findByRole('radio', { name: 'AWS Fargate' }))
    fireEvent.click(screen.getAllByRole('button', { name: 'Add profile' }).find(b => b.closest('[hidden]') === null)!)
    fireEvent.change(screen.getByLabelText('AWS CLI profile'), { target: { value: 'sandbox' } })
    fireEvent.change(screen.getByLabelText('AWS region'), { target: { value: 'us-east-1' } })
    fireEvent.change(screen.getByLabelText('ECS cluster name or ARN'), { target: { value: 'dev' } })
    fireEvent.change(screen.getByLabelText('Task definition name:revision or ARN'), { target: { value: 'session-dev:1' } })
    fireEvent.change(screen.getByLabelText('Subnet IDs, comma-separated'), { target: { value: 'subnet-12345678, subnet-87654321,' } })
    fireEvent.change(screen.getByLabelText('Security group IDs, comma-separated'), { target: { value: 'sg-12345678' } })
    expect(screen.getByRole('switch', { name: 'Assign public IP' })).not.toBeChecked()
    fireEvent.click(save())
    await screen.findByText(SAVED)
    expect(writes).toEqual([{ path: 'session_environments', value: {
      coder_url: '', profiles: [], fargate_profiles: [{
        id: 'fargate-1', name: '', aws_profile: 'sandbox', region: 'us-east-1', cluster: 'dev',
        task_definition: 'session-dev:1', subnets: ['subnet-12345678', 'subnet-87654321'],
        security_groups: ['sg-12345678'], assign_public_ip: false, workdir: '/workspace',
      }],
    } }])
  })

  it('keeps a typed comma and space in a list field and saves the parsed list', async () => {
    stored = { session_environments: { coder_url: '', profiles: [], fargate_profiles: [{ ...fargate, subnets: [] }] } }
    const u = userEvent.setup()
    mount()
    await screen.findByRole('radio', { name: 'AWS Fargate' })
    expand('AWS development')
    const subnets = screen.getByLabelText('Subnet IDs, comma-separated')
    await u.click(subnets)
    await u.keyboard('subnet-a, subnet-b')
    expect(subnets).toHaveValue('subnet-a, subnet-b')
    await u.keyboard(',')
    expect(subnets).toHaveValue('subnet-a, subnet-b,')
    // Leaving the field tidies it to the list the draft holds.
    await u.tab()
    expect(subnets).toHaveValue('subnet-a, subnet-b')
    fireEvent.click(save())
    await screen.findByText(SAVED)
    expect(writes).toEqual([{ path: 'session_environments', value: {
      coder_url: '', profiles: [], fargate_profiles: [{ ...fargate, subnets: ['subnet-a', 'subnet-b'] }],
    } }])
  })

  it('says a Fargate task bills while it runs and names both things that stop it', async () => {
    stored = { session_environments: { coder_url: '', profiles: [], fargate_profiles: [{ ...fargate }] } }
    mount()
    await screen.findByRole('radio', { name: 'AWS Fargate' })
    const manage = screen.getByRole('region', { name: '3. Manage' })
    expect(manage).toHaveTextContent('which AWS bills for while it runs, even while nobody uses it')
    // The same two stop conditions the composer notice and the shutdown note give.
    expect(manage).toHaveTextContent(
      'It stops when you archive the chat, or on its own about 15 minutes after Kiro Crew stops running.',
    )
  })

  it('reports a failed Fargate check through the error notice', async () => {
    stored = { session_environments: { coder_url: '', profiles: [], fargate_profiles: [{ ...fargate }] } }
    server.use(http.post('*/api/session-environments/check', () => HttpResponse.json({
      coder: { ok: false, error: 'no Coder URL is saved' },
      profiles: [],
      fargate: [{ id: 'aws-dev', ok: false, error: 'the task definition sets an entryPoint' }],
      git: {},
    })))
    mount()
    await screen.findByRole('radio', { name: 'AWS Fargate' })
    fireEvent.click(check())
    const results = await screen.findByRole('list', { name: 'Check results' })
    expect(within(results).getByRole('alert')).toHaveTextContent('AWS development: the task definition sets an entryPoint')
  })

  it('lists only the chosen provider\'s environments', async () => {
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [{ ...profile }], fargate_profiles: [{ ...fargate }] } }
    server.use(http.get('*/api/session-environments/workspaces', () => HttpResponse.json({
      workspaces: [
        { workspace: 'crewenv-devuser-aaaa', slot: 'chat-1', state: 'running', template: 't', created_at: '2026-10-09T10:00:00Z', deletion_due_at: '' },
        { workspace: 'abc123def456', provider: 'fargate', slot: 'chat-2', state: 'running', template: 'aws-dev', created_at: '', deletion_due_at: '' },
      ],
    })))
    mount()
    const coderList = await screen.findByRole('list', { name: 'Workspaces' })
    expect(coderList).toHaveTextContent('crewenv-devuser-aaaa')
    expect(coderList).not.toHaveTextContent('abc123def456')
    fireEvent.click(screen.getByRole('radio', { name: 'AWS Fargate' }))
    const fargateList = await screen.findByRole('list', { name: 'Workspaces' })
    expect(fargateList).toHaveTextContent('abc123def456')
    expect(fargateList).toHaveTextContent('Fargate session task')
    expect(screen.queryByRole('link', { name: 'Open Coder' })).toBeNull()
  })

  it('offers a Fargate profile as the default and drops it with the profile', async () => {
    stored = { session_environments: { coder_url: '', profiles: [], fargate_profiles: [{ ...fargate }], default_profile: 'aws-dev' } }
    mount()
    await screen.findByRole('radio', { name: 'AWS Fargate' })
    expect(screen.getByRole('switch', { name: 'Start new chats on an environment profile' })).toBeChecked()
    expand('AWS development')
    const remove = screen.getAllByRole('button', { name: 'Remove profile' }).find(b => b.closest('[hidden]') === null)!
    fireEvent.click(remove)
    fireEvent.click(save())
    await screen.findByText(SAVED)
    expect(writes).toEqual([{ path: 'session_environments', value: {
      coder_url: '', profiles: [], fargate_profiles: [], default_profile: '',
    } }])
  })

  it('says under the shutdown switch that Fargate tasks are not stopped and idle-exit instead', async () => {
    const note = /^Has no effect on AWS Fargate tasks: they keep running at shutdown.*A task stops when you archive its chat, or on its own about 15 minutes after Kiro Crew stops running\.$/
    stored = { session_environments: { coder_url: '', profiles: [], fargate_profiles: [{ ...fargate }], stop_on_shutdown: true } }
    mount()
    const shutdown = await screen.findByRole('switch', { name: /^Stop workspaces when .* shuts down$/ })
    const text = screen.getByText(note)
    expect(text).toBeVisible()
    expect(shutdown.getAttribute('aria-describedby')?.split(' ')).toContain(text.id)
    cleanup(); qc.clear()
    // A Coder-only setup on a Fargate-capable gateway has no Fargate tasks to explain.
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [{ ...profile }], fargate_profiles: [], stop_on_shutdown: true } }
    mount()
    expect(await screen.findByRole('switch', { name: /^Stop workspaces when .* shuts down$/ })).toBeVisible()
    expect(screen.queryByText(note)).toBeNull()
  })

  it('says switching providers keeps both providers\' profiles', async () => {
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [{ ...profile }], fargate_profiles: [{ ...fargate }] } }
    mount()
    const choice = (await screen.findByRole('radio', { name: 'AWS Fargate' })).closest('fieldset')!
    expect(choice).toHaveAccessibleDescription(/Each provider keeps its own profiles.*nothing is lost/)
    fireEvent.click(screen.getByRole('radio', { name: 'AWS Fargate' }))
    fireEvent.click(screen.getByRole('radio', { name: 'Coder' }))
    expect(screen.getByLabelText('Coder URL')).toHaveValue('https://coder.example.test')
    expect(save()).toBeDisabled()
  })

  it('names the removed profile and says Discard changes brings it back', async () => {
    stored = { session_environments: { coder_url: '', profiles: [], fargate_profiles: [{ ...fargate }] } }
    mount()
    await screen.findByRole('radio', { name: 'AWS Fargate' })
    expect(screen.queryByText(/Removed “/)).toBeNull()
    expandAll()
    fireEvent.click(screen.getByRole('button', { name: 'Remove profile' }))
    expect(screen.getByRole('status')).toHaveTextContent(
      'Removed “AWS development”. Discard changes restores it and undoes any other unsaved edits.',
    )
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes' }))
    expect(screen.queryByText(/Removed “/)).toBeNull()
    expect(screen.getByText('AWS development')).toBeVisible()
    expect(writes).toEqual([])
  })
})
