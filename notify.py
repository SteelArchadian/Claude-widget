"""Command Center: push a notification to the user's widget.

  python notify.py "<chat name>" "<one line>" [--session local_id] [--kind question|ready|blocked|broken]

Only for things the user must act on (a QUESTION, a READY that needs a look, something blocked or broken).
Batch several items into one line. Not for progress or successful releases.
"""
import argparse, json, os, re, sys, time

def _inbox():
    """<mcwDir>/notify/inbox, where mcwDir comes from the widget's config.json (else its data folder)."""
    data = os.path.join(os.environ.get("LOCALAPPDATA", "."), "Command Center")
    try:
        with open(os.path.join(data, "config.json"), encoding="utf-8") as f:
            base = json.load(f).get("mcwDir") or data
    except (OSError, ValueError):
        base = data
    return os.path.join(base, "notify", "inbox")


INBOX = _inbox()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("chat")
    ap.add_argument("text")
    ap.add_argument("--session", default="")
    ap.add_argument("--kind", default="question", choices=["question", "ready", "blocked", "broken"])
    a = ap.parse_args()
    if a.session and not re.fullmatch(r"local_[A-Za-z0-9-]{1,64}", a.session):
        sys.exit("bad --session (expected local_<id>)")
    os.makedirs(INBOX, exist_ok=True)
    name = f"{time.time():.6f}-{os.getpid()}"
    tmp = os.path.join(INBOX, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"chat": a.chat.strip()[:60], "text": a.text.strip()[:300], "session": a.session,
                   "kind": a.kind, "ts": time.time()}, f)
    os.replace(tmp, os.path.join(INBOX, name + ".json"))
    print("queued")


if __name__ == "__main__":
    main()
