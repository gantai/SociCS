# SociCS

A polite archiver for the 新清华 (XQH) issue PDFs on the Tsinghua journal server:

```
https://thujournal.lib.tsinghua.edu.cn/swfPath/xqh/0001.pdf
                                              ... 1670.pdf
                                              ... 1670Z22.pdf   <- supplements
```

It downloads the base range `0001`–`1670`, finds supplementary issues without
guessing at them, and is built to take a long time on purpose.

Python 3.8+. No dependencies — standard library only.

## Quick start

```bash
# 1. Ask the site which issues exist (a few dozen requests)
python -m thu_xqh discover --out issue-ids.txt

# 2. Fetch them (slow by design — see "Load on the server")
python -m thu_xqh download --ids issue-ids.txt --dest pdfs

# 3. Check what landed
python -m thu_xqh verify --dest pdfs
python -m thu_xqh status --dest pdfs
```

Stop it at any point with Ctrl-C and rerun the same command — it picks up where
it left off. Nothing is ever fetched twice.

## Finding the supplements

This is the part worth explaining, because the obvious approach is bad.

The obvious approach is to guess: take each of the 1670 base numbers, append
every plausible marker (`Z1`, `Z2`, … `Z30`), and see what comes back. That is
**~100,000 requests** to a small university library server to find perhaps a
few dozen files — and it still only finds markers you thought to guess. If
supplements use `S1` or `ZK` or a marker no one predicted, they stay invisible.

So the tool asks the site instead. The archive publishes a browse index:

```
/QHHome/SecondIndex?sysId=23&displayDBCode=XQH&displayDBName=新清华
                   &displayyear=1955&displaymonth=11
```

`displayDBCode=XQH` is the same collection as the `swfPath/xqh/` PDF directory,
and `displayyear` accepts `全部` ("all"). Walking that index lists every issue
the archive knows about — supplements included, whatever they are named — in a
few dozen requests.

The walk **escalates only as far as it needs to**:

| Level | Views fetched | When it is used |
|---|---|---|
| `all` | 1 | always tried first |
| `month` | 12 | if `all` didn't cover the range |
| `year` | 54 | if `month` didn't |
| `year-month` | 648 | last resort |

After each level it measures coverage — how much of the expected `0001`–`1670`
range it accounted for — and stops as soon as it clears `--coverage-target`
(default 98%). Pagination links within each view are followed automatically.

### Confirmed vs. candidate ids

Discovery distinguishes two kinds of finding, because conflating them produces
confident nonsense:

- **Confirmed** — read straight out of a `swfPath/xqh/….pdf` link. A fact.
- **Candidate** — an id-shaped token found in some other link (`?id=1670Z22`).
  A lead, not a fact.

Candidates are reported separately and are only added to the list if you pass
`--confirm-candidates`, which existence-checks each one. Bare four-digit numbers
in page *text* are ignored entirely — on a real page those are years and row
counts far more often than issue numbers.

### If the catalog isn't enough

Guessing is still available as a fallback, but it is opt-in and always costed
out before it runs:

```bash
# Try Z1..Z30 against three specific issues
python -m thu_xqh discover --probe-bases 0900,1200,1670 --probe-suffixes 'Z{1..30}'

# See the price of a full sweep without paying it
python -m thu_xqh discover --probe-bases all --dry-run
#   Probe sweep: 1670 base(s) x 30 suffix(es)
#   up to 100200 requests, roughly 8350 minute(s) at 5.0s spacing.
```

`--stop-after-misses N` abandons a base after N consecutive empty suffixes,
which cuts the cost sharply when most bases have no supplements. Probing never
runs without either an interactive confirmation or `--yes`.

## Load on the server

The traffic profile is the main design constraint:

- **One connection, reused.** No parallelism at all — there is no concurrency
  flag to turn on. ~1700 files over one keep-alive connection means ~1700 fewer
  TCP and TLS handshakes than a naive fetcher.
- **One request at a time**, spaced `--delay` seconds apart (default 5.0) with
  ±25% jitter so the pattern isn't a metronome.
- **It only ever slows down.** A 429 or 503 doubles the base delay for the rest
  of the run and honours `Retry-After`. The delay never goes back down.
- **Nothing is fetched twice.** The manifest records every outcome, including
  "this issue does not exist", so a rerun costs zero requests for settled ids.
- **`robots.txt` is checked** before anything else and respected. `--ignore-robots`
  exists but should only be used with the site operator's permission.
- **`--max-requests N`** caps a session so the work can be spread over days.

A full base-range run is ~1670 requests, about **two and a half hours of pure
spacing** at the default 5s delay, plus transfer time. That is the intended
cost. If you are in a hurry, be in a hurry somewhere else.

## What counts as a successful download

A `200 OK` is not enough. Servers like this one commonly answer a missing file
with a friendly HTML page and a `200` status, and a naive downloader happily
saves 1670 copies of an error page.

So every response is checked for the `%PDF-` magic bytes, and against
`Content-Length` where present. Bytes are written to a `.part` file and only
renamed into place after they pass. Anything that isn't a PDF is recorded as
*absent*, not saved. Files that are valid PDFs but lack a trailing `%%EOF` are
kept and flagged — that usually means the archive's own copy is truncated.

`verify` re-checks everything on disk against the manifest. It is read-only: it
writes a `retry-ids.txt` you can feed back to `download --force`, and never
deletes anything itself.

## Commands

All options can be written after the subcommand.

| Command | What it does |
|---|---|
| `discover` | Walks the catalog, writes an id list |
| `download` | Fetches PDFs from an id list |
| `verify` | Re-checks downloaded files, writes `retry-ids.txt` |
| `status` | Summarises manifest progress |

Common options: `--delay`, `--dest`, `--manifest`, `--max-requests`,
`--first`/`--last`, `--base-url`, `--user-agent`, `--ignore-robots`, `-q`.

`discover`: `--out`, `--max-level`, `--coverage-target`, `--only-discovered`,
`--confirm-candidates`, `--probe-bases`, `--probe-suffixes`,
`--stop-after-misses`, `--save-html`, `--dry-run`, `--yes`.

`download`: `--ids`, `--limit`, `--force`, `--recheck-missing`, `--dry-run`.

By default `discover` writes the full `0001`–`1670` range plus any supplements
it found, on the assumption that the catalog may be incomplete — an id that
turns out not to exist costs one request and is then remembered as absent. Pass
`--only-discovered` to trust the catalog and write only what it confirmed.

## Verification status

The test suite (`python -m unittest discover -s tests`, 35 tests) runs against a
local stand-in server that reproduces paginated index pages, real PDFs, plain
404s, HTML-error-pages-with-status-200, `HEAD`-rejecting servers, truncated
PDFs, and resume behaviour.

**The code has not been run against the live site.** The network policy of the
environment it was written in blocks `thujournal.lib.tsinghua.edu.cn` outright,
so the index URL shape, its pagination markup, and the supplement naming beyond
the one known example (`1670Z22`) are all inferred from the site's public URLs
rather than observed. The parsers are written defensively for that reason, but
the first live run deserves a look:

```bash
# Cheapest possible check: one index page, saved for inspection
python -m thu_xqh discover --max-level all --coverage-target 0 \
    --save-html debug/ --out /tmp/ids.txt
```

If that reports `0 confirmed`, the index markup differs from what is assumed —
the saved HTML in `debug/` will show what the parser actually received, and the
patterns to adjust are `PDF_HREF_RE`, `LINKY_ATTR_RE`, and `INDEX_PATH` at the
top of `thu_xqh/discover.py`.

## Layout

```
thu_xqh/
  client.py     one connection, paced requests, backoff, robots
  ids.py        issue id shapes and parsing
  discover.py   catalog walk, and the probing fallback
  download.py   fetching, PDF validation, verification
  manifest.py   resumable run state
  cli.py        command line
tests/
  test_thu_xqh.py
```
