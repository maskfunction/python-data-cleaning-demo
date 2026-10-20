"""Build synthetic fixtures, run the CLI, and verify observable delivery properties."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "repair_jsonl.py"


def load_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def invoke(source: Path, destination: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(source), str(destination)],
        capture_output=True,
        check=False,
    )


def check_delivery(source: Path, destination: Path, good: int, bad: int) -> None:
    original = source.read_bytes()
    report = json.loads((destination / "summary.json").read_bytes())
    assert report["complete"] is True
    assert not (destination / "INCOMPLETE.txt").exists()
    assert (destination / "input.original").read_bytes() == original
    assert report["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert report["source_bytes"] == len(original)
    assert report["accepted_records"] == good
    assert report["rejected_records"] == bad
    audit = load_lines(destination / "audit.jsonl")
    quarantined = load_lines(destination / "rejected.jsonl")
    cleaned = (destination / "clean.jsonl").read_bytes().splitlines(keepends=True)
    assert len(audit) == report["total_lines"] == good + bad
    assert len(cleaned) == good
    assert len(quarantined) == bad
    next_offset = 0
    for index, record in enumerate(audit, start=1):
        assert record["source_line"] == index
        assert record["source_byte_offset"] == next_offset
        length = record["source_byte_length"]
        raw = original[next_offset : next_offset + length]
        next_offset += length
        assert record["source_sha256"] == hashlib.sha256(raw).hexdigest()
        if record["status"] == "rejected":
            saved = quarantined[record["output_line"] - 1]
            assert saved["source_line"] == index
            assert base64.b64decode(saved["raw_base64"], validate=True) == raw
        else:
            output = cleaned[record["output_line"] - 1]
            assert isinstance(json.loads(output), dict)
            assert hashlib.sha256(output).hexdigest() == record["output_sha256"]
    assert next_offset == len(original)


def main() -> None:
    passed: list[str] = []
    # Temporary verification files stay inside this demo's directory.
    with tempfile.TemporaryDirectory(prefix=".verify-", dir=HERE) as folder:
        root = Path(folder).resolve()
        assert root.parent == HERE
        source = root / "mixed.jsonl"
        mixed = (
            b'\xef\xbb\xbf{ "id": 1, "name": "\xe8\x8b\x8f\xe5\xb7\x9e" }\r\n'
            b'{"id":2,"value":\r\n'
            b'{"id":3,"id":99}\n'
            b' \t\n'
            b'{"id":4,"message":"\xff"}\n'
            b'{"id":5,"value":NaN}\n'
            b'["not","an","object"]\n'
            b'{"id":6,"note":"line\\nbreak","enabled":true}'
        )
        source.write_bytes(mixed)
        assert invoke(source, root / "mixed-result").returncode == 0
        check_delivery(source, root / "mixed-result", good=2, bad=6)
        assert source.read_bytes() == mixed
        assert load_lines(root / "mixed-result" / "clean.jsonl") == [
            {"id": 1, "name": "苏州"},
            {"id": 6, "note": "line\nbreak", "enabled": True},
        ]
        passed.append("Mixed corruption: 8 lines = 2 accepted + 6 quarantined; values correct.")
        passed.append("Every input byte preserved; bad-record base64 and all line hashes verified.")
        passed.append("UTF-8 BOM, CRLF, escaped newline, and final line without newline handled.")

        first = {
            item.name: item.read_bytes()
            for item in (root / "mixed-result").iterdir()
        }
        assert invoke(source, root / "mixed-result").returncode == 2
        assert first == {
            item.name: item.read_bytes()
            for item in (root / "mixed-result").iterdir()
        }
        passed.append("Rerun into existing destination refused without changing any output file.")

        empty = root / "empty.jsonl"
        empty.write_bytes(b"")
        assert invoke(empty, root / "empty-result").returncode == 0
        check_delivery(empty, root / "empty-result", 0, 0)
        passed.append("Empty file produces a valid zero-record report.")

        edge = root / "edge.jsonl"
        edge.write_bytes(
            b'{"outer":{"x":1,"x":2}}\n'
            b'{"x":"\\ud800"}\n'
            b'{"x":1e999}\n'
            b'{"x":1}{"x":2}\n'
            b'{"x":9007199254740993}\n'
        )
        assert invoke(edge, root / "edge-result").returncode == 0
        check_delivery(edge, root / "edge-result", 1, 4)
        assert load_lines(root / "edge-result" / "clean.jsonl") == [
            {"x": 9007199254740993}
        ]
        passed.append("Nested duplicates, lone surrogate, numeric overflow, and joined records quarantined.")
        passed.append("Large integer preserved exactly.")

        assert invoke(root / "missing.jsonl", root / "missing-result").returncode == 2
        assert not (root / "missing-result").exists()
        passed.append("Missing input fails before creating output.")
    print(json.dumps({"status": "PASS", "checks": passed}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
