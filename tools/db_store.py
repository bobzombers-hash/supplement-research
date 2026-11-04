#!/usr/bin/env python3
"""Pack / unpack the SQLite database for storage as a gzip release asset.

  python tools/db_store.py pack   data/research.db  dist/research.db.gz   # consistent snapshot + integrity check + gzip
  python tools/db_store.py unpack data/research.db.gz data/research.db    # gunzip + integrity check (atomic)

Standard library only, so it works before any dependencies are installed.
"""
import gzip, hashlib, os, shutil, sqlite3, sys
from pathlib import Path

def check(db):
    con = sqlite3.connect(str(db))
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        n = con.execute("SELECT count(*) FROM articles").fetchone()[0]
    finally:
        con.close()
    if ok != "ok":
        sys.exit(f"integrity check failed for {db}: {ok}")
    return n

def pack(src, dst):
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    snap = dst.with_name(dst.name + ".snapshot.db")
    s = sqlite3.connect(str(src)); d = sqlite3.connect(str(snap))
    s.backup(d); d.execute("VACUUM"); d.close(); s.close()
    n = check(snap)
    tmp = dst.with_name(dst.name + ".part")
    with open(snap, "rb") as f, open(tmp, "wb") as raw:
        with gzip.GzipFile(filename="research.db", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as g:
            shutil.copyfileobj(f, g, 1 << 20)
    os.replace(tmp, dst); snap.unlink()
    h = hashlib.sha256(dst.read_bytes()).hexdigest()
    dst.with_name(dst.name + ".sha256").write_text(f"{h}  {dst.name}\n", encoding="utf-8")
    print(f"packed {src} -> {dst} ({dst.stat().st_size / 1e6:.1f} MB, {n} articles, sha256 {h[:12]})")

def unpack(src, dst):
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    with gzip.open(src, "rb") as g, open(tmp, "wb") as f:
        shutil.copyfileobj(g, f, 1 << 20)
    n = check(tmp)
    os.replace(tmp, dst)
    print(f"unpacked {src} -> {dst} ({dst.stat().st_size / 1e6:.1f} MB, {n} articles)")

if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in ("pack", "unpack"):
        sys.exit(__doc__)
    (pack if sys.argv[1] == "pack" else unpack)(sys.argv[2], sys.argv[3])
