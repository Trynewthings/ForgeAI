"""Phase 2 stub: the demo seeder used the legacy RequirementSessionService
which was deleted when front-chain v2 took over the session main path.

Re-seeding a demo project against the new front-chain pipeline is a
separate follow-up task. For now this script is a placeholder so imports
don't blow up when the scripts directory is pickled by tooling.
"""
from __future__ import annotations

import sys


def main() -> None:
    print(
        "seed_demo.py: legacy demo path removed in Phase 2. "
        "Re-seed manually against /api/v1/projects + /api/v1/front-chain/sessions.",
        file=sys.stderr,
    )
    raise SystemExit(1)


if __name__ == "__main__":
    main()
