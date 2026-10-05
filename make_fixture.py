#!/usr/bin/env python3
"""Build synthetic .ichat files (NSKeyedArchiver plists) for testing.

Written from the format spec rather than by copying a real file, so it
exercises the parser independently.
"""
import plistlib
import sys
import uuid
from datetime import datetime, timezone

APPLE_EPOCH = 978307200


class Builder:
    def __init__(self):
        self.objs = ["$null"]
        self._classes = {}

    def add(self, obj):
        self.objs.append(obj)
        return plistlib.UID(len(self.objs) - 1)

    def cls(self, name, hierarchy):
        if name not in self._classes:
            self._classes[name] = self.add({"$classname": name, "$classes": hierarchy})
        return self._classes[name]

    def string(self, s):
        return self.add(s)

    def array(self, uids, mutable=True):
        name = "NSMutableArray" if mutable else "NSArray"
        hier = ["NSMutableArray", "NSArray", "NSObject"] if mutable else ["NSArray", "NSObject"]
        return self.add({"$class": self.cls(name, hier), "NS.objects": list(uids)})

    def dictionary(self, pairs):
        keys = [self.string(k) for k, _ in pairs]
        vals = [v for _, v in pairs]
        return self.add({
            "$class": self.cls("NSMutableDictionary",
                               ["NSMutableDictionary", "NSDictionary", "NSObject"]),
            "NS.keys": keys, "NS.objects": vals,
        })

    def date(self, dt):
        return self.add({"$class": self.cls("NSDate", ["NSDate", "NSObject"]),
                         "NS.time": dt.timestamp() - APPLE_EPOCH})

    def attributed(self, text):
        return self.add({
            "$class": self.cls("NSAttributedString", ["NSAttributedString", "NSObject"]),
            "NSString": self.string(text),
        })

    def presentity(self, handle, owner):
        return self.add({
            "$class": self.cls("Presentity",
                               ["Presentity", "DirectlyObservableObject", "NSObject"]),
            "ChatHandleKey": False,
            "ID": self.string(handle),
            "ServiceName": self.string("AIM"),
            "ServiceLoginID": self.string(owner),
            "AccountID": self.string("00000000-0000-4000-8000-000000000000"),
        })

    def message(self, sender_uid, when, text, flags, guid=None, original=None):
        return self.add({
            "$class": self.cls("InstantMessage", ["InstantMessage", "NSObject"]),
            "GUID": self.string(guid or str(uuid.uuid4()).upper()),
            "OriginalMessage": self.string(
                original if original is not None
                else f"<html><body><font face=\"Arial\">{text}</font></body></html>"),
            "MessageText": self.attributed(text),
            "Sender": sender_uid,
            "Flags": flags,
            "Time": self.date(when),
        })


def write(path, rows, owner=("alice_h", "Alice Hart"),
          peers=(("bobby42", "Bobby"),)):
    """rows: (iso_time, handle, text, flags, guid, original_html_or_None)"""
    b = Builder()
    owner_handle, owner_name = owner
    everyone = [(owner_handle, owner_name)] + list(peers)
    uids = {h: b.presentity(h, owner_handle) for h, _ in everyone}

    msgs, times = [], []
    for iso, handle, text, flags, guid, original in rows:
        when = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
        times.append(when)
        msgs.append(b.message(uids[handle], when, text, flags, guid, original))

    msg_array = b.array(msgs)
    participants = b.array([b.string(n) for _, n in everyone])
    presentity_ids = b.array([b.string(h) for h, _ in everyone])
    empty = b.string("")

    root = b.array([b.string("AIM"), empty, msg_array,
                    b.array([uids[h] for h, _ in peers]),
                    empty, b.add(len(everyone)), empty, empty])

    metadata = b.dictionary([
        ("Participants", participants),
        ("EndTime", b.date(max(times))),
        ("StartTime", b.date(min(times))),
        ("Service", b.string("AOL Instant Messenger")),
        ("PresentityIDs", presentity_ids),
        ("ChatRoom", b.string("groupchat-1" if len(peers) > 1 else "")),
    ])

    plist = {
        "$version": 100000,
        "$archiver": "NSKeyedArchiver",
        "$top": {"root": root, "metadata": metadata},
        "$objects": b.objs,
    }
    with open(path, "wb") as fh:
        plistlib.dump(plist, fh, fmt=plistlib.FMT_BINARY)
    print("wrote %s (%d messages)" % (path, len(msgs)))


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fixtures"
    import os
    os.makedirs(out, exist_ok=True)

    SHARED = "AAAA1111-0000-0000-0000-00000000DUPE"

    # file A: March 2008, includes a system line and an image-only message
    write(f"{out}/Bobby_on_2008-03-02.ichat", [
        ("2008-03-02T19:04:11", "alice_h", "are you around tomorrow", 5, None, None),
        ("2008-03-02T19:04:58", "bobby42", "Bobby has joined the chat", 1, None, None),
        ("2008-03-02T19:05:40", "bobby42", "yeah, after 3", 1, SHARED, None),
        ("2008-03-02T19:49:02", "alice_h", "", 5, None,
         '<html><body><img src="cid:thing.jpg"></body></html>'),
        ("2008-03-02T19:52:30", "bobby42", "ha, that's the one", 1, None, None),
    ])

    # file B: same two people, overlaps A by one GUID -> must fold into ONE
    # conversation with A, and the duplicate must be dropped
    write(f"{out}/Bobby_on_2008-03-02_at_19.05.ichat", [
        ("2008-03-02T19:05:40", "bobby42", "yeah, after 3", 1, SHARED, None),
        ("2008-04-11T08:15:00", "alice_h", "line one\nline two", 5, None, None),
        ("2008-04-11T08:59:00", "bobby42", "is now away", 1, None, None),
        ("2008-04-11T09:40:00", "alice_h", "unicode check: caf\u00e9 \u2014 na\u00efve \u2713", 5, None, None),
    ])

    # file C: a different correspondent -> its own conversation / own link
    write(f"{out}/Carol_on_2009-06-20.ichat", [
        ("2009-06-20T14:00:00", "carol_w", "did you get the files", 1, None, None),
        ("2009-06-20T14:02:00", "alice_h", "yep, thanks", 5, None, None),
        ("2011-02-01T11:30:00", "carol_w", "long time!", 1, None, None),
    ], peers=(("carol_w", "Carol West"),))

    # file D: a group chat -> a third, separate conversation
    write(f"{out}/group_on_2010-05-05.ichat", [
        ("2010-05-05T17:00:00", "alice_h", "dinner thursday?", 5, None, None),
        ("2010-05-05T17:01:00", "bobby42", "in", 1, None, None),
        ("2010-05-05T17:03:00", "carol_w", "can't, travelling", 1, None, None),
    ], peers=(("bobby42", "Bobby"), ("carol_w", "Carol West")))

    # file E: a handle with no display name in Participants
    write(f"{out}/stranger_on_2010-01-09.ichat", [
        ("2010-01-09T22:10:00", "dmz9", "who is this", 1, None, None),
        ("2010-01-09T22:11:00", "alice_h", "no idea", 5, None, None),
    ], peers=(("dmz9", ""),))

    # file F: not a valid archive at all -- must be skipped, not fatal
    with open(f"{out}/broken.ichat", "wb") as fh:
        fh.write(b"bplist00\x00\x00garbage")
    print("wrote broken.ichat (intentionally invalid)")
