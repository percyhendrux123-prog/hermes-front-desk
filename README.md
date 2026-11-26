# hermes-front-desk

Your agents escalate to the orchestrator first, and the orchestrator decides whether you need to hear about it.

When you run a team of Hermes agents, everything wants your attention: a card waiting on a decision, a worker that crashed, a scheduled job that failed, a message from someone important. Most of it the orchestrator can handle on its own. Front Desk makes that the default:

1. **Intake.** Every escalation lands at the desk, from any lane: a Kanban card that is blocked or failed (any profile's worker), a card a responder opened, a scheduled job error, a message from a person you named, or any agent that calls `front_desk_escalate`.
2. **Triage.** The escalation goes to the orchestrator's live session first. That's the chat you're actually in, not your phone. The orchestrator handles it itself or tells you.
3. **Notify.** When you are needed, your settings decide where and when you hear about it. If you're at the computer, it shows up in the chat you're in. If you're away and it's urgent, it goes to your phone chat. Everything else waits for the next digest.

It never messages anyone but you, and it approves nothing. Sends to anyone else still go through your normal approval rules.

## Settings, in plain words

Type `/notify` to see your settings. Change them the way you'd say it:

```
/notify stop telling me about finished cards
/notify interrupt me for failed cards
/notify put cron errors in the digest
/notify always interrupt for Dana Reyes
/notify quiet hours 10pm to 7am
/notify digest at 9am and 5pm
```

Each change is echoed back with the exact line that undoes it. Words it doesn't understand change nothing. It's a fixed phrase list, not a model guess, so the same words always make the same change.

The file behind it is plain YAML at `<hermes home>/plugin-data/front-desk/settings.yaml`. You can edit it directly; a broken file is refused and the last good settings stay in force. A full example:

```yaml
# What happens to each kind of escalation:
#   interrupt = tell me now     digest = put it in the next summary
#   log       = record it, don't tell me     never = ignore it
levels:
  client_escalation: interrupt
  money: interrupt
  deploy: interrupt          # deploys and live changes
  card_needs_input: interrupt  # a card is waiting on a decision
  card_failed: digest        # crashed, gave up, timed out, stuck
  card_done: digest
  cron_error: digest
  inbound_person: digest     # a message from someone listed under people
  other: digest

# These reach you even if the orchestrator handled them, and even in quiet hours.
always_interrupt:
  categories: [money]
  people: [Dana Reyes]

people:
  - name: Dana Reyes
    match: [dana, "555000111"]   # names or ids to recognise them by

# Where you hear about it, depending on whether you're at the computer.
channels:
  interrupt: {present: in_session, away: phone, unknown: phone}
  digest:    {present: in_session, away: phone, unknown: in_session}

# Your own phone chat on any gateway platform. Empty = no phone alerts.
# Front Desk never guesses a chat, because a guessed chat could be someone else's.
phone:
  session_key: "agent:main:telegram:dm:<your chat id>"

quiet_hours: {start: "22:00", end: "07:00"}   # or: quiet_hours: off
timezone: ""                                  # empty = this computer's
digest_times: ["09:00", "17:00"]
presence:
  present_idle_minutes: 10   # input within this long = you're at the computer
triage:
  orchestrator_first: true   # false = skip triage, settings decide directly
  timeout_minutes: 10        # urgent items not triaged by then follow the settings
keywords:                    # words that move a blocked/failed card into a category
  money: [payment, invoice, refund, chargeback, payout, billing, stripe]
  deploy: [deploy, production, go live, dns, rollback, release]
  client_escalation: [client escalation, customer escalation, escalat]
```

## How a decision is made

| Situation | What happens |
|---|---|
| Urgent item, you're at the computer | The orchestrator is woken in your session and triages. If it says `tell_boss`, it tells you in the same reply. |
| Urgent item, you're away | The orchestrator triages first. If it says `tell_boss`, the notice goes to your phone chat. |
| Urgent item, nobody triages within `timeout_minutes` | The settings decide on their own: you're told by the channel your presence picks. |
| Digest item | The orchestrator sees it on its next turn. Anything it sends on, or doesn't triage, goes in the next digest. |
| `log` item | The orchestrator sees it as an FYI. You are not told. |
| `never` item | Recorded, shown to no one. |
| `always_interrupt` category or person | You are told now, even in quiet hours and even if the orchestrator handled it. Its note is attached. |
| Quiet hours | Urgent items wait for the morning digest unless they are `always_interrupt`. Quiet hours never hide something while you're actively at the computer. |
| No phone chat set and you're away | Urgent items go to the digest instead. Nothing is dropped. |

Each escalation is one row with a unique key, so it can never alert you twice. That holds whether it arrives by a hook, a board poll, or a repeat call. Each delivery is claimed before it is sent. If the host refuses a delivery it is retried, and after three refusals the item goes to the digest. If the process dies between claiming and sending, that one notice is not resent. Missing one notice you can see in the log is better than being messaged twice.

## Presence: what needs Hermes v0.23.4

On **Hermes v0.23.4 or newer**, Front Desk knows whether you're at the computer from Hermes' own client activity reports. The desktop and web apps send how long ago you last touched the device and whether the screen is locked (`client.activity`). Plugins read this through `session_viewers()`. Front Desk reads it whenever the orchestrator's session runs a turn or a tool call, then ages that snapshot when it has to decide from a timer. On **v0.23.3** it runs in a degraded mode. "At the computer" then means you typed in a desktop, TUI or CLI session within `present_idle_minutes`, read-only from `state.db`, and the desk's own injected text doesn't count as you. That works, but it can't tell that you walked away right after typing, or that you're reading without typing. Front Desk checks for `session_viewers` and uses it when it's there. It never reads the version number, so updating Hermes turns full presence on by itself.

## Install

```bash
hermes plugins install <owner>/hermes-front-desk
hermes config set plugins.entries.front-desk.allow_gateway_injection true
hermes plugins enable front-desk
```

`allow_gateway_injection` is what lets the desk put a line into your session or your phone chat. Without it Front Desk still records and triages everything, but it can't tell you anything outside the orchestrator's next turn. Then set your phone chat with `/notify`, or in the settings file under `phone.session_key`. The gateway log shows your chat's session key.

Tools for the orchestrator and for other lanes:

- `front_desk_escalate(category, title, detail?, ref?, person?, key?)`: any agent hands an escalation to the desk instead of messaging you itself.
- `front_desk_triage(event_id, decision, note?, urgent?)`: the orchestrator's call, `handled` or `tell_boss`. Only a person's own session can triage; Kanban workers and scheduled runs are refused.
- `front_desk_status()`: counts, presence, and the last ten escalations.

For scripts: `hermes front-desk escalate --category cron_error --title "Backup failed"`, plus `hermes front-desk status` and `hermes front-desk settings`.

## How it differs from agentinbox

`agentinbox` (in the catalog) brings inbound messages from Telegram, Slack and email to the agent. Front Desk does a different job: it is a triage policy for the escalations your own agents raise. It decides whether you hear about one at all, and if so where and when, based on settings you own and whether you're at the computer. They can run together.

## Disclosures

What it reads, writes and runs, checked against the code:

- **Writes** only inside its data dir `<hermes home>/plugin-data/front-desk/`: `desk.db` (SQLite: escalation titles, short details, decisions and timestamps) and `settings.yaml` (+ `settings.yaml.bak`). `settings_path` can point the settings elsewhere.
- **Reads, read-only:** the Kanban board (`kanban.db`: event kinds, card titles, block reasons, summaries), `<hermes home>/cron/jobs.json` (job name, status, error text), and `<hermes home>/state.db` (only the newest user-message timestamp in local sessions, for v0.23.3 presence).
- **Inbound messages:** the `pre_gateway_dispatch` hook only observes. It never skips or rewrites a message, and it stores the name you gave that person and the chat id, never the message text. It records only senders you listed under `people`.
- **Sends:** only through `ctx.inject_message`, and only to the orchestrator's live session or the phone chat you configured. It never auto-picks a chat.
- **Background work:** a short-lived thread runs one poll-and-deliver pass, at most every 20 seconds, started from a Kanban dispatcher tick, a board event or a turn. No long-running thread, no timer of its own.
- **None of:** network calls, shell commands, subprocesses, Hermes child sessions, credentials, telemetry, approvals, or edits to Hermes config.
- **Private imports**, each wrapped so it fails open: `hermes_cli.plugin_session.session_key()` (the routing key for the live session; v0.23.3 has no public form of it) and `hermes_cli.kanban_db.kanban_db_path()` (where the board is; falls back to `<hermes home>/kanban.db`).

## Not covered

- The phone notice is a line injected into your phone chat, so the agent there sees it and relays it. That's the only way a plugin can reach a chat today, so the wording on your phone is the agent's, not a fixed template.
- Timer-driven work (digests, triage timeouts) runs on Kanban dispatcher ticks and turns. With no dispatcher and no activity, a digest is sent at the next tick or turn after its time.
- A delivery claimed and then lost to a crash is not resent (see above).

## Tests

```bash
uv run --no-project --python 3.12 --with pyyaml python -m unittest discover -s tests
```

The suite covers routing to the live session, every settings level, quiet hours (including wrap past midnight), overrides, presence present/away/unknown in both modes, the v0.23.3 degraded path, feature detection, board and cron intake, dedupe (one escalation never alerts twice), delivery retries, the `/notify` phrases and the plugin wiring against stubbed hosts.

## License

MIT
