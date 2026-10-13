"""The room's developer side: requests the room's Claude sent with room_feedback.

  python dev.py list                        every request and its status
  python dev.py wait                        block until a new request arrives, print it, exit
                                            (run in the background to be woken up by one)
  python dev.py reply ID STATUS MESSAGE     answer it in the room; STATUS = approved, declined,
                                            done (shipped), question
"""
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
FEEDBACK = os.path.join(ROOT, "feedback.json")
SEEN = os.path.join(ROOT, ".dev_seen")


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def show(item):
    when = time.strftime("%d.%m %H:%M", time.localtime(item["ts"]))
    print(f"#{item['id']} [{item['status']}] {item['kind']} from {item['lane']}'s turn, {when}: {item['title']}")
    if item.get("details"):
        print("    " + item["details"].replace("\n", "\n    "))
    if item.get("reply"):
        print(f"    → {item['reply']}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        for item in load(FEEDBACK, []):
            show(item)
    elif cmd == "wait":
        seen = set(load(SEEN, []))
        while True:
            new = [i for i in load(FEEDBACK, []) if i["status"] == "new" and i["id"] not in seen]
            if new:
                for item in new:
                    show(item)
                with open(SEEN, "w", encoding="utf-8") as f:
                    json.dump(sorted(seen | {i["id"] for i in new}), f)
                return
            time.sleep(3)
    elif cmd == "reply" and len(sys.argv) >= 4:
        cfg = load(os.path.join(ROOT, "room.json"), {})
        body = json.dumps({"id": sys.argv[2].lstrip("#"), "status": sys.argv[3],
                           "message": " ".join(sys.argv[4:])}).encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{cfg.get('port', 8765)}/dev_reply", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Room-Dev": cfg.get("dev_token", "")})
        print(urllib.request.urlopen(req, timeout=10).read().decode())
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
