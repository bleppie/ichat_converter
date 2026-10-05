# ichat-book

Turn a folder of legacy Apple iChat / Messages `.ichat` transcripts into a
browsable HTML archive — one link per conversation, timestamped messages
inside — or into a single continuous EPUB.

Zero dependencies (stock `python3`). Nothing leaves your machine.

## Use

```bash
# default: a folder of linked HTML pages, one page per conversation
python3 ichat_book.py ~/Documents/iChats -o archive
open archive/index.html

# other formats
python3 ichat_book.py ~/Documents/iChats -f epub -o archive.epub
python3 ichat_book.py ~/Documents/iChats -f md   -o archive.md
python3 ichat_book.py ~/Documents/iChats -f txt  -o archive.txt
```

## HTML output

- `index.html` lists every conversation with message count and date range,
  plus a live filter box.
- One page per conversation, grouped by **who was in it** — every transcript
  with the same participant set folds into a single page, so all your chats
  with one person are under one link no matter how many files they came from.
- Group chats become their own conversation, keyed by the full participant set.
- Inside a page: sticky day headings, per-message timestamps, your own
  messages tinted, and a search box that filters as you type.
- Dark mode follows the system setting. No JS frameworks, no network calls.

## What it does

- Reads the NSKeyedArchiver object graph directly (no deserializer dependency).
- Resolves handles to real names from each file's `Participants` /
  `PresentityIDs` metadata, pooled across **all** files, so a handle named in
  one transcript is named everywhere. Override with `--names`.
- Merges duplicate messages by GUID — overlapping saved transcripts are common.
- Drops iChat's own join/leave/status lines (`--keep-system` to retain them).
- Keeps attachment-only messages as `[sent an image]` rather than dropping them.
- Skips unreadable files with a warning instead of aborting the run.
- Timestamps at message granularity, rendered in the local timezone.

Runs on Python 3.8+.

## Testing

`make_fixture.py` builds synthetic `.ichat` archives from the format spec
(not by copying real files), covering dedupe across overlapping transcripts,
multiple correspondents, group chats, system lines, attachment-only messages,
unnamed handles, unicode, multiline text and a corrupt file:

```bash
python3 make_fixture.py /tmp/fixtures
python3 ichat_book.py /tmp/fixtures -o /tmp/site && open /tmp/site/index.html
```
