import { useId, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Trans, useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { Check, ChevronRight, CircleDashed, ExternalLink, Lock, Minus, Plus, Trash2 } from 'lucide-react'
import { AnimatePresence, motion } from 'framer-motion'
import type { ReactNode } from 'react'

import { api, type SessionEnvironmentsCheck } from '../../api/client'
import { AGENT_SANDBOX_PATH } from '../overview/agentSandboxLink'
import { fmtDateTime } from '../../i18n/format'
import { reportForError } from '../../utils/errorReport'
import { safeHttpUrl } from '../../lib/safeUrl'
import Clickable from '../../components/Clickable'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsField, SettingsInput, SettingsSelect, SettingsToggle } from '../../components/settings'
import { Btn, PanelSectionHeader } from '../../components/ui'

interface EnvironmentProfile {
  id: string
  name: string
  template: string
  workdir: string
  /** False for a template whose stop destroys its files. Absent means true:
   * the card writes it only once toggled, so an older gateway never sees it. */
  compute_survives_stop?: boolean
}
/** How often the card re-reads the workspace list. */
const WORKSPACES_POLL_MS = 15_000
type ProfileTextKey = 'id' | 'name' | 'template' | 'workdir'
interface EnvironmentSettings {
  coder_url: string
  profiles: EnvironmentProfile[]
  /** Profile id new chats start on; "" is this machine. Older gateways omit it. */
  default_profile?: string
  /** Stop every remote environment when the gateway shuts down. Older gateways omit it. */
  stop_on_shutdown?: boolean
}
/** A removed profile cannot stay the default (the gateway refuses that save),
 * so removing the default falls back to this machine. */
function withoutDefault(next: EnvironmentSettings, removedId: string): EnvironmentSettings {
  return next.default_profile === removedId ? { ...next, default_profile: '' } : next
}
interface ConfigResponse {
  session_environments?: EnvironmentSettings
}

const SETUP_DOCS = 'https://coder.com/docs/admin/setup'

/** One saved profile as a disclosure; collapsing loses nothing (values live
 * in the parent draft). */
function ProfileDisclosure({ title, subtitle, open, onToggle, children }: {
  title: string
  subtitle?: string
  open: boolean
  onToggle: () => void
  children: ReactNode
}) {
  const bodyId = useId()
  return (
    <div className="min-w-0 rounded-lg border border-border">
      <Clickable onClick={onToggle} aria-expanded={open} aria-controls={bodyId}
        className="group flex min-w-0 items-center gap-2.5 rounded-lg px-3 py-2.5 cursor-pointer hover:bg-bg-hover/40 transition-colors">
        <motion.span className="flex shrink-0 items-center text-muted group-hover:text-text"
          animate={{ rotate: open ? 90 : 0 }} transition={{ type: 'spring', damping: 22, stiffness: 300 }}>
          <ChevronRight className="lucide-inline" aria-hidden="true" />
        </motion.span>
        <span className="truncate text-[13px] font-semibold text-text">{title}</span>
        {subtitle && <span className="truncate text-[12px] text-muted">{subtitle}</span>}
      </Clickable>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div key="body" id={bodyId}
            initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }} exit={{ height: 0, opacity: 0 }}
            transition={{ type: 'spring', damping: 26, stiffness: 280, mass: 0.7 }} style={{ overflow: 'hidden' }}>
            <fieldset aria-label={title} className="min-w-0 space-y-3 px-3 pb-3">{children}</fieldset>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

/** How a check line's glyph reads. Only `ok` is coloured: a git host is
 * optional, so a signed-out or withheld one is never shown as a failure (the
 * blocking Coder and template failures render through ErrorNotice instead).
 * Each non-ok state has a glyph of its own; the text carries the meaning.
 * Signed-out is a dashed ring, a state rather than an arrow or button shape,
 * since the line itself is not something to click. */
type CheckLineStatus = 'ok' | 'signIn' | 'withheld' | 'unknown'
const CHECK_LINE_ICONS = { ok: Check, signIn: CircleDashed, withheld: Lock, unknown: Minus } as const

/** One check line: a status glyph and its text, announced as a list item. */
function CheckLine({ status, children }: { status: CheckLineStatus; children: ReactNode }) {
  const Icon = CHECK_LINE_ICONS[status]
  const tone = status === 'ok' ? 'text-ok' : 'text-muted'
  return (
    <li className="flex min-w-0 items-start gap-2 text-[13px] text-text">
      <Icon size={14} className={`mt-0.5 shrink-0 ${tone}`} aria-hidden="true" />
      <span className="min-w-0 break-words">{children}</span>
    </li>
  )
}

/** Display names for git hosts whose brand is a do-not-translate term; any
 * other host is shown as its host name. */
const GIT_HOST_NAMES: Record<string, string> = {
  'github.com': 'GitHub',
  'gitlab.com': 'GitLab',
  'bitbucket.org': 'Bitbucket',
  'dev.azure.com': 'Azure DevOps',
}

/** The gateway-machine command that signs each git host in. A host with no
 * CLI here (Bitbucket) falls back to git's credential helper and is reported
 * unchecked, never signed out. Commands, not prose: never translated. */
const GIT_SIGN_IN_COMMANDS: Record<string, string> = {
  'github.com': 'gh auth login',
  'gitlab.com': 'glab auth login',
  'dev.azure.com': 'az login',
}

/** What the readiness check found, in the order a person fixes things. */
function CheckResults({ result, profiles }: {
  result: SessionEnvironmentsCheck
  profiles: EnvironmentProfile[]
}) {
  const { t } = useTranslation()
  const names = new Map(profiles.map(p => [p.id, p]))
  // A gateway that predates the git rows sends no `git` at all.
  const git = Object.entries(result.git ?? {})
  // The sandbox tier lives on Developer → Config; link there only when it is
  // what withheld a login, since otherwise nothing here is the sandbox's doing.
  const withheld = git.some(([, status]) => status === 'withheld')
  // 'strict' withholds every host alike, so one line says it once instead of
  // repeating the same sentence per host.
  const allWithheld = withheld && git.every(([, status]) => status === 'withheld')
  return (
    <>
    <ul aria-label={t('sessionEnvironments.checkResults')} className="space-y-1.5">
      {result.coder.ok ? (
        <CheckLine status="ok">{t('sessionEnvironments.checkCoderOk', { owner: result.coder.owner ?? '' })}</CheckLine>
      ) : (
        <li><ErrorNotice variant="inline" message={t('sessionEnvironments.checkCoderFailed', { error: result.coder.error ?? '' })} askAgent /></li>
      )}
      {/* No sign-in, no template check, whatever a row says. */}
      {!result.coder.ok ? profiles.map(p => (
        <li key={p.id} className="flex min-w-0 items-start gap-2 text-[13px] text-muted">
          <Minus size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
          {t('sessionEnvironments.checkTemplateNotChecked', { name: p.name || p.id })}
        </li>
      )) : result.profiles.map(row => {
        const profile = names.get(row.id)
        const name = profile?.name || row.id
        return row.ok ? (
          <CheckLine key={row.id} status="ok">
            {t('sessionEnvironments.checkTemplateOk', { name, template: profile?.template ?? '' })}
          </CheckLine>
        ) : (
          <li key={row.id}><ErrorNotice variant="inline" message={t('sessionEnvironments.checkTemplateFailed', { name, error: row.error ?? '' })} askAgent /></li>
        )
      })}
      {allWithheld && <CheckLine status="withheld">{t('sessionEnvironments.checkGitWithheldAll')}</CheckLine>}
      {!allWithheld && git.map(([host, status]) => {
        const name = GIT_HOST_NAMES[host] ?? host
        // Signed out names a sign-in command; a host with none reads as unchecked.
        const signedOut = status === 'not_signed_in' && !!GIT_SIGN_IN_COMMANDS[host]
        return (
          <CheckLine key={host} status={status === 'signed_in' ? 'ok' : signedOut ? 'signIn' : status === 'withheld' ? 'withheld' : 'unknown'}>
            {status === 'signed_in'
              ? t('sessionEnvironments.checkGitSignedIn', { name })
              : signedOut
                ? (
                  <Trans
                    i18nKey="sessionEnvironments.checkGitSignedOut"
                    values={{ name, command: GIT_SIGN_IN_COMMANDS[host] }}
                    components={{ code: <code className="rounded bg-bg-elevated px-1 font-mono text-[12px] text-text" translate="no" /> }}
                  />
                )
                : status === 'withheld'
                  ? t('sessionEnvironments.checkGitWithheld', { name })
                  : t('sessionEnvironments.checkGitUnchecked', { name })}
          </CheckLine>
        )
      })}
    </ul>
    {withheld && (
      <p className="text-[13px]">
        <Link to={AGENT_SANDBOX_PATH} className="inline-flex min-w-0 items-center gap-0.5 rounded text-accent hover:underline focus-ring">
          <span className="min-w-0">{t('sessionEnvironments.checkGitSandboxLink')}</span>
          <ChevronRight size={13} aria-hidden="true" className="shrink-0" />
        </Link>
      </p>
    )}
    </>
  )
}

/** Due today or earlier (an unreadable date is not). */
function dueByToday(iso: string): boolean {
  const endOfToday = new Date()
  endOfToday.setHours(23, 59, 59, 999)
  return new Date(iso).getTime() <= endOfToday.getTime()
}

/** The workspaces Kiro Crew owns, from the gateway's registry (no Coder call). */
function WorkspaceList() {
  const { t } = useTranslation()
  const query = useQuery({
    queryKey: ['sessionEnvironmentsWorkspaces'],
    queryFn: () => api.sessionEnvironmentsWorkspaces(),
    refetchInterval: WORKSPACES_POLL_MS,
    retry: false,
  })
  if (query.isPending) return null
  if (query.isError) return <ErrorNotice askAgent message={t('sessionEnvironments.workspacesError')} />
  const rows = query.data.workspaces
  if (!rows.length) return <p className="text-[13px] text-muted">{t('sessionEnvironments.workspacesNone')}</p>
  return (
    <ul aria-label={t('sessionEnvironments.workspaces')} className="divide-y divide-border rounded-lg border border-border">
      {rows.map(row => (
        <li key={row.workspace} className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-0.5 px-3 py-2 text-[13px]">
          <span className="truncate font-mono text-[12px] text-text">{row.workspace}</span>
          {/* An archived session's workspace is not in use: its due date says so. */}
          {row.state !== 'deleting' && !row.deletion_due_at && (
            <span className="text-muted">{t('sessionEnvironments.workspaceSession')}</span>
          )}
          {(row.state === 'deleting' || row.deletion_due_at) && (
            <span className="ml-auto text-[12px] text-muted">
              {row.state === 'deleting'
                ? t('sessionEnvironments.workspaceDeleting')
                : dueByToday(row.deletion_due_at)
                  ? t('sessionEnvironments.workspaceDeletesSoon')
                  : t('sessionEnvironments.workspaceDeletes', { when: fmtDateTime(row.deletion_due_at) })}
            </span>
          )}
        </li>
      ))}
    </ul>
  )
}

/** Saving starts no compute. The preview switch is indexed in
 * FeaturePreviewsSection; the URL setting is searchable only after opting in. */
export function SessionEnvironmentsCard({ enabled }: { enabled: boolean }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const controlId = useId()
  const query = useQuery<ConfigResponse>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    enabled,
  })
  // Null follows fresh server data. Once edited, background refetches never
  // replace the draft. Saving freezes controls until the server responds.
  const [draft, setDraft] = useState<EnvironmentSettings | null>(null)
  const [saved, setSaved] = useState(false)
  // Removal has no confirm: the draft keeps it undoable until Save.
  const [removed, setRemoved] = useState(false)
  // Saved profiles start collapsed; a newly added one opens so it can be
  // filled in. Tracked by row index, so add/remove keep it aligned.
  const [openRows, setOpenRows] = useState<boolean[]>([])
  const toggleRow = (index: number) => setOpenRows(rows => {
    const next = [...rows]
    next[index] = !next[index]
    return next
  })
  const addRow = (index: number) => setOpenRows(rows => {
    const next = [...rows]
    next[index] = true
    return next
  })
  const removeRow = (index: number) => setOpenRows(rows => rows.filter((_, i) => i !== index))
  const mutation = useMutation({
    mutationFn: (value: EnvironmentSettings) => api.patchConfig('session_environments', value) as Promise<ConfigResponse>,
    onMutate: () => qc.cancelQueries({ queryKey: ['kirocrewConfig'] }),
    onSuccess: response => {
      qc.setQueryData(['kirocrewConfig'], response)
      setDraft(null)
      setSaved(true)
      setRemoved(false)
    },
  })
  const config = draft ?? query.data?.session_environments
  // The check reads the SAVED configuration on the gateway, so it is offered
  // only with no unsaved draft, and its answer is dropped when one appears.
  const savedCoderUrl = safeHttpUrl(query.data?.session_environments?.coder_url ?? '')
  const savedProfiles = query.data?.session_environments?.profiles ?? []
  const checkMutation = useMutation({ mutationFn: () => api.sessionEnvironmentsCheck() })
  const change = (next: EnvironmentSettings) => {
    setDraft(next)
    setSaved(false)
    mutation.reset()
  }

  if (!enabled && !draft) return null
  if (query.isPending) return <p role="status" className="text-[13px] text-muted">{t('sessionEnvironments.loading')}</p>
  if (query.isError && !query.data) return (
    <ErrorNotice message={t('sessionEnvironments.loadError')} askAgent footer={
      <Btn onClick={() => void query.refetch()}>{t('sessionEnvironments.retry')}</Btn>
    } />
  )
  // An older gateway has no section: never show a form that cannot save.
  if (!config) return <ErrorNotice message={t('sessionEnvironments.unavailable')} askAgent />

  const profileFields: { key: ProfileTextKey; label: string; description?: string }[] = [
    { key: 'id', label: t('sessionEnvironments.id'), description: t('sessionEnvironments.idHint') },
    { key: 'name', label: t('sessionEnvironments.name'), description: t('sessionEnvironments.nameHint') },
    { key: 'template', label: t('sessionEnvironments.template'), description: t('sessionEnvironments.templateHint') },
    { key: 'workdir', label: t('sessionEnvironments.workdir'), description: t('sessionEnvironments.workdirHint') },
  ]
  // Per-profile like the fields above, so not a searchable settings row.
  const survivesStopLabel = t('sessionEnvironments.computeSurvivesStop')
  const updateProfile = (index: number, field: ProfileTextKey, value: string) => {
    const old = config.profiles[index]?.id
    const current = config.default_profile
    // Renaming the default's id carries the default along: a dangling one is
    // refused on save. A rename that passes through an empty id keeps the old
    // default until the row has an id again, so retyping it does not drop it.
    const orphaned = !!current && !config.profiles.some(profile => profile.id === current)
    const follows = field === 'id' && !!value && (old ? old === current : orphaned)
    change({
      ...config,
      ...(follows ? { default_profile: value } : {}),
      profiles: config.profiles.map((profile, i) => i === index ? { ...profile, [field]: value } : profile),
    })
  }
  const addProfile = () => {
    let n = 1
    while (config.profiles.some(profile => profile.id === `profile-${n}`)) n++
    addRow(config.profiles.length)
    change({ ...config, profiles: [...config.profiles, { id: `profile-${n}`, name: '', template: '', workdir: '' }] })
  }

  return (
    <form className="min-w-0 space-y-3 pt-3" onSubmit={event => {
      event.preventDefault()
      if (draft && !mutation.isPending) mutation.mutate(draft)
    }}>
      {/* No hand-off: navigating would discard the unsaved environment draft. */}
      <ErrorNotice message={query.isError ? t('sessionEnvironments.loadError') : null} footer={
        <Btn type="button" onClick={() => void query.refetch()}>{t('sessionEnvironments.retry')}</Btn>
      } />
      <section aria-label={t('sessionEnvironments.configureStep')} className="space-y-3 pt-3">
      <PanelSectionHeader label={t('sessionEnvironments.configureStep')} />
      <details className="rounded-lg border border-border p-3">
        <summary className="cursor-pointer text-[13px] font-semibold text-text">{t('sessionEnvironments.setup')}</summary>
        <ol className="list-decimal pl-5 space-y-2 pt-3 text-[13px] text-muted">
          <li>{t('sessionEnvironments.coderSetup1')}</li>
          <li>{t('sessionEnvironments.coderSetup2')}</li>
          <li>{t('sessionEnvironments.coderSetup3')}</li>
        </ol>
        <a className="inline-block pt-3 text-[13px] text-accent underline" target="_blank" rel="noopener noreferrer"
          href={SETUP_DOCS}>
          {t('sessionEnvironments.setupDocs')}
        </a>
      </details>
      <fieldset disabled={mutation.isPending} className="min-w-0 space-y-3">
        <SettingsInput
          configKey="session_environments.coder_url"
          label={t('sessionEnvironments.url')}
          description={t('sessionEnvironments.urlHint')}
          value={config.coder_url}
          onChange={value => change({ ...config, coder_url: value })}
        />
        <SettingsField configKey="session_environments.profiles" label={t('sessionEnvironments.profiles')}
          description={t('sessionEnvironments.profilesCoderHint')}>
          <div className="min-w-0 space-y-3">
            {config.profiles.map((profile, index) => (
              // Index stays stable while the id is edited; removal is explicit.
              <ProfileDisclosure key={index} title={profile.name || profile.id} subtitle={profile.template}
                open={!!openRows[index]} onToggle={() => toggleRow(index)}>
                {profileFields.map(field => (
                  <SettingsInput key={field.key} label={field.label} description={field.description} value={profile[field.key]} onChange={value => updateProfile(index, field.key, value)} />
                ))}
                <SettingsToggle anchor={false} label={survivesStopLabel}
                  description={t('sessionEnvironments.computeSurvivesStopHint')}
                  checked={profile.compute_survives_stop !== false}
                  onChange={on => change({ ...config, profiles: config.profiles.map((p, i) => i === index ? { ...p, compute_survives_stop: on } : p) })} />
                <Btn type="button" onClick={() => {
                  removeRow(index)
                  setRemoved(true)
                  change(withoutDefault({ ...config, profiles: config.profiles.filter((_, i) => i !== index) }, profile.id))
                }}>
                  <Trash2 size={14} aria-hidden="true" />{t('sessionEnvironments.remove')}
                </Btn>
              </ProfileDisclosure>
            ))}
            <Btn type="button" disabled={config.profiles.length >= 20} onClick={addProfile}>
              <Plus size={14} aria-hidden="true" />{t('sessionEnvironments.add')}
            </Btn>
          </div>
        </SettingsField>
      </fieldset>
      </section>
      <section aria-label={t('sessionEnvironments.verifyStep')} className="space-y-3 pt-3">
        <PanelSectionHeader label={t('sessionEnvironments.verifyStep')} />
        <p id={`${controlId}-verification`} className="text-[13px] text-muted">
          {draft ? t('sessionEnvironments.checkSaveFirst') : t('sessionEnvironments.verifyHint')}
        </p>
        <Btn type="button" disabled={!!draft || !savedCoderUrl || checkMutation.isPending}
          aria-describedby={`${controlId}-verification`} onClick={() => checkMutation.mutate()}>
          {checkMutation.isPending ? t('sessionEnvironments.checkRunning') : t('sessionEnvironments.check')}
        </Btn>
        {checkMutation.data && !draft && <CheckResults result={checkMutation.data} profiles={savedProfiles} />}
        <ErrorNotice askAgent message={checkMutation.isError ? t('sessionEnvironments.checkError') : null} />
      </section>
      <section aria-label={t('sessionEnvironments.manageStep')} className="space-y-3 pt-3">
        <PanelSectionHeader label={t('sessionEnvironments.manageStep')} />
        <p className="text-[13px] text-muted">{t('sessionEnvironments.managementHint')}</p>
        {savedCoderUrl && <WorkspaceList />}
        {savedCoderUrl && (
          <a href={savedCoderUrl} target="_blank" rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-[13px] text-accent hover:underline">
            {t('sessionEnvironments.openCoder')}<ExternalLink size={12} className="lucide-inline" aria-hidden="true" />
          </a>
        )}
      </section>
      {/* Global: these apply to every profile. They follow the numbered
        * steps, which a first setup walks in order. Older gateways omit
        * either key, and then its control stays hidden. */}
      {(config.default_profile !== undefined || config.stop_on_shutdown !== undefined) && (() => {
        const all = config.profiles.filter(p => p.id)
        const defaultOn = !!config.default_profile
        return (
          <section aria-label={t('sessionEnvironments.globalDefaults')} className="space-y-3 pt-3">
            <PanelSectionHeader label={t('sessionEnvironments.globalDefaults')} />
            <fieldset disabled={mutation.isPending} className="min-w-0 space-y-3">
              {config.default_profile !== undefined && <>
                <SettingsToggle configKey="session_environments.default_profile"
                  label={t('sessionEnvironments.defaultEnabled')}
                  description={all.length ? t('sessionEnvironments.defaultEnabledHint') : t('sessionEnvironments.defaultEnabledNoProfiles')}
                  checked={defaultOn} disabled={!all.length && !defaultOn}
                  onChange={on => change({ ...config, default_profile: on ? all[0]?.id ?? '' : '' })} />
                {defaultOn && (
                  <SettingsSelect label={t('sessionEnvironments.defaultProfile')}
                    description={t('sessionEnvironments.defaultProfileHint')}
                    value={config.default_profile ?? ''}
                    options={all.map(p => p.id)}
                    optionLabels={all.map(p => p.name || p.id)}
                    // Never ''. The picker reports '' when its value briefly
                    // leaves the options (a rename); that is not a choice.
                    onChange={value => { if (value) change({ ...config, default_profile: value }) }} />
                )}
              </>}
              {config.stop_on_shutdown !== undefined && (
                <SettingsToggle configKey="session_environments.stop_on_shutdown"
                  label={t('sessionEnvironments.stopOnShutdown')}
                  description={t('sessionEnvironments.stopOnShutdownHint')}
                  checked={!!all.length && config.stop_on_shutdown} disabled={!all.length}
                  onChange={on => change({ ...config, stop_on_shutdown: on })} />
              )}
            </fieldset>
          </section>
        )
      })()}
      {/* Last, after the defaults, and stuck to the bottom of the panel while
        * there are unsaved edits, so Save stays in view from every section. */}
      <div data-testid="session-environments-save-bar"
        className={`flex flex-wrap items-center gap-2 ${draft ? 'sticky bottom-0 z-10 -mx-1 border-t border-border bg-bg px-1 py-2' : ''}`}>
        <Btn type="submit" primary={!!draft} disabled={!draft || mutation.isPending}>{t('sessionEnvironments.save')}</Btn>
        <Btn type="button" disabled={!draft || mutation.isPending} onClick={() => {
          setDraft(null)
          setSaved(false)
          setRemoved(false)
          setOpenRows([])
          mutation.reset()
        }}>{t('sessionEnvironments.discard')}</Btn>
        {(saved || (removed && draft)) && <span role="status" className="text-[13px] text-muted">
          {saved ? t('sessionEnvironments.saved') : t('sessionEnvironments.removed')}</span>}
      </div>
      {/* No hand-off: navigating would discard the unsaved environment draft.
        * The server's detail names the field it refused. */}
      <ErrorNotice report={reportForError(mutation.error)} message={mutation.isError
        ? t('sessionEnvironments.saveError', { detail: mutation.error.message === 'coder_url must not include a query string'
          ? t('sessionEnvironments.urlQueryError')
          : mutation.error.message || t('pages.settings.instancesPanel.unknown_error') })
        : null} />
    </form>
  )
}
