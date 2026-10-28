---
name: watch
description: The recurring price check for one family-resort-watch trip. Fetches resort and flight prices, decides whether the note needs rewriting, and updates the trip's artifact database. Meant to run on a schedule; pass the trip name.
argument-hint: "<trip name>"
---

# Recurring price check

Code does all fetching and maths. Your job is a short judgement and one
database write. Most runs nothing changes, and a one-line run is a complete,
correct run.

`frw` below means `"${CLAUDE_PLUGIN_DATA}/frw" --trip $ARGUMENTS`, always
quoted that way. If `$ARGUMENTS` is empty, drop `--trip`; that works when only
one trip exists.

## 0. Context

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/bootstrap.sh" "${CLAUDE_PLUGIN_ROOT}" "${CLAUDE_PLUGIN_DATA}"
"${CLAUDE_PLUGIN_DATA}/frw" --trip $ARGUMENTS context
```

The context JSON says who is travelling, the dates, the home airports, the
flight rule, how many seats fares are priced for, the artifact URL and the
paths below (`changes`, `analysis`, `publish_plan`). Hotel prices are member
totals for the whole party and stay, taxes included. Fares are round trips
for `seats_priced` seats (Google won't price most international routes with
a child, so adults stand in), priced with the cheapest return that lands at a
home airport. Each airline's fare is the airline's own price (never a
third-party site) for its cheapest tier with free seat selection and a
carry-on, such as Delta Main Classic rather than Main Basic, when Chrome is
installed to look it up. Trip total = hotel + fare. A resort whose airports
have no eligible flight has no trip total.

If `artifact` is null, the trip was never published: say so in one line and
stop.

## 1. Run the check

```bash
"${CLAUDE_PLUGIN_DATA}/frw" --trip $ARGUMENTS run
```

Hotels are checked every run. Flights run only when due (every 6 hours for
preferred stays, 12 for the rest), and a flight run also looks up each
outbound's paired returns, so some runs take 15 minutes or more. Allow it.
"flights: {'skipped': 1}" means another flight sweep holds the lock; that is
fine. "fares:" warnings mean Google refused a fare-tier lookup;
those fares reuse the last known tier.

Stop and write nothing to the artifact, reporting the error in one line, if:

- it exits non-zero;
- `changes` shows more than half the hotel requests as errors;
- every resort suddenly shows no rooms, or the output says the hotel list
  was not found. Say the booking engine may have changed shape.

Flight search errors alone are not a reason to stop. A few routes or return
lookups failing is normal, because Google's payload is fragile. Mention it
only if every flight search in this run errored.

## 2. Read what changed

Read `changes` (small) and `publish_plan`. Do not read the files under the
report's `db/` folder; they are large. Read `analysis` to see the current
note and its `written_at`.

## 3. Decide whether to rewrite the note

Rewrite `analysis` only if at least one of these holds:

- an event involves a resort in the top 10 of the trip-total ranking, or any
  preferred-stay `new_low` or `flight_new_low`;
- a flight event (`flight_drop`, `flight_rise`) changes the trip total of a
  top-5 resort by $100 or more;
- the top 3 of the trip-total ranking differ from what the current note
  describes;
- the "Stay trends" table shows a stay's median trip moving $150 or more in
  24 hours;
- the current note's `written_at` is more than 24 hours old, or there is no
  note yet.

Otherwise leave it untouched and leave it out of the batch.

When rewriting, keep exactly this shape (JSON):

```json
{"schema": 1, "scan_id": <scan number from changes>, "written_at": "<now, ISO 8601 UTC>",
 "signal": "quiet|notable|act", "headline": "...", "paragraphs": ["..."], "bullets": ["..."]}
```

- Lead with trip totals (hotel + flights). Say when the cheapest resort has
  no eligible flight, and what that costs it in the ranking.
- `changes` has a "Stay trends" table: each stay's median trip across all
  kids'-club resorts, with 24-hour and 7-day changes. It shows whether a
  whole date is getting cheaper, not just one resort. When a stay moved $100
  or more, or the order of the stays changed, give it one bullet.
- `changes` lists the cheapest fare per airline, with its outbound departure
  and paired return (local times). When one airline is far cheaper than the
  rest on a leading route, say so and give the next airline's fare. Mention
  flight times when they matter for this party: a return landing near
  midnight with young children, a very early departure. The page's flight
  filters belong to the user; your ranking always uses unfiltered fares.
  `changes` names each airline's tier and what it adds over the bottom tier;
  name the tier when it explains why a fare is higher than Google shows.
- Signal:
  - `act`: the cheapest trip total for a preferred stay hit a new low at
    least 5% under its previous low, a top-3 trip dropped 10% or more, or the
    leading option shows 3 or fewer rooms left.
  - `notable`: any other material move in the top 10.
  - `quiet`: nothing material (a stale-note refresh).
- Headline: under 90 characters, naming the resort and the trip total that
  matters.
- Paragraphs: 1–3, each at most 3 short sentences: what moved (hotel or
  fare), what leads now, and refundable vs non-refundable where it matters (a
  refundable low rate is cheap to hold). Mention points only if a resort's
  cpp upper bound in `changes` is 1.5¢ or more.
- Bullets: at most 4. Plain, direct sentences; no em-dashes; no hype.
- Use only numbers and times present in `changes`. Never invent prices,
  airlines, times, dates or policies.

## 4. Write to the artifact database

The collection name is in each `publish_plan` entry.

a. ArtifactData action `list`, with the context's `artifact` URL and that
   collection. It gives each document's current version. Don't read any
   files it saves.

b. ArtifactData action `batch`:
   - one `set` per `publish_plan` entry, with `collection`, `doc_id` and
     `file_path` from the plan, and `if_version` = that doc's listed version.
     Omit `if_version` only for a doc the list did not show;
   - if you rewrote the note, one more `set` for doc_id `analysis` with
     `file_path` = the context's `analysis` and its listed `if_version`.

c. If the batch succeeds, run `"${CLAUDE_PLUGIN_DATA}/frw" --trip $ARGUMENTS published`.
   If it fails on a version conflict, list again and retry the batch once.
   Otherwise report the failure and stop. Do not run `published`.

## 5. Report

One line: the scan number, the events count, and the current trip-total
leader with its total. Add "note rewritten (<signal>)" when you rewrote it.

## Never

- Never book, hold or open a booking or a flight, and never contact anyone.
- Never edit code, the trip config, or the page, and never republish the
  page itself (it reads the database).
- Never delete database documents or the trip's database file.
