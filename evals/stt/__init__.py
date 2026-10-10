# ruff: noqa: E501
"""STT Evaluation Set (Epic 76, story 76.3, spec A5; SM-51): 30 synthetic en-IN and 20 hi-IN / Hinglish screening-call
clips (TTS-generated reference audio, no real people) with reference text, and the word error rate.

``python -m evals.stt --vendor fake --dry-run`` (CI) checks the WER computation with the Fake vendor (which returns a
deterministic hypothesis per clip); the scheduled real-vendor run (Epic 79, 79.7) synthesises the clips and reports WER
per language. Target: en-IN <= 20 %; Hindi / Hinglish is reported only (no Phase 5 target).
"""

import re
import unicodedata

__all__ = ["CLIPS", "EN_TARGET", "normalise", "wer"]

EN_TARGET = 0.20

_EN = (
    "Hello, am I speaking with the candidate for the Python developer role?",
    "Is this a good time to talk for ten minutes?",
    "How many years of experience do you have with Python?",
    "I have about five years of experience in backend development.",
    "What is your current notice period?",
    "My notice period is sixty days but it is negotiable.",
    "Are you open to relocating to Bengaluru?",
    "Yes, I can relocate within a month.",
    "What is your current role and team size?",
    "I lead a team of four engineers at a fintech company.",
    "Have you worked with Django or FastAPI?",
    "I have used FastAPI for the last two years.",
    "Do you have experience with PostgreSQL?",
    "Yes, I design schemas and tune queries regularly.",
    "Are you comfortable with a hybrid work model?",
    "Hybrid works for me, three days in the office is fine.",
    "When can you attend the technical interview?",
    "I am available next Tuesday afternoon.",
    "Do you hold any offers at the moment?",
    "I have one offer but I prefer this role.",
    "What motivates you to change jobs now?",
    "I want to work on larger distributed systems.",
    "Have you worked in an agile team before?",
    "Yes, we run two week sprints with daily standups.",
    "Can you describe a production incident you handled?",
    "We fixed a database deadlock during a payment spike.",
    "Do you have a valid passport for client travel?",
    "Yes, my passport is valid for another six years.",
    "Thank you, the recruiter will share the next steps by email.",
    "Thank you for your time, have a good day.",
)
_HI = (
    "Namaste, kya aap abhi baat kar sakte hain?",
    "Haan, main abhi free hoon.",
    "Aapke paas kitne saal ka experience hai?",
    "Mere paas paanch saal ka experience hai.",
    "Aapka notice period kitna hai?",
    "Mera notice period do mahine ka hai.",
    "Kya aap Pune shift ho sakte hain?",
    "Haan, main Pune shift ho sakta hoon.",
    "Aap Python mein kaam karte hain kya?",
    "Haan, main roz Python aur SQL use karta hoon.",
    "Interview ke liye aap kab available hain?",
    "Main agle hafte Monday ko available hoon.",
    "Kya aapke paas abhi koi offer hai?",
    "Abhi mere paas ek offer hai.",
    "Aap hybrid model ke saath comfortable hain?",
    "Haan, hybrid model mere liye theek hai.",
    "Aapki team kitni badi hai?",
    "Meri team mein chaar log hain.",
    "Dhanyavaad, hum aapko email bhejenge.",
    "Shukriya, aapka din shubh ho.",
)
CLIPS: tuple[dict[str, str], ...] = tuple(
    [{"id": f"en-{i + 1:02d}", "language": "en-IN", "reference": t} for i, t in enumerate(_EN)]
    + [{"id": f"hi-{i + 1:02d}", "language": "hi-IN", "reference": t} for i, t in enumerate(_HI)]
)


def normalise(text: str) -> list[str]:
    """Lower-case, strip accents / punctuation, collapse spaces; words."""
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[^\w\s']", " ", text)
    return [w for w in text.replace("'", "").split() if w]


def wer(reference: str, hypothesis: str) -> float:
    """Word-level Levenshtein distance / reference words (after normalisation)."""
    ref, hyp = normalise(reference), normalise(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, start=1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1] / len(ref)
