"""冒烟入口：无参数时打印基础契约样例；带参数时转发给完整命令行。"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))


def smoke() -> None:
    from overflow_evidence.contracts import EvidenceItem, EvidenceKind

    item = EvidenceItem("E-9", EvidenceKind.SAMPLE, datetime(2026, 9, 29, 8, tzinfo=timezone.utc), "BLOCK-2")
    print(
        json.dumps(
            {"evidence_id": item.evidence_id, "kind": item.kind.value, "location": item.location_code},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    if len(sys.argv) > 1:
        from overflow_evidence.cli import main

        raise SystemExit(main(sys.argv[1:]))
    smoke()
