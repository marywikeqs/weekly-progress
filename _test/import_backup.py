"""
Load a Weekly Progress backup (admin → /api/admin/backup) into Supabase, for
the move to Vercel. Refuses if Supabase already has updates, unless --force.

    SUPABASE_URL=... SUPABASE_SECRET_KEY=... python _test/import_backup.py backup.json

The backup holds everyone's Todoist tokens: delete the file once it's loaded.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import store  # noqa: E402

KEYS = ("config", "updates", "usage", "tasks", "sent", "drafts", "todoist")


def main():
    args = [a for a in sys.argv[1:] if a != "--force"]
    if len(args) != 1 or not store.ON:
        sys.exit(__doc__)
    data = json.loads(open(args[0], encoding="utf-8").read())
    missing = [k for k in KEYS if k not in data]
    if missing:
        sys.exit(f"Not a Weekly Progress backup (missing {', '.join(missing)}).")
    if any(store.get(f"wp:{k}") for k in ("updates", "usage")) and "--force" not in sys.argv:
        sys.exit("Supabase already has updates. Run again with --force to replace them.")
    for k in KEYS:
        store.put(f"wp:{k}", data[k])
    for f, b64 in data.get("screenshots", {}).items():
        store.put(f"wp:shot:{f}", b64)
    for week, rec in data["sent"].items():      # weeks already emailed stay emailed
        store.put(f"wp:sentmark:{week}", rec.get("at", "imported"))
    print(f"Loaded {len(data['updates'])} project updates, {len(data['usage'])} usage records, {len(data['tasks'])} other work, "
          f"{len(data.get('screenshots', {}))} screenshots, {len(data['sent'])} sent weeks, {len(data['config']['roster'])} people on the roster.")


if __name__ == "__main__":
    main()
