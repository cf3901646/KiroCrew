import { memo, useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, Globe, Loader2 } from 'lucide-react'

import {
  api,
  browserInstallConflictJob,
  type BrowserInstallData,
  type BrowserInstallJob,
} from '../../api/client'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { hasBrowserInstallMarker } from '../../utils/detectBrowserInstallMarker'
import { STAGE_KEY } from '../settings/BrowserInstallStatus'
import type { PendingRequest } from '../settings/browserInstallState'
import { browserInstallCardState } from './browserInstallCardState'

/** Shared with Settings -> Browser, so both read (and poll) one status. */
const INSTALL_KEY = ['browserInstall'] as const

/** Re-read cadence while an install runs; at rest the card does not poll. */
const RUNNING_POLL_MS = 2_000

const BROWSER_SETTINGS_ROUTE = '/settings/browser'

// Same button and link primitives as the redaction cards; touch targets grow
// to 44px on hover-less screens.
const touchTarget = '[@media(hover:none)]:min-h-11'
const btnBase = [
  'inline-flex', 'items-center', 'justify-center', 'whitespace-nowrap', 'rounded-md', 'border', 'bg-card',
  'px-2.5', 'py-[3px]', 'text-[12px]', 'leading-[18px]', 'min-h-7', 'text-card-fg', 'cursor-pointer',
  'hover:border-border-strong', 'hover:bg-bg-hover', 'disabled:cursor-default', 'disabled:opacity-70', touchTarget,
].join(' ')
const btnCls = [btnBase, 'border-border'].join(' ')
const btnPrimaryCls = [btnBase, 'border-accent', 'bg-accent-subtle'].join(' ')
const linkCls = [
  'inline-flex', 'items-center', 'border-0', 'bg-transparent', 'px-0.5', 'py-1', 'min-h-6', 'text-[12px]',
  'text-accent', 'no-underline', 'hover:underline', touchTarget,
].join(' ')

/** Put text in the composer for the reader to send; never sends it. */
function prefillComposer(text: string) {
  window.dispatchEvent(new CustomEvent('mc:prefill-composer', { detail: { text } }))
}

function withConflictJob(job: BrowserInstallJob) {
  return (old: BrowserInstallData | undefined): BrowserInstallData | undefined =>
    old ? { ...old, install_job: job, installing: true } : old
}

/**
 * The in-chat "Install browser support" card, shown under a finished reply that
 * carries `<!-- kirocrew:browser-install -->` (see detectBrowserInstallMarker).
 *
 * The agent writes that marker when its `browser` tool reports that browsing
 * is not set up on this host, so a customer who asked to open a page can fix it
 * where they asked, without knowing Settings -> Browser exists. The card runs
 * the same owner-gated install that page runs, shows its progress, and on
 * success offers to continue: that pre-fills a message in the composer for the
 * reader to send, as every other in-chat suggestion does.
 *
 * Rendering it installs nothing and starts no poll at rest; the status is read
 * once, re-read on window focus, and polled only while an install runs.
 */
const BrowserInstallCard = memo(function BrowserInstallCard({ content }: {
  /** The reply's text as the bubble shows it. */
  content: string
}) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const offered = useMemo(() => hasBrowserInstallMarker(content), [content])
  if (!offered) return null
  return <BrowserInstallCardBody />
})

function BrowserInstallCardBody() {
  const qc = useQueryClient()
  const [continued, setContinued] = useState(false)

  const installMut = useMutation({
    mutationFn: () => api.installBrowserCli(),
    onSuccess: (fresh) => qc.setQueryData(INSTALL_KEY, fresh),
    onError: (e) => {
      const job = browserInstallConflictJob(e)
      if (job) qc.setQueryData(INSTALL_KEY, withConflictJob(job))
    },
    onSettled: () => { void qc.invalidateQueries({ queryKey: INSTALL_KEY }) },
  })
  const pending: PendingRequest | null = installMut.isPending ? { kind: 'cli_setup' } : null

  const q = useQuery<BrowserInstallData>({
    queryKey: INSTALL_KEY,
    queryFn: api.getBrowserInstall,
    refetchOnWindowFocus: true,
    refetchInterval: (query) => {
      const d = query.state.data
      const busy = d?.installing || d?.install_job?.status === 'running' || installMut.isPending
      return busy ? RUNNING_POLL_MS : false
    },
  })

  const state = q.isError && !q.data
    ? { kind: 'unavailable' as const }
    : browserInstallCardState(q.data, pending, installMut.submittedAt)

  // The POST itself was refused (a non-owner 403, a 500, a lost answer). A 409
  // naming the running job is not an error here: the card shows that job.
  const requestError =
    installMut.isError && !browserInstallConflictJob(installMut.error)
      ? installMut.error instanceof Error && installMut.error.message
        ? installMut.error.message
        : i18nT('pages.settings.browserPanel.install_request_failed')
      : null

  const install = () => { setContinued(false); installMut.mutate() }
  const settingsLink = (
    <a href={BROWSER_SETTINGS_ROUTE} className={linkCls} data-testid="browser-install-card-settings">
      {i18nT('pages.chat.browserInstallCard.open_settings')}
    </a>
  )

  let icon = <Globe size={16} className="text-accent shrink-0 mt-[2px]" aria-hidden="true" />
  let title: string
  let body: string | null = null
  let actions: ReactNode = null

  switch (state.kind) {
    case 'checking':
      icon = <Loader2 size={16} className="text-muted shrink-0 mt-[2px] animate-spin" aria-hidden="true" />
      title = i18nT('pages.chat.browserInstallCard.checking')
      break
    case 'unavailable':
      title = i18nT('pages.chat.browserInstallCard.status_unavailable')
      actions = (
        <>
          <button type="button" className={btnCls} onClick={() => void q.refetch()}>
            {i18nT('pages.chat.browserInstallCard.retry')}
          </button>
          {settingsLink}
        </>
      )
      break
    case 'running':
      icon = <Loader2 size={16} className="text-accent shrink-0 mt-[2px] animate-spin" aria-hidden="true" />
      title = i18nT('pages.chat.browserInstallCard.installing')
      body = state.stage && STAGE_KEY[state.stage]
        ? i18nT('pages.settings.browserPanel.stage_line', { stage: i18nT(STAGE_KEY[state.stage]) })
        : null
      break
    case 'installed':
      icon = <CheckCircle2 size={16} className="text-ok shrink-0 mt-[2px]" aria-hidden="true" />
      title = i18nT('pages.chat.browserInstallCard.installed')
      body = continued ? i18nT('pages.chat.browserInstallCard.continue_prefilled') : null
      actions = (
        <button
          type="button"
          className={btnPrimaryCls}
          data-testid="browser-install-card-continue"
          onClick={() => {
            prefillComposer(i18nT('pages.chat.browserInstallCard.continue_prompt'))
            setContinued(true)
          }}
        >
          {i18nT('pages.chat.browserInstallCard.continue')}
        </button>
      )
      break
    case 'failed':
      icon = <AlertTriangle size={16} className="text-danger shrink-0 mt-[2px]" aria-hidden="true" />
      title = i18nT('pages.chat.browserInstallCard.failed')
      body = state.detail || i18nT('pages.settings.browserPanel.failure_no_detail')
      actions = (
        <>
          <button type="button" className={btnPrimaryCls} onClick={install} data-testid="browser-install-card-install">
            {i18nT('pages.chat.browserInstallCard.retry')}
          </button>
          {settingsLink}
        </>
      )
      break
    case 'needed':
      title = i18nT('pages.chat.browserInstallCard.needed_title')
      body = i18nT('pages.chat.browserInstallCard.needed_body')
      actions = (
        <>
          <button type="button" className={btnPrimaryCls} onClick={install} data-testid="browser-install-card-install">
            {i18nT('pages.chat.browserInstallCard.install')}
          </button>
          {settingsLink}
        </>
      )
      break
  }

  return (
    <section
      aria-label={i18nT('pages.chat.browserInstallCard.region_label')}
      className="ft-block-reveal mt-2 mb-1.5 max-w-[520px] rounded-md border border-border bg-card text-card-fg px-3 py-2.5 flex flex-col gap-2"
      data-testid="browser-install-card"
      data-state={state.kind}
    >
      {/* One live region: stage changes and the outcome are announced, the
          buttons are not. */}
      <div role="status" className="flex items-start gap-2">
        {icon}
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-medium">{title}</div>
          {body && (
            <div
              className={
                state.kind === 'failed'
                  ? 'text-[12px] text-muted font-mono whitespace-pre-wrap [overflow-wrap:anywhere]'
                  : 'text-[13px] text-muted'
              }
            >
              {body}
            </div>
          )}
        </div>
      </div>
      {requestError && (
        <div className="text-[12px] text-danger pl-6" role="alert" data-testid="browser-install-card-error">
          {requestError}
        </div>
      )}
      {actions && <div className="flex flex-wrap items-center gap-2 pl-6">{actions}</div>}
    </section>
  )
}

export default BrowserInstallCard
