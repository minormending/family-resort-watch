# Family-fit research brief

Researchers rate seven criteria from 1 to 5 with evidence. Code turns those
into the family-fit score (`family_resort_watch.reviews`), so the scale stays
the same across resorts and researchers. Don't write an overall score.

The research skill puts a **Trip** block above this brief, filled in from
`frw context`: who is travelling (adults and children's ages), the dates,
the home airports, and anything the family asked to weigh (a diet, a
stroller, a child who won't join a kids' club). Read every criterion for
*that* party and *those* dates. "The youngest child" below means the
youngest child in the Trip block. Prices are tracked elsewhere, so don't
research rates.

The reviews that ship with the tool were researched on 2026-10-03 for two
adults and a young child travelling in late April. Their scores are a
reasonable starting point for other families; redoing them for your own
party is what the research skill is for.

## Criteria and anchors

Use the anchors. A 4 or a 2 means "between the anchors". When evidence is
thin, score 3 and say so in the evidence.

| Criterion | Weight | 5 | 3 | 1 |
|---|---|---|---|---|
| `kids_club` | 22% | Included at no charge, takes the party's children without a parent, long hours (~9am–9pm), its own space, consistently good reports | Exists and takes the children, but short hours, a fee, or mixed reports | None for the children's ages, or poorly reviewed |
| `water_play` | 15% | Splash pad or water park with slides, plus a shallow kids' pool | Kids' pool only | Nothing aimed at children |
| `beach` | 18% | Calm, swimmable, soft sand, low sargassum risk in the travel month | Fine beach with real seasonal sargassum risk, or some swell or rocks | Rough, rocky or unswimmable, or sargassum usually heavy in the travel month |
| `rooms` | 10% | Standard rooms fit the party comfortably (2 beds or family suites), connecting rooms available | Fits with a sofa bed or a tight squeeze | Hard for this party |
| `food` | 15% | Many à la carte restaurants, little reservation friction, child-friendly, suits any diet in the Trip block | Adequate variety with recurring complaints | Limited or poorly reviewed |
| `transfer` | 8% | ≤30 min from the main airport | 45–75 min | Over 2 hours |
| `condition` | 12% | New or recently renovated, consistently good reviews from the last two years | Mixed or dated in places | Run down, or poor recent reviews |

Transfer anchors in between: 4 = 30–45 min, 2 = 75–120 min. Use the
resort's area airport from `frw` (`data/airports.toml`, plus any
`[airports]` override in the trip).

Sargassum: spring and summer are the season in Punta Cana, the Riviera Maya
and much of the Caribbean. Say what the resort's own beach is like in the
travel month (some resorts sit on protected bays or have reef breaks), and
set `sargassum_late_april` to low, moderate, high or unknown. The field name
is historical: rate the travel month, and say which month in the evidence.

## Output (one JSON file per resort)

`<home>/research/resorts/<code>.json` (a file here overrides the shipped
review for that resort):

```json
{
  "code": "sucdp",
  "name": "Sunscape Coco Punta Cana",
  "researched_on": "YYYY-MM-DD",
  "criteria": {
    "kids_club":  {"score": 4, "evidence": "..."},
    "water_play": {"score": 3, "evidence": "..."},
    "beach":      {"score": 3, "evidence": "..."},
    "rooms":      {"score": 4, "evidence": "..."},
    "food":       {"score": 3, "evidence": "..."},
    "transfer":   {"score": 5, "evidence": "..."},
    "condition":  {"score": 3, "evidence": "..."}
  },
  "kids_club": {"name": "Explorer's Club", "ages": "3-12", "hours": "9am-5pm", "cost": "included", "notes": "..."},
  "transfer_minutes": {"PUJ": 25},
  "sargassum_late_april": "moderate",
  "pros": ["...", "..."],
  "cons": ["...", "..."],
  "verdict": "One sentence for this family.",
  "confidence": "high | medium | low",
  "sources": [{"title": "...", "url": "https://..."}]
}
```

Rules:

- Evidence: 1–2 short sentences per criterion, in your own words.
- Pros and cons: 3–5 each, short and specific to this party. Say "the kids'
  club closes at 5pm", not "great for families".
- Don't paste or closely paraphrase reviews. Summarize what several sources
  agree on, and quote nothing longer than a few words.
- Every factual claim should trace to a listed source. Prefer the resort's
  own site (hyattinclusivecollection.com, bahia-principe.com), then recent
  traveller reports (Reddit, family travel blogs, TripAdvisor if it loads),
  then news. Use at least 3 sources per resort.
- Say "unverified" rather than guess. Mark `confidence` low when sources are
  thin or old.
- Note anything recent: renovations, rebrands, closures, construction.
- Research only. Don't contact resorts, sign up, or book anything.

## Airlines

`<home>/research/airlines.json` describes airlines, not individual flights:
timing flags (late arrivals, very early departures) are computed in code
from the scraped times. A file there overrides the shipped one.

```json
{
  "researched_on": "YYYY-MM-DD",
  "airlines": {
    "Arajet": {
      "summary": "...",
      "fare_includes": "what the cheapest non-Basic economy fare includes: carry-on, checked bag, seat selection",
      "family_notes": "seating families together, kids' meals or entertainment, stroller and car seat rules",
      "seat_and_comfort": "seat pitch, screens, wifi, power",
      "reliability": "on-time and cancellation record",
      "pros": ["..."], "cons": ["..."],
      "sources": [{"title": "...", "url": "..."}]
    }
  }
}
```
