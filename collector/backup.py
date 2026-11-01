"""Nightly backups of the database, so years of history survive a failed disk or a bad upgrade.

SQLite's online backup copies the database while the collector keeps writing to it; the copy is
checked, gzipped and kept in the backups folder (default: "backups" in the data folder) as
sigen-YYYY-MM-DD.db.gz. The most recent BACKUP_KEEP_DAILY days are kept, plus one a week for
BACKUP_KEEP_WEEKLY weeks before that.

To restore: stop the container, unzip a backup over sigen.db (and delete sigen.db-wal and
sigen.db-shm if they exist), start it again.
"""
import gzip
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

NAME = re.compile(r"^(?P<stem>[a-z]+)-(?P<day>\d{4}-\d\d-\d\d)\.db\.gz$")


class Backups:
    def __init__(self, db_path, folder, tz, keep_daily=7, keep_weekly=4, at="02:30", enabled=True,
                 event=None, log=print):
        self.db_path, self.folder, self.tz = Path(db_path), Path(folder), tz
        self.stem = self.db_path.stem  # "sigen", or "simulator" in the simulator
        self.keep_daily, self.keep_weekly = max(1, int(keep_daily)), max(0, int(keep_weekly))
        try:
            h, m = (int(x) for x in (at or "02:30").split(":"))
            self.at = (h % 24, m % 60)
        except ValueError:
            self.at = (2, 30)
        self.enabled, self.event, self.log = enabled, event or (lambda k, m: None), log
        self.lock = threading.Lock()
        self.busy = False
        self.last_error = None
        self.last_run = None  # {"ts", "file", "bytes", "seconds"}

    # ---------------------------------------------------------------- listing
    def files(self):
        out = []
        if self.folder.is_dir():
            for f in self.folder.iterdir():
                m = NAME.match(f.name)
                if m and m.group("stem") == self.stem:
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    out.append({"file": f.name, "day": m.group("day"), "bytes": st.st_size, "ts": int(st.st_mtime)})
        return sorted(out, key=lambda x: x["day"], reverse=True)

    def status(self):
        files = self.files() if self.enabled else []
        last = files[0] if files else None
        return {"enabled": self.enabled, "busy": self.busy, "error": self.last_error,
                "last": self.last_run or last, "count": len(files), "bytes": sum(f["bytes"] for f in files),
                "next": self.next_run() if self.enabled else None,
                "keep_daily": self.keep_daily, "keep_weekly": self.keep_weekly, "folder": self.folder.name}

    def next_run(self, now=None):
        now = datetime.fromtimestamp(now or time.time(), self.tz)
        t = now.replace(hour=self.at[0], minute=self.at[1], second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        return int(t.timestamp())

    # ---------------------------------------------------------------- running
    def run(self):
        if not self.enabled:
            return
        time.sleep(600)  # let the collector settle first
        files = self.files()
        if not files or time.time() - files[0]["ts"] > 26 * 3600:
            self.backup_now()  # none yet, or the last one is old: don't wait for tonight
        while True:
            time.sleep(max(30, self.next_run() - time.time()))
            self.backup_now()

    def backup_now(self):
        with self.lock:
            if self.busy:
                return {"ok": False, "error": "a backup is already running"}
            self.busy = True
        t0 = time.time()
        day = datetime.fromtimestamp(t0, self.tz).date().isoformat()
        final = self.folder / f"{self.stem}-{day}.db.gz"
        tmp = self.folder / f".{self.stem}-{day}.db.tmp"
        part = self.folder / f".{final.name}.part"
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            size = self.db_path.stat().st_size
            free = shutil.disk_usage(self.folder).free
            if free < size * 1.5:
                raise OSError(f"only {free / 1e6:.0f} MB free in {self.folder}, the database is {size / 1e6:.0f} MB")
            src = sqlite3.connect(str(self.db_path), timeout=30)
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst, pages=2048, sleep=0.01)  # in steps, so the collector keeps writing
                ok = dst.execute("PRAGMA quick_check").fetchone()[0]
                if ok != "ok":
                    raise sqlite3.DatabaseError(f"the copy failed its check: {ok}")
            finally:
                dst.close()
                src.close()
            with open(tmp, "rb") as fi, gzip.open(part, "wb", compresslevel=6) as fo:
                shutil.copyfileobj(fi, fo, 1 << 20)
            os.replace(part, final)
            secs = round(time.time() - t0, 1)
            self.last_run = {"ts": int(time.time()), "file": final.name, "bytes": final.stat().st_size, "seconds": secs}
            if self.last_error:
                self.event("backup", "Database backups working again")
            self.last_error = None
            self.log(f"Backup: {final.name} ({final.stat().st_size / 1e6:.1f} MB) in {secs:g} s")
            self.prune()
            return {"ok": True, **self.last_run}
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if msg != self.last_error:
                self.event("backup", f"Database backup failed: {msg}")
            self.last_error = msg
            self.log("Backup failed:", msg)
            return {"ok": False, "error": msg}
        finally:
            for f in (tmp, part):
                try:
                    f.unlink()
                except OSError:
                    pass
            self.busy = False

    def prune(self):
        """Keep the newest keep_daily backups, then the newest of each week for keep_weekly weeks."""
        files = self.files()
        keep = {f["file"] for f in files[:self.keep_daily]}
        weeks = []
        for f in files[self.keep_daily:]:
            wk = date.fromisoformat(f["day"]).isocalendar()[:2]
            if wk not in weeks and len(weeks) < self.keep_weekly:
                weeks.append(wk)
                keep.add(f["file"])
        for f in files:
            if f["file"] not in keep:
                try:
                    (self.folder / f["file"]).unlink()
                except OSError as e:
                    self.log(f"Backup: couldn't remove {f['file']}: {e}")
