// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { useLocation } from 'react-router-dom'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { createTestStore, renderHookWithProviders as renderHookSync } from '../test/helpers'
import {
  ITEM_ID_RE,
  OPEN_SESSION_MESSAGE_TYPE,
  openSessionItemOf,
  useFrameOpenSession,
} from './useFrameOpenSession'

// The real `switchSlot` is a thunk that reads the gateway. What is under test here is that the
// hook dispatches it for the slot the gateway named, so it becomes a plain action.
vi.mock('../store/chatSlice', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../store/chatSlice')>()),
  switchSlot: (arg: unknown) => ({ type: 'test/switchSlot', payload: arg }),
}))

const ITEM = 'it_3f9a01c2'
const WORKER_SLOT = 'chat-4-worker'
const SESSION = { kind: 'session', slot: 'chat-1-root' } as const

/** A frame stand-in whose window is a unique object: the hook compares identity only. */
function mountFrame() {
  const win = { postMessage: vi.fn() } as unknown as Window
  return { contentWindow: win } as HTMLIFrameElement
}

function post(data: unknown, source: unknown) {
  const event = new MessageEvent('message', { data })
  Object.defineProperty(event, 'source', { value: source })
  window.dispatchEvent(event)
}

/** Render, then let the providers' own mount effects settle so no update lands after the test's last barrier. */
async function renderHookWithProviders<T>(hook: () => T, options?: Parameters<typeof renderHookSync<T>>[1]) {
  const rendered = renderHookSync(hook, options)
  await act(async () => {})
  return rendered
}

function refusal(status: number, code: string): ApiError {
  return new ApiError(status, 'refused', JSON.stringify({ ok: false, error: 'refused', code }))
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('openSessionItemOf', () => {
  it('accepts the shape the work ledger mints and nothing else', async () => {
    expect(openSessionItemOf({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM })).toBe(ITEM)
    for (const bad of ['', 'it_1', 'IT_3F9A01C2', 'it_3F9A01C2', 'it_3f9a01c2 ', 'x it_3f9a01c2', 'chat-4-worker', 5, null]) {
      expect(openSessionItemOf({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: bad })).toBeNull()
    }
    expect(openSessionItemOf({ type: 'kirocrew-dashboard:open', item_id: ITEM })).toBeNull()
    expect(openSessionItemOf(null)).toBeNull()
    expect(ITEM_ID_RE.test(ITEM)).toBe(true)
  })
})

describe('useFrameOpenSession', () => {
  it('asks the gateway for the card\'s worker and opens it, from its own frame after a gesture', async () => {
    const frame = mountFrame()
    const open = vi.fn()
    const report = vi.fn()
    const ask = vi.spyOn(api, 'crewBoardOpen').mockResolvedValue({ ok: true, action: 'open', item_id: ITEM, slot: WORKER_SLOT })
    await renderHookWithProviders(() => useFrameOpenSession([{ current: frame }], SESSION, report, { open, gesture: () => true }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    await waitFor(() => expect(open).toHaveBeenCalledWith(WORKER_SLOT))
    expect(ask).toHaveBeenCalledTimes(1)
    expect(ask).toHaveBeenCalledWith(SESSION, ITEM)
    expect(report).toHaveBeenLastCalledWith(null)
    // The key stays in the host: nothing is posted back into the page.
    expect(frame.contentWindow.postMessage).not.toHaveBeenCalled()
  })

  it('names the member dashboard when the frame shows a crewmate\'s page', async () => {
    const frame = mountFrame()
    const member = { kind: 'member', slug: 'atlas', member: 'Atlas' } as const
    const ask = vi.spyOn(api, 'crewBoardOpen').mockResolvedValue({ ok: true, action: 'open', item_id: ITEM, slot: WORKER_SLOT })
    await renderHookWithProviders(() => useFrameOpenSession([{ current: frame }], member, vi.fn(), { open: vi.fn(), gesture: () => true }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    await waitFor(() => expect(ask).toHaveBeenCalledWith(member, ITEM))
  })

  it('takes the dashboard from the host, never from the message', async () => {
    const frame = mountFrame()
    const ask = vi.spyOn(api, 'crewBoardOpen').mockResolvedValue({ ok: true, action: 'open', item_id: ITEM, slot: WORKER_SLOT })
    await renderHookWithProviders(() => useFrameOpenSession([{ current: frame }], SESSION, vi.fn(), { open: vi.fn(), gesture: () => true }))
    post(
      { type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM, dashboard: { kind: 'session', slot: 'chat-other' }, slot: WORKER_SLOT, conductor: 'x' },
      frame.contentWindow,
    )
    await waitFor(() => expect(ask).toHaveBeenCalledTimes(1))
    expect(ask).toHaveBeenCalledWith(SESSION, ITEM)
  })

  it('navigates to /chat and switches to the slot the gateway named, the way a session link does', async () => {
    const frame = mountFrame()
    vi.spyOn(api, 'crewBoardOpen').mockResolvedValue({ ok: true, action: 'open', item_id: ITEM, slot: WORKER_SLOT })
    // The in-app session-link path raises the window first; jsdom has no window.focus.
    vi.spyOn(window, 'focus').mockImplementation(() => {})
    const store = createTestStore()
    const dispatch = vi.spyOn(store, 'dispatch')
    const { result } = await renderHookWithProviders(
      () => {
        useFrameOpenSession([{ current: frame }], SESSION, vi.fn(), { gesture: () => true })
        return useLocation()
      },
      { store, route: '/members' },
    )
    expect(result.current.pathname).toBe('/members')
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    await waitFor(() => expect(result.current.pathname).toBe('/chat'))
    expect(dispatch).toHaveBeenCalledWith({
      type: 'test/switchSlot',
      payload: { key: WORKER_SLOT, announceOnMissing: true },
    })
  })

  it('ignores a valid message from a window that is not its frame', async () => {
    const frame = mountFrame()
    const ask = vi.spyOn(api, 'crewBoardOpen')
    await renderHookWithProviders(() => useFrameOpenSession([{ current: frame }], SESSION, vi.fn(), { open: vi.fn(), gesture: () => true }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, window)
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, mountFrame().contentWindow)
    expect(ask).not.toHaveBeenCalled()
  })

  it('ignores a message when the person made no gesture in the frame', async () => {
    const frame = mountFrame()
    const ask = vi.spyOn(api, 'crewBoardOpen')
    await renderHookWithProviders(() => useFrameOpenSession([{ current: frame }], SESSION, vi.fn(), { open: vi.fn(), gesture: () => false }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    expect(ask).not.toHaveBeenCalled()
  })

  it('ignores an id that is not one the work ledger mints', async () => {
    const frame = mountFrame()
    const ask = vi.spyOn(api, 'crewBoardOpen')
    await renderHookWithProviders(() => useFrameOpenSession([{ current: frame }], SESSION, vi.fn(), { open: vi.fn(), gesture: () => true }))
    for (const item_id of ['', 'it_1', 'chat-4-worker', '../it_3f9a01c2', undefined, 7]) {
      post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id }, frame.contentWindow)
    }
    expect(ask).not.toHaveBeenCalled()
  })

  it('does nothing when the frame has no dashboard to name', async () => {
    const frame = mountFrame()
    const ask = vi.spyOn(api, 'crewBoardOpen')
    await renderHookWithProviders(() => useFrameOpenSession([{ current: frame }], null, vi.fn(), { open: vi.fn(), gesture: () => true }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    expect(ask).not.toHaveBeenCalled()
  })

  it('reports the gateway\'s refusal code, opens nothing and posts nothing into the frame', async () => {
    const frame = mountFrame()
    const open = vi.fn()
    const report = vi.fn()
    vi.spyOn(api, 'crewBoardOpen').mockRejectedValue(refusal(409, 'worker_closed'))
    await renderHookWithProviders(() => useFrameOpenSession([{ current: frame }], SESSION, report, { open, gesture: () => true }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    await waitFor(() => expect(report).toHaveBeenCalledWith('worker_closed'))
    expect(open).not.toHaveBeenCalled()
    expect(frame.contentWindow.postMessage).not.toHaveBeenCalled()
  })

  it('reports a generic failure when the request itself fails', async () => {
    const frame = mountFrame()
    const report = vi.fn()
    vi.spyOn(api, 'crewBoardOpen').mockRejectedValue(new Error('offline'))
    await renderHookWithProviders(() => useFrameOpenSession([{ current: frame }], SESSION, report, { open: vi.fn(), gesture: () => true }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    await waitFor(() => expect(report).toHaveBeenCalledWith('open_failed'))
  })

  it('treats an answer with no slot as a failure rather than navigating', async () => {
    const frame = mountFrame()
    const open = vi.fn()
    const report = vi.fn()
    vi.spyOn(api, 'crewBoardOpen').mockResolvedValue({ ok: true, action: 'open', item_id: ITEM, slot: '' })
    await renderHookWithProviders(() => useFrameOpenSession([{ current: frame }], SESSION, report, { open, gesture: () => true }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    await waitFor(() => expect(report).toHaveBeenCalledWith('open_failed'))
    expect(open).not.toHaveBeenCalled()
  })

  it('sends one request for a double click, and accepts a new click once the first settles', async () => {
    const frame = mountFrame()
    const open = vi.fn()
    let settle: (v: { ok: boolean; action: 'open'; item_id: string; slot: string }) => void = () => {}
    const ask = vi.spyOn(api, 'crewBoardOpen').mockImplementation(
      () => new Promise((resolve) => { settle = resolve }),
    )
    await renderHookWithProviders(() => useFrameOpenSession([{ current: frame }], SESSION, vi.fn(), { open, gesture: () => true }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    expect(ask).toHaveBeenCalledTimes(1)
    await act(async () => {
      settle({ ok: true, action: 'open', item_id: ITEM, slot: WORKER_SLOT })
    })
    await waitFor(() => expect(open).toHaveBeenCalledTimes(1))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    expect(ask).toHaveBeenCalledTimes(2)
  })

  it('stops listening when the host unmounts', async () => {
    const frame = mountFrame()
    const ask = vi.spyOn(api, 'crewBoardOpen')
    const { unmount } = await renderHookWithProviders(() => useFrameOpenSession([{ current: frame }], SESSION, vi.fn(), { open: vi.fn(), gesture: () => true }))
    unmount()
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    expect(ask).not.toHaveBeenCalled()
  })
})
