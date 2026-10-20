import { useCallback, useEffect, useRef, type RefObject } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { apiErrorCode } from '../api/apiError'
import type { CrewBoardOpenDashboard } from '../api/crewBoard'
import { useMayLeaveForNavigation } from '../components/NavigationLeaveGuard'
import { useAppDispatch } from '../store'
import { switchSlot } from '../store/chatSlice'
import { applyNavIntentInMain } from '../utils/navIntent'
import { hasGesture } from './useFrameOpenLink'

/**
 * A sandboxed org-chart page asking its host to open the session behind one worker card.
 *
 * The page posts `{type: 'kirocrew-dashboard:open-session', item_id}` and nothing else.
 * It holds no session key and no board it could name: the fold masks the key, and a board
 * alias is minted per render. So the host asks the gateway to find the item in the
 * dashboard this frame shows (`api.crewBoardOpen`), gets the worker's slot key back, and
 * navigates there itself. The key reaches this hook's JavaScript only. It is never posted
 * into the frame and never appears in the document.
 *
 * The message is honoured only when ALL of these hold, like the pull-request bridge in
 * `useFrameOpenLink`:
 *
 * - it came from one of THIS host's own frames (`event.source`);
 * - `item_id` is exactly the shape the work ledger mints (`it_` and 8 hex digits);
 * - the person just clicked or pressed a key IN that frame (`hasGesture`).
 *
 * A refusal from the gateway (the worker closed, the id is on two boards, a stale card)
 * is reported through `onError` with its code, and nothing is posted back into the frame.
 */
export const OPEN_SESSION_MESSAGE_TYPE = 'kirocrew-dashboard:open-session'

/** The only item ids a page may ask about. Mirrors `work_ledger_store._ITEM_ID_RE`. */
export const ITEM_ID_RE = /^it_[0-9a-f]{8}$/

/** The item id an open-session message asks for, or null when the message is not one this host honours. */
export function openSessionItemOf(data: unknown): string | null {
  if (!data || typeof data !== 'object') return null
  const msg = data as { type?: unknown; item_id?: unknown }
  if (msg.type !== OPEN_SESSION_MESSAGE_TYPE || typeof msg.item_id !== 'string') return null
  return ITEM_ID_RE.test(msg.item_id) ? msg.item_id : null
}

/** What the hook reports to its caller: `null` clears the notice, a string is the refusal code. */
export type OpenSessionReport = (code: string | null) => void

/**
 * Honour open-session messages from the given frames. `open` and `gesture` are injectable
 * for tests only; by default the session opens the way every in-app session link does
 * (`applyNavIntentInMain`: leave check, slot switch, then `/chat`).
 */
export function useFrameOpenSession(
  frames: ReadonlyArray<RefObject<HTMLIFrameElement | null>>,
  dashboard: CrewBoardOpenDashboard | null,
  onReport: OpenSessionReport,
  inject: {
    open?: (slot: string) => void
    gesture?: (frame: HTMLIFrameElement) => boolean
  } = {},
): void {
  const navigate = useNavigate()
  const dispatch = useAppDispatch()
  const mayLeave = useMayLeaveForNavigation()
  const openInMain = useCallback(
    (slot: string) =>
      applyNavIntentInMain(
        // `path` carries no query string, and ChatPage writes `?sid=` back itself once
        // the session is active.
        { path: '/chat', slotKey: slot },
        {
          navigate,
          switchSlot: (key) => {
            void dispatch(switchSlot({ key, announceOnMissing: true }))
          },
          mayLeave,
        },
      ),
    [navigate, dispatch, mayLeave],
  )

  const framesRef = useRef(frames)
  framesRef.current = frames
  const dashboardRef = useRef(dashboard)
  dashboardRef.current = dashboard
  const reportRef = useRef(onReport)
  reportRef.current = onReport
  const openRef = useRef(inject.open ?? openInMain)
  openRef.current = inject.open ?? openInMain
  const gestureRef = useRef(inject.gesture ?? hasGesture)
  gestureRef.current = inject.gesture ?? hasGesture
  /** One request at a time: a second click while the first is out would navigate twice. */
  const pending = useRef(false)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (!event.source) return
      const mine = framesRef.current.find((f) => f.current?.contentWindow === event.source)?.current
      if (!mine) return
      const itemId = openSessionItemOf(event.data)
      if (!itemId || !gestureRef.current(mine)) return
      const asked = dashboardRef.current
      if (!asked || pending.current) return
      pending.current = true
      api
        .crewBoardOpen(asked, itemId)
        .then((answer) => {
          if (!mounted.current) return
          if (!answer || typeof answer.slot !== 'string' || !answer.slot) {
            reportRef.current('open_failed')
            return
          }
          reportRef.current(null)
          openRef.current(answer.slot)
        })
        .catch((err: unknown) => {
          if (mounted.current) reportRef.current(apiErrorCode(err) || 'open_failed')
        })
        .finally(() => {
          pending.current = false
        })
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])
}
