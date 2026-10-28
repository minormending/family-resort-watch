---
name: setup
description: Set up a family all-inclusive price watch for one trip. Interviews the user (who is going, home airports, dates, nonstop or not), creates the trip, runs the first price check, publishes a private claude.ai artifact report, and schedules the recurring check. Use when the user wants to start watching Hyatt Inclusive Collection resort and flight prices for a trip, or add another trip.
argument-hint: "[trip name]"
---

# Set up a family resort watch

This plugin watches Hyatt Inclusive Collection all-inclusive prices (member
rates, whole party, taxes in) for every resort that takes the family's
children, plus round-trip Google Flights fares from the family's home
airports. Code does all fetching and maths; a scheduled Claude session reads
the summary, writes a short note, and updates a private artifact page.

Work through the steps in order. Keep the user informed in one or two
sentences per step; don't paste config files or JSON at them.

## 0. Install the command

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/bootstrap.sh" "${CLAUDE_PLUGIN_ROOT}" "${CLAUDE_PLUGIN_DATA}"
```

It needs Python 3.11+. If it says Python is missing, tell the user how to get
it (python.org, or `brew install python@3.12` on a Mac) and stop.

From here on, `frw` means `"${CLAUDE_PLUGIN_DATA}/frw"`, always quoted that
way in Bash. Trips, prices and reports live in `~/.family-resort-watch`
(or `$FRW_HOME`), so they survive plugin updates.

Run `frw trips`. If a trip exists, ask whether this is a new trip or a redo
of that one (a redo passes `--overwrite` in step 2 and keeps its history).

## 1. Interview

Ask with AskUserQuestion where the answer is a choice, and in plain chat
where it is free text. Ask only what you can't infer. Collect:

1. **Who is going.** Adults (1–3) and each child's age *on the trip*. One
   room is priced, so at most 5 people and 3 children. Ages matter: kids'
   clubs usually start at 3 or 4, under-2s are babies.
2. **Home airports.** Which airports they would fly out of and back into.
   Turn a city into codes yourself and confirm ("Chicago: O'Hare and Midway,
   ORD and MDW?"). When all of them share a Google Flights metro code (NYC,
   CHI, WAS, LON, YTO, HOU, DTT and so on), search that one code instead of
   each airport: fewer searches, same flights. Also pick a short label for
   the page ("Chicago").
3. **Dates.** The days they can be away (a school break, say), then the
   stays to watch as check-in/check-out pairs. One or two preferred stays
   are checked every run; up to 6 more "also" stays are checked every 6
   hours. At most 8 in all; each one multiplies the searches. Offer to
   suggest stays (for example every 5-night stay that fits the break).
4. **Flights.** Nonstop only (the default) or allow one stop.
5. **Where.** The default covers the Americas: Mexico, Dominican Republic,
   Jamaica, Aruba, Curaçao, Costa Rica, Panamá, Colombia, Saint Lucia. Ask
   only if they want to drop some. Spain, Portugal, Greece and Bulgaria
   exist too, but there are few family resorts there.
6. **How often.** Every hour (most responsive, about 24 short sessions a
   day), every 3 hours, or twice a day. Use this in step 6.

Pick a trip name: short, lowercase, letters, digits and dashes
(`spring-2027`, `summer`). Use `$ARGUMENTS` if the user gave one.

## 2. Create the trip

Run `frw example` to see the answer shape. Write the user's answers in that
shape to `~/.family-resort-watch/answers-<trip>.json`, with a `title` (under
60 characters) and a `recess_label` that reads well on the page, such as
"Spring break · Sat Apr 17 – Sun Apr 25, 2027". Then:

```bash
"${CLAUDE_PLUGIN_DATA}/frw" new-trip <trip> --answers ~/.family-resort-watch/answers-<trip>.json
"${CLAUDE_PLUGIN_DATA}/frw" --trip <trip> check
```

An `error:` line names the bad answer. Fix it with the user and run again.

## 3. First price check

```bash
"${CLAUDE_PLUGIN_DATA}/frw" --trip <trip> run --all-windows
```

The first run finds which resorts take the children, prices every stay at
each of them, and searches every flight route with its paired returns. It
takes 10–30 minutes. Run it in the background and tell the user it is
running. When it finishes, run `frw --trip <trip> geo` (it routes the drive
from the airport for any resort the tool has a location for but no drive
time yet; it lists resorts with no location, whose drive stays unknown), then
`frw --trip <trip> report` and `frw --trip <trip> page`.

If it exits non-zero, or most hotel requests in the trip's `changes.md`
are errors, show the user the last lines of the output and stop. A few flight
searches failing is normal.

## 4. Write the first note

`frw --trip <trip> context` prints the paths you need (`changes`,
`analysis`, `publish_plan`, `page`). Read `changes` and write `analysis`
exactly as the watch skill's step 3 describes (`/family-resort-watch:watch`;
its rules are in `${CLAUDE_PLUGIN_ROOT}/skills/watch/SKILL.md`). This first
note is a rewrite by definition.

## 5. Publish the report

1. Load the `artifact-capabilities` skill, as the Artifact tool requires
   before you pass capabilities.
2. Publish the page as a new, private artifact. Use the Artifact tool with
   `file_path` = the context's `page`, `icon` = "beach", a one-sentence
   `description`, and these capabilities:
   `{"db": {"rules": [{"path": "", "read": "view", "write": "admin"}]}}`.
   The page is already built, so don't edit it. It reads everything from the
   artifact's database, so it is empty until the next step.
3. `frw --trip <trip> set-artifact <url>` to remember the link.
4. Seed the database with ArtifactData action `batch`: one
   `{"op": "set", "collection": ..., "doc_id": ..., "file_path": ...}` per
   entry in `publish_plan`, plus one for doc_id `analysis` (same collection)
   from the `analysis` file. No `if_version` on a new artifact.
5. If the batch succeeded, run `frw --trip <trip> published`.

Give the user the link. The artifact is private until they share it.

## 6. Schedule the check

The recurring check is the `watch` skill for this trip. The prompt to
schedule is exactly:

```
/family-resort-watch:watch <trip>
```

- **Claude desktop app:** create a scheduled task with the
  scheduled-tasks tools (`create_scheduled_task`): name `<trip>-resort-watch`,
  that prompt, and the cadence from the interview (hourly is cron `7 * * * *`;
  pick an odd minute). Then ask the user to open it and click **Run now** once,
  and to approve the `frw` commands and the artifact database writes it asks
  about. Scheduled runs can't stop to ask.
- **No desktop app:** say that recurring checks need a scheduler. Options:
  the desktop app's scheduled tasks, or cron running
  `claude -p "/family-resort-watch:watch <trip>"` with the needed tools
  allowed (see `claude --help` for `--allowedTools`). Until then, they can
  run `/family-resort-watch:watch <trip>` by hand.

## 7. Wrap up

Tell the user, in a few lines:

- the artifact link, and that it updates on its own schedule;
- how many family resorts were found, and the cheapest trip total from the
  note;
- that the shipped family-fit reviews were written for two adults and a young
  child travelling in late April, and `/family-resort-watch:research <trip>`
  redoes them for their own family (about an hour of web research);
- to change the trip later, edit the answers file and run setup again with
  the same trip name.

## Never

- Never book, hold or start a booking, never sign in anywhere, never contact
  a resort or airline.
- Never publish the artifact publicly or share it; the user decides that.
- Never edit the plugin's code or shipped data.
