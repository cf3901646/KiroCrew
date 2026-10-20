---
title: A Dashboard card can send its reply in one click
status: draft
author: chenmingwei23
created: 2026-10-20
last-audited: 2026-10-20
audited-at: cb756d7256
doc-pr: 22379
implementation-prs: [22379]
tracking-issues: [22367]
supersedes: []
superseded-by: []
---

# RFC: A Dashboard Card Can Send Its Reply in One Click

> **Status:** `draft`. Acceptance is requested from a maintainer; the status
> flips to `accepted` when one records it in section 7 with the date. Verified at
> `cb756d7256`: in `website/src/pages/members/CrewDynamicDashboard.tsx`, a page's
> `kirocrew-dashboard:act` message is validated by `actText` and handed to `onAct`,
> which `MembersPageSidePanel.tsx` implements as `suggestPaneDraft` plus
> `revealComposer`. Nothing a page can post sends a turn. The implementation ships
> in the same pull request as this document. This document lands first in review
> because it adds a message to the page-to-host contract and lets a page cause a
> user-role turn, and the First Principles lane reads that decision from the base
> branch.

## 1. Summary

A button the crewmate's page draws can ask the host to send its reply into that
crewmate's own thread. Today the same button can only put the reply in the chat
box, and the person then switches to the chat and presses send. The new message,
`kirocrew-dashboard:send {text}`, is a request, not a command. The host sends
only after a real click inside the page, only into the thread of the tab the page
is shown in, and the gateway refuses it in every state where a person's typed
words would mean more than the page's. Every refusal falls back to today's
behaviour, so a reply is never lost.

## 2. Background

A Dashboard page is HTML the crewmate wrote. It runs in a frame with
`allow-scripts` and nothing else, on an opaque origin, with `connect-src 'none'`.
Its only channels to the host are postMessage types. Three are in use:

- `kirocrew-dashboard:ready` and `kirocrew-dashboard:refetch`, which carry no
  user content (`dashboard_frame.READY_MESSAGE_TYPE`, `REFETCH_MESSAGE_TYPE`).
- `kirocrew-dashboard:act {text}`, which the host turns into a composer draft.
- `kirocrew-dashboard:open {url}`, which opens one GitHub pull-request URL, and
  only with a live user activation (`useFrameOpenLink`).

`act` is fill-only because a page that could speak as the person would let a
crewmate answer for them. That line is correct and this RFC keeps it: `send` does
not make the page the person. It lets the person's click, which the page cannot
fake, carry the page's words into the thread, tagged as the page's.

For the cards the built-in pages draw (a needs-you row with offered answers, a
review row with two buttons), the second step adds nothing: the person chose the
reply by clicking it. Issue
[#22367](https://github.com/kirodotdev/KiroCrew/issues/22367) asked for the click
to send.

## 3. Design

### 3.1 The message

`{type: 'kirocrew-dashboard:send', text}`. The text rules are `act`'s: trimmed,
at most 600 characters, no control character except a newline. The type is
exported as `dashboard_frame.SEND_MESSAGE_TYPE` (and `ACT_MESSAGE_TYPE`, which the
frame did not export before), pinned to the tab's literals by a test. The message
names no target, no session and no agent.

### 3.2 What the host does

The host sends only when all of these hold, and otherwise behaves as `act`:

- The message comes from the frame's own window (as `act` already requires).
- The click was inside the page: the browser reports transient user activation
  (`navigator.userActivation.isActive`) AND the page's frame holds focus
  (`hasGesture`, the test the pull-request link bridge already uses). A script in
  the page can post at any time but cannot make either true; the browser sets them
  from real input, and a click on the composer or the sidebar activates the window
  without focusing the frame. Both propagate from the sandboxed child frame to the
  host document, which was checked in Chromium. A browser without the API fills the
  composer. A real click anywhere in the page arms its sends for the activation
  window (a few seconds), so the rate limit below is what bounds a hostile page in
  that window.
- The Dashboard tab passes `onSend`. It binds the tab's confirmed thread slot
  (`activeSlot`). The session Overview passes none and so treats `send` as `act`.
- No send went out in the last 3 seconds and none is in flight. A second message in
  that window is dropped.

The send uses the composer's own transport (`sendTurn`) with `origin:
"dashboard_card"` in the body and a client send id, so the row is echoed to the tab.
A refusal (HTTP 4xx) or a request that never left fills the composer instead. A late
or unreadable answer does not, because the turn may have started.

### 3.3 What the gateway does

`POST /api/chat` with `origin: "dashboard_card"` runs `card_send.card_send_refusal`
before the slot is looked up for creation, so a refused send leaves no trace. It is
refused unless:

- The caller is the owner's own dashboard. An app token, a bare internal key and a
  non-owner are refused.
- The target is an existing crewmate DM thread whose binding matches its member.
- The crewmate's `dashboard_card_send` setting is on (section 3.5).
- The text obeys the rules in 3.1, re-checked on the server.
- The thread has no open decision: no pending tool approval future, no permission
  prompt the client is waiting on, no open question card (blocking or not), no
  question parked in the gateway for that slot.
- The thread is idle: no running or reserved turn, nothing queued, no sub-agent
  hold. A card send never queues and never steers.
- No card send went through on the thread in the last 3 seconds.

Anything else in the body (agent, theme, steer, responsive, files, quote, meta) is
dropped. Only the client send id survives.

### 3.4 What the turn is

The text is `[Dashboard card] <text>`. The tag is the first word of what the agent
receives and of what the transcript shows, so it cannot be mistaken for typed
text, and a leading `/clear` or `/new` is not a command. The row carries
`meta.cardOrigin: true`, a key a caller's own meta cannot write
(`RESERVED_ROW_META_KEYS`), and `meta.human: true`, because a person clicked.

The turn does not carry a person's authority over session directives
(`_directive_user_origin` is false), and it does not mark the thread as attended by
a human.

### 3.5 The Profile switch

`dashboard_card_send` on the crewmate's record, default on, read as on when absent
or not a bool. The Profile tab has one labelled switch beside the layout lock. Off,
every card send is refused with `card_send_off` and the host fills the composer.
The setting is written through the existing `PUT /api/agents/{name}`, which refuses
a non-bool.

### 3.6 A card turn is never an approval

Approvals resolve only on their own id-keyed routes: `POST /api/approvals/{id}/{action}`,
`POST /api/chat/slots/{slot}/approve` and `POST /api/ask-question/{ask_id}/answer`.
None reads chat text. Two side effects of a user turn could still look like an
answer, and both are closed by refusing the send while a decision is open:

- A live user row retires every non-blocking question card (`_ChatSlot.append`),
  which is how a typed card answer is consumed.
- A turn start marks unresolved permission rows stale (`_sweep_stale_permissions`).

The host also never calls the composer's "retire the waiting ask after a send" path
for a card send.

## 4. Case against

A page can now cause a user-role turn without the person typing. The activation
requirement means a page needs a real click to do it, but a page can draw a button
that looks like something else, and the person's click is then on the page's
words. What limits the harm is that the words arrive tagged, with less authority
than typed words, never as an approval, only into the crewmate that wrote the page
(which the person already trusts to act), at most once per 3 seconds, and with a
switch that turns it off.

The 3 second interval, the 600 character bound and the `[Dashboard card]` tag are
chosen, not derived. The bound is `act`'s existing bound; the interval is long
enough to absorb a double click and short enough not to feel dead.

## 5. Alternatives considered

Keep fill-only. Safe and unchanged, and the one-click case stays two steps.

Let the page name the target session. Rejected: a page would then write into any
session.

Treat a card turn like a typed turn. Rejected: a page could then answer an open
question card or steer session directives as the person.

Queue or steer a card turn behind a running turn. Rejected for this change: it
adds three more delivery paths to audit, and a card is normally shown while the
crewmate waits on the person. A busy thread falls back to the composer.

A per-card confirmation dialog. Rejected: it restores the second step this change
removes.

## 6. Compatibility

Compatible. No existing page changes: the built-in pages post `act` and keep
filling the composer. The host and the gateway ship in one build (the gateway
serves the SPA), so the host never sends `origin` to a gateway that ignores it.
`dashboard_card_send` is absent on every existing record and reads as on, which
changes nothing until a page posts `send`.

## 7. Acceptance

Proposed 2026-10-20 by chenmingwei23. Not yet accepted. A maintainer accepting
this document is recorded here with their name and the date, and the status
above moves to `accepted` in the same change.
