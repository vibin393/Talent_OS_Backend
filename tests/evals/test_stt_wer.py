"""Epic 76 (A5): the STT Evaluation Set and the WER function (Fake dry run; no network)."""

from evals.stt import CLIPS, normalise, wer
from evals.stt.__main__ import main, report


def test_clip_set_shape() -> None:
    langs = [c["language"] for c in CLIPS]
    assert (langs.count("en-IN"), langs.count("hi-IN")) == (30, 20)
    assert len({c["id"] for c in CLIPS}) == 50


def test_wer() -> None:
    assert wer("Hello there friend", "hello, there friend!") == 0.0
    assert wer("a b c d", "a x c") == 0.5  # one substitution + one deletion
    assert wer("a b", "a b c d") == 1.0  # two insertions over two words
    assert wer("", "") == 0.0
    assert normalise("It's FINE.") == ["its", "fine"]


def test_fake_dry_run() -> None:
    out = report("fake")
    assert out["clips"] == 50 and out["en_ok"] is True
    assert 0 < out["wer"]["en-IN"] < 0.1  # type: ignore[index]
    assert main(["--vendor", "fake", "--dry-run"]) == 0
