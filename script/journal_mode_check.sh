#!/usr/bin/env bash
# Live SQLite on an aloelite FUSE mount — journal-mode certification.
# Usage: ./journal_mode_check.sh /mnt/photos
#
# Recipe notes (document these alongside):
#  - journal_mode=PERSIST (or TRUNCATE) avoids unlink-while-open of the
#    journal; DELETE mode is untested against path-keyed handles.
#  - Every page write mints a content version: set a retention policy on
#    the db file and run prune_content periodically, e.g.
#      m.set_retention("/test.db", keep=1); fs.prune_content()
set -e
MNT="${1:?usage: $0 <mountpoint>}"
DB="$MNT/test.db"
rm -f "$DB"

echo "== create + insert (PERSIST journal) =="
sqlite3 "$DB" <<'SQL'
PRAGMA journal_mode=PERSIST;
CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);
INSERT INTO t(v) VALUES ('one'),('two'),('three');
SQL

echo "== read back =="
test "$(sqlite3 "$DB" 'SELECT count(*) FROM t;')" = "3"

echo "== multi-txn churn =="
for i in $(seq 1 20); do
  sqlite3 "$DB" "INSERT INTO t(v) VALUES ('row$i'); DELETE FROM t WHERE id % 7 = 0;"
done

echo "== concurrent reader during writer =="
( for i in $(seq 1 10); do sqlite3 "$DB" "PRAGMA busy_timeout=5000; INSERT INTO t(v) VALUES('w$i');"; done ) &
W=$!
for i in $(seq 1 10); do sqlite3 "$DB" "PRAGMA busy_timeout=5000; SELECT count(*) FROM t;" >/dev/null; done
wait $W

echo "== integrity =="
test "$(sqlite3 "$DB" 'PRAGMA integrity_check;')" = "ok"

echo "== TRUNCATE mode too =="
sqlite3 "$DB" "PRAGMA journal_mode=TRUNCATE; INSERT INTO t(v) VALUES('trunc'); PRAGMA integrity_check;" | tail -1 | grep -qx ok

echo "ALL PASS"
