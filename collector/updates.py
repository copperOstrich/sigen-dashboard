"""Is there a newer collector release? Asks GitHub at most twice a day, in the background.

Only the public releases list is read (no account, nothing about your system is sent beyond the
request itself). Turn it off with UPDATE_CHECK=off.
"""
import json
import re
import threading
import time
import urllib.request

REPO = "copperOstrich/sigen-dashboard"
EVERY_S = 12 * 3600
RETRY_S = 3600


def _parts(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def newer(latest, current):
    return _parts(latest) > _parts(current)


class UpdateCheck:
    def __init__(self, current, enabled=True, log=print, fetch=None):
        self.current, self.enabled, self.log = current, enabled, log
        self._fetch = fetch or self._github
        self._at, self._busy, self.info = 0.0, False, None
        self._lock = threading.Lock()

    def _github(self):
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                     headers={"Accept": "application/vnd.github+json",
                                              "User-Agent": "sigen-dashboard/" + self.current})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
        return {"latest": (d.get("tag_name") or "").lstrip("v"), "url": d.get("html_url"),
                "published": (d.get("published_at") or "")[:10], "notes": (d.get("body") or "")[:4000]}

    def _run(self):
        try:
            info = self._fetch()
            info["newer"] = newer(info.get("latest"), self.current)
            if info["newer"] and (not self.info or self.info.get("latest") != info["latest"]):
                self.log(f"Update: collector {info['latest']} is available (this is {self.current})")
            self.info, self._at = info, time.time()
        except Exception as e:  # offline, rate limited: try again in an hour
            self.info = {**(self.info or {}), "error": str(e)[:120]}
            self._at = time.time() - EVERY_S + RETRY_S
        finally:
            self._busy = False

    def status(self, wait=False):
        """What's known now; starts a check in the background when the last one is stale."""
        if not self.enabled:
            return {"enabled": False, "current": self.current}
        with self._lock:
            start = not self._busy and time.time() - self._at > EVERY_S
            if start:
                self._busy = True
        if start:
            t = threading.Thread(target=self._run, daemon=True)
            t.start()
            if wait:
                t.join(20)
        out = {"enabled": True, "current": self.current}
        if self.info:
            out.update({k: v for k, v in self.info.items() if k in ("latest", "url", "published", "newer", "error", "notes")})
        return out
