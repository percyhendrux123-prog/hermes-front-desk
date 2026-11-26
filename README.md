# hermes-front-desk

Your agents escalate to the orchestrator first, and the orchestrator decides whether you need to hear about it.

When you run a team of Hermes agents, everything wants your attention: a card waiting on a decision, a worker that crashed, a scheduled job that failed, a message from someone important. Most of it the orchestrator can handle on its own. Front Desk makes that the default:

1. **Intake.** Every escalation lands at the desk, from any lane: a Kanban card that is blocked or failed (any profile's worker), a card a responder opened, a scheduled job error, a message from a person you named, or any agent that calls `front_desk_escalate`.
2. **Triage.** The escalation goes to the orchestrator's live session first. That's the chat you're actually in, not your phone. The orchestrator handles it itself or tells you.
3. **Notify.** When you are needed, your settings decide where and when you hear about it. If you're at the computer, it shows up in the chat you're in. If you're away and it's urgent, it goes to your phone chat. Everything else waits for the morning brief.

It keeps two rhythms. A **morning brief** (7:30 AM by default) lists what is waiting on you and the items that could wait. A **check every few hours** (3 by default) runs your standing watches and stays silent unless something needs you. Between briefs you hear only about what can't wait.

It never messages anyone but you, and it approves nothing. Sends to anyone else still go through your normal approval rules.

## Settings, in plain words

Type `/notify` to see your settings. Change them the way you'd say it:

```
/notify stop telling me about finished cards
/notify interrupt me for failed cards
/notify put cron errors in the digest
/notify always interrupt for Dana Reyes
/notify quiet hours 10pm to 7am
/notify brief at 7:30am
/notify check every 3 hours
/notify watch card waiting on me over 2 hours
/notify that could have waited, vendor updates only matter when a date or price changes
```

Each change is echoed back with the exact line that undoes it. Words it doesn't understand change nothing. It's a fixed phrase list, not a model guess, so the same words always make the same change.

The file behind it is plain YAML at `<hermes home>/plugin-data/front-desk/settings.yaml`. You can edit it directly; a broken file is refused and the last good settings stay in force. A full example:

```yaml
# What happens to each kind of escalation:
#   interrupt = tell me now     digest = put it in the morning brief
#   log       = record it, don't tell me     never = ignore it
levels:
  client_escalation: interrupt
  money: interrupt           # locked: can't go below digest
  approval: interrupt        # a send waiting on your approval; locked like money
  deploy: interrupt          # deploys and live changes
  card_needs_input: interrupt  # a card is waiting on a decision
  card_failed: digest        # crashed, gave up, timed out, stuck
  card_done: digest
  cron_error: digest
  inbound_person: digest     # a message from someone listed under people
  responsibility: interrupt  # a standing watch was breached
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
brief: {time: "07:30"}       # the morning brief; or: brief: off
check_every_hours: 3         # how often standing watches are checked; 0 = never
digest_times: []             # optional extra digest slots, e.g. ["17:00"]

# Standing watches, in your own words. Each check raises its own alert when one is breached.
responsibilities:
  - card waiting on me over 2 hours
  - nobody waits more than two days for a reply
  - text: tell me if a client card is blocked over 90 minutes
    card: true               # also show this watch as a card on the Kanban board
    level: interrupt         # or digest: breaches go to the morning brief

# Saved from your corrections ("/notify that could have waited, <why>"). /notify forget rule <n> removes one.
rules:
  - category: deploy
    level: digest
    reason: releases only matter to me when one fails
presence:
  present_idle_minutes: 10   # input within this long = you're at the computer
triage:
  orchestrator_first: true   # false = skip triage, settings decide directly
  timeout_minutes: 10        # urgent items not triaged by then follow the settings
keywords:                    # words that move a blocked/failed card into a category
  money: [payment, invoice, refund, chargeback, payout, billing, stripe]
  approval: [waiting on approval, awaiting approval, needs approval, ready to send, draft for approval]
  deploy: [deploy, production, go live, dns, rollback, release]
  client_escalation: [client escalation, customer escalation, escalat]
```

## How a decision is made

| Situation | What happens |
|---|---|
| Urgent item, you're at the computer | The orchestrator is woken in your session and triages. If it says `tell_boss`, it tells you in the same reply. |
| Urgent item, you're away | If your computer session is still open, the orchestrator is woken there and triages first. If it says `tell_boss`, the notice goes to your phone chat. With no computer session open, the item waits for the triage timeout. |
| Urgent item, nobody triages within `timeout_minutes` | The settings decide on their own: you're told by the channel your presence picks. |
| Digest item | The orchestrator sees it on its next turn. Anything it sends on, or doesn't triage, goes in the morning brief (or an extra digest slot, if you set one). |
| `log` item | The orchestrator sees it as an FYI. You are not told. |
| `never` item | Recorded, shown to no one. |
| `always_interrupt` category or person | You are told now, even in quiet hours and even if the orchestrator handled it. Its note is attached. |
| Quiet hours | Urgent items wait for the morning brief unless they are `always_interrupt`. Quiet hours never hide something while you're actively at the computer. |
| No phone chat set and you're away | Urgent items go to the digest instead. Nothing is dropped. |

Each escalation is one row with a unique key, so it can never alert you twice. That holds whether it arrives by a hook, a board poll, or a repeat call. Each delivery, a single notice or a whole digest, is claimed before it is sent. If the host refuses a delivery it is retried, and after three refusals the item goes to the digest. If the process dies between claiming and sending, the notice is not resent, because it may already have arrived. After ten minutes it is marked `unconfirmed` and shown to the orchestrator, so nothing disappears silently. Missing one notice you can see is better than being messaged twice.

Only a desktop, TUI or CLI session can be the live session. A gateway chat never is, because the orchestrator also answers other people there. Your own phone chat (`phone.session_key`) counts only as a sign that you're on your phone, not at the computer.

## The morning brief and the regular check

**The morning brief** comes once a day at `brief.time` (7:30 AM local by default), in the chat you're in if you're at the computer, otherwise your phone chat. It has:

- **Waiting on you:** every Kanban card blocked on a person right now (the first 15 listed, the rest counted), by its card code (a leading tag like `A101`, otherwise the card id), with how long it has waited. Cards waiting on another card, or on a flaky retry, aren't listed, because they aren't waiting on you.
- **For you:** everything set to `digest`, everything held overnight by quiet hours, and anything the orchestrator passed on without urgency.
- **Handled for you:** money and approval items the orchestrator dealt with itself that no brief has shown yet. Each is shown once (see locked floors).
- **Alerts:** how many times you were interrupted yesterday and so far today. This is the approval-fatigue counter: if the number is high, tell the desk what could have waited.

Installing the desk never replays a brief it missed: installed after today's brief time, the first brief is tomorrow's; installed before it, today's. A morning with nothing at all to say (nothing waiting, nothing held, nothing handled, no alerts yesterday) gets no brief. Turning the brief off needs at least one `digest_times` slot, so digest items always have somewhere to go.

**The regular check** runs every `check_every_hours` (3 by default). It evaluates your standing watches. A check never sends anything by itself. A breached watch becomes an escalation like any other: it reaches the orchestrator first, then follows your settings. When nothing is breached, you hear nothing.

## Standing watches (responsibilities)

Some duties aren't events. "Nobody waits more than two days for a reply" only matters when it stops being true. Write these in plain words under `responsibilities`, or add one with `/notify watch <words>`. The desk understands two kinds:

| You write | It watches |
|---|---|
| `card waiting on me over 2 hours` | any Kanban card blocked on you longer than that |
| `tell me if a client card is blocked over 90 minutes` | the same, only cards with "client" in the title |
| `nobody waits more than two days for a reply` | anyone on your `people` list whose message has gone unanswered that long |
| `Dana Reyes not answered in 1 day` | one person from your `people` list |

Durations can be numbers or words ("two days", "90 minutes", "2h", "a week"), one per watch ("150 minutes", not "2 hours and 30 minutes"). A watch it can't read is refused with the reason, and your settings stay as they were. Each breach alerts once. A card blocked again later is a new breach. For a reply watch, each wait is reported once; the next unanswered message from that person starts a new wait.

"Answered" means the desk saw it: the orchestrator marked that message `handled`, or you said `/notify answered Dana Reyes`. Front Desk keeps no message text, so it can't see replies sent from your phone's own apps.

**On the board.** Add `card: true` to a watch and the desk keeps a card for it on your Kanban board ("Watch: ..."), made once, with a comment on each breach. The card has no assignee, so no worker is ever started for it. The desk's own cards never count as escalations or as cards waiting on you. Watch cards use the host's Kanban API when it is there; with no Kanban, watches on people still work.

## Corrections with a reason

When an alert could have waited, say so in plain words, with why:

```
/notify that could have waited, vendor updates only matter when a date or price changes
```

The desk finds the last alert that interrupted you (within the last day) and offers a rule: "deploys and live changes -> the morning brief", with your reason. Nothing is saved until you reply `/notify yes` (the offer lasts an hour). Then it echoes the saved rule with its undo line, `/notify forget rule 1`. The phrases are fixed ("that could have waited", "this should have been in the brief", "that wasn't urgent"). The desk doesn't guess what you meant.

A rule moves alerts like that one (same kind, and the same person or watch if it had one) to the morning brief. The reason is kept with the rule and attached to every alert the rule moves, so the orchestrator sees it when it triages. A rule can't hold back something set to always interrupt. To change that, use `/notify stop always interrupting for ...`.

## Locked floors

Money and sends waiting on your approval can never be set to `log` or `never`, in the file, by `/notify`, or by a rule. The validator refuses and says why: these always reach you, at least in the morning brief. If the orchestrator handles one itself, it still appears under "Handled for you" in the next brief.

## Known limits of the rhythms

- **Anything that arrives between checks waits for the next one.** A watch is only evaluated at a check, so a card that crosses "2 hours" just after a check is reported at the next check, up to `check_every_hours` later. Events themselves (a card blocked, a job failing) don't wait for a check; they're picked up on the next pass.
- **Passes need a heartbeat.** Passes, briefs and checks run on Kanban dispatcher ticks, turns and inbound messages. With none of those, a brief or check runs at the next one after its time.
- **Watches can't read meaning.** A reason in a rule is shown to the orchestrator, but the rule itself matches only the kind of alert, the person and the watch. "Only when a date changes" is for the orchestrator to judge when it triages.

## Presence: what needs Hermes v0.23.4

On **Hermes v0.23.4 or newer**, Front Desk knows whether you're at the computer from Hermes' own client activity reports. The desktop and web apps send how long ago you last touched the device and whether the screen is locked (`client.activity`). Plugins read this through `session_viewers()`. Front Desk reads it whenever the orchestrator's session runs a turn or a tool call, then ages that snapshot when it has to decide from a timer. On **v0.23.3** it runs in a degraded mode. "At the computer" then means you typed in a desktop, TUI or CLI session within `present_idle_minutes`, read-only from `state.db`, and the desk's own injected text doesn't count as you. That works, but it can't tell that you walked away right after typing, or that you're reading without typing. Front Desk checks for `session_viewers` and uses it when it's there. It never reads the version number, so updating Hermes turns full presence on by itself.

The new parts use presence in one place: picking where the morning brief and a breached watch's alert go (the chat you're in, or your phone). On v0.23.3 that choice uses the degraded reading above. The brief's contents, the check schedule, watches, corrections and locked floors don't depend on presence, and they work the same on both versions.

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

For scripts: `hermes front-desk escalate --category cron_error --title "Backup failed"`, plus `hermes front-desk status`, `hermes front-desk settings`, `hermes front-desk brief` (preview the brief; sends nothing) and `hermes front-desk check` (run the watches now).

## How it differs from agentinbox

`agentinbox` (in the catalog) brings inbound messages from Telegram, Slack and email to the agent. Front Desk does a different job: it is a triage policy for the escalations your own agents raise. It decides whether you hear about one at all, and if so where and when, based on settings you own and whether you're at the computer. They can run together.

## Disclosures

What it reads, writes and runs, checked against the code:

- **Writes** inside its data dir `<hermes home>/plugin-data/front-desk/`: `desk.db` (SQLite: escalation titles, short details, decisions, timestamps, daily alert counts, a pending rule offer) and `settings.yaml` (+ `settings.yaml.bak`). `settings_path` can point the settings elsewhere.
- **Writes to the Kanban board** only for a watch you marked `card: true`: one unassigned card per watch and a comment per breach, through the host's own `kanban_db.create_task` / `add_comment` (never raw SQL). Nothing else on the board is ever changed.
- **Reads, read-only:** the Kanban board (`kanban.db`: event kinds, card titles, statuses, block reasons, summaries), `<hermes home>/cron/jobs.json` (job name, status, error text), and `<hermes home>/state.db` (only the newest user-message timestamp in local sessions, for v0.23.3 presence).
- **Inbound messages:** the `pre_gateway_dispatch` hook only observes. It never skips or rewrites a message, and it stores the name you gave that person and the chat id, never the message text. It records only senders you listed under `people`.
- **Sends:** only through `ctx.inject_message`, and only to the orchestrator's live session or the phone chat you configured. It never auto-picks a chat.
- **Background work:** a short-lived thread runs one poll-and-deliver pass. It starts from a Kanban dispatcher tick, a turn or an inbound gateway message, at most every 20 seconds. A board event (`kanban_task_blocked` / `kanban_task_completed`) or a `front_desk_escalate` call starts one right away. There is never more than one pass at a time, no long-running thread, and no timer of its own. The gateway hook only puts the sender on an in-memory queue. That pass does the matching and storing, so the hook never touches disk.
- **None of:** network calls, shell commands, subprocesses, Hermes child sessions, credentials, telemetry, approvals, or edits to Hermes config.
- **Private imports**, each wrapped so it fails open: `hermes_cli.plugin_session.session_key()` (the routing key for the live session; v0.23.3 has no public form of it), `hermes_cli.kanban_db.kanban_db_path()` (where the board is; falls back to `<hermes home>/kanban.db`), `hermes_cli.kanban_db.create_task` / `add_comment` with `hermes_cli.kanban_db_connect.connect_closing` (watch cards only; no public plugin API for Kanban writes exists yet, and without them watch cards are simply off), `hermes_constants.get_hermes_home()` (falls back to `$HERMES_HOME` or `~/.hermes`) and `plugins.plugin_storage.plugin_data_dir()` (falls back to `<hermes home>/plugin-data/front-desk`).

## Not covered

- The phone notice is a line injected into your phone chat, so the agent there sees it and relays it. That's the only way a plugin can reach a chat today, so the wording on your phone is the agent's, not a fixed template.
- Timer-driven work (digests, triage timeouts) runs on Kanban dispatcher ticks and turns. With no dispatcher and no activity, a digest is sent at the next tick or turn after its time.
- A delivery claimed and then lost to a crash is not resent. It is shown to the orchestrator as `unconfirmed` (see above).
- In v0.23.3 degraded mode, "at the computer" reads input in any local session of the profile, not only the orchestrator's.

## Tests

```bash
uv run --no-project --python 3.12 --with pyyaml python -m unittest discover -s tests
```

On a bare Python with no PyYAML (`python3 -m unittest discover -s tests`), everything runs except the one test that reads `plugin.yaml`, which skips with the command above. Without PyYAML the desk reads and writes its settings as JSON, which is valid YAML. Hand-written YAML then fails loud with that reason. Hermes ships PyYAML, so installed copies never hit this. The suite starts no subprocesses, so no test can crash a real Python process.

The suite covers routing to the live session, every settings level, quiet hours (including wrap past midnight), overrides, presence present/away/unknown in both modes, the v0.23.3 degraded path, feature detection, board and cron intake, dedupe (one escalation never alerts twice), delivery retries, the `/notify` phrases, the morning brief (time, once a day, contents, phone when away, off), the regular check (silent when nothing is breached, its interval, dedupe per breach), standing watches (parsing, refusals, filters, people, answered, watch cards), corrections (phrases, offer, yes, undo, expiry, always-interrupt conflicts), locked floors on every path, the alert counter, and the plugin wiring against stubbed hosts.

## License

MIT
