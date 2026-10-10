# ruff: noqa: E501
"""``python -m evals.stt --vendor <key> [--dry-run]``: WER per language over the STT Evaluation Set.

``--dry-run`` (CI) never touches a network: the ``fake`` vendor returns a deterministic hypothesis per clip (one word
dropped from every fourth clip) so the WER arithmetic is exercised. A real vendor run needs synthesised clip audio and
credentials and is scheduled by Epic 79 (release gate 79.7)."""

import argparse
import json
import sys

from evals.stt import CLIPS, EN_TARGET, wer


def fake_hypothesis(i: int, reference: str) -> str:
    words = reference.split()
    return " ".join(words[:-1]) if i % 4 == 3 and len(words) > 1 else reference


def report(vendor: str) -> dict[str, object]:
    per: dict[str, list[float]] = {}
    for i, clip in enumerate(CLIPS):
        if vendor != "fake":
            msg = "Only the fake vendor runs here; real vendors run in the scheduled release gate (Epic 79)."
            raise SystemExit(msg)
        per.setdefault(clip["language"], []).append(wer(clip["reference"], fake_hypothesis(i, clip["reference"])))
    out = {lang: round(sum(v) / len(v), 4) for lang, v in per.items()}
    return {"vendor": vendor, "clips": len(CLIPS), "wer": out, "en_target": EN_TARGET, "en_ok": out.get("en-IN", 1) <= EN_TARGET}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.stt")
    parser.add_argument("--vendor", default="fake")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    result = report("fake" if args.dry_run else args.vendor)
    sys.stdout.write(json.dumps(result) + "\n")
    return 0 if result["en_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
