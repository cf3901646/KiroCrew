"""The channel's context bar: repo, branch, PR, the channel actions, the workers.

Slack keeps at most five items, keyed uniquely; ``context_bar_items`` is a FULL
replacement on every ``setProperties``, so the whole list is rebuilt and resent
each time. Keys follow Slack's documented conventions (``repo``, ``branch``,
``pr``); icons come from its fixed set; a key is at most 64 characters, a label
128 and a url 2048 (`Code channel properties`_).

An item is either a LINK (``url``) or an ACTION (``item_type: "action"``, no
url): Slack saves an item carrying both as plain info and warns, so
:func:`_action` never sets a url. A click on an action reaches the app as a
``code_channel_action`` event (:mod:`..actions`).

**Which five.** The bar shows, in this order, the first five that apply:

1. ``repo`` (the folder, without git)
2. ``branch``
3. ``pr`` -- the PR and its CI in one label (``PR #42 is open · tests
   passing``), linked to the PR; with no PR yet and a channel that can open one,
   the ``create-pr`` action takes this slot instead
4. ``stop`` -- the Stop action (the same stop as Slack's own Stop button)
5. one worker slot per remaining slot for a conductor-led channel
   (:func:`..board.render.worker_bar_items`: its aggregate line when only one
   slot is left)
6. the agent's own items (``code_channel_set_context_bar``)
7. ``dashboard`` -- Open in dashboard, a plain link (never a sign-in token), in
   a slot nothing above took

CI shares the PR's item because five slots cannot hold repo, branch, PR, CI and
the actions the channel needs; the PR link still leads to its checks.

.. _Code channel properties: https://docs.slack.dev/ai/slack-code/code-channel-properties
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kiro_crew.platform.context import redact_via_context as redact
from kiro_crew.slack.code_channels.workspace.pr import PullRequest
from kiro_crew.slack.code_channels.workspace.remote import RepoRef

if TYPE_CHECKING:
    from kiro_crew.slack.code_channels.board.model import BoardSnapshot

MAX_ITEMS = 5
MAX_KEY = 64
MAX_LABEL = 128
MAX_URL = 2048

#: Slack's icon vocabulary for context-bar items.
ICONS = frozenset(
    {
        "branch",
        "folder",
        "hierarchy",
        "life-ring",
        "link",
        "globe",
        "terminal",
        "code",
        "search",
        "lock",
    }
)

#: Action keys this module places; :mod:`..actions` answers their clicks.
ACTION_CREATE_PR = "create-pr"
ACTION_STOP = "stop"
DASHBOARD_KEY = "dashboard"

#: Labels for the built-in actions and the dashboard link.
CREATE_PR_LABEL = "Create PR"
STOP_LABEL = "Stop"
DASHBOARD_LABEL = "Open in dashboard"

_PR_LABEL = {
    "open": "PR #{n} is open",
    "draft": "Draft PR #{n}",
    "merged": "PR #{n} merged",
    "closed": "PR #{n} closed",
}
_CI_LABEL = {"passed": "tests passing", "failed": "tests failing", "running": "tests running"}


@dataclass(frozen=True)
class BarState:
    """What the bar shows. Every field is optional; absent fields add no item."""

    #: Folder name for a project that is not a git repository.
    folder: str = ""
    repo: RepoRef | None = None
    #: Repository directory name when the remote is not a recognised forge.
    repo_name: str = ""
    branch: str = ""
    #: Whether ``branch`` exists on the remote, so its web link resolves.
    branch_pushed: bool = False
    pr: PullRequest | None = None
    #: The channel conductor's work board, when it has one: one item per worker.
    board: BoardSnapshot | None = None
    #: Whether the channel can open a pull request (``commands.pr_capability``).
    can_open_pr: bool = False
    #: Whether the Stop action is offered (a bound session to stop).
    stop: bool = False
    #: The bound session's dashboard link, without any credential ("" = none).
    dashboard_url: str = ""


def _clean_url(url: str) -> str:
    """*url* when it is an https link Slack accepts and redaction leaves intact."""
    if url and len(url) <= MAX_URL and url.startswith("https://") and redact(url) == url:
        return url
    return ""


def _item(key: str, label: str, icon: str, url: str = "") -> dict[str, str]:
    # Labels and links are repository- and forge-derived text bound for Slack:
    # the gateway's outbound redaction runs on them like on any other post.
    label = redact(label)
    item = {"key": key[:MAX_KEY], "label": label[:MAX_LABEL] or key[:MAX_KEY], "icon": icon}
    clean = _clean_url(url)
    if clean:
        item["url"] = clean
    return item


def _action(key: str, label: str, icon: str) -> dict[str, str]:
    """An action item: never a url, or Slack saves it as plain info."""
    return {
        "key": key[:MAX_KEY],
        "label": redact(label)[:MAX_LABEL] or key[:MAX_KEY],
        "icon": icon,
        "item_type": "action",
    }


def pr_label(pr: PullRequest) -> str:
    """``PR #42 is open · tests passing``: the PR's state with a live PR's CI."""
    label = _PR_LABEL[pr.state].format(n=pr.number)
    # CI belongs to a live PR; a merged or closed one's last run is history.
    if pr.ci in _CI_LABEL and pr.state in {"open", "draft"}:
        label += f" · {_CI_LABEL[pr.ci]}"
    return label


def build_items(state: BarState) -> list[dict[str, str]]:
    """The context-bar items for ``state``: at most five, keys unique, valid icons."""
    items: list[dict[str, str]] = []
    if state.repo is not None:
        items.append(_item("repo", state.repo.slug, "folder", state.repo.web_url))
    elif state.repo_name:
        items.append(_item("repo", state.repo_name, "folder"))
    elif state.folder:
        items.append(
            _item("folder", os.path.basename(state.folder.rstrip("/\\")) or state.folder, "folder")
        )
    if state.branch:
        url = state.repo.tree_url(state.branch) if state.repo and state.branch_pushed else ""
        items.append(_item("branch", state.branch, "branch", url))
    pr = state.pr
    if pr is not None and pr.state in _PR_LABEL:
        items.append(_item("pr", pr_label(pr), "hierarchy", pr.url))
    elif state.can_open_pr and state.branch:
        items.append(_action(ACTION_CREATE_PR, CREATE_PR_LABEL, "hierarchy"))
    if state.stop:
        items.append(_action(ACTION_STOP, STOP_LABEL, "terminal"))
    unique = _unique(items)
    if state.board is not None and state.board.rows:
        from kiro_crew.slack.code_channels.board.render import worker_bar_items

        for spec in worker_bar_items(state.board, MAX_ITEMS - len(unique)):
            unique.append(_item(spec["key"], spec["label"], spec["icon"], spec.get("url", "")))
        unique = _unique(unique)
    return with_dashboard(unique, state.dashboard_url)


def with_dashboard(items: list[dict[str, str]], url: str) -> list[dict[str, str]]:
    """*items* plus Open in dashboard in a free slot, when *url* is a usable link.

    Last, after the agent's own items too (the workspace merges those in between),
    so it only ever takes a slot nothing else wanted.
    """
    items = list(items)[:MAX_ITEMS]
    if _clean_url(url) and len(items) < MAX_ITEMS:
        items = _unique(items + [_item(DASHBOARD_KEY, DASHBOARD_LABEL, "link", url)])
    return items


def _unique(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    unique = []
    for item in items:
        if item["key"] not in seen and item["icon"] in ICONS:
            seen.add(item["key"])
            unique.append(item)
    return unique


def conforms(item: dict[str, str]) -> bool:
    """Whether one item is within Slack's documented schema (tests, the client)."""
    key, label, icon = item.get("key", ""), item.get("label", ""), item.get("icon", "")
    url, kind = item.get("url", ""), item.get("item_type", "info")
    allowed = {"key", "label", "icon", "url", "item_type"}
    return (
        set(item) <= allowed
        and 0 < len(key) <= MAX_KEY
        and 0 < len(label) <= MAX_LABEL
        and (not icon or icon in ICONS)
        and kind in {"info", "action"}
        and not (kind == "action" and url)
        and (not url or (len(url) <= MAX_URL and url.startswith("https://")))
    )


#: ``agent_resource`` limits (code channel properties).
MAX_RESOURCE_TITLE = 255
_RESOURCE_STATE = {"draft": "draft", "merged": "merged", "closed": "closed"}


def pr_resource(number: int, url: str, title: str, state: str) -> dict[str, str] | None:
    """The channel's ``agent_resource`` for its pull request, or None without one.

    ``title`` reads ``Fix the flaky login test (#42)``, with ``, merged`` /
    ``, closed`` / ``, draft`` inside the parentheses once the PR is no longer
    simply open, so the property follows the PR as it is renamed and lands.
    Redacted like every other forge-derived text bound for Slack, and cut to
    Slack's 255 characters.
    """
    clean = _clean_url(url)
    if number <= 0 or not clean:
        return None
    suffix = (
        f" (#{number}" + (f", {_RESOURCE_STATE[state]}" if state in _RESOURCE_STATE else "") + ")"
    )
    name = redact((title or "").strip()) or f"Pull request #{number}"
    room = MAX_RESOURCE_TITLE - len(suffix)
    if len(name) > room:
        name = name[: room - 1].rstrip() + "…"
    return {
        "url": clean,
        "resource_type": "pull_request",
        "title": name + suffix,
        "provider": "github",
    }
