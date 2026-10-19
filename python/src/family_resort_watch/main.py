from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import config


def _party(cfg) -> str:
    kids = ", ".join(f"child ({a})" if a >= 2 else f"baby ({a})" for a in cfg.party.child_ages)
    return f"{cfg.party.adults} adult{'s' if cfg.party.adults > 1 else ''}" + (f" + {kids}" if kids else "")


def context(cfg) -> dict:
    """Everything a session needs to know about one trip, so skills stay generic."""
    f = cfg.flights
    return {
        "trip": config.TRIP,
        "title": cfg.trip.get("title"),
        "dates": cfg.trip.get("recess_label"),
        "party": _party(cfg),
        "home": f"{f.home_label} ({', '.join(f.departure_airports)})" if f else None,
        "flights": ("nonstop only" if f and f.max_stops == 0 else f"at most {f.max_stops} stop(s)") if f else None,
        "seats_priced": f.proxy_adults if f else None,
        "preferred_stays": [w.key for w in cfg.primary],
        "other_stays": [w.key for w in cfg.windows if w.tier != "primary"],
        "artifact": cfg.trip.get("artifact"),
        "changes": str(config.REPORT_DIR / "changes.md"),
        "analysis": str(config.REPORT_DIR / "analysis.json"),
        "publish_plan": str(config.REPORT_DIR / "publish_plan.json"),
        "page": str(config.PAGE_PATH),
    }


def resorts(conn, cfg) -> list[dict]:
    """The trip's family resorts, their airports and whose review each one has."""
    from . import reviews

    found, _ = reviews.load_resorts()
    own = config.HOME / "research" / "resorts"
    rows = conn.execute(
        "SELECT code, name, country, area FROM hotels WHERE takes_party = 1 ORDER BY country, area, name"
    ).fetchall()
    out = []
    for code, name, country, area in rows:
        if country not in cfg.countries:
            continue
        r = found.get(code)
        out.append({
            "code": code, "name": name, "country": country, "area": area,
            "airports": list(cfg.airports.get(area, ())),
            "review": None if r is None else ("own" if (own / f"{code}.json").exists() else "shipped"),
            "researched_on": r and r.get("researched_on"),
            "fit": r and r["fit"],
        })
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="frw", description="Watch family all-inclusive and flight prices.")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--home", default=str(config.DEFAULT_HOME), help="folder holding trips, data and reports")
    parser.add_argument("--trip", help="which trip (a <home>/trips/<name>.toml)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("trips", help="list trips and their artifacts")
    sub.add_parser("example", help="print example answers for new-trip")
    p = sub.add_parser("new-trip", help="create <home>/trips/<name>.toml from answers")
    p.add_argument("name")
    p.add_argument("--answers", required=True, help="JSON file (see `frw example`)")
    p.add_argument("--overwrite", action="store_true")
    p = sub.add_parser("set-artifact", help="remember the trip's artifact URL")
    p.add_argument("url")
    sub.add_parser("context", help="print what a session needs to know about the trip (JSON)")
    sub.add_parser("check", help="validate the trip config and research files")
    p = sub.add_parser("classify", help="refresh the hotel list and find which resorts take the party")
    p.add_argument("--force", action="store_true", help="re-probe every in-scope resort")
    p = sub.add_parser("scan", help="fetch prices for every family resort and due stay window")
    p.add_argument("--all-windows", action="store_true", help="ignore flex-window cadence")
    p = sub.add_parser("flights", help="search flights for every due route")
    p.add_argument("--all", action="store_true", help="search every route now, ignoring cadence")
    sub.add_parser("report", help="rebuild the trip's report from its database")
    p = sub.add_parser("reviews", help="validate research and print each resort's family-fit score")
    p.add_argument("--thin", action="store_true", help="list only reviews that need another research pass")
    sub.add_parser("resorts", help="list the trip's family resorts and whose review each has (JSON)")
    sub.add_parser("published", help="record the publish plan as written to the artifact db")
    sub.add_parser("page", help="render the trip's artifact page")
    p = sub.add_parser("run", help="classify if stale, scan hotels and due flights, then report (the hourly job)")
    p.add_argument("--all-windows", action="store_true")
    p.add_argument("--no-flights", action="store_true", help="skip the flight searches this run")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    home = Path(args.home)
    config.select(home)

    if args.command == "example":
        from . import trips

        print(json.dumps(trips.EXAMPLE, indent=2, ensure_ascii=False))
        return 0
    if args.command == "new-trip":
        from . import trips

        try:
            path = trips.create(config.HOME, args.name, json.loads(Path(args.answers).read_text()), args.overwrite)
        except (trips.AnswerError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        config.select(home, args.name)
        config.load()
        print(f"wrote {path}")
        return 0
    if args.command == "trips":
        for name in config.trips():
            config.select(home, name)
            print(f"{name}\t{config.load().trip.get('artifact') or '(no artifact yet)'}")
        return 0
    if args.command == "reviews" and not args.trip:
        args.trip = None
    elif not args.trip:
        names = config.trips()
        if len(names) != 1:
            print(f"error: pass --trip (trips in {config.HOME}: {', '.join(names) or 'none'})", file=sys.stderr)
            return 2
        args.trip = names[0]

    if args.trip:
        config.select(home, args.trip)
        cfg = config.load()
    if args.command == "set-artifact":
        from . import trips

        trips.set_artifact(config.CONFIG_PATH, args.url)
        print(f"artifact for {args.trip}: {args.url}")
        return 0
    if args.command == "context":
        print(json.dumps(context(cfg), indent=2, ensure_ascii=False))
        return 0
    if args.command in ("check", "reviews"):
        from . import reviews

        found, problems = reviews.load_resorts()
        if args.command == "reviews" and args.thin:
            for code, r in sorted(found.items()):
                why = reviews.thinness(r)
                if why:
                    print(f"{code:<13} {r['name']:<46} " + "; ".join(why))
            return 0
        if args.command == "reviews":
            for code, r in sorted(found.items(), key=lambda kv: -kv[1]["fit"]):
                scores = " ".join(f"{k[:5]}={r['criteria'][k]['score']}" for k in reviews.WEIGHTS)
                print(f"{r['fit']:>4}  {code:<13} {r['name']:<46} {scores}  ({r['confidence']})")
        for code, errs in sorted(problems.items()):
            print(f"INVALID {code}: " + "; ".join(errs))
        if args.command == "check":
            print(f"{config.CONFIG_PATH}: ok, {len(cfg.windows)} stays, {len(found)} reviewed resorts")
        return 1 if problems else 0
    if args.command == "page":
        from . import page

        print(f"wrote {page.build(cfg.trip)}")
        return 0
    if args.command == "published":
        from . import report

        print("marked published: " + ", ".join(report.mark_published()))
        return 0

    from . import scan, storage

    with storage.connect(config.DB_PATH) as conn:
        if args.command == "resorts":
            print(json.dumps(resorts(conn, cfg), indent=1, ensure_ascii=False))
            return 0
        if args.command in ("classify", "run"):
            counts = scan.classify(conn, cfg, force=getattr(args, "force", False))
            if any(counts.values()):
                print(f"classified: {counts}")
            conn.commit()
        if args.command in ("scan", "run"):
            windows = list(cfg.windows) if args.all_windows else None
            scan_id = scan.scan(conn, cfg, windows)
            pruned = storage.prune(conn)
            print(f"scan {scan_id} done" + (f", pruned {pruned} old offer rows" if pruned else ""))
        if args.command == "flights" or (args.command == "run" and not args.no_flights):
            counts = scan.scan_flights(conn, cfg, force=getattr(args, "all", False))
            if any(counts.values()):
                print(f"flights: {counts}")
        if args.command in ("report", "run"):
            from . import report

            for p in report.build(conn, cfg):
                print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
