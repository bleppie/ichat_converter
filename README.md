# ichat-book

Turn a folder of legacy Apple iChat / Messages `.ichat` transcripts into one
continuous, readable EPUB — chronological, chaptered by month, built for
sitting down and reading rather than searching.

Zero dependencies (stock macOS `python3`). Nothing leaves your machine.

## Use

```bash
python3 ichat_book.py ~/Documents/iChats -o archive.epub -t "Archived Conversations"
python3 ichat_book.py ~/Documents/iChats -f md -o archive.md     # or txt
python3 ichat_book.py ~/Documents/iChats --names somehandle="Their Name"
```

Then drop the `.epub` into Books / Kindle / any reader.

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

## Testing

`make_fixture.py` builds synthetic `.ichat` archives from the format spec
(not by copying a real file) covering dedupe, month splits, system lines,
attachment-only messages, unnamed handles, unicode and a corrupt file:

```bash
python3 make_fixture.py /tmp/fixtures && python3 ichat_book.py /tmp/fixtures -f txt -o /tmp/out.txt
```
