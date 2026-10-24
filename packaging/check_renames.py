"""CI check: the newest saved plan suggests names for the demo folder's generic files.

Run after `<app> plan <demo folder>` with LAZYFOLD_HOME set. Fails if fewer than
MIN renames were suggested, which happens when documents could not be read in
the built app (e.g. the parser worker did not start).
  python packaging/check_renames.py [MIN]
"""

import json
import os
import sys
from pathlib import Path


def main() -> int:
    need = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    plans = sorted((Path(os.environ["LAZYFOLD_HOME"]) / "plans").glob("*.json"), key=os.path.getmtime)
    if not plans:
        print("::error::no plan was saved")
        return 1
    plan = json.loads(plans[-1].read_text(encoding="utf-8"))
    renames = [(p["rel_path"], p["new_name"]) for p in plan["proposals"] if p.get("new_name")]
    for old, new in renames:
        print(f"  {old} -> {new}")
    if len(renames) < need:
        print(f"::error::only {len(renames)} rename suggestions in {plans[-1].name} (expected at least {need})")
        return 1
    print(f"{len(renames)} rename suggestions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
