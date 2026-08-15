# SociCS

A polite archiver for the Tsinghua journal platform (`thujournal.lib.tsinghua.edu.cn`),
which hosts several of the university's historical publications:

```
/QHHome/SecondIndex?sysId=23&displayDBCode=XQH&displayDBName=新清华&displayyear=2006
                                          ^^^ selects the journal

https://thujournal.lib.tsinghua.edu.cn/swfPath/xqh/0001.pdf
                                               ^^^ same code, lowercased
                                              ... 1670.pdf
                                              ... 1670Z22.pdf   <- supplements
```

It downloads any collection on the platform, finds supplementary issues without
guessing at them, and is built to take a long time on purpose.

Python 3.8+. No dependencies — standard library only.

> The package is still called `thu_xqh` from when it only handled 新清华. The
> name is a leftover; it drives every collection now.

## Quick start

```bash
# 1. See which journals the platform offers
python3 -m thu_xqh collections --save

# 2. Ask one of them which issues exist
python3 -m thu_xqh discover -c XQH --out ids-xqh.txt

# 3. Fetch them
python3 -m thu_xqh download -c XQH --ids ids-xqh.txt

# 4. Check what landed
python3 -m thu_xqh verify -c XQH
python3 -m thu_xqh status -c XQH
```

Files land in `pdfs/<collection>/` by default, each with its own manifest, so
two journals that both number an issue `0001` never collide.

Stop at any point with Ctrl-C and rerun the same command — it resumes. Nothing
is ever fetched twice.

## Collections

`-c/--collection` takes the `displayDBCode` from the browse URL. Three ship
with confirmed PDF directories and issue ranges:

| Code | PDF directory | Issues | Count |
|---|---|---|---|
| `XQH` | `/swfPath/xqh/` | `0001`–`1670` (+ supplements) | 1670 |
| `QHXK` | `/swfPath/qhxk/` | `0008`–`0029` | 22 |
| `QHXXXK` | `/swfPath/qhxxxk/` | `0001`–`0029` | 29 |

Only XQH has a confirmed display name (新清华) and `sysId`. For the other two
those fields are left blank rather than guessed — the download path does not
need them, and `collections` fills them in from the site. An empty field is
simply omitted from the index URL rather than sent as a wrong value.

```bash
python3 -m thu_xqh collections            # ask the site, print what it finds
python3 -m thu_xqh collections --save     # ...and cache it in collections.json
python3 -m thu_xqh collections --offline  # just show the cache
```

Because QHXK and QHXXXK have known ranges, they need no discovery at all:

```bash
python3 -m thu_xqh download -c QHXK      # 22 files
python3 -m thu_xqh download -c QHXXXK    # 29 files
```

### Everything at once

`-c all` runs a command over every collection in the registry, each with its
own destination and manifest:

```bash
python3 -m thu_xqh download -c all --dry-run   # price the whole platform
python3 -m thu_xqh download -c all
python3 -m thu_xqh status -c all
```

Single-collection flags (`--dest`, `--out`, `--ids`, `--first`, `--pdf-dir`, …)
are rejected with `-c all`, since one explicit path would have each journal
overwrite the last one's work.

An unknown code still works — defaults are derived from it (`QHZK` →
`/swfPath/qhzk/`), and anything wrong can be overridden:

```bash
python3 -m thu_xqh discover -c QHZK --db-name 清华周刊 --sys-id 24 \
    --pdf-dir /swfPath/qhzk --id-width 4 --first 1 --last 900 \
    --first-year 1914 --last-year 1937
```

For a collection with no known issue range, `discover` writes only what the
site confirmed, and `download` requires either `--ids` or an explicit
`--first`/`--last`.

## Finding the supplements

This is the part worth explaining, because the obvious approach is bad.

The obvious approach is to guess: take each base number, append every plausible
marker (`Z1`, `Z2`, … `Z30`), and see what comes back. For XQH alone that is
**~100,000 requests** to a small university library server to find perhaps a
few dozen files — and it still only finds markers you thought to guess.

So the tool reads the site's own browse index instead. The walk **escalates
only as far as it needs to**:

| Level | Views fetched | When it is used |
|---|---|---|
| `all` | 1 | always tried first |
| `month` | 12 | if `all` didn't cover the range |
| `year` | 54 | if `month` didn't |
| `year-month` | 648 | last resort |

After each level it measures coverage against the collection's expected range
and stops as soon as it clears `--coverage-target` (default 98%). Pagination
links within each view are followed automatically.

### Two routes to a PDF id

Index rows may link straight to a PDF, or only to a detail page:

```
/DetaliSwfInfo?dbName=XQH&sysID=135717
```

Note that `sysID` there is *not* the PDF number — the mapping lives in the
detail page's own markup. So discovery works in two passes:

1. **Cheap pass** — scrape PDF hrefs directly off the index. Free, if present.
2. **`--follow-details`** — open each linked detail page and read the PDF link
   out of it. One request per issue, so it is opt-in, and it is the reliable
   route when the index only links to detail pages.

If the cheap pass finds nothing but detail links exist, `discover` says so and
tells you what the second pass would cost.

### Confirmed vs. candidate ids

Discovery distinguishes two kinds of finding, because conflating them produces
confident nonsense:

- **Confirmed** — read straight out of a `swfPath/<code>/….pdf` link. A fact.
- **Candidate** — an id-shaped token in some other link. A lead, not a fact.

Candidates are reported separately and only added if you pass
`--confirm-candidates`, which existence-checks each one. Bare four-digit
numbers in page *text* are ignored entirely — on a real page those are years
and row counts far more often than issue numbers.

### If the catalog isn't enough

Guessing is still available, but opt-in and always costed out first:

```bash
python3 -m thu_xqh discover -c XQH --probe-bases 0900,1200,1670 \
    --probe-suffixes 'Z{1..30}' --dry-run
```

`--stop-after-misses N` abandons a base after N consecutive empty suffixes.
Probing never runs without an interactive confirmation or `--yes`.

## Load on the server

The traffic profile is the main design constraint:

- **One connection, reused.** No parallelism at all — there is no concurrency
  flag to turn on.
- **One request at a time**, spaced `--delay` seconds apart (default 5.0) with
  ±25% jitter so the pattern isn't a metronome.
- **It only ever slows down.** A 429 or 503 doubles the base delay for the rest
  of the run and honours `Retry-After`. The delay never goes back down.
- **Nothing is fetched twice.** The manifest records every outcome, including
  "this issue does not exist", so a rerun costs zero requests for settled ids.
- **`robots.txt` is checked** before anything else and respected.
- **`--max-requests N`** caps a session so the work can be spread over days.

A full XQH run is ~1670 requests, about **two and a half hours of pure spacing**
at the default 5s delay, plus transfer time. QHXK and QHXXXK are trivial by
comparison — 22 and 29 files, a few minutes each. Adding `--follow-details`
roughly doubles a collection's cost. Check before starting:

```bash
python3 -m thu_xqh download -c all --dry-run
```

## What counts as a successful download

A `200 OK` is not enough. Servers like this one commonly answer a missing file
with a friendly HTML page and a `200` status, and a naive downloader happily
saves thousands of copies of an error page.

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
| `collections` | Lists the journals the platform offers |
| `discover` | Walks a collection's catalog, writes an id list |
| `download` | Fetches PDFs from an id list |
| `verify` | Re-checks downloaded files, writes `retry-ids.txt` |
| `status` | Summarises manifest progress |

Common: `--delay`, `--dest`, `--manifest`, `--max-requests`, `--base-url`,
`--user-agent`, `--ignore-robots`, `-q`.

Collection picker (on `discover`, `download`, `verify`, `status`):
`-c/--collection`, `--registry`, `--sys-id`, `--db-name`, `--pdf-dir`,
`--id-width`, `--first`, `--last`, `--first-year`, `--last-year`.

`discover`: `--out`, `--max-level`, `--coverage-target`, `--only-discovered`,
`--follow-details`, `--max-details`, `--confirm-candidates`, `--probe-bases`,
`--probe-suffixes`, `--stop-after-misses`, `--save-html`, `--dry-run`, `--yes`.

`download`: `--ids`, `--limit`, `--force`, `--recheck-missing`, `--dry-run`.

## Verification status

The test suite (`python3 -m unittest discover -s tests`, 60 tests) runs against
a local stand-in server covering two collections, paginated index pages,
detail-page-only indexes, real PDFs, plain 404s, HTML-error-pages-with-status-200,
`HEAD`-rejecting servers, truncated PDFs, per-collection manifest isolation,
request budgets, `-c all` fan-out, and resume behaviour. The issue range of
each registered collection is pinned by a test so a refactor cannot drift it.

**The code has not been run against the live site.** The network policy of the
environment it was written in blocks `thujournal.lib.tsinghua.edu.cn` outright,
so the page markup, the display names, and supplement naming beyond the one
known example (`1670Z22`) are inferred rather than observed. The PDF
directories and issue ranges in the table above came from the person who asked
for this, not from the site. The parsers are written defensively for that reason, but the first
live run deserves a look:

```bash
python3 -m thu_xqh discover -c XQH --max-level all --coverage-target 0 \
    --save-html debug/ --out /tmp/ids.txt
```

If that reports `0 confirmed`, the saved HTML in `debug/` shows what the parser
actually received. The patterns to adjust are `pdf_href_re`, `LINKY_ATTR_RE`
and `DETAIL_RE`, in `thu_xqh/discover.py` and `thu_xqh/collections.py`.

## Layout

```
thu_xqh/
  client.py       one connection, paced requests, backoff, robots
  collections.py  which journals exist, and their URL settings
  ids.py          issue id shapes and parsing
  discover.py     catalog walk, detail pages, probing fallback
  download.py     fetching, PDF validation, verification
  manifest.py     resumable run state
  cli.py          command line
tests/
  test_thu_xqh.py
```
