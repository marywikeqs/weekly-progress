"""
Load a Weekly Progress backup (admin → /api/admin/backup) into Upstash Redis,
for the move to Vercel. Refuses if Redis already has data, unless --force.

    KV_REST_API_URL=... KV_REST_API_TOKEN=... python _test/import_backup.py backup.json

The backup holds everyone's Todoist tokens: delete the file once it's loaded.
"""

import json
import os
import sys
import urllib.request

URL = os.environ["KV_REST_API_URL"].rstrip("/")
TOKEN = os.environ["KV_REST_API_TOKEN"]
KEYS = ("config", "updates", "usage", "tasks", "sent", "drafts", "todoist", "roadmap")


def kv(*cmd):
    req = urllib.request.Request(URL, data=json.dumps(cmd).encode("utf-8"), method="POST",
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    out = json.loads(urllib.request.urlopen(req, timeout=30).read())
    if "error" in out:
        sys.exit(f"Redis said: {out['error']}")
    return out["result"]


def main():
    args = [a for a in sys.argv[1:] if a != "--force"]
    if len(args) != 1:
        sys.exit(__doc__)
    data = json.loads(open(args[0], encoding="utf-8").read())
    data.setdefault("roadmap", [])      # backups from before the roadmap
    missing = [k for k in KEYS if k not in data]
    if missing:
        sys.exit(f"Not a Weekly Progress backup (missing {', '.join(missing)}).")
    if any(kv("GET", f"wp:{k}") for k in ("updates", "usage")) and "--force" not in sys.argv:
        sys.exit("Redis already has updates. Run again with --force to replace them.")
    for k in KEYS:
        kv("SET", f"wp:{k}", json.dumps(data[k], ensure_ascii=False))
        kv("INCR", f"wp:{k}:v")       # any copy of the app that read the old data can't save over this
    for f, b64 in data.get("screenshots", {}).items():
        kv("SET", f"wp:shot:{f}", b64)
    for week, rec in data["sent"].items():      # weeks already emailed stay emailed
        kv("HSET", "wp:sentmark", week, rec.get("at", "imported"))
    print(f"Loaded {len(data['updates'])} project updates, {len(data['usage'])} usage records, {len(data['tasks'])} other work, "
          f"{len(data.get('screenshots', {}))} screenshots, {len(data['sent'])} sent weeks, {len(data['config']['roster'])} people on the roster.")


if __name__ == "__main__":
    main()
