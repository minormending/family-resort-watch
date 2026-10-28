"""What a family would actually pay the airline, from Google Flights' booking page.

A Google Flights result's price is the airline's own cheapest fare matching
the search (it is not a third-party site's price, even when one is cheaper).
That fare can be the airline's bottom tier: Delta Main Basic or JetBlue Main
Base include a carry-on, so the carry-on filter keeps them, but they come
with no seat selection or seats for a fee. A family that wants to sit
together books the next tier up, often $80-100 more per person.

The tiers are only on the booking page, which Google fills in with a
GetBookingResults call that carries a BotGuard token only Google's own page
script can make. So this drives a real headless Chrome over its debugging
pipe (no extra Python packages): load the search with the outbound chosen,
click the return, and read that call's response.

For each route and stay, `scan` looks up each airline's cheapest pairing and
applies the gap between Google's list price and the chosen tier to all of
that airline's flights on that route and stay. The chosen tier is the
cheapest one sold by the airline itself with free seat selection and a
carry-on; failing that, the cheapest airline tier not marked basic; failing
that, the airline's own price as listed.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
)

# Feature codes in a fare tier's [code, value] pairs, read off the booking
# page's own labels (2026-10-28).
SEAT = 4        # 1 free seat selection, 2 for a fee, 4 none
CARRY_ON = 8    # 1 included


class FareError(RuntimeError):
    pass


@dataclass(frozen=True)
class Fare:
    seller: str
    airline_direct: bool
    price: int  # round trip, whole party
    tier: str | None = None
    basic: bool = False
    seat: int | None = None
    carry_on: int | None = None


@dataclass(frozen=True)
class Choice:
    """The fare picked for one airline on one route and stay."""

    airline: str
    list_price: int  # Google's result price for the looked-up pairing
    price: int  # the picked fare, same pairing
    tier: str | None
    basis: str  # seat | not_basic | airline | third_party
    list_tier: str | None = None  # the airline tier Google's result price is, when one matches

    @property
    def uplift(self) -> int:
        return self.price - self.list_price


def find_chrome(configured: str | None = None) -> str | None:
    for c in ((configured,) if configured else CHROME_CANDIDATES):
        path = c if os.path.isabs(c) else shutil.which(c)
        if path and os.path.exists(path):
            return path
    return None


def _wrb(text: str):
    """Each decoded payload in a batchexecute response."""
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("[["):
            continue
        try:
            entries = json.loads(line)
        except json.JSONDecodeError:
            continue
        for e in entries:
            if isinstance(e, list) and len(e) > 2 and e[0] == "wrb.fr" and isinstance(e[2], str):
                try:
                    yield json.loads(e[2])
                except json.JSONDecodeError:
                    continue


def parse_booking(text: str) -> list[Fare]:
    """Every booking option in a GetBookingResults response, one per fare tier."""
    fares = []
    for d in _wrb(text):
        groups = d[1] if isinstance(d, list) and len(d) > 1 and isinstance(d[1], list) else []
        for grp in groups:
            for o in grp if isinstance(grp, list) else []:
                try:
                    seller = o[1][0]
                    if not (isinstance(seller, list) and len(seller) == 4 and isinstance(seller[1], str)):
                        continue
                    price = o[7][0][1]
                    if not isinstance(price, int) or price <= 0:
                        continue
                except (IndexError, TypeError):
                    continue
                t = o[21] if len(o) > 21 and isinstance(o[21], list) and len(o[21]) > 3 else None
                feats = {f[0]: f[1] for f in (t[1] or []) if isinstance(f, list) and len(f) == 2} if t else {}
                fares.append(Fare(
                    seller=seller[1], airline_direct=seller[3] is True, price=price,
                    tier=t[3] if t and isinstance(t[3], str) else None, basic=bool(t and t[2]),
                    seat=feats.get(SEAT), carry_on=feats.get(CARRY_ON),
                ))
    return fares


def choose(airline: str, list_price: int, fares: list[Fare]) -> Choice:
    direct = [f for f in fares if f.airline_direct]
    listed = next((f.tier for f in sorted(direct, key=lambda f: f.price) if f.price == list_price), None)
    with_bag = [f for f in direct if f.carry_on in (1, None)]
    for basis, pool in (
        ("seat", [f for f in with_bag if f.seat == 1]),
        ("not_basic", [f for f in with_bag if f.tier and not f.basic]),
        ("airline", direct),
    ):
        if pool:
            best = min(pool, key=lambda f: f.price)
            return Choice(airline, list_price, best.price, best.tier, basis, listed)
    # Sold only by third parties: keep Google's price, but say so.
    return Choice(airline, list_price, list_price, None, "third_party")


def clock(hhmm: str) -> str:
    """'14:05' -> '2:05 PM', as the results list prints it."""
    h, m = (int(x) for x in hhmm[:5].split(":"))
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


class Browser:
    """One headless Chrome, driven over --remote-debugging-pipe (fd 3 in, fd 4 out)."""

    def __init__(self, chrome: str, timeout: float = 25.0, settle: float = 4.0):
        self.timeout = timeout
        # Seconds to let the results page finish loading its scripts before
        # clicking. Clicked too early, the booking call goes out without a
        # valid BotGuard token and Google answers with error 13.
        self.settle = settle
        self._profile = tempfile.mkdtemp(prefix="frw-chrome-")
        cmd_r, self._cmd_w = os.pipe()
        self._resp_r, resp_w = os.pipe()

        def wire():  # in the child: put the pipes where Chrome expects them
            a, b = os.dup(cmd_r), os.dup(resp_w)
            os.dup2(a, 3)
            os.dup2(b, 4)
            # dup2 onto itself (when dup returned 3 or 4) keeps close-on-exec,
            # so set it explicitly. Every other descriptor Python opened is
            # close-on-exec, so only these two reach Chrome.
            os.set_inheritable(3, True)
            os.set_inheritable(4, True)

        self._proc = subprocess.Popen(
            [chrome, "--headless=new", "--remote-debugging-pipe", f"--user-data-dir={self._profile}",
             "--no-first-run", "--no-default-browser-check", "--window-size=1280,900", "about:blank"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=wire, close_fds=False,
        )
        os.close(cmd_r)
        os.close(resp_w)
        self._id = 0
        self._replies: dict[int, queue.Queue] = {}
        self._events: queue.Queue = queue.Queue()
        self._session = None
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf = b""
        while True:
            try:
                chunk = os.read(self._resp_r, 1 << 16)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\0" in buf:
                raw, buf = buf.split(b"\0", 1)
                msg = json.loads(raw)
                if "id" in msg and msg["id"] in self._replies:
                    self._replies[msg["id"]].put(msg)
                elif "method" in msg:
                    self._events.put(msg)

    def call(self, method: str, params: dict | None = None, session: bool = False) -> dict:
        self._id += 1
        i = self._id
        self._replies[i] = q = queue.Queue()
        msg = {"id": i, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = self._session
        os.write(self._cmd_w, json.dumps(msg).encode() + b"\0")
        try:
            reply = q.get(timeout=self.timeout)
        except queue.Empty:
            raise FareError(f"chrome: {method} timed out") from None
        finally:
            self._replies.pop(i, None)
        if "error" in reply:
            raise FareError(f"chrome: {method}: {reply['error'].get('message')}")
        return reply.get("result", {})

    def _eval(self, expression: str):
        r = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, session=True)
        return (r.get("result") or {}).get("value")

    def booking_options(self, search_url: str, depart: str, airline: str, attempts: int = 2) -> str:
        """Open a returns list, click the return leaving at `depart` on `airline`, return GetBookingResults.

        Each attempt gets a fresh tab, so nothing carries over from the last
        booking page.
        """
        for attempt in range(attempts):
            try:
                return self._in_new_tab(search_url, depart, airline)
            except FareError as exc:
                if attempt == attempts - 1:
                    raise
                log.debug("retrying %s %s: %s", airline, depart, exc)
                time.sleep(self.settle)
        raise AssertionError("unreachable")

    def _in_new_tab(self, search_url: str, depart: str, airline: str) -> str:
        target = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        try:
            self._session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
            while not self._events.empty():
                self._events.get_nowait()
            self.call("Network.enable", {}, session=True)
            return self._booking_options(search_url, depart, airline)
        finally:
            self.call("Target.closeTarget", {"targetId": target})
            self._session = None

    def _booking_options(self, search_url: str, depart: str, airline: str) -> str:
        self.call("Page.navigate", {"url": search_url}, session=True)
        label = clock(depart)
        find = (
            # Google prints times with a narrow no-break space ("7:10\u202fPM").
            "(() => { const text = l => l.innerText.replace(/\\s+/g, ' ').trim();"
            " const li = [...document.querySelectorAll('li')].find(l => "
            f"text(l).startsWith({json.dumps(label)}) && text(l).includes({json.dumps(airline)})"
            " && /round trip/.test(text(l))); if (!li) return null; li.scrollIntoView({block: 'center'});"
            " const r = li.getBoundingClientRect(); return {x: r.left + r.width * 0.3, y: r.top + r.height / 2}; })()"
        )
        deadline = time.monotonic() + self.timeout
        at = None
        while time.monotonic() < deadline and not at:
            time.sleep(0.5)
            at = self._eval(find)
        if at:
            time.sleep(self.settle)
            at = self._eval(find)  # the list may have shifted while it settled
        if not at:
            seen = self._eval("document.body ? document.body.innerText.replace(/\\s+/g, ' ').slice(0, 160) : ''")
            raise FareError(f"no {airline} return at {label} on the page ({seen!r})")
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent",
                      {"type": kind, "x": at["x"], "y": at["y"], "button": "left", "clickCount": 1}, session=True)
        wanted: set[str] = set()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                ev = self._events.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                break
            p = ev.get("params", {})
            if ev["method"] == "Network.requestWillBeSent" and "GetBookingResults" in p.get("request", {}).get("url", ""):
                wanted.add(p["requestId"])
            elif ev["method"] == "Network.loadingFinished" and p.get("requestId") in wanted:
                body = self.call("Network.getResponseBody", {"requestId": p["requestId"]}, session=True)
                if body.get("body") and parse_booking(body["body"]):
                    return body["body"]
                log.debug("booking response without options: %s", (body.get("body") or "")[:300])
            elif ev["method"] == "Network.loadingFailed" and p.get("requestId") in wanted:
                log.debug("booking request failed: %s", p.get("errorText"))
        raise FareError(f"no booking options for {airline} at {label} ({len(wanted)} booking requests seen)")

    def close(self):
        try:
            self.call("Browser.close")
        except FareError:
            pass
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        for fd in (self._cmd_w, self._resp_r):
            try:
                os.close(fd)
            except OSError:
                pass
        shutil.rmtree(self._profile, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
