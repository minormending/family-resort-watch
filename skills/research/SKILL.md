---
name: research
description: Research family-fit reviews (kids' club, water play, beach, rooms, food, transfer, condition, with pros and cons) of every resort a family-resort-watch trip tracks, for that family's own party and dates, and publish them to the trip's artifact. Use when the user wants the reviews redone for their family, or a resort has no review.
argument-hint: "<trip name> [all | missing | thin | resort codes]"
---

# Family-fit research

The plugin ships reviews written for two adults and a young child
travelling in late April. This skill writes the user's own, which override
the shipped ones resort by resort. It is web research: plan on about an hour
for 30 resorts. Tell the user that first and confirm before starting.

`frw` means `"${CLAUDE_PLUGIN_DATA}/frw"`. The first word of `$ARGUMENTS`
is the trip; the rest picks which resorts to do:

- `missing` (the default): resorts with no review at all;
- `thin`: also redo reviews that `frw reviews --thin` flags;
- `all`: every resort the trip tracks, so every review fits this family;
- or a list of resort codes.

## 1. Gather

```bash
"${CLAUDE_PLUGIN_DATA}/frw" --trip <trip> context
"${CLAUDE_PLUGIN_DATA}/frw" --trip <trip> resorts
"${CLAUDE_PLUGIN_DATA}/frw" --trip <trip> reviews --thin
```

`resorts` lists each family resort with its area, airports and whether its
review is `own`, `shipped` or missing (null). If it lists nothing, the trip
hasn't run yet: run setup first.

Ask the user one question before starting: anything the reviews should
weigh for their family, such as a diet, a child who won't join a kids'
club, a stroller, or a pool-or-beach preference. Keep their answer
non-identifying. Write "one adult avoids pork", not names or health details.

## 2. Brief

Read `${CLAUDE_PLUGIN_ROOT}/python/src/family_resort_watch/data/research/RUBRIC.md`.
Build the brief: a **Trip** block on top, then the rubric. The Trip block
holds the party (adults, children's ages on the trip), the travel month and
dates, the home airports, and the user's answer from step 1. Today's date
goes in `researched_on`.

## 3. Research

Split the resorts into batches of 4–6 by area (nearby resorts share
sources) and hand each batch to a subagent with the full brief, its resort
codes, names, areas and airports, and the output folder
`~/.family-resort-watch/research/resorts/`. Run up to 4 at a time. Each one
writes one `<code>.json` per resort and reports problems, not the reviews
themselves.

If the subagents run out of web searches, stop, tell the user how many
resorts are done, and say that running the skill again with `missing` picks
up the rest.

## 4. Check

```bash
"${CLAUDE_PLUGIN_DATA}/frw" --trip <trip> reviews
```

Any `INVALID` line names a file and the problem. Fix that file (or send it
back to a subagent) until there are none. Then skim the new verdicts. Each
should be about *this* party, and no review should name the user or their
family.

## 5. Publish

```bash
"${CLAUDE_PLUGIN_DATA}/frw" --trip <trip> report
```

Then write the trip's `publish_plan` to its artifact as the watch skill's
step 4 does (list, then a batch with `if_version`, then `published`). The
reviews document is in the plan whenever it changed.

Tell the user how many reviews were written, and which three resorts now fit
best, with their scores. Mention any resort whose score moved by 0.5 or more
against the shipped review, and say why in a few words.

## Never

- Research only. Never contact a resort, sign up, book or start a booking.
- Don't paste or closely paraphrase reviews; summarise in your own words.
- Never edit the shipped research inside the plugin folder; the user's own
  reviews go in `~/.family-resort-watch/research/`.
