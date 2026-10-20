"""Offline JSONL inspection and quarantine. Python 3.10+, standard library only."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, BinaryIO


class InvalidRecord(ValueError):
    """A record cannot safely enter the clean object stream."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidRecord("duplicate_key", "An object contains a repeated key.")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise InvalidRecord("nonstandard_number", f"{value} is not a JSON number.")


def encode_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def inspect_record(raw: bytes, line: int) -> bytes:
    try:
        # An optional BOM is accepted only at the start of the input file.
        text = raw.decode("utf-8-sig" if line == 1 else "utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidRecord("invalid_utf8", f"Invalid UTF-8 at byte {exc.start}.") from exc
    if not text.strip():
        raise InvalidRecord("blank_line", "A blank line is not an object record.")
    try:
        value = json.loads(
            text, object_pairs_hook=unique_object, parse_constant=reject_constant
        )
    except json.JSONDecodeError as exc:
        raise InvalidRecord(
            "invalid_json", f"{exc.msg}; character {exc.pos}."
        ) from exc
    except (RecursionError, ValueError) as exc:
        if isinstance(exc, InvalidRecord):
            raise
        raise InvalidRecord("parse_limit", type(exc).__name__) from exc
    if not isinstance(value, dict):
        raise InvalidRecord("non_object", "This delivery accepts JSON objects only.")
    try:
        return encode_json(value)
    except UnicodeEncodeError as exc:
        raise InvalidRecord(
            "invalid_unicode", "The record contains an unpaired Unicode surrogate."
        ) from exc
    except ValueError as exc:
        # A standard-looking exponent can overflow Python's floating-point range.
        raise InvalidRecord(
            "number_range", "A number cannot be represented as finite JSON."
        ) from exc


def write_json(stream: BinaryIO, value: Any) -> None:
    stream.write(encode_json(value))


def fingerprint(path: Path) -> tuple[int, int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


def process(source: Path, destination: Path) -> dict[str, Any]:
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError("The input must be a regular file.")
    before = fingerprint(source)
    destination.mkdir(parents=True, exist_ok=False)
    marker = destination / "INCOMPLETE.txt"
    marker.write_text(
        "This run is incomplete. Do not use its clean output as a finished delivery.\n",
        encoding="utf-8",
    )
    source_hash = hashlib.sha256()
    counts: dict[str, int] = {}
    accepted = rejected = lines = offset = 0
    with (
        source.open("rb") as incoming,
        (destination / "input.original").open("xb") as snapshot,
        (destination / "clean.jsonl").open("xb") as clean,
        (destination / "rejected.jsonl").open("xb") as bad,
        (destination / "audit.jsonl").open("xb") as audit,
    ):
        for lines, raw in enumerate(incoming, start=1):
            snapshot.write(raw)
            source_hash.update(raw)
            entry: dict[str, Any] = {
                "source_line": lines,
                "source_byte_offset": offset,
                "source_byte_length": len(raw),
                "source_sha256": hashlib.sha256(raw).hexdigest(),
            }
            offset += len(raw)
            try:
                normalized = inspect_record(raw, lines)
            except InvalidRecord as exc:
                rejected += 1
                counts[exc.code] = counts.get(exc.code, 0) + 1
                entry.update(
                    status="rejected", output_line=rejected, reason=exc.code
                )
                write_json(
                    bad,
                    {
                        **entry,
                        "detail": exc.detail,
                        "raw_base64": base64.b64encode(raw).decode("ascii"),
                    },
                )
            else:
                accepted += 1
                clean.write(normalized)
                entry.update(
                    status="accepted",
                    output_line=accepted,
                    output_sha256=hashlib.sha256(normalized).hexdigest(),
                )
            write_json(audit, entry)
    if fingerprint(source) != before:
        raise ValueError(
            "The input changed during processing; the output is marked incomplete."
        )
    summary = {
        "complete": True,
        "format_version": 1,
        "source_name": source.name,
        "source_bytes": offset,
        "source_sha256": source_hash.hexdigest(),
        "total_lines": lines,
        "accepted_records": accepted,
        "rejected_records": rejected,
        "rejection_reasons": counts,
        "original_preserved": "input.original",
        "policy": "UTF-8 JSON objects; duplicate keys rejected; no guessed repairs.",
    }
    with (destination / "summary.json").open("xb") as report:
        write_json(report, summary)
    marker.unlink()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Existing UTF-8 JSONL input")
    parser.add_argument("destination", type=Path, help="New output directory")
    args = parser.parse_args()
    try:
        summary = process(args.source, args.destination)
    except FileExistsError:
        print("Refused: the destination already exists; nothing was overwritten.", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
