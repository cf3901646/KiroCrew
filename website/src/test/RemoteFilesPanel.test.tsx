import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

// The real editor is a lazy Shiki/Pierre chunk; a textarea stands in for it so
// the panel's buffer, dirty and save handling are what is under test.
vi.mock('../pierre', () => ({
  PierreEditor: function Editor({ file, onChange, onSave }: { file: { contents: string }; onChange: (s: string) => void; onSave?: () => void }) {
    const [value, setValue] = React.useState(file.contents)
    React.useEffect(() => setValue(file.contents), [file])
    return (
      <textarea
        aria-label="editor"
        value={value}
        onChange={(e) => { setValue(e.target.value); onChange(e.target.value) }}
        onKeyDown={(e) => { if (e.key === 's' && e.metaKey) onSave?.() }}
      />
    )
  },
}))

import RemoteFilesPanel from '../pages/chat/RemoteFilesPanel'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { copyToClipboard } from '../utils/clipboard'
import '../i18n'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn() }))

const clients: QueryClient[] = []

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  clients.push(qc)
  return render(ui, { wrapper: ({ children }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider> })
}

afterEach(() => { clients.splice(0).forEach(client => client.clear()) })

const ROOT = { root: '/workspace/w', truncated: false }
const CODER = 'Coder MicroVM'
const panel = (slot = 'chat-1', name = 'Remote') => <RemoteFilesPanel slot={slot} name={name} workdir="/workspace/w" />
const save = () => screen.getByRole('button', { name: 'Save' })
const editor = () => screen.getByLabelText('editor')
const openedEditor = () => screen.findByLabelText('editor')
const fileChanged = () => new ApiError(409, 'changed', JSON.stringify({ code: 'file_changed' }))
async function open(u: ReturnType<typeof userEvent.setup>, ...names: RegExp[]) {
  for (const name of names) await u.click(await screen.findByRole('treeitem', { name }))
}
function unloadBlocked() {
  const exit = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(exit)
  return exit.defaultPrevented
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({
    profile: 'dev', provider: 'coder', profiles: [{ id: 'dev', name: 'Coder', provider: 'coder', workdir: '/workspace/w' }],
  })
})

/** The placement query resolves, the browser mounts, then its listing query
 * rejects: two awaited requests before a listing alert can render. */
const CHAINED_LISTING_TIMEOUT_MS = 5_000

describe('RemoteFilesPanel', () => {
  function mockTree() {
    vi.spyOn(api, 'chatSlotEnvironmentList').mockImplementation(async (_slot, path) =>
      path === '.'
        ? { ...ROOT, path: '.', entries: [{ name: 'src', dir: true }, { name: 'README.md', dir: false }] }
        : { ...ROOT, path: 'src', entries: [{ name: 'main.py', dir: false }] },
    )
    return vi.spyOn(api, 'chatSlotEnvironmentRead').mockResolvedValue({
      ...ROOT, path: 'src/main.py', content: 'print("hi")\n', binary: false,
    })
  }

  it('lists the remote root, expands folders lazily and opens a file in the editor', async () => {
    const read = mockTree()
    const u = userEvent.setup()
    wrap(panel('chat-1', CODER))
    expect(await screen.findByText('Files on Coder MicroVM')).toBeInTheDocument()
    await open(u, /src/, /main\.py/)
    expect(read).toHaveBeenCalledWith('chat-1', 'src/main.py')
    expect(await openedEditor()).toHaveValue('print("hi")\n')
    expect(save()).toBeDisabled()
  })

  it('labels the terminal button with visible text', async () => {
    mockTree()
    const onOpenTerminal = vi.fn()
    wrap(<RemoteFilesPanel slot="chat-1" name="Remote" workdir="/workspace/w" onOpenTerminal={onOpenTerminal} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Terminal' }))
    expect(onOpenTerminal).toHaveBeenCalledOnce()
  })

  it('saves an edit with the opened text as the expected base', async () => {
    mockTree()
    const write = vi.spyOn(api, 'chatSlotEnvironmentWrite').mockResolvedValue({ ok: true, path: 'src/main.py' })
    const u = userEvent.setup()
    wrap(panel('chat-1', CODER))
    await open(u, /src/, /main\.py/)
    await u.type(await openedEditor(), '# edit')
    await u.click(save())
    expect(write).toHaveBeenCalledWith('chat-1', 'src/main.py', 'print("hi")\n# edit', 'print("hi")\n')
    await waitFor(() => expect(save()).toBeDisabled())
  })

  it('keeps text typed while a save is pending dirty and saves it against the new base', async () => {
    mockTree()
    let finish!: (result: { ok: boolean; path: string }) => void
    const pending = new Promise<{ ok: boolean; path: string }>((resolve) => { finish = resolve })
    const write = vi.spyOn(api, 'chatSlotEnvironmentWrite').mockReturnValueOnce(pending)
      .mockResolvedValue({ ok: true, path: 'src/main.py' })
    const u = userEvent.setup()
    wrap(panel('chat-1', CODER))
    await open(u, /src/, /main\.py/)
    await u.type(await openedEditor(), '# saved')
    await u.click(save())
    await waitFor(() => expect(write).toHaveBeenCalledTimes(1))
    await u.type(editor(), '\n# newer')
    await act(async () => finish({ ok: true, path: 'src/main.py' }))
    await waitFor(() => expect(save()).toBeEnabled())
    expect(editor()).toHaveValue('print("hi")\n# saved\n# newer')
    await u.click(save())
    expect(write).toHaveBeenLastCalledWith('chat-1', 'src/main.py', 'print("hi")\n# saved\n# newer', 'print("hi")\n# saved')
    await waitFor(() => expect(save()).toBeDisabled())
  })

  it('reports a stale save instead of overwriting, and keeps the edit', async () => {
    mockTree()
    vi.spyOn(api, 'chatSlotEnvironmentWrite').mockRejectedValue(fileChanged())
    const u = userEvent.setup()
    wrap(panel('chat-1', CODER))
    await open(u, /src/, /main\.py/)
    await u.type(await openedEditor(), 'x')
    await u.click(save())
    expect(await screen.findByText(/changed in the workspace, so it was not saved/)).toBeInTheDocument()
    expect(screen.getByText(/Your edits are kept: use Copy my edits.*Refresh \(the ⟳ button at the top of the file list\)/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeInTheDocument()
    expect(save()).toBeEnabled()
    // "Copied" waits for the clipboard to confirm.
    const copy = vi.mocked(copyToClipboard).mockResolvedValue(true)
    await u.click(screen.getByRole('button', { name: 'Copy my edits' }))
    expect(copy).toHaveBeenCalledWith('print("hi")\nx')
    expect(await screen.findByRole('button', { name: 'Copied!' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Copy my edits' })).toBeNull()
  })

  it('never offers to save a truncated file', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentList').mockResolvedValue({ ...ROOT, path: '.', entries: [{ name: 'big.log', dir: false }] })
    vi.spyOn(api, 'chatSlotEnvironmentRead').mockResolvedValue({ ...ROOT, path: 'big.log', content: 'x', binary: false, truncated: true })
    const u = userEvent.setup()
    wrap(panel('chat-1', CODER))
    await open(u, /big\.log/)
    expect(await screen.findByText(/showing the first 256 KB/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('says the workspace is stopped instead of a generic failure', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentList').mockRejectedValue(
      new ApiError(409, 'stopped', JSON.stringify({ error: 'stopped', code: 'workspace_stopped' })),
    )
    wrap(panel('chat-1', CODER))
    expect(await screen.findByText(/The workspace is stopped/)).toBeInTheDocument()
  })

  it('shows a refused cloud browse as an error with the hand-off, and no refresh, tree, viewer or terminal', async () => {
    const list = vi.spyOn(api, 'chatSlotEnvironmentList').mockRejectedValue(
      new ApiError(409, 'cloud', JSON.stringify({ error: 'cloud', code: 'cloud_sandbox' })),
    )
    wrap(<RemoteFilesPanel slot="chat-1" name="Kiro Cloud" workdir="" onOpenTerminal={() => {}} />)
    // A rejected request is an error (errors-use-error-notice); only a recorded
    // cloud placement is the neutral notice. Placement -> browser -> listing chain.
    const notice = await screen.findByRole('alert', {}, { timeout: CHAINED_LISTING_TIMEOUT_MS })
    // Its own sentence says what failed, so it never reads as the neutral notice.
    expect(notice).toHaveTextContent("Couldn't open this chat's files")
    expect(notice).not.toHaveTextContent('This chat runs in a Kiro cloud sandbox')
    expect(within(notice).getByRole('button', { name: /Ask the agent/ })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Refresh' })).toBeNull()
    expect(screen.queryByText(/Select a file to view it/)).toBeNull()
    expect(screen.queryByRole('tree')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Open a terminal in this workspace' })).toBeNull()
    // The panel and the root tree share one listing request.
    expect(list).toHaveBeenCalledTimes(1)
  })

  it('keeps a real listing failure as an error with the hand-off', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentList').mockRejectedValue(new ApiError(500, 'boom', '{}'))
    wrap(<RemoteFilesPanel slot="chat-1" name="Coder MicroVM" workdir="/workspace/w" />)
    // Placement -> browser -> listing chain.
    const failure = await screen.findByRole('alert', {}, { timeout: CHAINED_LISTING_TIMEOUT_MS })
    expect(failure).toHaveTextContent('Could not read the workspace')
    expect(within(failure).getByRole('button', { name: /Ask the agent/ })).toBeVisible()
    expect(screen.getByText(/Select a file to view it/)).toBeInTheDocument()
  })
  it('retains session-owned drafts and the unload guard across tab, route and chat switches', async () => {
    mockTree().mockImplementation(async slot => ({ ...ROOT, path: 'README.md', content: slot, binary: false }))
    const u = userEvent.setup()
    const view = wrap(panel('chat-a'))
    await open(u, /README/)
    await u.type(await openedEditor(), ' draft-a')
    view.rerender(<div>Terminal</div>)
    expect(unloadBlocked()).toBe(true)
    view.rerender(panel('chat-a'))
    expect(await openedEditor()).toHaveValue('chat-a draft-a')
    view.rerender(panel('chat-b'))
    expect(screen.queryByLabelText('editor')).toBeNull()
    await open(u, /README/)
    expect(await openedEditor()).toHaveValue('chat-b')
    await u.type(editor(), ' draft-b')
    view.rerender(panel('chat-a'))
    expect(await openedEditor()).toHaveValue('chat-a draft-a')
    view.rerender(panel('chat-b'))
    expect(await openedEditor()).toHaveValue('chat-b draft-b')
  })

  it('keeps the original expected text after a refresh observes an agent edit', async () => {
    const read = mockTree()
    const write = vi.spyOn(api, 'chatSlotEnvironmentWrite').mockRejectedValue(fileChanged())
    const u = userEvent.setup()
    wrap(panel())
    await open(u, /README/)
    await u.type(await openedEditor(), '# draft')
    read.mockResolvedValue({ ...ROOT, path: 'README.md', content: '# agent', binary: false })
    await u.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2))
    expect(editor()).toHaveValue('print("hi")\n# draft')
    await u.click(save())
    expect(write).toHaveBeenCalledWith('chat-1', 'README.md', 'print("hi")\n# draft', 'print("hi")\n')
    expect(await screen.findByText(/changed in the workspace, so it was not saved/)).toBeInTheDocument()
  })

  it('applies an in-flight save to its original session after a chat switch', async () => {
    mockTree().mockImplementation(async slot => ({ ...ROOT, path: 'README.md', content: slot, binary: false }))
    let finish!: (result: { ok: boolean; path: string }) => void
    const write = vi.spyOn(api, 'chatSlotEnvironmentWrite').mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    const u = userEvent.setup()
    const view = wrap(panel('chat-a'))
    await open(u, /README/)
    await u.type(await openedEditor(), ' saved')
    await u.click(save())
    expect(write).toHaveBeenCalledWith('chat-a', 'README.md', 'chat-a saved', 'chat-a')
    view.rerender(panel('chat-b'))
    await open(u, /README/)
    await u.type(await openedEditor(), ' draft-b')
    await act(async () => finish({ ok: true, path: 'README.md' }))
    expect(editor()).toHaveValue('chat-b draft-b')
    view.rerender(panel('chat-a'))
    expect(await openedEditor()).toHaveValue('chat-a saved')
    expect(save()).toBeDisabled()
  })

  it('disarms the unload guard only after the last dirty draft is saved', async () => {
    mockTree()
    vi.spyOn(api, 'chatSlotEnvironmentWrite').mockResolvedValue({ ok: true, path: 'README.md' })
    const u = userEvent.setup()
    wrap(panel())
    await open(u, /README/)
    await u.type(await openedEditor(), ' draft')
    expect(unloadBlocked()).toBe(true)
    await open(u, /src/, /main\.py/)
    await u.type(await openedEditor(), ' second draft')
    await u.click(save())
    await waitFor(() => expect(save()).toBeDisabled())
    expect(unloadBlocked()).toBe(true)
    await u.click(screen.getByRole('treeitem', { name: /README/ }))
    expect(await openedEditor()).toHaveValue('print("hi")\n draft')
    await u.click(save())
    await waitFor(() => expect(save()).toBeDisabled())
    expect(unloadBlocked()).toBe(false)
  })

  it('does not enable editing or saving undecodable guest content', async () => {
    mockTree().mockResolvedValue({ ...ROOT, path: 'README.md', content: '', binary: true })
    const u = userEvent.setup()
    wrap(panel())
    await open(u, /README/)
    expect(await screen.findByText(/binary/i)).toBeInTheDocument()
    expect(screen.queryByLabelText('editor')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('only replaces the stale baseline after an explicitly confirmed discard', async () => {
    const read = mockTree()
    const write = vi.spyOn(api, 'chatSlotEnvironmentWrite').mockResolvedValue({ ok: true, path: 'README.md' })
    const u = userEvent.setup()
    wrap(panel())
    await open(u, /README/)
    await u.type(await openedEditor(), '# draft')
    read.mockResolvedValue({ ...ROOT, path: 'README.md', content: '# agent', binary: false })
    await u.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2))
    await u.click(screen.getByRole('button', { name: 'Discard changes' }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    await u.click(screen.getByRole('button', { name: 'Cancel', exact: true }))
    expect(editor()).toHaveValue('print("hi")\n# draft')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await u.click(screen.getByRole('button', { name: 'Discard changes' }))
    const dialog = await screen.findByRole('dialog')
    await u.click(within(dialog).getByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(editor()).toHaveValue('# agent'))
    await u.type(editor(), ' merged')
    await u.click(screen.getByRole('button', { name: 'Save', exact: true }))
    expect(write).toHaveBeenCalledWith('chat-1', 'README.md', '# agent merged', '# agent')
  })

})

describe('recorded cloud placement', () => {
  it('shows neutral info and makes no browse requests for files', async () => {
    vi.mocked(api.chatSlotEnvironment).mockResolvedValue({ profile: 'removed-cloud', provider: 'kiro_cloud', profiles: [] })
    const list = vi.spyOn(api, 'chatSlotEnvironmentList')
    const read = vi.spyOn(api, 'chatSlotEnvironmentRead')
    wrap(<RemoteFilesPanel slot="chat-1" name="Kiro Cloud" workdir="" onOpenTerminal={() => {}} />)
    const notice = await screen.findByRole('status')
    expect(notice).toHaveAttribute('data-tone', 'info')
    expect(notice).toHaveTextContent('This chat runs in a Kiro cloud sandbox')
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByRole('tree')).toBeNull()
    expect(screen.queryByRole('button', { name: /terminal/i })).toBeNull()
    expect(list).not.toHaveBeenCalled()
    expect(read).not.toHaveBeenCalled()
  })

  it('waits for placement before mounting files browse', async () => {
    let finish!: (v: Awaited<ReturnType<typeof api.chatSlotEnvironment>>) => void
    vi.mocked(api.chatSlotEnvironment).mockReturnValue(new Promise(resolve => { finish = resolve }))
    const list = vi.spyOn(api, 'chatSlotEnvironmentList')
    wrap(<RemoteFilesPanel slot="chat-1" name="Cloud" workdir="" />)
    expect(await screen.findByText('Loading…')).toHaveAttribute('aria-busy', 'true')
    expect(list).not.toHaveBeenCalled()
    await act(async () => finish({ profile: 'cloud', provider: 'kiro_cloud', profiles: [] }))
    await screen.findByRole('status')
    expect(list).not.toHaveBeenCalled()
  })

  it('re-reads a stale placement after a cloud refusal and settles on the neutral notice', async () => {
    const placement = vi.mocked(api.chatSlotEnvironment)
      .mockResolvedValueOnce({ profile: 'dev', provider: 'coder', profiles: [{ id: 'dev', name: 'Coder', provider: 'coder', workdir: '/workspace/w' }] })
      .mockResolvedValue({ profile: 'cloud', provider: 'kiro_cloud', profiles: [] })
    vi.spyOn(api, 'chatSlotEnvironmentList').mockRejectedValue(
      new ApiError(409, 'cloud', JSON.stringify({ error: 'cloud', code: 'cloud_sandbox' })),
    )
    wrap(<RemoteFilesPanel slot="chat-1" name="Coder" workdir="/workspace/w" />)
    // Placement (coder) -> browser -> listing refused -> placement re-read
    // (kiro_cloud) -> wrapper swaps to the neutral notice: three awaited requests.
    const notice = await screen.findByRole('status', {}, { timeout: STALE_PLACEMENT_TIMEOUT_MS })
    expect(notice).toHaveAttribute('data-tone', 'info')
    expect(notice).toHaveTextContent('This chat runs in a Kiro cloud sandbox')
    expect(screen.queryByRole('alert')).toBeNull()
    expect(placement).toHaveBeenCalledTimes(2)
  })
})

/** Placement read, browser listing refusal, then the placement re-read the
 * refusal triggers: three awaited requests before the neutral notice renders. */
const STALE_PLACEMENT_TIMEOUT_MS = 5_000
