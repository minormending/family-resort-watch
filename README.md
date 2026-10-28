# family-resort-watch

A Claude Code plugin that watches **Hyatt Inclusive Collection** all-inclusive
prices for your family's trip, along with round-trip **Google Flights** fares
from your home airports. It keeps a private claude.ai artifact up to date:
trip totals (hotel plus flights) ranked across every resort that takes your
children, price history for each stay, flight filters, family-fit reviews,
and a short note written by Claude when something moves.

You tell Claude who is going, where you fly from and which dates you are
weighing. It sets up the rest: the trip, the first price check, your own
artifact and a recurring check.

## Install

In Claude Code:

```
/plugin marketplace add minormending/family-resort-watch
/plugin install family-resort-watch@family-resort-watch
```

Then ask Claude to set it up, or run:

```
/family-resort-watch:setup
```

You need Python 3.11 or newer on your PATH, and Google Chrome for fare tiers
(optional; see below). The plugin installs its own
virtual environment the first time it runs.

## What setup asks

- who is going: adults, and each child's age on the trip (one room, up to 5
  people);
- your home airports, such as "Chicago" → ORD and MDW;
- the days you can travel, and the check-in/check-out pairs to watch (up to 8);
- nonstop only, or one stop allowed;
- how often to check: hourly, every 3 hours, or twice a day.

It then runs the first check (10–30 minutes), publishes your artifact
(private until you share it), and schedules the recurring check.

## Skills

| Skill | What it does |
|---|---|
| `/family-resort-watch:setup [trip]` | Interview, create the trip, first check, publish the artifact, schedule the check |
| `/family-resort-watch:watch <trip>` | The recurring check: fetch prices, maybe rewrite the note, update the artifact's database |
| `/family-resort-watch:research <trip> [missing\|thin\|all]` | Redo the family-fit reviews for your own party and dates (web research, about an hour for 30 resorts) |

You can watch more than one trip. Each trip gets its own artifact and its
own schedule.

## How it works

Code does all the fetching and maths. Claude only reads the summary and
writes the note.

- **Hotels:** the public Hyatt Inclusive Collection booking engine, at the
  member price for the whole party with taxes. Adults-only resorts drop out
  on their own, because they return no rooms for your party.
- **Flights:** Google Flights through
  [fast-flights](https://github.com/AWeirdDev/flights).
  - Every eligible itinerary is kept and paired with its real returns.
  - Fares are searched as adult seats, because Google returns nothing for
    most international routes when a child is in the search. Child fares on
    these routes are usually the adult fare.
  - Booking links open with your real party.
  - **Fare tiers:** Google's list price is the airline's own bottom tier,
    which is often Basic: no seat selection, or seats for a fee. So the tool
    opens Google's booking page in headless Chrome and prices each airline's
    cheapest tier sold by the airline itself with free seat selection and a
    carry-on (for example Delta Main Classic rather than Main Basic). It
    looks up each airline's cheapest flight, adds the gap to that airline's
    other flights, and reuses the result for 48 hours. Google refuses some of
    these lookups; then the last known gap is used, and the page says which
    tier each fare is. Without Google Chrome installed, fares stay at
    Google's list price.
- **Reviews:** 32 resorts ship with dated reviews: seven scored criteria,
  pros and cons, and sources. They were researched in October 2026 for two
  adults and a young child travelling in late April. The research skill
  writes your own, which replace the shipped ones resort by resort.
- **Report:**
  - Each run writes small JSON documents. Only the changed ones go to the
    artifact's database.
  - The artifact page never needs republishing.
  - Most runs are one short Claude session that ends in a single line.

Your trips, prices and reports live in `~/.family-resort-watch` (set
`FRW_HOME` to move it). The plugin's own folder holds only its Python
environment.

## Cost and limits

- Each scheduled check is a Claude session that counts against your usage.
  Hourly is about 24 short sessions a day; choose a slower cadence if that
  matters to you.
- Scheduled checks need the Claude desktop app's scheduled tasks, or a cron
  job running `claude -p "/family-resort-watch:watch <trip>"`.
- Both data sources are unofficial and can change shape without notice. When
  the hotel engine changes, the check stops and says so instead of writing
  bad numbers.
- The tool only reads prices. It never books, holds, signs in or contacts
  anyone.
- Not affiliated with Hyatt or Google. For personal use: keep the cadence
  modest.

## Development

```bash
cd python
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/frw --help
```

To try the plugin from a checkout, run `claude --plugin-dir .`.

## License

MIT
