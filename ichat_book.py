#!/usr/bin/env python3
"""
ichat_book - turn a pile of legacy Apple iChat/Messages .ichat transcripts
into one continuous, readable book (EPUB by default; Markdown or text too).

Zero dependencies. Nothing leaves your machine.

    python3 ichat_book.py ~/Documents/iChats -o archive.epub
    python3 ichat_book.py ~/Documents/iChats --format md -o archive.md
    python3 ichat_book.py file1.ichat file2.ichat --names somehandle="Their Name"

.ichat files are binary plists wrapping an NSKeyedArchiver object graph:

    $top.root     -> [service, '', [InstantMessage...], [Presentity...], ...]
    $top.metadata -> {Participants, PresentityIDs, StartTime, EndTime, Service}
    InstantMessage{ Sender->Presentity, Time->NSDate, MessageText->NSAttributedString }

We resolve UID references ourselves rather than depending on a deserializer,
so this runs on a stock macOS python3 with nothing installed.
"""

from __future__ import annotations

import argparse
import html
import os
import plistlib
import re
import sys
import unicodedata
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

# NSDate reference date: 2001-01-01 00:00:00 UTC
APPLE_EPOCH = 978307200

# Flags bit observed to mean "sent by the account owner" in iChat archives.
# We don't rely on it -- sender identity comes from Presentity.ID -- but it's
# a useful tiebreaker when a transcript has no usable ServiceLoginID.
FLAG_OUTGOING = 0b100


# --------------------------------------------------------------------------
# NSKeyedArchiver resolution
# --------------------------------------------------------------------------

class Archive:
    """Minimal NSKeyedArchiver reader: dereferences plistlib.UID pointers."""

    def __init__(self, plist: dict):
        if plist.get("$archiver") != "NSKeyedArchiver":
            raise ValueError("not an NSKeyedArchiver plist")
        self.objects = plist["$objects"]
        self.top = plist["$top"]

    def deref(self, ref):
        """Follow a UID one level. Non-UIDs pass through unchanged."""
        if isinstance(ref, plistlib.UID):
            obj = self.objects[ref.data]
            return None if obj == "$null" else obj
        return ref

    def classname(self, obj) -> str | None:
        if not isinstance(obj, dict):
            return None
        cls = self.deref(obj.get("$class"))
        if isinstance(cls, dict):
            return cls.get("$classname")
        return None

    def items(self, obj) -> list:
        """NS.objects of an NSArray-ish object, dereferenced one level."""
        if not isinstance(obj, dict):
            return []
        return [self.deref(x) for x in obj.get("NS.objects", [])]

    def mapping(self, obj) -> dict:
        """NS.keys/NS.objects of an NSDictionary-ish object."""
        if not isinstance(obj, dict):
            return {}
        keys = [self.deref(k) for k in obj.get("NS.keys", [])]
        vals = [self.deref(v) for v in obj.get("NS.objects", [])]
        return dict(zip(keys, vals))

    def string(self, ref) -> str:
        """Text of a plain string or an NSAttributedString."""
        obj = self.deref(ref)
        if obj is None:
            return ""
        if isinstance(obj, str):
            return obj
        if isinstance(obj, dict) and "NSString" in obj:
            s = self.deref(obj["NSString"])
            return s if isinstance(s, str) else ""
        return ""

    def date(self, ref) -> datetime | None:
        obj = self.deref(ref)
        if isinstance(obj, datetime):
            return obj.replace(tzinfo=timezone.utc)
        if isinstance(obj, dict) and "NS.time" in obj:
            return datetime.fromtimestamp(obj["NS.time"] + APPLE_EPOCH, tz=timezone.utc)
        return None


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

@dataclass
class Message:
    when: datetime
    sender: str          # resolved display name
    handle: str          # raw presentity id
    text: str
    outgoing: bool
    guid: str
    source: str          # file it came from

    @property
    def local(self) -> datetime:
        return self.when.astimezone()


IMG_RE = re.compile(r"<img\b", re.I)
TAG_RE = re.compile(r"<[^>]+>")

# Transcript lines iChat wrote itself; not things a human said.
SYSTEM_RE = re.compile(
    r"^(?:.{0,60}\s)?(?:"
    r"has (?:joined|left|declined|come online|gone offline)"
    r"|(?:joined|left) the chat"
    r"|is now (?:available|away|idle|offline)"
    r"|(?:started|ended|declined|cancelled|canceled) (?:a |an )?"
    r"(?:audio |video |screen.?sharing )?(?:chat|call|invitation|session)"
    r"|(?:accepted|declined) your invitation"
    r"|(?:began|stopped) (?:screen ?sharing)"
    r"|file transfer (?:complete|cancell?ed|failed)"
    r")\b",
    re.I,
)


def clean_text(s: str) -> str:
    s = unicodedata.normalize("NFC", s)
    s = s.replace(" ", "\n").replace(" ", "\n")
    s = s.replace("￼", "")           # object-replacement char (inline attachments)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return s.strip()


def parse_file(path: str, keep_system: bool) -> tuple[list[Message], dict]:
    """Return (messages, metadata) for one .ichat file.

    Senders are left as raw handles here; display names are resolved by
    collect() once every file's metadata has been pooled, so a handle named
    in one transcript is named everywhere.
    """
    with open(path, "rb") as fh:
        plist = plistlib.load(fh)
    ar = Archive(plist)

    meta_raw = ar.mapping(ar.deref(ar.top.get("metadata")))
    participants = [p for p in ar.items(meta_raw.get("Participants")) if isinstance(p, str)]
    presentity_ids = [p for p in ar.items(meta_raw.get("PresentityIDs")) if isinstance(p, str)]
    service = meta_raw.get("Service") or ""

    # handle -> display name, paired positionally as iChat wrote them
    names: dict[str, str] = {}
    for handle, name in zip(presentity_ids, participants):
        if handle and name:
            names[handle.lower()] = name

    # the message array is the first NSArray-of-InstantMessage under root
    root = ar.deref(ar.top.get("root"))
    messages_raw: list = []
    owner_handle = ""
    for entry in ar.items(root):
        kids = ar.items(entry)
        if kids and ar.classname(kids[0]) == "InstantMessage":
            messages_raw = kids
            break

    out: list[Message] = []
    for im in messages_raw:
        if ar.classname(im) != "InstantMessage":
            continue
        when = ar.date(im.get("Time"))
        if when is None:
            continue

        sender_obj = ar.deref(im.get("Sender"))
        handle = ""
        if isinstance(sender_obj, dict):
            handle = ar.string(sender_obj.get("ID")) or ""
            if not owner_handle:
                owner_handle = (ar.string(sender_obj.get("ServiceLoginID")) or "").lower()

        text = clean_text(ar.string(im.get("MessageText")))
        flags = im.get("Flags") or 0

        if not text:
            original = ar.string(im.get("OriginalMessage"))
            if IMG_RE.search(original or ""):
                text = "[sent an image]"
            else:
                stripped = clean_text(html.unescape(TAG_RE.sub("", original or "")))
                text = stripped or "[attachment]"

        if not keep_system and SYSTEM_RE.search(text):
            continue

        key = handle.lower()
        outgoing = bool(owner_handle) and key == owner_handle
        if not owner_handle:
            outgoing = bool(flags & FLAG_OUTGOING)

        out.append(Message(
            when=when, sender="", handle=handle, text=text,
            outgoing=outgoing,
            guid=ar.string(im.get("GUID")) or f"{path}:{when.timestamp()}:{text[:24]}",
            source=os.path.basename(path),
        ))

    return out, {"service": service, "participants": participants, "names": names}


def collect(paths: list[str], overrides: dict[str, str], keep_system: bool):
    files: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, names in os.walk(p):
                for n in sorted(names):
                    if n.lower().endswith(".ichat"):
                        files.append(os.path.join(root, n))
        else:
            files.append(p)

    messages: list[Message] = []
    seen: set[str] = set()
    names: dict[str, str] = {}
    services: Counter = Counter()
    skipped: list[tuple[str, str]] = []
    dupes = 0

    for f in sorted(files):
        try:
            msgs, meta = parse_file(f, keep_system)
        except Exception as exc:                       # keep going; report at the end
            skipped.append((os.path.basename(f), f"{type(exc).__name__}: {exc}"))
            continue
        names.update({k: v for k, v in meta["names"].items() if k not in names})
        if meta["service"]:
            services[meta["service"]] += 1
        for m in msgs:
            if m.guid in seen:                         # overlapping saved transcripts
                dupes += 1
                continue
            seen.add(m.guid)
            messages.append(m)

    # Second pass: every handle seen anywhere in the archive now has a name.
    for m in messages:
        key = m.handle.lower()
        m.sender = overrides.get(key) or names.get(key) or m.handle or "unknown"

    messages.sort(key=lambda m: m.when)
    stats = {
        "files": len(files), "skipped": skipped, "dupes": dupes,
        "services": services, "names": names,
    }
    return messages, stats


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def chapters(messages: list[Message]) -> list[tuple[str, list[Message]]]:
    """Group chronologically into one chapter per month."""
    out: list[tuple[str, list[Message]]] = []
    for m in messages:
        label = m.local.strftime("%B %Y")
        if not out or out[-1][0] != label:
            out.append((label, []))
        out[-1][1].append(m)
    return out


CSS = """\
@page { margin: 1.2em; }
html { -webkit-hyphens: auto; hyphens: auto; }
body {
  font-family: Palatino, "Iowan Old Style", Georgia, serif;
  font-size: 1em; line-height: 1.55;
  margin: 0 auto; padding: 0 0.4em; max-width: 34em;
  color: #16161a;
}
h1 { font-size: 1.5em; font-weight: normal; letter-spacing: 0.02em;
     margin: 1.2em 0 0.2em; page-break-before: always; }
h1.first { page-break-before: avoid; }
h2 { font-size: 0.82em; font-weight: normal; text-transform: uppercase;
     letter-spacing: 0.14em; color: #8a8a94;
     margin: 2.1em 0 0.9em; padding-bottom: 0.3em;
     border-bottom: 1px solid #e3e3e8; }
.msg { margin: 0 0 0.62em; text-indent: 0; }
.who { font-variant: small-caps; letter-spacing: 0.04em; color: #2d2d33; }
.me .who { font-weight: 600; }
.t { font-size: 0.74em; color: #a6a6b0; margin-right: 0.45em;
     font-family: "Helvetica Neue", Helvetica, sans-serif; }
.gap { margin-top: 1.5em; }
.sys { color: #9a9aa4; font-style: italic; }
.title { text-align: center; margin-top: 22%; }
.title h1 { font-size: 2.1em; page-break-before: avoid; margin-bottom: 0.1em; }
.title p { color: #8a8a94; font-size: 0.9em; margin: 0.3em 0; }
.meta { margin-top: 3em; font-size: 0.8em; color: #9a9aa4; }
.meta li { margin-bottom: 0.3em; }
"""

XHTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><meta charset="utf-8"/><title>{title}</title>
<link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
{body}
</body></html>
"""

# A visual break when the conversation goes quiet for a while.
GAP_MINUTES = 25


def render_messages(msgs: list[Message]) -> str:
    parts: list[str] = []
    day = None
    prev: datetime | None = None
    for m in msgs:
        d = m.local.date()
        if d != day:
            day = d
            parts.append(f'<h2>{html.escape(m.local.strftime("%A, %-d %B %Y"))}</h2>')
            prev = None
        gap = ""
        if prev and (m.local - prev).total_seconds() > GAP_MINUTES * 60:
            gap = " gap"
        cls = "msg" + (" me" if m.outgoing else "") + gap
        sys_cls = " sys" if m.text.startswith("[") and m.text.endswith("]") else ""
        body = html.escape(m.text).replace("\n", "<br/>")
        parts.append(
            f'<p class="{cls}"><span class="t">{m.local.strftime("%-I:%M %p").lower()}</span>'
            f'<span class="who">{html.escape(m.sender)}</span>'
            f'<span class="{sys_cls.strip() or "body"}"> &#8203;{body}</span></p>'
        )
        prev = m.local
    return "\n".join(parts)


def build_epub(messages: list[Message], stats: dict, out_path: str, title: str) -> dict:
    chaps = chapters(messages)
    first, last = messages[0].local, messages[-1].local
    span = f'{first.strftime("%B %Y")} – {last.strftime("%B %Y")}'
    people = sorted({m.sender for m in messages})
    uid = f"urn:uuid:{uuid.uuid4()}"

    merged = f", {stats['dupes']} duplicate messages merged" if stats["dupes"] else ""
    cover = (
        '<div class="title">'
        f'<h1 class="first">{html.escape(title)}</h1>'
        f"<p>{html.escape(span)}</p>"
        f"<p>{html.escape(', '.join(people[:8]))}"
        f"{' and others' if len(people) > 8 else ''}</p>"
        f"<p>{len(messages):,} messages</p>"
        "</div>"
        '<ul class="meta">'
        f"<li>{stats['files']} transcript file(s) read{html.escape(merged)}</li>"
        f"<li>{html.escape(', '.join(sorted(stats['services']))) or 'unknown service'}</li>"
        "<li>Times shown in this computer&#8217;s local timezone.</li>"
        "</ul>"
    )

    docs: list[tuple[str, str, str]] = [("cover.xhtml", title, cover)]
    for i, (label, msgs) in enumerate(chaps, 1):
        body = f"<h1>{html.escape(label)}</h1>\n" + render_messages(msgs)
        docs.append((f"ch{i:03d}.xhtml", label, body))

    nav_items = "\n".join(
        f'<li><a href="{fn}">{html.escape(lbl)}</a></li>'
        for fn, lbl, _ in docs[1:]
    )
    nav = (
        '<nav epub:type="toc" id="toc"><h1>Contents</h1><ol>'
        f"{nav_items}</ol></nav>"
    )

    ncx_points = "\n".join(
        f'<navPoint id="n{i}" playOrder="{i}"><navLabel><text>{html.escape(lbl)}</text>'
        f'</navLabel><content src="{fn}"/></navPoint>'
        for i, (fn, lbl, _) in enumerate(docs[1:], 1)
    )

    manifest = "\n".join(
        f'<item id="{fn.split(".")[0]}" href="{fn}" media-type="application/xhtml+xml"/>'
        for fn, _, _ in docs
    )
    spine = "\n".join(f'<itemref idref="{fn.split(".")[0]}"/>' for fn, _, _ in docs)

    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="bookid">{uid}</dc:identifier>
<dc:title>{html.escape(title)}</dc:title>
<dc:language>en</dc:language>
<dc:date>{last.strftime("%Y-%m-%d")}</dc:date>
{chr(10).join(f"<dc:creator>{html.escape(p)}</dc:creator>" for p in people[:8])}
<meta property="dcterms:modified">{datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}</meta>
</metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
<item id="css" href="style.css" media-type="text/css"/>
{manifest}
</manifest>
<spine toc="ncx">
{spine}
</spine>
</package>
"""

    ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="{uid}"/></head>
<docTitle><text>{html.escape(title)}</text></docTitle>
<navMap>
{ncx_points}
</navMap>
</ncx>
"""

    container = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf"
 media-type="application/oebps-package+xml"/></rootfiles></container>
"""

    with zipfile.ZipFile(out_path, "w") as z:
        # mimetype must be first and stored uncompressed
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/toc.ncx", ncx)
        z.writestr("OEBPS/nav.xhtml", XHTML.format(title="Contents", body=nav),
                   zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/style.css", CSS, zipfile.ZIP_DEFLATED)
        for fn, lbl, body in docs:
            z.writestr(f"OEBPS/{fn}", XHTML.format(title=html.escape(lbl), body=body),
                       zipfile.ZIP_DEFLATED)

    return {"chapters": len(chaps), "span": span, "people": people}


def build_markdown(messages: list[Message], title: str) -> str:
    lines = [f"# {title}", ""]
    day = None
    month = None
    for m in messages:
        lbl = m.local.strftime("%B %Y")
        if lbl != month:
            month = lbl
            lines += [f"## {lbl}", ""]
        d = m.local.date()
        if d != day:
            day = d
            lines += [f"### {m.local.strftime('%A, %-d %B %Y')}", ""]
        t = m.local.strftime("%-I:%M %p").lower()
        text = m.text.replace("\n", "  \n    ")
        lines.append(f"- `{t}` **{m.sender}** — {text}")
    lines.append("")
    return "\n".join(lines)


def build_text(messages: list[Message], title: str) -> str:
    lines = [title, "=" * len(title), ""]
    day = None
    for m in messages:
        d = m.local.date()
        if d != day:
            day = d
            header = m.local.strftime("%A, %-d %B %Y")
            lines += ["", header, "-" * len(header), ""]
        lines.append(f"{m.local.strftime('%-I:%M %p').lower():>9}  {m.sender}: {m.text}")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Convert legacy .ichat transcripts into one readable book.")
    ap.add_argument("paths", nargs="+", help=".ichat files and/or folders to walk")
    ap.add_argument("-o", "--out", help="output file (default: ichat-archive.<ext>)")
    ap.add_argument("-f", "--format", choices=["epub", "md", "txt"], default="epub")
    ap.add_argument("-t", "--title", default="Archived Conversations")
    ap.add_argument("--names", nargs="*", default=[], metavar="handle=Name",
                    help="override handle -> display name")
    ap.add_argument("--keep-system", action="store_true",
                    help="keep join/leave/status lines (dropped by default)")
    args = ap.parse_args(argv)

    overrides: dict[str, str] = {}
    for pair in args.names:
        if "=" not in pair:
            ap.error(f"--names expects handle=Name, got {pair!r}")
        k, v = pair.split("=", 1)
        overrides[k.strip().lower()] = v.strip()

    messages, stats = collect(args.paths, overrides, args.keep_system)

    for name, err in stats["skipped"]:
        print(f"  ! skipped {name}: {err}", file=sys.stderr)

    if not messages:
        print("No messages found. Nothing written.", file=sys.stderr)
        return 1

    out = args.out or f"ichat-archive.{args.format}"
    if args.format == "epub":
        info = build_epub(messages, stats, out, args.title)
        print(f"{out}  —  {len(messages):,} messages, "
              f"{info['chapters']} chapters, {info['span']}")
        print(f"  people: {', '.join(info['people'])}")
    else:
        body = (build_markdown if args.format == "md" else build_text)(messages, args.title)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(body)
        print(f"{out}  —  {len(messages):,} messages, {len(body):,} chars")

    read = stats["files"] - len(stats["skipped"])
    print(f"  read {read}/{stats['files']} file(s)"
          + (f", merged {stats['dupes']} duplicate messages" if stats["dupes"] else ""))
    unknown = sorted({m.handle for m in messages if m.sender == m.handle and m.handle})
    if unknown:
        print(f"  no display name for: {', '.join(unknown)}"
              f"  (use --names {unknown[0]}=\"Their Name\")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
