# 0019. An anchor for the log head, outside the store
- Status: Proposed
- Date: 2026-10-12

## Context
The head of the log — the sequence number and hash of its last record — lives in the same
SQLite file as the records themselves. An attacker who owns the file can replace the whole
log with a consistently re-chained forged copy, move the head with it, and the verification
tool finds nothing (ATK-69). The threat model asks for truncation and full replacement to be
detected "through an external anchor or a stored head"; the stored head alone does not cover
the replacement.

## Decision
The store keeps a second copy of each stream's head — the anchor — in a file beside the
database (`log-anchor.json`, written atomically after every append). The verification tool
compares the last record and the stored head with the anchor; a mismatch is reported as a
replacement or a truncation, naming the first record that disagrees. The anchor is not
authoritative: when it is absent, the log verifies on its own head alone.

## Consequences
- Positive: a full replacement of the database file is detected; a truncation is detected even
  when the head table was moved with it.
- Positive: nothing changes for the callers; the anchor is written by the store.
- Negative: an attacker with write access to the whole data directory can still forge both the
  log and the anchor. The anchor belongs on another medium (a second host, a read-only mount)
  when the threat model requires it — the reason this record stays Proposed.
- Negative: one more file to keep with the data directory in backups.

## Alternatives considered
- A remote anchor (a second service or a syslog destination): more moving parts for a
  single-host deployment; kept for the strict level of the strictness setting.
- Signing each head with a key held elsewhere: the same trust question, one more secret to
  keep; may replace this record in a later revision.
