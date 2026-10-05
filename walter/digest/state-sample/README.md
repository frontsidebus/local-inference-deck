# Digest state sample (sanitized)

One file per watch, with the same structure as the real watch state files the pipeline reads and
writes at `STATE_DIR/state/<watch>.json` (`/srv/digest/state/state/<watch>.json` on Walter):

| File | Watch | `seen` buckets |
|---|---|---|
| `default.json` | Threat Intel | `cves`, `events` |
| `ai-security.json` | AI Security | `papers`, `items` |
| `ai-research.json` | AI Research | `papers`, `items` |

**Every value is invented.** The ids (`CVE-2099-0001`, arXiv-style `9999.00001`,
`ICSA-99-999-01`), titles ("Sample advisory 1"), URLs (`example.com` / `example.org`), dates
(January 2026) and error texts are made up. Only the key names, the nesting, the value types and the
date formats come from the real files. The source names are the collectors' own `SOURCES` keys
(they must match for the per-source cutoffs to apply). Each bucket has a handful of entries; real
files have tens to hundreds.

These files are not deployed (`walter/deploy.sh` installs only `build/`, the compose file and
the README) and are **not a seed**: never install them on a host.

**Who uses them**
- `tests/test_state_sample.py` loads them through the real pipeline functions (`_load_state`,
  `_dedupe`, `run_watch`).
- Agents changing state handling should read these files, not the live state. The agent
  judge's R8 code review includes data files the agent read or the diff names, but only from
  infra-class locations, so it can see these files and cannot see the real ones.

## Fields

Top level:

| Key | Type | Meaning |
|---|---|---|
| `watch_slug` | string | the watch (`default`, `ai-security`, `ai-research`) |
| `created` | ISO 8601 UTC | when the state file was first written |
| `last_run` | ISO 8601 UTC | set by `_save_state` on every save |
| `cutoff` | ISO 8601 UTC or null | the watch-level cutoff: advances to the run time when any source succeeded. Sources with no `cutoff` of their own fall back to it |
| `window_days` | number | from the seed files (a fresh state gets `7`); the pipeline does not read it |
| `notes`, `audience` | string | free text from the seed files (`audience` only in `default`); kept, not read |
| `sources` | object | per source name, see below |
| `seen` | object | the ids already reported, per bucket, see below |
| `failures` | object | always `{}` today (seed files and a fresh state); kept, not read |

`sources.<name>`:

| Key | Type | Meaning |
|---|---|---|
| `url` | string | the feed URL, from the seed files. Kept by the pipeline, not read (the collectors have their own list) |
| `ok` | bool | whether the source succeeded on the last run |
| `cutoff` | ISO 8601 UTC or null | the source's own cutoff. **Absent** in seeded entries that have not run yet: the watch-level `cutoff` applies. `null` means the source has never succeeded: no date filter, id dedupe only. A failed source keeps its old value |
| `last_error` | string | the last run's error; removed when the source succeeds again |

The samples show each case: a source that ran (`cutoff`), a failed one (`ok: false`, an older
`cutoff`, `last_error`) and a seeded one with no `cutoff` key (`BleepingComputer`,
`KrebsOnSecurity`, `ImportAI`). `cutoff: null` does not occur in the real files today, so it is
not in the samples; `tests/test_pipeline.py::test_dedupe_cutoff_resolution` covers it.

`seen.<bucket>.<key>`: the pipeline drops an item whose key is in **any** of the watch's buckets.

| Bucket | Key | Fields |
|---|---|---|
| `cves` (default) | CVE id from the KEV feed | `first_seen`, `date_added`, `due_date` (YYYY-MM-DD), `product`, `ransomware` (`Known` / `Unknown`) |
| `events` (default) | `_norm_key(title)`: lowercase, non-alphanumerics to `-`, 60 chars | `first_seen`, `title`, `url`, `date` (as the feed gave it: RFC 822, sometimes with a 2-digit year, or ISO 8601), `source` |
| `papers` (AI) | bare arXiv id (`NNNN.NNNNN`, no version, no `arxiv:` prefix) | `first_seen`, `title`, `link` |
| `items` (AI) | `_norm_key(title)` | same fields as `events` |

`first_seen` is ISO 8601 UTC. The default watch's news items live in `events`; the AI watches'
news items live in `items`. Code that dedupes or records news must use the bucket for the watch.
