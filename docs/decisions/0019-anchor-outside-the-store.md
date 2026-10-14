# 0019. An anchor for the log head, outside the store
- Status: Accepted, 2026-10-14
- Decided by the project owner on 2026-10-14.
- Date: 2026-10-12, revised 2026-10-14

## Context
The head of the log — the sequence number and hash of its last record — lives in the same
SQLite file as the records themselves. An attacker who owns the file can replace the whole
log with a consistently re-chained forged copy, move the head with it, and the verification
tool finds nothing (ATK-69). The threat model asks for truncation and full replacement to be
detected "through an external anchor or a stored head"; the stored head alone does not cover
the replacement.

The first version of the anchor read a missing or unreadable file as "no anchor", and rewrote
each stream's entry from the store at every append. Deleting the file beside the database
turned the check off, and so did the hub's next call after a replacement: the anchor followed
the forged head.

## Decision
The store keeps a second copy of each stream's head — the anchor — in a file beside the
database (`log-anchor.json`, written atomically). The verification tool compares the last
record and the stored head with the anchor; a mismatch is reported as a replacement or a
truncation, naming the first record that disagrees.

- **Start.** The anchor starts with the log: when the hub opens a store that holds no record
  and has no anchor, it creates an empty one and records `log.anchor` (`started`) in its own
  stream. Nothing starts an anchor again.
- **No silent fall-back.** A store that keeps an anchor never answers as if it kept none. An
  anchor that is gone, that cannot be read, or that does not name a stream that has records
  fails the verification of that stream with a problem that says so. When the hub opens the
  log with an anchor gone or unreadable, it records `log.anchor` (`missing` or `unreadable`,
  outcome error), goes on working, and leaves the file as it found it.
- **The anchor never follows the store.** It moves only from the head it names: before a
  record is stored, the anchor names it as the next record of its stream, provided that the
  record follows the anchor's head; once the record is stored, the anchor moves to it. A
  stream absent from the anchor comes back only with its first record. A store whose records
  or heads were replaced therefore stays detected after the hub appends to it.
- **A stop between the two writes is not a tampering.** An entry that names a next record
  counts as that record when the store holds exactly it (same sequence number, same hash), and
  as its own head otherwise.

## Consequences
- Positive: a full replacement of the database file is detected, before and after the hub's
  next call; a truncation is detected even when the head table was moved with it.
- Positive: deleting, corrupting or emptying the anchor of a stream is reported, never ignored.
- Positive: nothing changes for the callers; the anchor is written by the store.
- Negative: an attacker with write access to the whole data directory can still forge both the
  log and the anchor. The anchor belongs on another medium (a second host, a read-only mount)
  when the threat model requires it. Accepted as it is, with this limit stated; an anchor on
  another medium would be a later decision.
- Negative: an anchor lost by accident (a backup restored without it) keeps the verification
  failing: the anchor is part of the backup of the data directory. Taking a new anchor from the
  store is an operation of its own, by the operator and recorded, that the hub does not offer
  yet.
- Negative: two writes of a small file for each record.

## Alternatives considered
- A remote anchor (a second service or a syslog destination): more moving parts for a
  single-host deployment; kept for the strict level of the strictness setting.
- Signing each head with a key held elsewhere: the same trust question, one more secret to
  keep; may replace this record in a later revision.
- Starting a new anchor from the store whenever none is found: it would accept whatever the
  store holds at that moment, which is exactly what an attacker who deletes the file wants.
