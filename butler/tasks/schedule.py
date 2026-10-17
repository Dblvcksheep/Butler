import re
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MIN_INTERVAL = 300  # 5 min floor protects LLM spend


def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError("bad time")
    return h, m


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown timezone {tz}")


def parse_schedule(text: str, tz: str, now: float) -> tuple[str, str]:
    """'daily 09:00' | 'weekly mon 09:00' | 'every 30m' | 'every 2h' | 'once 2026-10-20T09:00'"""
    _zone(tz)
    t = text.lower().split()
    try:
        if t[0] == "now" and len(t) == 1:
            return "now", ""
        if t[0] == "after" and len(t) == 2 and re.fullmatch(r"[a-z0-9]+(,[a-z0-9]+){0,4}", t[1]):
            return "after", ",".join(sorted(set(t[1].split(","))))      # up to 5 parents
        if t[0] == "daily" and len(t) == 2:
            _hm(t[1]); spec = t[1]
        elif t[0] == "weekly" and len(t) == 3 and t[1] in DAYS:
            _hm(t[2]); spec = f"{t[1]} {t[2]}"
        elif t[0] == "every" and len(t) == 2 and t[1][-1] in "mh":
            secs = int(t[1][:-1]) * (60 if t[1][-1] == "m" else 3600)
            if secs < MIN_INTERVAL:
                raise ValueError("minimum interval is 5m")
            spec = str(secs)
        elif t[0] == "once" and len(t) == 2:
            datetime.fromisoformat(t[1]); spec = t[1]
        else:
            raise ValueError("bad schedule")
    except (ValueError, IndexError) as e:
        raise ValueError(f"{e}. Use: now | after <task_id>[,<task_id>...] | daily HH:MM | weekly mon HH:MM | every 30m | once 2026-10-20T09:00")
    kind = t[0]
    if next_run(kind, spec, tz, now) is None:
        raise ValueError("that time is in the past")
    return kind, spec


def next_run(kind: str, spec: str, tz: str, after: float) -> float | None:
    z = _zone(tz)
    if kind == "now":
        return after
    if kind == "after":
        return None          # trigger-driven, never time-driven
    if kind == "once":
        ts = datetime.fromisoformat(spec).replace(tzinfo=z).timestamp()
        return ts if ts > after else None
    if kind == "every":
        return after + int(spec)
    d0: date = datetime.fromtimestamp(after, z).date()
    if kind == "daily":
        h, m = _hm(spec)
        for add in range(3):
            c = datetime.combine(d0 + timedelta(days=add), dtime(h, m), tzinfo=z).timestamp()
            if c > after:
                return c
    if kind == "weekly":
        day, hm = spec.split()
        h, m = _hm(hm)
        for add in range(8):
            d = d0 + timedelta(days=add)
            if d.weekday() == DAYS.index(day):
                c = datetime.combine(d, dtime(h, m), tzinfo=z).timestamp()
                if c > after:
                    return c
    raise ValueError("bad schedule")
