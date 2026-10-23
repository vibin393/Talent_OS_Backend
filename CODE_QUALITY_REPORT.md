# Talent OS Backend — Ruff, Mypy & Bandit Code Quality Report

_Generated 2026-10-23 · scope: `Backend/app/` · ruff 0.16.4 · bandit 1.9.4 · 338106 LOC scanned_

## Summary

| Tool | Purpose | Count |
|---|---|---|
| **Ruff** | lint / style | true current count **11,875** (`--no-fix`) → **0** after Tier A |
| **mypy** (strict) | type checking | original **1431** → **579** errors in **270** files (Phase 1 + 2 + Tier A + B + C ongoing) |
| **Bandit** | security | original report **315** → **0** (config skips + reviewed `# nosec` on all 42 findings) |

> **Auto-fix note:** `pyproject.toml` sets `[tool.ruff] fix = true`, so `ruff check` applied **5,621 safe fixes across ~1,639 files** automatically. Those are already applied and not listed individually; this report covers the **remaining** issues needing manual attention.

> **Cleanup progress (type-only / documented-config, no behavior change — app graph still imports, 137 hooks autodiscovered):**
> - **Phase 1** — deleted **528** stale `# type: ignore` comments + 4 safe ruff fixes → mypy **1431 → 904**.
> - **Phase 2** — `ErasureHandler` Protocol members made read-only `@property` (`app/services/privacy/registry.py`) → cleared **61** `arg-type` errors → mypy **904 → 843**.
> - **Tier A** — type-only code fixes (`type-arg` ×7, `no-untyped-def` ×9, `no-untyped-call` ×9 via `_redis()->Any`, `fit_rows`, RUF012→`ClassVar`) → mypy **843 → 818** (no category rose; zero regressions). Ruff **11,875 → 0** (E501 + interface/style rules ignored with justification in `pyproject.toml`; 65 intentional side-effect/re-export imports `# noqa: F401`; dead RUF028 comments removed; D301/isort). Bandit **315 → 43** (`bandit.yml` skips B101/B105/B106 as asserts/false-positives; B701 already fixed).
> - **Tier B** — synced `TaskEntity` Literal to the DB CHECK (`TASK_ENTITY_TYPES`, 7→28) and annotated the FieldDef `Op`/`Agg` operator-set constants → mypy **818 → 787** (`arg-type` 258→227; no other category changed; zero regressions). Surfaced (not masked) a latent bug: `tools_e53.py` passes `interview`/`timesheet`/`invoice`, which are not DB-valid task entities.
> - **Tier C (started)** — fixed the `tasks.create` Copilot tool offering non-DB-valid entity types (`interview`/`timesheet`/`invoice`), and cleared the `var-annotated` cluster by converting ~24 `dict(rows.all())` to the `dict(rows.tuples().all())` idiom (runtime-identical; clears the paired dict `arg-type` too). mypy **787 → 715** (`var-annotated` 47→6, `arg-type` 227→196; zero regressions).
> - **Tier C (cont.)** — made re-exported names explicit with `name as name` aliases (strict `implicit_reexport`) → mypy **715 → 685** (`attr-defined` 139→110; zero regressions).
> - **Tier C (cont.)** — documented mypy per-module override for `app.router.public_v1.*` disabling `valid-type` (the templated `scope()` dependency-alias pattern) → mypy **685 → 616** (`valid-type` 84→15; nothing else moved).
> - **Tier C (cont.)** — Celery `@task` decorator mypy override for `app.tasks.*` → mypy **616 → 579** (`misc` 50→13). **Bandit → 0**: reviewed all 42 findings and marked each with an inline `# nosec <id> - <reason>` (B608 static SQL, B406 XML encoders, B113 local-dict `.get()`, B311 non-crypto, B110/B112 best-effort, B603/B404 fixed subprocess, B613 detection regex) — all false-positive / accepted-risk, no behavior change.
> - Remaining **mypy 579** is behavior-sensitive Tier C: `select(union)`→`Base` (~87, deciding NOT to add cast() band-aids), `no-any-return` (84), `assignment` (65), None-paths/`union-attr`, etc. — per-item fix-or-ignore. **Ruff 0, Bandit 0.**

---

## 1. Ruff - remaining issues by rule

| Count | Code | Rule |
|---:|---|---|
| 1144 | `E501` | line-too-long |
| 193 | `RUF067` | non-empty-init-module |
| 66 | `D401` | non-imperative-mood |
| 26 | `ARG001` | unused-function-argument |
| 11 | `E402` | module-import-not-at-top-of-file |
| 5 | `TC003` | typing-only-standard-library-import |
| 5 | `RUF001` | ambiguous-unicode-character-string |
| 5 | `ARG002` | unused-method-argument |
| 5 | `RUF028` | invalid-formatter-suppression-comment |
| 4 | `D400` | missing-trailing-period |
| 3 | `D415` | missing-terminal-punctuation |
| 2 | `F401` | unused-import |
| 2 | `RUF002` | ambiguous-unicode-character-docstring |
| 2 | `N818` | error-suffix-on-exception-name |
| 2 | `C416` | unnecessary-comprehension |
| 2 | `RUF052` | used-dummy-variable |
| 1 | `RUF075` | fallible-context-manager |
| 1 | `SIM101` | duplicate-isinstance-call |
| 1 | `D301` | escape-sequence-in-docstring |
| 1 | `UP047` | non-pep695-generic-function |
| 1 | `TC002` | typing-only-third-party-import |
| 1 | `SIM113` | enumerate-for-loop |

**Total remaining: 1483** - dominated by `E501` line-too-long (mostly long strings/docstrings). The non-E501 items are the ones worth triaging.

### 1a. Actionable ruff issues (everything except E501)

<details><summary><b>RUF067</b> non-empty-init-module (193)</summary>

- `app/events/__init__.py:14` - `__init__` module should only contain docstrings and re-exports
- `app/events/__init__.py:17` - `__init__` module should only contain docstrings and re-exports
- `app/events/__init__.py:27` - `__init__` module should only contain docstrings and re-exports
- `app/job/tasks/__init__.py:7` - `__init__` module should only contain docstrings and re-exports
- `app/job/tasks/__init__.py:13` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:11` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:105` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:113` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:128` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:143` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:159` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:177` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:184` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:198` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:212` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:219` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:241` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:255` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:262` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:269` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:276` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:289` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:303` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:316` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:324` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:331` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:345` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:358` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:366` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:374` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:382` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:390` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:398` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:405` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:414` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:422` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:441` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:449` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:456` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:464` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:472` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:480` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:489` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:497` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:505` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:514` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:523` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:534` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:542` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:550` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:558` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:566` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:575` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:583` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:591` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:599` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:607` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:615` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:624` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:633` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:643` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:651` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:661` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:669` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:678` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:687` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:695` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:703` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:711` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:720` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:727` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:735` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:743` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:750` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:758` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:766` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:779` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:788` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:797` - `__init__` module should only contain docstrings and re-exports
- `app/router/__init__.py:805` - `__init__` module should only contain docstrings and re-exports
- `app/router/public_v1/__init__.py:11` - `__init__` module should only contain docstrings and re-exports
- `app/router/public_v1/__init__.py:17` - `__init__` module should only contain docstrings and re-exports
- `app/router/public_v1/__init__.py:24` - `__init__` module should only contain docstrings and re-exports
- `app/router/public_v1/__init__.py:31` - `__init__` module should only contain docstrings and re-exports
- `app/router/public_v1/__init__.py:68` - `__init__` module should only contain docstrings and re-exports
- `app/services/__init__.py:12` - `__init__` module should only contain docstrings and re-exports
- `app/services/__init__.py:15` - `__init__` module should only contain docstrings and re-exports
- `app/services/analytics/features/__init__.py:15` - `__init__` module should only contain docstrings and re-exports
- `app/services/analytics/features/__init__.py:22` - `__init__` module should only contain docstrings and re-exports
- `app/services/analytics/models/__init__.py:20` - `__init__` module should only contain docstrings and re-exports
- `app/services/analytics/models/__init__.py:21` - `__init__` module should only contain docstrings and re-exports
- `app/services/analytics/models/__init__.py:24` - `__init__` module should only contain docstrings and re-exports
- `app/services/analytics/models/__init__.py:32` - `__init__` module should only contain docstrings and re-exports
- `app/services/analytics/models/__init__.py:43` - `__init__` module should only contain docstrings and re-exports
- `app/services/analytics/models/__init__.py:47` - `__init__` module should only contain docstrings and re-exports
- `app/services/analytics/models/__init__.py:96` - `__init__` module should only contain docstrings and re-exports
- `app/services/analytics/models/__init__.py:121` - `__init__` module should only contain docstrings and re-exports
- `app/services/candidates/__init__.py:14` - `__init__` module should only contain docstrings and re-exports
- `app/services/candidates/__init__.py:22` - `__init__` module should only contain docstrings and re-exports
- `app/services/connections/__init__.py:13` - `__init__` module should only contain docstrings and re-exports
- `app/services/connections/__init__.py:16` - `__init__` module should only contain docstrings and re-exports
- `app/services/connections/__init__.py:25` - `__init__` module should only contain docstrings and re-exports
- `app/services/connections/__init__.py:29` - `__init__` module should only contain docstrings and re-exports
- `app/services/demands/__init__.py:49` - `__init__` module should only contain docstrings and re-exports
- `app/services/demands/__init__.py:52` - `__init__` module should only contain docstrings and re-exports
- `app/services/demands/__init__.py:63` - `__init__` module should only contain docstrings and re-exports
- `app/services/demands/__init__.py:68` - `__init__` module should only contain docstrings and re-exports
- `app/services/demands/__init__.py:80` - `__init__` module should only contain docstrings and re-exports
- `app/services/demands/__init__.py:85` - `__init__` module should only contain docstrings and re-exports
- `app/services/demands/__init__.py:89` - `__init__` module should only contain docstrings and re-exports
- `app/services/demands/__init__.py:107` - `__init__` module should only contain docstrings and re-exports
- `app/services/marketplace/checks/__init__.py:20` - `__init__` module should only contain docstrings and re-exports
- `app/services/marketplace/checks/__init__.py:28` - `__init__` module should only contain docstrings and re-exports
- `app/services/marketplace/checks/__init__.py:29` - `__init__` module should only contain docstrings and re-exports
- `app/services/marketplace/checks/__init__.py:30` - `__init__` module should only contain docstrings and re-exports
- `app/services/marketplace/checks/__init__.py:31` - `__init__` module should only contain docstrings and re-exports
- `app/services/marketplace/checks/__init__.py:34` - `__init__` module should only contain docstrings and re-exports
- `app/services/marketplace/checks/__init__.py:44` - `__init__` module should only contain docstrings and re-exports
- `app/services/marketplace/checks/__init__.py:52` - `__init__` module should only contain docstrings and re-exports
- `app/services/marketplace/checks/__init__.py:56` - `__init__` module should only contain docstrings and re-exports
- `app/services/marketplace/checks/__init__.py:77` - `__init__` module should only contain docstrings and re-exports
- `app/services/marketplace/checks/__init__.py:91` - `__init__` module should only contain docstrings and re-exports
- `app/services/marketplace/checks/__init__.py:108` - `__init__` module should only contain docstrings and re-exports
- `app/services/marketplace/checks/__init__.py:128` - `__init__` module should only contain docstrings and re-exports
- `app/services/marketplace/checks/__init__.py:137` - `__init__` module should only contain docstrings and re-exports
- `app/services/marketplace/checks/__init__.py:151` - `__init__` module should only contain docstrings and re-exports
- `app/services/marketplace/checks/__init__.py:160` - `__init__` module should only contain docstrings and re-exports
- `app/services/marketplace/checks/__init__.py:187` - `__init__` module should only contain docstrings and re-exports
- `app/services/marketplace/checks/__init__.py:188` - `__init__` module should only contain docstrings and re-exports
- `app/services/marketplace/checks/__init__.py:191` - `__init__` module should only contain docstrings and re-exports
- `app/services/matching/__init__.py:21` - `__init__` module should only contain docstrings and re-exports
- `app/services/offers/__init__.py:64` - `__init__` module should only contain docstrings and re-exports
- `app/services/offers/__init__.py:69` - `__init__` module should only contain docstrings and re-exports
- `app/services/offers/__init__.py:74` - `__init__` module should only contain docstrings and re-exports
- `app/services/offers/__init__.py:78` - `__init__` module should only contain docstrings and re-exports
- `app/services/offers/__init__.py:82` - `__init__` module should only contain docstrings and re-exports
- `app/services/offers/__init__.py:86` - `__init__` module should only contain docstrings and re-exports
- `app/services/offers/__init__.py:92` - `__init__` module should only contain docstrings and re-exports
- `app/services/offers/__init__.py:101` - `__init__` module should only contain docstrings and re-exports
- `app/services/offers/__init__.py:119` - `__init__` module should only contain docstrings and re-exports
- `app/services/offers/__init__.py:122` - `__init__` module should only contain docstrings and re-exports
- `app/services/offers/__init__.py:135` - `__init__` module should only contain docstrings and re-exports
- `app/services/offers/__init__.py:139` - `__init__` module should only contain docstrings and re-exports
- `app/services/onboarding/__init__.py:22` - `__init__` module should only contain docstrings and re-exports
- `app/services/onboarding/__init__.py:47` - `__init__` module should only contain docstrings and re-exports
- `app/services/pipeline/__init__.py:56` - `__init__` module should only contain docstrings and re-exports
- `app/services/pipeline/__init__.py:69` - `__init__` module should only contain docstrings and re-exports
- `app/services/pipeline/__init__.py:71` - `__init__` module should only contain docstrings and re-exports
- `app/services/pipeline/__init__.py:74` - `__init__` module should only contain docstrings and re-exports
- `app/services/pipeline/__init__.py:88` - `__init__` module should only contain docstrings and re-exports
- `app/services/pipeline/__init__.py:109` - `__init__` module should only contain docstrings and re-exports
- `app/services/pipeline/__init__.py:134` - `__init__` module should only contain docstrings and re-exports
- `app/services/pipeline/__init__.py:144` - `__init__` module should only contain docstrings and re-exports
- `app/services/portal/__init__.py:28` - `__init__` module should only contain docstrings and re-exports
- `app/services/privacy/__init__.py:37` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:48` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:52` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:56` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:89` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:113` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:118` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:122` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:127` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:134` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:138` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:142` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:146` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:150` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:154` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:159` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:160` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:161` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:162` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:165` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:192` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:230` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:239` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:242` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:249` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/__init__.py:263` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/metrics/__init__.py:25` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/metrics/__init__.py:26` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/metrics/__init__.py:28` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/metrics/__init__.py:38` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/metrics/__init__.py:71` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/metrics/__init__.py:85` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/metrics/__init__.py:91` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/metrics/__init__.py:98` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/metrics/__init__.py:103` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/metrics/__init__.py:109` - `__init__` module should only contain docstrings and re-exports
- `app/services/reporting/metrics/__init__.py:113` - `__init__` module should only contain docstrings and re-exports
- `app/services/search/__init__.py:12` - `__init__` module should only contain docstrings and re-exports
- `app/services/workflow/__init__.py:30` - `__init__` module should only contain docstrings and re-exports

</details>

<details><summary><b>D401</b> non-imperative-mood (66)</summary>

- `app/core/actors.py:83` - First line of docstring should be in imperative mood: "The actor of a request / task context. An automated context kind (``workflow`` rule runs, ``ai``,"
- `app/core/business_hours.py:132` - First line of docstring should be in imperative mood: "The session tenant's business calendar and timezone (a tenant without settings has no working days,"
- `app/core/celery.py:30` - First line of docstring should be in imperative mood: "The queue a task declares: its own ``queue`` option, else its module's ``QUEUE`` (None = undeclared)."
- `app/core/events.py:210` - First line of docstring should be in imperative mood: "Called by ``tasks.runtime.enqueue`` in eager mode: True when the call was deferred."
- `app/core/fiscal.py:16` - First line of docstring should be in imperative mood: "The calendar date of ``instant`` in ``tz``. Naive datetimes are refused (``ValueError``)."
- `app/core/integrations/context.py:89` - First line of docstring should be in imperative mood: "The adapter mode for a configuration: the requested mode when the manifest has it, else the first of"
- `app/core/integrations/fakes.py:29` - First line of docstring should be in imperative mood: "A minimal, valid one-page PDF showing ``text`` (deterministic bytes)."
- `app/core/integrations/fakes.py:67` - First line of docstring should be in imperative mood: "The YAML script of a fake connector (``{}`` when there is none); cached per process."
- `app/core/integrations/manifest.py:125` - First line of docstring should be in imperative mood: "The secret field holding the webhook verification secret (default ``webhook_secret``)."
- `app/core/integrations/registry.py:123` - First line of docstring should be in imperative mood: "Tests only."
- `app/core/integrations/registry.py:146` - First line of docstring should be in imperative mood: "True when the configuration resolves to a ``fake`` / ``manual`` adapter (allowed in a Sandbox Tenant)."
- `app/core/mailer.py:356` - First line of docstring should be in imperative mood: "True when the message's tenant (``message.tenant_id``, else the context tenant) is a Sandbox Tenant."
- `app/core/migration_helpers.py:664` - First line of docstring should be in imperative mood: "The restrictive policy expression. Branches whose column is not given are omitted (no columns ="
- `app/core/security.py:125` - First line of docstring should be in imperative mood: "The configured Fernet key (url-safe base64) for ``purpose``, or its dev key outside production."
- `app/core/tenancy.py:223` - First line of docstring should be in imperative mood: "True when ``tenants.mode = 'sandbox'`` (cached 60 s per process). Enforcement points: connectors resolve only"
- `app/core/tenant_registry.py:165` - First line of docstring should be in imperative mood: "This service's registered tenants (for migration fan-out). ``only_live`` filters statuses."
- `app/dependencies/auth.py:100` - First line of docstring should be in imperative mood: "A ``tos_`` API key bearer (Epic 12). OAuth artefacts (``tos_at_`` ...) are not API keys."
- `app/router/__init__.py:498` - First line of docstring should be in imperative mood: "Custom report builder & scheduled reports (Epic 47): ``/reports/custom/*``, ``/me/report-pins``,"
- `app/router/__init__.py:551` - First line of docstring should be in imperative mood: "Internal mobility (Epic 41): ``/mobility/*`` (board, apply, my applications, settings, eligibility rules,"
- `app/router/__init__.py:608` - First line of docstring should be in imperative mood: "Sourcing & Matching Agent (Epic 56): ``/demands/{id}/agent-shortlist*``, ``/demands/{id}/sourcing-runs``,"
- `app/router/admin.py:69` - First line of docstring should be in imperative mood: "The per-module capability catalog (Croar / Feedback IQ / Job Portal) for the role editor."
- `app/router/modules.py:119` - First line of docstring should be in imperative mood: "A stable short hash of the user's effective module caps (canonical JSON)."
- `app/router/public_esign.py:53` - First line of docstring should be in imperative mood: "The frozen PDF (records a ``viewed`` event only; mail-scanner GETs never change a status)."
- `app/services/approvals/service.py:149` - First line of docstring should be in imperative mood: "Delegates to ``core.actors.actor_from_ctx`` (Epic 13)."
- `app/services/approvals/service.py:776` - First line of docstring should be in imperative mood: "A Client User of the step's client who holds the step's portal permission (``client_any``)."
- `app/services/central_token.py:34` - First line of docstring should be in imperative mood: "A content-derived key id: a short hash of the PUBLIC key. A rotated signing key therefore advertises"
- `app/services/central_token.py:48` - First line of docstring should be in imperative mood: "The RS256 signing key for central tokens."
- `app/services/comms/inbound.py:319` - First line of docstring should be in imperative mood: "The original email (P1 read rules: the sender or a reader of a related record) and its captured replies."
- `app/services/company_onboarding.py:109` - First line of docstring should be in imperative mood: "The admin's invitation link on the tenant's own subdomain (the accept page reads ``?token=``)."
- `app/services/company_onboarding.py:114` - First line of docstring should be in imperative mood: "True if ``slug`` is not registered for ANY service in the control registry."
- `app/services/connections/provider.py:42` - First line of docstring should be in imperative mood: "True when ``scopes`` include every reply-capture scope of the provider (the fake provider stands in for"
- `app/services/connectors/registry.py:255` - First line of docstring should be in imperative mood: "True when a configuration resolves to a ``fake`` / ``manual`` adapter (allowed in a Sandbox Tenant)."
- `app/services/connectors/registry.py:290` - First line of docstring should be in imperative mood: "The webhook verification secret of an Installation config (the manifest's ``webhook_secret`` field, from the"
- `app/services/custom_domains.py:328` - First line of docstring should be in imperative mood: "The tenant whose **verified** custom domain is ``host`` (cached 5 minutes)."
- `app/services/demands/sla.py:34` - First line of docstring should be in imperative mood: "Delegates to ``core.business_hours.tenant_calendar`` (Epic 13)."
- `app/services/documents/certificate.py:138` - First line of docstring should be in imperative mood: "The P1 one-page offer acceptance certificate (kept for offers accepted before Epic 22)."
- `app/services/documents/esign.py:299` - First line of docstring should be in imperative mood: "(provider key, adapter, config id): the template's config, else the tenant default, else built-in."
- `app/services/documents/esign.py:486` - First line of docstring should be in imperative mood: "Internal API access: the document's access rule (404 otherwise)."
- `app/services/documents/esign.py:526` - First line of docstring should be in imperative mood: "Checks the latest unconsumed code in its own committed transaction (failed attempts persist even"
- `app/services/documents/esign.py:659` - First line of docstring should be in imperative mood: "The frozen version's bytes (``final`` once completed). Records a ``viewed`` event, never a status."
- `app/services/documents/esign.py:1171` - First line of docstring should be in imperative mood: "The open e-acceptance request of the released letter; a different letter cancels the prior one."
- `app/services/documents/merge.py:226` - First line of docstring should be in imperative mood: "The merge context for ``fields``: ``merge_data`` first, then registered sources (by ``merge_refs``)."
- `app/services/documents/render.py:208` - First line of docstring should be in imperative mood: "A QR code image flowable (segno PNG, in memory)."
- `app/services/documents/repository.py:226` - First line of docstring should be in imperative mood: "A document row without versions (callers add the first version)."
- `app/services/documents/service.py:266` - First line of docstring should be in imperative mood: "The signed-in tenant user's own signer row (404 otherwise)."
- `app/services/documents/templates.py:145` - First line of docstring should be in imperative mood: "A new draft version (syntax-checked; unknown fields are reported at publish)."
- `app/services/documents/templates.py:244` - First line of docstring should be in imperative mood: "Same kind, Legal Entity variant of ``template_id`` when one exists (else ``template_id``)."
- `app/services/entitlements.py:62` - First line of docstring should be in imperative mood: "True if using this module draws on the credit/metering sub-system (debited after the check)."
- `app/services/entitlements.py:161` - First line of docstring should be in imperative mood: "The modules this tenant may use (cached). Unassigned tenants fall back to DEFAULT_PLAN."
- `app/services/external_auth/signin.py:247` - First line of docstring should be in imperative mood: "The Client Portal sign-in page (company sign-in is offered there after email discovery)."
- `app/services/legal_entities/gstin.py:16` - First line of docstring should be in imperative mood: "The mod-36 check character for the first 14 characters of a GSTIN."
- `app/services/legal_entities/numbering.py:144` - First line of docstring should be in imperative mood: "The number ``assign`` would return now (no lock; display only, never use it for a document)."
- `app/services/legal_entities/periods.py:88` - First line of docstring should be in imperative mood: "Current month (in ``tz``) through ``months_ahead`` months ahead."
- `app/services/legal_entities/service.py:619` - First line of docstring should be in imperative mood: "The company account printed on invoices / statements: the default account for ``currency`` (or for"
- `app/services/messaging/sending.py:680` - First line of docstring should be in imperative mood: "A recruiter records the candidate's WhatsApp opt-in (``recruiter_attestation``) against the shown notice."
- `app/services/module_rbac.py:98` - First line of docstring should be in imperative mood: "The full capability catalog for the role-editor UI (``GET /admin/module-capabilities``)."
- `app/services/module_rbac.py:133` - First line of docstring should be in imperative mood: "Module caps a role confers. The system Tenant Admin always grants every current cap of every app"
- `app/services/notifications_service.py:450` - First line of docstring should be in imperative mood: "A portal deep link (relative to the app origin): never an internal-app link."
- `app/services/notifications_service.py:911` - First line of docstring should be in imperative mood: "The caller as a notification recipient (users, candidates, Client / Vendor Users)."
- `app/services/pipeline/portal.py:227` - First line of docstring should be in imperative mood: "The resume file sent with the submission (the client copy), never the candidate's master resume."
- `app/services/placements/portal.py:250` - First line of docstring should be in imperative mood: "The client's workforce within ``visible_placement_ids`` (never rates, bank / ID data or vendor identity)."
- `app/services/postings.py:352` - First line of docstring should be in imperative mood: "True when either ledger holds a non-released posting of the source."
- `app/services/privacy/consent.py:601` - First line of docstring should be in imperative mood: "True while the candidate has a non-terminal placement (table read through metadata, no domain import)."
- `app/services/privacy/consent.py:621` - First line of docstring should be in imperative mood: "The active consent of ``candidate_id`` for ``purpose``, else 409 ``consent_required``."
- `app/services/sandbox/seed.py:283` - First line of docstring should be in imperative mood: "An inactive, password-less internal user that owns the seeded records (FK target for ``owner_id``)."
- `app/services/workflow/registry.py:294` - First line of docstring should be in imperative mood: "The registered condition's answer, or ``None`` when no domain registered ``key``."

</details>

<details><summary><b>ARG001</b> unused-function-argument (26)</summary>

- `app/core/encrypted.py:72` - Unused function argument: `value`
- `app/services/ai_governance/hooks.py:108` - Unused function argument: `action`
- `app/services/ai_governance/search.py:33` - Unused function argument: `kind`
- `app/services/client_delegations.py:71` - Unused function argument: `ctx`
- `app/services/comms/channel_settings.py:53` - Unused function argument: `ctx`
- `app/services/demands/portal.py:118` - Unused function argument: `ctx`
- `app/services/documents/esign.py:953` - Unused function argument: `ctx`
- `app/services/ecosystem_governance/data_flows.py:78` - Unused function argument: `db`
- `app/services/fairness/aedt_p4.py:242` - Unused function argument: `params`
- `app/services/interview_captures/service.py:54` - Unused function argument: `ctx`
- `app/services/interview_captures/withdrawal.py:163` - Unused function argument: `action`
- `app/services/legal_entities/service.py:948` - Unused function argument: `ctx`
- `app/services/messaging/inbound.py:57` - Unused function argument: `ctx`
- `app/services/messaging/opt_outs.py:191` - Unused function argument: `ctx`
- `app/services/messaging/portal.py:60` - Unused function argument: `ctx`
- `app/services/messaging/sending.py:573` - Unused function argument: `ctx`
- `app/services/messaging/templates.py:145` - Unused function argument: `ctx`
- `app/services/placements/creation.py:440` - Unused function argument: `ctx`
- `app/services/placements/gates.py:235` - Unused function argument: `ctx`
- `app/services/placements/gates.py:297` - Unused function argument: `ctx`
- `app/services/placements/prejoining.py:78` - Unused function argument: `ctx`
- `app/services/placements/rate_segments.py:45` - Unused function argument: `ctx`
- `app/services/placements/rate_segments.py:250` - Unused function argument: `ctx`
- `app/services/placements/workers.py:238` - Unused function argument: `ctx`
- `app/services/placements/workers.py:266` - Unused function argument: `ctx`
- `app/tasks/ecosystem_governance.py:41` - Unused function argument: `dsr_service`

</details>

<details><summary><b>E402</b> module-import-not-at-top-of-file (11)</summary>

- `app/services/activity_service.py:37` - Module level import not at top of file
- `app/services/activity_service.py:38` - Module level import not at top of file
- `app/services/ai/__init__.py:92` - Module level import not at top of file
- `app/services/candidates/__init__.py:41` - Module level import not at top of file
- `app/services/external_auth/hooks.py:73` - Module level import not at top of file
- `app/services/messaging/sending.py:725` - Module level import not at top of file
- `app/services/oauth/hooks.py:66` - Module level import not at top of file
- `app/services/reporting/dashboards.py:409` - Module level import not at top of file
- `app/services/reporting/dashboards.py:414` - Module level import not at top of file
- `app/services/reporting/dashboards.py:419` - Module level import not at top of file
- `app/services/workflow/catalogue.py:209` - Module level import not at top of file

</details>

<details><summary><b>TC003</b> typing-only-standard-library-import (5)</summary>

- `app/schemas/ecosystem_governance.py:7` - Move standard library import `datetime` into a type-checking block
- `app/schemas/ecosystem_governance.py:8` - Move standard library import `uuid` into a type-checking block
- `app/services/documents/repository.py:19` - Move standard library import `collections.abc.Iterable` into a type-checking block
- `app/services/documents/repository.py:20` - Move standard library import `datetime.date` into a type-checking block
- `app/services/talent_pools/templates.py:13` - Move standard library import `uuid` into a type-checking block

</details>

<details><summary><b>RUF001</b> ambiguous-unicode-character-string (5)</summary>

- `app/services/campaign_copy/checks.py:175` - String contains ambiguous `–` (EN DASH). Did you mean `-` (HYPHEN-MINUS)?
- `app/services/client_portal/approvals.py:140` - String contains ambiguous `–` (EN DASH). Did you mean `-` (HYPHEN-MINUS)?
- `app/services/client_portal/approvals.py:153` - String contains ambiguous `–` (EN DASH). Did you mean `-` (HYPHEN-MINUS)?
- `app/services/pipeline/portal.py:293` - String contains ambiguous `–` (EN DASH). Did you mean `-` (HYPHEN-MINUS)?
- `app/services/pipeline/portal.py:378` - String contains ambiguous `–` (EN DASH). Did you mean `-` (HYPHEN-MINUS)?

</details>

<details><summary><b>ARG002</b> unused-method-argument (5)</summary>

- `app/services/comms_service.py:130` - Unused method argument: `db`
- `app/services/connectors/accounting/manual.py:21` - Unused method argument: `payload`
- `app/services/connectors/einvoice/manual.py:19` - Unused method argument: `payload`
- `app/services/connectors/esign/manual.py:19` - Unused method argument: `payload`
- `app/services/connectors/payroll/manual.py:20` - Unused method argument: `payload`

</details>

<details><summary><b>RUF028</b> invalid-formatter-suppression-comment (5)</summary>

- `app/services/connectors/accounting/manifests.py:108` - This suppression comment is invalid because it cannot be in an expression, pattern, argument list, or other non-statement
- `app/services/sandbox/seed.py:623` - This suppression comment is invalid because it cannot be in an expression, pattern, argument list, or other non-statement
- `app/services/sandbox/seed.py:637` - This suppression comment is invalid because it cannot be in an expression, pattern, argument list, or other non-statement
- `app/services/sandbox/seed.py:652` - This suppression comment is invalid because it cannot be in an expression, pattern, argument list, or other non-statement
- `app/services/sandbox/seed.py:663` - This suppression comment is invalid because it cannot be in an expression, pattern, argument list, or other non-statement

</details>

<details><summary><b>D400</b> missing-trailing-period (4)</summary>

- `app/core/integrations/errors.py:1` - First line should end with a period
- `app/services/approvals/service.py:796` - First line should end with a period
- `app/services/comms/hooks.py:1` - First line should end with a period
- `app/services/messaging/hooks.py:1` - First line should end with a period

</details>

<details><summary><b>D415</b> missing-terminal-punctuation (3)</summary>

- `app/core/integrations/errors.py:1` - First line should end with a period, question mark, or exclamation point
- `app/services/comms/hooks.py:1` - First line should end with a period, question mark, or exclamation point
- `app/services/messaging/hooks.py:1` - First line should end with a period, question mark, or exclamation point

</details>

<details><summary><b>F401</b> unused-import (2)</summary>

- `app/models/__init__.py:8` - `app.core.audit` imported but unused; consider removing, adding to `__all__`, or using a redundant alias
- `app/models/__init__.py:9` - `app.core.secure_links` imported but unused; consider removing, adding to `__all__`, or using a redundant alias

</details>

<details><summary><b>RUF002</b> ambiguous-unicode-character-docstring (2)</summary>

- `app/models/client_portal.py:1` - Docstring contains ambiguous `–` (EN DASH). Did you mean `-` (HYPHEN-MINUS)?
- `app/services/pipeline/portal.py:9` - Docstring contains ambiguous `–` (EN DASH). Did you mean `-` (HYPHEN-MINUS)?

</details>

<details><summary><b>N818</b> error-suffix-on-exception-name (2)</summary>

- `app/router/modules.py:50` - Exception name `ModuleUnavailable` should be named with an Error suffix
- `app/router/modules.py:58` - Exception name `ModuleTimeout` should be named with an Error suffix

</details>

<details><summary><b>C416</b> unnecessary-comprehension (2)</summary>

- `app/services/sandbox/seed.py:432` - Unnecessary dict comprehension (rewrite using `dict()`)
- `app/services/sandbox/seed.py:589` - Unnecessary dict comprehension (rewrite using `dict()`)

</details>

<details><summary><b>RUF052</b> used-dummy-variable (2)</summary>

- `app/tasks/pipeline.py:67` - Local dummy variable `_db_name` is accessed
- `app/tasks/pipeline.py:67` - Local dummy variable `_schema_name` is accessed

</details>

<details><summary><b>RUF075</b> fallible-context-manager (1)</summary>

- `app/core/manager.py:21` - Context manager does not catch exceptions

</details>

<details><summary><b>SIM101</b> duplicate-isinstance-call (1)</summary>

- `app/core/money.py:44` - Multiple `isinstance` calls for `value`, merge into a single call

</details>

<details><summary><b>D301</b> escape-sequence-in-docstring (1)</summary>

- `app/services/ai/redaction.py:1` - Use `r"""` if any backslashes in a docstring

</details>

<details><summary><b>UP047</b> non-pep695-generic-function (1)</summary>

- `app/services/crm/won.py:146` - Generic function `_step` should use type parameters

</details>

<details><summary><b>TC002</b> typing-only-third-party-import (1)</summary>

- `app/services/documents/repository.py:24` - Move third-party import `sqlalchemy.ext.asyncio.AsyncSession` into a type-checking block

</details>

<details><summary><b>SIM113</b> enumerate-for-loop (1)</summary>

- `app/services/knowledge/index.py:208` - Use `enumerate()` for index variable `pos` in `for` loop

</details>

### 1b. E501 line-too-long - full list

<details><summary>Show all E501 locations</summary>

- `app/api.py:37` - Line too long (113 > 110)
- `app/cli.py:88` - Line too long (121 > 110)
- `app/cli.py:206` - Line too long (120 > 110)
- `app/cli.py:224` - Line too long (111 > 110)
- `app/cli.py:231` - Line too long (114 > 110)
- `app/core/actors.py:3` - Line too long (111 > 110)
- `app/core/actors.py:5` - Line too long (112 > 110)
- `app/core/actors.py:6` - Line too long (120 > 110)
- `app/core/actors.py:7` - Line too long (138 > 110)
- `app/core/actors.py:114` - Line too long (115 > 110)
- `app/core/celery.py:7` - Line too long (112 > 110)
- `app/core/celery.py:8` - Line too long (111 > 110)
- `app/core/celery.py:30` - Line too long (111 > 110)
- `app/core/celery.py:39` - Line too long (111 > 110)
- `app/core/constants.py:16` - Line too long (115 > 110)
- `app/core/constants.py:24` - Line too long (116 > 110)
- `app/core/constants.py:34` - Line too long (121 > 110)
- `app/core/constants.py:35` - Line too long (121 > 110)
- `app/core/constants.py:36` - Line too long (125 > 110)
- `app/core/constants.py:37` - Line too long (120 > 110)
- `app/core/constants.py:39` - Line too long (128 > 110)
- `app/core/constants.py:41` - Line too long (127 > 110)
- `app/core/constants.py:57` - Line too long (121 > 110)
- `app/core/constants.py:59` - Line too long (115 > 110)
- `app/core/constants.py:61` - Line too long (114 > 110)
- `app/core/constants.py:64` - Line too long (114 > 110)
- `app/core/constants.py:66` - Line too long (111 > 110)
- `app/core/constants.py:69` - Line too long (113 > 110)
- `app/core/constants.py:143` - Line too long (112 > 110)
- `app/core/constants.py:147` - Line too long (112 > 110)
- `app/core/constants.py:151` - Line too long (112 > 110)
- `app/core/constants.py:156` - Line too long (124 > 110)
- `app/core/constants.py:160` - Line too long (122 > 110)
- `app/core/db_errors.py:49` - Line too long (111 > 110)
- `app/core/encrypted.py:99` - Line too long (114 > 110)
- `app/core/errors.py:47` - Line too long (114 > 110)
- `app/core/errors.py:78` - Line too long (111 > 110)
- `app/core/events.py:18` - Line too long (118 > 110)
- `app/core/events.py:19` - Line too long (122 > 110)
- `app/core/events.py:20` - Line too long (114 > 110)
- `app/core/events.py:21` - Line too long (119 > 110)
- `app/core/fairness.py:55` - Line too long (117 > 110)
- `app/core/fairness.py:74` - Line too long (117 > 110)
- `app/core/fairness.py:82` - Line too long (113 > 110)
- `app/core/fairness.py:99` - Line too long (116 > 110)
- `app/core/hooks.py:22` - Line too long (114 > 110)
- `app/core/idempotency.py:1` - Line too long (111 > 110)
- `app/core/idempotency.py:7` - Line too long (119 > 110)
- `app/core/idempotency.py:10` - Line too long (111 > 110)
- `app/core/idempotency.py:173` - Line too long (112 > 110)
- `app/core/integrations/capabilities.py:3` - Line too long (116 > 110)
- `app/core/integrations/context.py:4` - Line too long (111 > 110)
- `app/core/integrations/context.py:85` - Line too long (113 > 110)
- `app/core/integrations/context.py:102` - Line too long (111 > 110)
- `app/core/integrations/context.py:144` - Line too long (111 > 110)
- `app/core/integrations/diff.py:33` - Line too long (114 > 110)
- `app/core/integrations/diff.py:34` - Line too long (114 > 110)
- `app/core/integrations/errors.py:1` - Line too long (119 > 110)
- `app/core/integrations/errors.py:5` - Line too long (113 > 110)
- `app/core/integrations/errors.py:8` - Line too long (120 > 110)
- `app/core/integrations/fakes.py:5` - Line too long (116 > 110)
- `app/core/integrations/fakes.py:7` - Line too long (114 > 110)
- `app/core/integrations/fakes.py:25` - Line too long (112 > 110)
- `app/core/integrations/fakes.py:125` - Line too long (111 > 110)
- `app/core/integrations/fakes.py:138` - Line too long (111 > 110)
- `app/core/integrations/manifest.py:1` - Line too long (117 > 110)
- `app/core/integrations/manifest.py:2` - Line too long (119 > 110)
- `app/core/integrations/manifest.py:31` - Line too long (113 > 110)
- `app/core/integrations/manifest.py:86` - Line too long (113 > 110)
- `app/core/integrations/manifest.py:108` - Line too long (112 > 110)
- `app/core/integrations/registry.py:1` - Line too long (118 > 110)
- `app/core/integrations/registry.py:4` - Line too long (120 > 110)
- `app/core/integrations/registry.py:5` - Line too long (117 > 110)
- `app/core/integrations/registry.py:50` - Line too long (117 > 110)
- `app/core/integrations/registry.py:52` - Line too long (116 > 110)
- `app/core/integrations/registry.py:75` - Line too long (113 > 110)
- `app/core/integrations/registry.py:100` - Line too long (114 > 110)
- `app/core/integrations/registry.py:115` - Line too long (115 > 110)
- `app/core/integrations/registry.py:146` - Line too long (112 > 110)
- `app/core/integrations/registry.py:238` - Line too long (113 > 110)
- `app/core/middleware/rate_limit.py:75` - Line too long (128 > 110)
- `app/core/middleware/rate_limit.py:115` - Line too long (119 > 110)
- `app/core/middleware/rate_limit.py:119` - Line too long (126 > 110)
- `app/core/middleware/sandbox.py:3` - Line too long (117 > 110)
- `app/core/middleware/sandbox.py:4` - Line too long (118 > 110)
- `app/core/middleware/sandbox.py:5` - Line too long (118 > 110)
- `app/core/migration_helpers.py:172` - Line too long (111 > 110)
- `app/core/migration_helpers.py:252` - Line too long (119 > 110)
- `app/core/migration_helpers.py:299` - Line too long (111 > 110)
- `app/core/migration_helpers.py:768` - Line too long (113 > 110)
- `app/core/migration_helpers.py:794` - Line too long (115 > 110)
- `app/core/org_grants.py:73` - Line too long (113 > 110)
- `app/core/permissions.py:210` - Line too long (117 > 110)
- `app/core/permissions.py:247` - Line too long (111 > 110)
- `app/core/permissions.py:275` - Line too long (112 > 110)
- `app/core/permissions.py:276` - Line too long (112 > 110)
- `app/core/permissions.py:290` - Line too long (116 > 110)
- `app/core/permissions.py:291` - Line too long (116 > 110)
- `app/core/permissions.py:292` - Line too long (116 > 110)
- `app/core/permissions.py:305` - Line too long (111 > 110)
- `app/core/permissions.py:342` - Line too long (116 > 110)
- `app/core/permissions.py:352` - Line too long (113 > 110)
- `app/core/permissions.py:353` - Line too long (112 > 110)
- `app/core/permissions.py:372` - Line too long (113 > 110)
- `app/core/permissions.py:373` - Line too long (115 > 110)
- `app/core/permissions.py:395` - Line too long (113 > 110)
- `app/core/permissions.py:396` - Line too long (114 > 110)
- `app/core/permissions.py:418` - Line too long (111 > 110)
- `app/core/permissions.py:419` - Line too long (113 > 110)
- `app/core/permissions.py:438` - Line too long (113 > 110)
- `app/core/permissions.py:455` - Line too long (111 > 110)
- `app/core/permissions.py:471` - Line too long (114 > 110)
- `app/core/permissions.py:472` - Line too long (111 > 110)
- `app/core/permissions.py:485` - Line too long (111 > 110)
- `app/core/permissions.py:499` - Line too long (111 > 110)
- `app/core/permissions.py:500` - Line too long (116 > 110)
- `app/core/permissions.py:519` - Line too long (115 > 110)
- `app/core/permissions.py:561` - Line too long (118 > 110)
- `app/core/permissions.py:571` - Line too long (111 > 110)
- `app/core/permissions.py:583` - Line too long (114 > 110)
- `app/core/permissions.py:584` - Line too long (111 > 110)
- `app/core/permissions.py:599` - Line too long (113 > 110)
- `app/core/permissions.py:600` - Line too long (118 > 110)
- `app/core/permissions.py:612` - Line too long (111 > 110)
- `app/core/permissions.py:613` - Line too long (112 > 110)
- `app/core/permissions.py:631` - Line too long (112 > 110)
- `app/core/permissions.py:646` - Line too long (113 > 110)
- `app/core/permissions.py:647` - Line too long (113 > 110)
- `app/core/permissions.py:648` - Line too long (118 > 110)
- `app/core/permissions.py:665` - Line too long (115 > 110)
- `app/core/permissions.py:666` - Line too long (117 > 110)
- `app/core/permissions.py:687` - Line too long (115 > 110)
- `app/core/permissions.py:711` - Line too long (112 > 110)
- `app/core/permissions.py:712` - Line too long (119 > 110)
- `app/core/permissions.py:713` - Line too long (120 > 110)
- `app/core/permissions.py:733` - Line too long (112 > 110)
- `app/core/permissions.py:734` - Line too long (117 > 110)
- `app/core/permissions.py:756` - Line too long (118 > 110)
- `app/core/permissions.py:757` - Line too long (116 > 110)
- `app/core/permissions.py:780` - Line too long (112 > 110)
- `app/core/permissions.py:781` - Line too long (116 > 110)
- `app/core/permissions.py:782` - Line too long (119 > 110)
- `app/core/permissions.py:792` - Line too long (115 > 110)
- `app/core/permissions.py:793` - Line too long (113 > 110)
- `app/core/permissions.py:794` - Line too long (116 > 110)
- `app/core/permissions.py:821` - Line too long (120 > 110)
- `app/core/permissions.py:822` - Line too long (115 > 110)
- `app/core/permissions.py:839` - Line too long (121 > 110)
- `app/core/permissions.py:841` - Line too long (116 > 110)
- `app/core/permissions.py:861` - Line too long (118 > 110)
- `app/core/permissions.py:883` - Line too long (111 > 110)
- `app/core/permissions.py:913` - Line too long (112 > 110)
- `app/core/permissions.py:921` - Line too long (111 > 110)
- `app/core/permissions.py:928` - Line too long (115 > 110)
- `app/core/permissions.py:932` - Line too long (120 > 110)
- `app/core/permissions.py:934` - Line too long (123 > 110)
- `app/core/permissions.py:943` - Line too long (118 > 110)
- `app/core/permissions.py:945` - Line too long (127 > 110)
- `app/core/permissions.py:949` - Line too long (118 > 110)
- `app/core/permissions.py:950` - Line too long (118 > 110)
- `app/core/permissions.py:953` - Line too long (121 > 110)
- `app/core/permissions.py:958` - Line too long (116 > 110)
- `app/core/permissions.py:960` - Line too long (115 > 110)
- `app/core/permissions.py:966` - Line too long (118 > 110)
- `app/core/permissions.py:973` - Line too long (119 > 110)
- `app/core/permissions.py:977` - Line too long (118 > 110)
- `app/core/permissions.py:978` - Line too long (118 > 110)
- `app/core/permissions.py:979` - Line too long (120 > 110)
- `app/core/permissions.py:988` - Line too long (113 > 110)
- `app/core/permissions.py:994` - Line too long (114 > 110)
- `app/core/permissions.py:999` - Line too long (118 > 110)
- `app/core/permissions.py:1047` - Line too long (115 > 110)
- `app/core/permissions.py:1048` - Line too long (115 > 110)
- `app/core/permissions.py:1049` - Line too long (121 > 110)
- `app/core/permissions.py:1434` - Line too long (114 > 110)
- `app/core/permissions.py:1473` - Line too long (116 > 110)
- `app/core/permissions.py:1503` - Line too long (111 > 110)
- `app/core/permissions.py:1581` - Line too long (112 > 110)
- `app/core/permissions.py:1608` - Line too long (116 > 110)
- `app/core/permissions.py:1609` - Line too long (115 > 110)
- `app/core/permissions.py:1610` - Line too long (117 > 110)
- `app/core/permissions.py:1634` - Line too long (111 > 110)
- `app/core/permissions.py:1649` - Line too long (113 > 110)
- `app/core/permissions.py:1677` - Line too long (114 > 110)
- `app/core/permissions.py:1690` - Line too long (114 > 110)
- `app/core/permissions.py:1691` - Line too long (114 > 110)
- `app/core/permissions.py:1703` - Line too long (115 > 110)
- `app/core/permissions.py:1716` - Line too long (117 > 110)
- `app/core/permissions.py:1728` - Line too long (114 > 110)
- `app/core/permissions.py:1769` - Line too long (112 > 110)
- `app/core/permissions.py:1783` - Line too long (118 > 110)
- `app/core/permissions.py:1784` - Line too long (118 > 110)
- `app/core/permissions.py:1811` - Line too long (116 > 110)
- `app/core/permissions.py:1812` - Line too long (114 > 110)
- `app/core/permissions.py:1829` - Line too long (117 > 110)
- `app/core/permissions.py:1855` - Line too long (111 > 110)
- `app/core/permissions.py:1856` - Line too long (118 > 110)
- `app/core/permissions.py:1948` - Line too long (114 > 110)
- `app/core/permissions.py:1949` - Line too long (111 > 110)
- `app/core/permissions.py:1973` - Line too long (115 > 110)
- `app/core/permissions.py:1986` - Line too long (111 > 110)
- `app/core/permissions.py:2014` - Line too long (113 > 110)
- `app/core/permissions.py:2026` - Line too long (112 > 110)
- `app/core/permissions.py:2044` - Line too long (111 > 110)
- `app/core/permissions.py:2054` - Line too long (117 > 110)
- `app/core/permissions.py:2069` - Line too long (119 > 110)
- `app/core/permissions.py:2070` - Line too long (115 > 110)
- `app/core/permissions.py:2071` - Line too long (122 > 110)
- `app/core/permissions.py:2088` - Line too long (118 > 110)
- `app/core/permissions.py:2127` - Line too long (118 > 110)
- `app/core/permissions.py:2128` - Line too long (114 > 110)
- `app/core/permissions.py:2193` - Line too long (114 > 110)
- `app/core/permissions.py:2242` - Line too long (124 > 110)
- `app/core/permissions.py:2243` - Line too long (119 > 110)
- `app/core/prediction_port.py:5` - Line too long (111 > 110)
- `app/core/prediction_port.py:6` - Line too long (112 > 110)
- `app/core/provisioning.py:2` - Line too long (111 > 110)
- `app/core/rate_limit_policies.py:48` - Line too long (111 > 110)
- `app/core/rate_limit_policies.py:62` - Line too long (116 > 110)
- `app/core/rate_limit_policies.py:64` - Line too long (111 > 110)
- `app/core/rate_limit_policies.py:65` - Line too long (126 > 110)
- `app/core/rate_limit_policies.py:68` - Line too long (118 > 110)
- `app/core/rate_limit_policies.py:69` - Line too long (117 > 110)
- `app/core/secure_links.py:11` - Line too long (111 > 110)
- `app/core/secure_links.py:220` - Line too long (115 > 110)
- `app/core/secure_links.py:248` - Line too long (113 > 110)
- `app/core/security.py:94` - Line too long (111 > 110)
- `app/core/security.py:95` - Line too long (117 > 110)
- `app/core/security.py:108` - Line too long (114 > 110)
- `app/core/settings.py:132` - Line too long (113 > 110)
- `app/core/settings.py:139` - Line too long (116 > 110)
- `app/core/settings.py:155` - Line too long (112 > 110)
- `app/core/settings.py:160` - Line too long (117 > 110)
- `app/core/settings.py:168` - Line too long (116 > 110)
- `app/core/settings.py:171` - Line too long (114 > 110)
- `app/core/settings.py:172` - Line too long (111 > 110)
- `app/core/settings.py:232` - Line too long (113 > 110)
- `app/core/settings.py:256` - Line too long (116 > 110)
- `app/core/settings.py:276` - Line too long (115 > 110)
- `app/core/settings.py:277` - Line too long (116 > 110)
- `app/core/settings.py:306` - Line too long (117 > 110)
- `app/core/settings.py:345` - Line too long (117 > 110)
- `app/core/settings.py:411` - Line too long (114 > 110)
- `app/core/skill_refs.py:3` - Line too long (111 > 110)
- `app/core/skill_refs.py:5` - Line too long (113 > 110)
- `app/core/tenancy.py:48` - Line too long (114 > 110)
- `app/core/tenancy.py:52` - Line too long (112 > 110)
- `app/core/tenancy.py:95` - Line too long (115 > 110)
- `app/core/tenancy.py:112` - Line too long (115 > 110)
- `app/core/tenancy.py:138` - Line too long (113 > 110)
- `app/core/tenancy.py:139` - Line too long (116 > 110)
- `app/core/tenancy.py:152` - Line too long (119 > 110)
- `app/core/tenancy.py:162` - Line too long (112 > 110)
- `app/core/tenancy.py:188` - Line too long (112 > 110)
- `app/core/tenancy.py:189` - Line too long (112 > 110)
- `app/core/tenancy.py:195` - Line too long (116 > 110)
- `app/core/tenancy.py:196` - Line too long (118 > 110)
- `app/core/tenancy.py:223` - Line too long (116 > 110)
- `app/core/tenancy.py:224` - Line too long (117 > 110)
- `app/dependencies/ai.py:30` - Line too long (112 > 110)
- `app/dependencies/candidate.py:8` - Line too long (111 > 110)
- `app/dependencies/candidate.py:48` - Line too long (113 > 110)
- `app/dependencies/client.py:1` - Line too long (112 > 110)
- `app/dependencies/worker.py:19` - Line too long (113 > 110)
- `app/events/activity.py:21` - Line too long (112 > 110)
- `app/events/hrms_sync.py:1` - Line too long (116 > 110)
- `app/events/ledger_sync.py:1` - Line too long (118 > 110)
- `app/job/scheduler.py:24` - Line too long (121 > 110)
- `app/job/schedules.py:157` - Line too long (113 > 110)
- `app/job/schedules.py:205` - Line too long (114 > 110)
- `app/job/schedules.py:231` - Line too long (113 > 110)
- `app/job/schedules.py:246` - Line too long (111 > 110)
- `app/job/schedules.py:310` - Line too long (113 > 110)
- `app/job/schedules.py:326` - Line too long (116 > 110)
- `app/job/schedules.py:327` - Line too long (112 > 110)
- `app/job/schedules.py:332` - Line too long (111 > 110)
- `app/job/schedules.py:344` - Line too long (113 > 110)
- `app/job/schedules.py:345` - Line too long (116 > 110)
- `app/job/schedules.py:357` - Line too long (114 > 110)
- `app/job/schedules.py:367` - Line too long (117 > 110)
- `app/job/schedules.py:368` - Line too long (120 > 110)
- `app/job/schedules.py:377` - Line too long (111 > 110)
- `app/job/schedules.py:387` - Line too long (115 > 110)
- `app/job/schedules.py:391` - Line too long (120 > 110)
- `app/job/schedules.py:405` - Line too long (116 > 110)
- `app/job/schedules.py:408` - Line too long (120 > 110)
- `app/job/schedules.py:409` - Line too long (116 > 110)
- `app/job/schedules.py:413` - Line too long (119 > 110)
- `app/job/schedules.py:414` - Line too long (120 > 110)
- `app/job/schedules.py:418` - Line too long (124 > 110)
- `app/job/schedules.py:428` - Line too long (112 > 110)
- `app/job/schedules.py:429` - Line too long (122 > 110)
- `app/job/schedules.py:431` - Line too long (113 > 110)
- `app/job/schedules.py:432` - Line too long (135 > 110)
- `app/job/schedules.py:434` - Line too long (122 > 110)
- `app/job/schedules.py:437` - Line too long (117 > 110)
- `app/job/schedules.py:440` - Line too long (111 > 110)
- `app/job/schedules.py:443` - Line too long (117 > 110)
- `app/job/schedules.py:445` - Line too long (120 > 110)
- `app/job/schedules.py:447` - Line too long (120 > 110)
- `app/job/schedules.py:457` - Line too long (120 > 110)
- `app/job/schedules.py:461` - Line too long (120 > 110)
- `app/job/schedules.py:462` - Line too long (112 > 110)
- `app/job/schedules.py:467` - Line too long (111 > 110)
- `app/job/schedules.py:468` - Line too long (125 > 110)
- `app/job/schedules.py:475` - Line too long (120 > 110)
- `app/job/schedules.py:492` - Line too long (119 > 110)
- `app/job/schedules.py:508` - Line too long (116 > 110)
- `app/job/schedules.py:520` - Line too long (120 > 110)
- `app/job/schedules.py:533` - Line too long (116 > 110)
- `app/job/schedules.py:542` - Line too long (114 > 110)
- `app/job/schedules.py:546` - Line too long (121 > 110)
- `app/job/schedules.py:551` - Line too long (114 > 110)
- `app/job/schedules.py:560` - Line too long (118 > 110)
- `app/job/schedules.py:565` - Line too long (123 > 110)
- `app/job/schedules.py:566` - Line too long (126 > 110)
- `app/job/schedules.py:570` - Line too long (113 > 110)
- `app/job/schedules.py:574` - Line too long (117 > 110)
- `app/job/schedules.py:575` - Line too long (122 > 110)
- `app/job/schedules.py:576` - Line too long (126 > 110)
- `app/job/schedules.py:581` - Line too long (116 > 110)
- `app/job/schedules.py:585` - Line too long (120 > 110)
- `app/job/schedules.py:586` - Line too long (131 > 110)
- `app/job/schedules.py:590` - Line too long (114 > 110)
- `app/job/schedules.py:599` - Line too long (119 > 110)
- `app/job/schedules.py:609` - Line too long (118 > 110)
- `app/main.py:116` - Line too long (115 > 110)
- `app/main.py:147` - Line too long (119 > 110)
- `app/main.py:151` - Line too long (115 > 110)
- `app/main.py:155` - Line too long (111 > 110)
- `app/models/__init__.py:137` - Line too long (114 > 110)
- `app/models/__init__.py:142` - Line too long (116 > 110)
- `app/models/__init__.py:188` - Line too long (118 > 110)
- `app/models/__init__.py:209` - Line too long (114 > 110)
- `app/models/__init__.py:221` - Line too long (117 > 110)
- `app/models/__init__.py:238` - Line too long (113 > 110)
- `app/models/__init__.py:243` - Line too long (122 > 110)
- `app/models/__init__.py:248` - Line too long (118 > 110)
- `app/models/__init__.py:254` - Line too long (112 > 110)
- `app/models/__init__.py:259` - Line too long (117 > 110)
- `app/models/__init__.py:270` - Line too long (119 > 110)
- `app/models/__init__.py:275` - Line too long (116 > 110)
- `app/models/__init__.py:281` - Line too long (113 > 110)
- `app/models/__init__.py:286` - Line too long (115 > 110)
- `app/models/__init__.py:304` - Line too long (114 > 110)
- `app/models/__init__.py:309` - Line too long (118 > 110)
- `app/models/__init__.py:314` - Line too long (111 > 110)
- `app/models/__init__.py:329` - Line too long (118 > 110)
- `app/models/__init__.py:335` - Line too long (114 > 110)
- `app/models/__init__.py:341` - Line too long (112 > 110)
- `app/models/__init__.py:346` - Line too long (118 > 110)
- `app/models/activity.py:31` - Line too long (118 > 110)
- `app/models/ai.py:45` - Line too long (112 > 110)
- `app/models/ai.py:51` - Line too long (114 > 110)
- `app/models/ai.py:54` - Line too long (116 > 110)
- `app/models/ai.py:59` - Line too long (111 > 110)
- `app/models/ai.py:145` - Line too long (117 > 110)
- `app/models/approvals.py:145` - Line too long (112 > 110)
- `app/models/approvals.py:177` - Line too long (111 > 110)
- `app/models/approvals.py:218` - Line too long (112 > 110)
- `app/models/approvals.py:220` - Line too long (112 > 110)
- `app/models/billing.py:179` - Line too long (115 > 110)
- `app/models/billing.py:230` - Line too long (118 > 110)
- `app/models/billing.py:584` - Line too long (120 > 110)
- `app/models/billing.py:608` - Line too long (115 > 110)
- `app/models/billing.py:632` - Line too long (121 > 110)
- `app/models/billing.py:642` - Line too long (115 > 110)
- `app/models/candidates.py:349` - Line too long (119 > 110)
- `app/models/client_portal.py:4` - Line too long (113 > 110)
- `app/models/client_portal.py:7` - Line too long (112 > 110)
- `app/models/commercials.py:240` - Line too long (114 > 110)
- `app/models/comms.py:47` - Line too long (118 > 110)
- `app/models/comms.py:182` - Line too long (115 > 110)
- `app/models/comms.py:213` - Line too long (111 > 110)
- `app/models/comms.py:225` - Line too long (123 > 110)
- `app/models/comms.py:251` - Line too long (118 > 110)
- `app/models/comms.py:282` - Line too long (120 > 110)
- `app/models/comms.py:283` - Line too long (118 > 110)
- `app/models/comms.py:302` - Line too long (112 > 110)
- `app/models/comms.py:323` - Line too long (112 > 110)
- `app/models/comms.py:342` - Line too long (114 > 110)
- `app/models/comms.py:349` - Line too long (118 > 110)
- `app/models/comms.py:359` - Line too long (111 > 110)
- `app/models/comms.py:369` - Line too long (112 > 110)
- `app/models/connectors.py:3` - Line too long (111 > 110)
- `app/models/connectors.py:5` - Line too long (111 > 110)
- `app/models/connectors.py:44` - Line too long (111 > 110)
- `app/models/connectors.py:49` - Line too long (112 > 110)
- `app/models/connectors.py:96` - Line too long (113 > 110)
- `app/models/connectors.py:127` - Line too long (112 > 110)
- `app/models/connectors.py:129` - Line too long (116 > 110)
- `app/models/connectors.py:157` - Line too long (117 > 110)
- `app/models/demands.py:130` - Line too long (113 > 110)
- `app/models/golive.py:4` - Line too long (113 > 110)
- `app/models/identity.py:113` - Line too long (127 > 110)
- `app/models/imports.py:1` - Line too long (113 > 110)
- `app/models/imports.py:64` - Line too long (112 > 110)
- `app/models/ingest.py:59` - Line too long (119 > 110)
- `app/models/ingest.py:91` - Line too long (118 > 110)
- `app/models/interviews.py:36` - Line too long (113 > 110)
- `app/models/legal_entities.py:72` - Line too long (112 > 110)
- `app/models/legal_entities.py:155` - Line too long (112 > 110)
- `app/models/legal_entities.py:266` - Line too long (115 > 110)
- `app/models/legal_entities.py:288` - Line too long (117 > 110)
- `app/models/legal_entities.py:333` - Line too long (119 > 110)
- `app/models/notifications.py:66` - Line too long (113 > 110)
- `app/models/notifications.py:76` - Line too long (113 > 110)
- `app/models/offers.py:11` - Line too long (111 > 110)
- `app/models/offers.py:56` - Line too long (124 > 110)
- `app/models/offers.py:95` - Line too long (112 > 110)
- `app/models/pipeline.py:61` - Line too long (117 > 110)
- `app/models/placements.py:1` - Line too long (113 > 110)
- `app/models/placements.py:5` - Line too long (111 > 110)
- `app/models/placements.py:7` - Line too long (111 > 110)
- `app/models/placements.py:214` - Line too long (111 > 110)
- `app/models/placements.py:384` - Line too long (114 > 110)
- `app/models/placements.py:408` - Line too long (111 > 110)
- `app/models/placements.py:410` - Line too long (113 > 110)
- `app/models/placements.py:414` - Line too long (113 > 110)
- `app/models/placements.py:418` - Line too long (115 > 110)
- `app/models/placements.py:427` - Line too long (112 > 110)
- `app/models/placements.py:429` - Line too long (113 > 110)
- `app/models/placements.py:455` - Line too long (113 > 110)
- `app/models/platform.py:224` - Line too long (112 > 110)
- `app/models/platform.py:225` - Line too long (119 > 110)
- `app/models/portal.py:106` - Line too long (111 > 110)
- `app/models/postings.py:6` - Line too long (111 > 110)
- `app/models/postings.py:40` - Line too long (117 > 110)
- `app/models/privacy.py:44` - Line too long (115 > 110)
- `app/models/privacy.py:45` - Line too long (112 > 110)
- `app/models/privacy.py:46` - Line too long (116 > 110)
- `app/models/privacy.py:82` - Line too long (112 > 110)
- `app/models/privacy.py:253` - Line too long (111 > 110)
- `app/models/privacy.py:324` - Line too long (112 > 110)
- `app/models/privacy.py:329` - Line too long (126 > 110)
- `app/models/settings.py:25` - Line too long (111 > 110)
- `app/models/sla.py:111` - Line too long (119 > 110)
- `app/models/sla.py:138` - Line too long (113 > 110)
- `app/models/tasks.py:27` - Line too long (116 > 110)
- `app/models/tasks.py:43` - Line too long (120 > 110)
- `app/models/time.py:350` - Line too long (114 > 110)
- `app/models/time.py:352` - Line too long (114 > 110)
- `app/models/time.py:385` - Line too long (111 > 110)
- `app/models/time.py:409` - Line too long (111 > 110)
- `app/models/time.py:463` - Line too long (111 > 110)
- `app/models/time.py:486` - Line too long (111 > 110)
- `app/models/time.py:547` - Line too long (112 > 110)
- `app/models/workflow.py:105` - Line too long (115 > 110)
- `app/models/workflow.py:106` - Line too long (118 > 110)
- `app/models/workflow.py:148` - Line too long (111 > 110)
- `app/router/__init__.py:39` - Line too long (114 > 110)
- `app/router/__init__.py:263` - Line too long (113 > 110)
- `app/router/__init__.py:325` - Line too long (112 > 110)
- `app/router/__init__.py:367` - Line too long (112 > 110)
- `app/router/__init__.py:375` - Line too long (111 > 110)
- `app/router/__init__.py:423` - Line too long (113 > 110)
- `app/router/__init__.py:465` - Line too long (113 > 110)
- `app/router/__init__.py:473` - Line too long (113 > 110)
- `app/router/__init__.py:482` - Line too long (112 > 110)
- `app/router/__init__.py:491` - Line too long (113 > 110)
- `app/router/__init__.py:507` - Line too long (117 > 110)
- `app/router/__init__.py:527` - Line too long (116 > 110)
- `app/router/__init__.py:535` - Line too long (114 > 110)
- `app/router/__init__.py:536` - Line too long (118 > 110)
- `app/router/__init__.py:543` - Line too long (115 > 110)
- `app/router/__init__.py:551` - Line too long (112 > 110)
- `app/router/__init__.py:552` - Line too long (118 > 110)
- `app/router/__init__.py:559` - Line too long (112 > 110)
- `app/router/__init__.py:569` - Line too long (113 > 110)
- `app/router/__init__.py:576` - Line too long (111 > 110)
- `app/router/__init__.py:584` - Line too long (111 > 110)
- `app/router/__init__.py:585` - Line too long (116 > 110)
- `app/router/__init__.py:592` - Line too long (112 > 110)
- `app/router/__init__.py:600` - Line too long (121 > 110)
- `app/router/__init__.py:608` - Line too long (112 > 110)
- `app/router/__init__.py:617` - Line too long (121 > 110)
- `app/router/__init__.py:626` - Line too long (115 > 110)
- `app/router/__init__.py:635` - Line too long (121 > 110)
- `app/router/__init__.py:645` - Line too long (112 > 110)
- `app/router/__init__.py:652` - Line too long (116 > 110)
- `app/router/__init__.py:671` - Line too long (121 > 110)
- `app/router/__init__.py:680` - Line too long (111 > 110)
- `app/router/__init__.py:688` - Line too long (115 > 110)
- `app/router/__init__.py:696` - Line too long (118 > 110)
- `app/router/__init__.py:705` - Line too long (112 > 110)
- `app/router/__init__.py:712` - Line too long (115 > 110)
- `app/router/__init__.py:713` - Line too long (117 > 110)
- `app/router/__init__.py:721` - Line too long (121 > 110)
- `app/router/__init__.py:728` - Line too long (120 > 110)
- `app/router/__init__.py:744` - Line too long (118 > 110)
- `app/router/__init__.py:751` - Line too long (119 > 110)
- `app/router/__init__.py:759` - Line too long (118 > 110)
- `app/router/__init__.py:767` - Line too long (118 > 110)
- `app/router/__init__.py:776` - Line too long (133 > 110)
- `app/router/__init__.py:780` - Line too long (113 > 110)
- `app/router/__init__.py:781` - Line too long (113 > 110)
- `app/router/__init__.py:789` - Line too long (116 > 110)
- `app/router/__init__.py:790` - Line too long (113 > 110)
- `app/router/__init__.py:798` - Line too long (112 > 110)
- `app/router/__init__.py:799` - Line too long (124 > 110)
- `app/router/__init__.py:806` - Line too long (115 > 110)
- `app/router/ai.py:84` - Line too long (115 > 110)
- `app/router/ai.py:95` - Line too long (124 > 110)
- `app/router/ai.py:96` - Line too long (113 > 110)
- `app/router/api_keys.py:34` - Line too long (115 > 110)
- `app/router/candidates.py:273` - Line too long (115 > 110)
- `app/router/careers.py:73` - Line too long (112 > 110)
- `app/router/careers.py:75` - Line too long (111 > 110)
- `app/router/client_portal_admin.py:3` - Line too long (115 > 110)
- `app/router/client_portal_admin.py:4` - Line too long (112 > 110)
- `app/router/client_portal_approvals.py:2` - Line too long (114 > 110)
- `app/router/client_portal_workforce.py:3` - Line too long (111 > 110)
- `app/router/connector_hooks.py:3` - Line too long (113 > 110)
- `app/router/connector_hooks.py:22` - Line too long (115 > 110)
- `app/router/matching.py:53` - Line too long (115 > 110)
- `app/router/placements.py:3` - Line too long (115 > 110)
- `app/router/placements.py:4` - Line too long (111 > 110)
- `app/router/placements.py:5` - Line too long (113 > 110)
- `app/router/platform.py:1` - Line too long (111 > 110)
- `app/router/platform.py:348` - Line too long (112 > 110)
- `app/router/platform.py:356` - Line too long (122 > 110)
- `app/router/platform.py:369` - Line too long (114 > 110)
- `app/router/portal_notifications.py:3` - Line too long (111 > 110)
- `app/router/portal_offers.py:3` - Line too long (111 > 110)
- `app/router/privacy.py:76` - Line too long (116 > 110)
- `app/router/privacy.py:98` - Line too long (114 > 110)
- `app/router/privacy.py:126` - Line too long (112 > 110)
- `app/router/privacy.py:145` - Line too long (116 > 110)
- `app/router/privacy.py:191` - Line too long (117 > 110)
- `app/router/privacy.py:220` - Line too long (114 > 110)
- `app/router/privacy.py:255` - Line too long (112 > 110)
- `app/router/privacy.py:259` - Line too long (126 > 110)
- `app/router/privacy.py:271` - Line too long (120 > 110)
- `app/router/privacy.py:280` - Line too long (116 > 110)
- `app/router/privacy.py:288` - Line too long (112 > 110)
- `app/router/public.py:158` - Line too long (114 > 110)
- `app/router/settings.py:36` - Line too long (117 > 110)
- `app/router/settings.py:45` - Line too long (113 > 110)
- `app/router/settings.py:50` - Line too long (117 > 110)
- `app/router/sso.py:10` - Line too long (112 > 110)
- `app/router/sso.py:11` - Line too long (111 > 110)
- `app/router/sso.py:127` - Line too long (118 > 110)
- `app/router/worker_portal_placements.py:3` - Line too long (112 > 110)
- `app/router/worker_portal_placements.py:4` - Line too long (112 > 110)
- `app/schemas/activity.py:9` - Line too long (117 > 110)
- `app/schemas/ai.py:57` - Line too long (115 > 110)
- `app/schemas/billing.py:519` - Line too long (112 > 110)
- `app/schemas/billing.py:521` - Line too long (120 > 110)
- `app/schemas/billing.py:549` - Line too long (113 > 110)
- `app/schemas/careers.py:135` - Line too long (114 > 110)
- `app/schemas/careers.py:172` - Line too long (114 > 110)
- `app/schemas/client_hub_portal.py:1` - Line too long (112 > 110)
- `app/schemas/client_portal_admin.py:1` - Line too long (111 > 110)
- `app/schemas/connections.py:51` - Line too long (112 > 110)
- `app/schemas/connectors.py:9` - Line too long (128 > 110)
- `app/schemas/connectors.py:10` - Line too long (122 > 110)
- `app/schemas/connectors.py:13` - Line too long (119 > 110)
- `app/schemas/connectors.py:35` - Line too long (118 > 110)
- `app/schemas/golive.py:12` - Line too long (115 > 110)
- `app/schemas/matching.py:154` - Line too long (111 > 110)
- `app/schemas/messaging.py:14` - Line too long (111 > 110)
- `app/schemas/mobility_portal.py:59` - Line too long (113 > 110)
- `app/schemas/offers.py:1` - Line too long (112 > 110)
- `app/schemas/placements.py:116` - Line too long (113 > 110)
- `app/schemas/placements_api.py:1` - Line too long (115 > 110)
- `app/schemas/placements_api.py:122` - Line too long (114 > 110)
- `app/schemas/portal.py:167` - Line too long (117 > 110)
- `app/schemas/privacy.py:274` - Line too long (114 > 110)
- `app/schemas/receivables_portal.py:1` - Line too long (113 > 110)
- `app/schemas/sandbox.py:19` - Line too long (112 > 110)
- `app/schemas/settings.py:2` - Line too long (112 > 110)
- `app/schemas/sla.py:134` - Line too long (113 > 110)
- `app/schemas/tasks.py:9` - Line too long (133 > 110)
- `app/services/activity_erasure.py:4` - Line too long (111 > 110)
- `app/services/activity_erasure.py:49` - Line too long (126 > 110)
- `app/services/activity_erasure.py:54` - Line too long (136 > 110)
- `app/services/activity_service.py:94` - Line too long (113 > 110)
- `app/services/activity_service.py:226` - Line too long (118 > 110)
- `app/services/activity_service.py:250` - Line too long (112 > 110)
- `app/services/agent_tools/predictions/__init__.py:1` - Line too long (111 > 110)
- `app/services/ai/admin.py:108` - Line too long (114 > 110)
- `app/services/ai/admin.py:182` - Line too long (111 > 110)
- `app/services/ai/gateway.py:109` - Line too long (115 > 110)
- `app/services/api_keys_service.py:7` - Line too long (111 > 110)
- `app/services/api_keys_service.py:10` - Line too long (111 > 110)
- `app/services/api_keys_service.py:48` - Line too long (120 > 110)
- `app/services/api_keys_service.py:56` - Line too long (114 > 110)
- `app/services/approvals/__init__.py:3` - Line too long (113 > 110)
- `app/services/approvals/__init__.py:4` - Line too long (112 > 110)
- `app/services/approvals/registry.py:83` - Line too long (112 > 110)
- `app/services/approvals/registry.py:121` - Line too long (112 > 110)
- `app/services/approvals/service.py:16` - Line too long (111 > 110)
- `app/services/approvals/service.py:85` - Line too long (113 > 110)
- `app/services/approvals/service.py:191` - Line too long (127 > 110)
- `app/services/approvals/service.py:240` - Line too long (113 > 110)
- `app/services/approvals/service.py:498` - Line too long (114 > 110)
- `app/services/approvals/service.py:502` - Line too long (123 > 110)
- `app/services/approvals/service.py:597` - Line too long (113 > 110)
- `app/services/approvals/service.py:664` - Line too long (113 > 110)
- `app/services/approvals/service.py:697` - Line too long (128 > 110)
- `app/services/approvals/service.py:794` - Line too long (113 > 110)
- `app/services/approvals/service.py:820` - Line too long (117 > 110)
- `app/services/approvals/service.py:853` - Line too long (111 > 110)
- `app/services/approvals/service.py:984` - Line too long (114 > 110)
- `app/services/auth_service.py:1365` - Line too long (122 > 110)
- `app/services/candidates/access.py:21` - Line too long (119 > 110)
- `app/services/candidates/access.py:28` - Line too long (119 > 110)
- `app/services/candidates/imports.py:477` - Line too long (121 > 110)
- `app/services/candidates/portal_port.py:246` - Line too long (111 > 110)
- `app/services/candidates/records.py:743` - Line too long (112 > 110)
- `app/services/candidates_erasure.py:1` - Line too long (111 > 110)
- `app/services/candidates_erasure.py:68` - Line too long (111 > 110)
- `app/services/candidates_erasure.py:73` - Line too long (118 > 110)
- `app/services/candidates_erasure.py:133` - Line too long (119 > 110)
- `app/services/candidates_erasure.py:138` - Line too long (113 > 110)
- `app/services/candidates_erasure.py:152` - Line too long (120 > 110)
- `app/services/candidates_erasure.py:177` - Line too long (143 > 110)
- `app/services/candidates_erasure.py:178` - Line too long (124 > 110)
- `app/services/client_delegations.py:1` - Line too long (111 > 110)
- `app/services/client_delegations.py:7` - Line too long (112 > 110)
- `app/services/client_portal/approvals.py:4` - Line too long (111 > 110)
- `app/services/client_portal/approvals.py:5` - Line too long (111 > 110)
- `app/services/client_portal/approvals.py:6` - Line too long (116 > 110)
- `app/services/client_portal/approvals.py:7` - Line too long (113 > 110)
- `app/services/client_portal/approvals.py:8` - Line too long (111 > 110)
- `app/services/client_portal/compliance.py:3` - Line too long (113 > 110)
- `app/services/client_portal/compliance.py:5` - Line too long (117 > 110)
- `app/services/client_portal/compliance.py:6` - Line too long (111 > 110)
- `app/services/client_portal/home.py:2` - Line too long (113 > 110)
- `app/services/client_portal/hooks.py:5` - Line too long (114 > 110)
- `app/services/client_portal/hooks.py:8` - Line too long (113 > 110)
- `app/services/client_portal/hooks.py:49` - Line too long (120 > 110)
- `app/services/client_portal/links.py:3` - Line too long (114 > 110)
- `app/services/client_portal/links.py:6` - Line too long (112 > 110)
- `app/services/client_portal/links.py:7` - Line too long (113 > 110)
- `app/services/client_portal/links.py:9` - Line too long (112 > 110)
- `app/services/client_portal/links.py:83` - Line too long (116 > 110)
- `app/services/client_portal_base.py:1` - Line too long (112 > 110)
- `app/services/clients_service.py:157` - Line too long (119 > 110)
- `app/services/clients_service.py:168` - Line too long (119 > 110)
- `app/services/clients_service.py:399` - Line too long (111 > 110)
- `app/services/comms/defaults.py:105` - Line too long (112 > 110)
- `app/services/comms/defaults.py:109` - Line too long (111 > 110)
- `app/services/comms/defaults.py:110` - Line too long (116 > 110)
- `app/services/comms/merge_fields.py:91` - Line too long (117 > 110)
- `app/services/comms/merge_fields.py:92` - Line too long (119 > 110)
- `app/services/comms_service.py:779` - Line too long (115 > 110)
- `app/services/comms_service.py:780` - Line too long (115 > 110)
- `app/services/comms_service.py:804` - Line too long (113 > 110)
- `app/services/comms_service.py:835` - Line too long (114 > 110)
- `app/services/comms_service.py:903` - Line too long (114 > 110)
- `app/services/company_onboarding.py:12` - Line too long (115 > 110)
- `app/services/company_onboarding.py:63` - Line too long (111 > 110)
- `app/services/connections/fake.py:3` - Line too long (113 > 110)
- `app/services/connections/google.py:1` - Line too long (115 > 110)
- `app/services/connections/microsoft.py:238` - Line too long (112 > 110)
- `app/services/connections/microsoft.py:263` - Line too long (122 > 110)
- `app/services/connections/provider.py:4` - Line too long (111 > 110)
- `app/services/connections/provider.py:26` - Line too long (111 > 110)
- `app/services/connections_service.py:510` - Line too long (111 > 110)
- `app/services/connections_service.py:512` - Line too long (114 > 110)
- `app/services/connectors/__init__.py:4` - Line too long (178 > 110)
- `app/services/connectors/accounting/fake.py:79` - Line too long (117 > 110)
- `app/services/connectors/accounting/manual.py:33` - Line too long (114 > 110)
- `app/services/connectors/accounting/port.py:6` - Line too long (111 > 110)
- `app/services/connectors/accounting/port.py:7` - Line too long (115 > 110)
- `app/services/connectors/base.py:3` - Line too long (115 > 110)
- `app/services/connectors/esign/builtin.py:3` - Line too long (111 > 110)
- `app/services/connectors/esign/port.py:7` - Line too long (114 > 110)
- `app/services/connectors/hrms/__init__.py:1` - Line too long (115 > 110)
- `app/services/connectors/hrms/fake.py:94` - Line too long (112 > 110)
- `app/services/connectors/hrms/hooks.py:1` - Line too long (121 > 110)
- `app/services/connectors/hrms/hooks.py:30` - Line too long (113 > 110)
- `app/services/connectors/hrms/manual.py:1` - Line too long (118 > 110)
- `app/services/connectors/hrms/manual.py:47` - Line too long (112 > 110)
- `app/services/connectors/hrms/manual.py:51` - Line too long (124 > 110)
- `app/services/connectors/hrms/port.py:1` - Line too long (117 > 110)
- `app/services/connectors/hrms/port.py:2` - Line too long (116 > 110)
- `app/services/connectors/job_board/__init__.py:1` - Line too long (120 > 110)
- `app/services/connectors/job_board/__init__.py:2` - Line too long (113 > 110)
- `app/services/connectors/job_board/fake.py:1` - Line too long (118 > 110)
- `app/services/connectors/job_board/fake.py:3` - Line too long (112 > 110)
- `app/services/connectors/job_board/fake.py:33` - Line too long (123 > 110)
- `app/services/connectors/job_board/hooks.py:1` - Line too long (117 > 110)
- `app/services/connectors/job_board/hooks.py:15` - Line too long (114 > 110)
- `app/services/connectors/job_board/hooks.py:22` - Line too long (119 > 110)
- `app/services/connectors/job_board/manual.py:2` - Line too long (119 > 110)
- `app/services/connectors/job_board/manual.py:85` - Line too long (113 > 110)
- `app/services/connectors/jobs.py:60` - Line too long (134 > 110)
- `app/services/connectors/jobs.py:98` - Line too long (136 > 110)
- `app/services/connectors/jobs.py:104` - Line too long (122 > 110)
- `app/services/connectors/jobs.py:108` - Line too long (112 > 110)
- `app/services/connectors/jobs.py:155` - Line too long (117 > 110)
- `app/services/connectors/jobs.py:158` - Line too long (112 > 110)
- `app/services/connectors/jobs.py:160` - Line too long (117 > 110)
- `app/services/connectors/jobs.py:163` - Line too long (113 > 110)
- `app/services/connectors/jobs.py:228` - Line too long (117 > 110)
- `app/services/connectors/payroll/fake.py:62` - Line too long (120 > 110)
- `app/services/connectors/payroll/port.py:1` - Line too long (137 > 110)
- `app/services/connectors/payroll/port.py:21` - Line too long (113 > 110)
- `app/services/connectors/registry.py:3` - Line too long (111 > 110)
- `app/services/connectors/registry.py:162` - Line too long (113 > 110)
- `app/services/connectors/registry.py:163` - Line too long (114 > 110)
- `app/services/connectors/registry.py:200` - Line too long (112 > 110)
- `app/services/connectors/registry.py:216` - Line too long (111 > 110)
- `app/services/connectors/registry.py:218` - Line too long (113 > 110)
- `app/services/connectors/registry.py:231` - Line too long (119 > 110)
- `app/services/connectors/registry.py:259` - Line too long (118 > 110)
- `app/services/connectors/registry.py:260` - Line too long (112 > 110)
- `app/services/connectors/registry.py:263` - Line too long (121 > 110)
- `app/services/connectors/registry.py:290` - Line too long (115 > 110)
- `app/services/connectors/service.py:72` - Line too long (122 > 110)
- `app/services/connectors/service.py:103` - Line too long (119 > 110)
- `app/services/connectors/service.py:120` - Line too long (122 > 110)
- `app/services/connectors/service.py:142` - Line too long (119 > 110)
- `app/services/connectors/service.py:151` - Line too long (118 > 110)
- `app/services/connectors/service.py:162` - Line too long (112 > 110)
- `app/services/connectors/service.py:207` - Line too long (115 > 110)
- `app/services/connectors/service.py:250` - Line too long (113 > 110)
- `app/services/connectors/service.py:312` - Line too long (114 > 110)
- `app/services/connectors/service.py:318` - Line too long (114 > 110)
- `app/services/connectors/stt/__init__.py:1` - Line too long (114 > 110)
- `app/services/connectors/stt/fake.py:1` - Line too long (112 > 110)
- `app/services/connectors/stt/fake.py:34` - Line too long (121 > 110)
- `app/services/connectors/stt/hooks.py:1` - Line too long (118 > 110)
- `app/services/connectors/stt/manual.py:1` - Line too long (116 > 110)
- `app/services/connectors/stt/port.py:1` - Line too long (120 > 110)
- `app/services/connectors/stt/port.py:2` - Line too long (115 > 110)
- `app/services/crm/duplicates.py:48` - Line too long (116 > 110)
- `app/services/demands/common.py:35` - Line too long (127 > 110)
- `app/services/demands/common.py:72` - Line too long (115 > 110)
- `app/services/demands/jd.py:164` - Line too long (115 > 110)
- `app/services/demands/portal.py:6` - Line too long (111 > 110)
- `app/services/demands/records.py:323` - Line too long (113 > 110)
- `app/services/demands/records.py:384` - Line too long (114 > 110)
- `app/services/demands/records.py:396` - Line too long (112 > 110)
- `app/services/demands/records.py:405` - Line too long (111 > 110)
- `app/services/demands/transitions.py:11` - Line too long (121 > 110)
- `app/services/demands_service.py:3` - Line too long (112 > 110)
- `app/services/documents/certificate.py:4` - Line too long (111 > 110)
- `app/services/documents/esign.py:5` - Line too long (113 > 110)
- `app/services/documents/esign.py:7` - Line too long (112 > 110)
- `app/services/documents/esign.py:8` - Line too long (111 > 110)
- `app/services/documents/esign.py:659` - Line too long (112 > 110)
- `app/services/documents/esign.py:924` - Line too long (113 > 110)
- `app/services/documents/merge.py:4` - Line too long (112 > 110)
- `app/services/documents/merge.py:199` - Line too long (116 > 110)
- `app/services/documents/render.py:4` - Line too long (113 > 110)
- `app/services/documents/repository.py:3` - Line too long (112 > 110)
- `app/services/documents/service.py:86` - Line too long (111 > 110)
- `app/services/documents/templates.py:216` - Line too long (112 > 110)
- `app/services/documents_erasure.py:4` - Line too long (112 > 110)
- `app/services/documents_erasure.py:9` - Line too long (111 > 110)
- `app/services/external_auth/access.py:334` - Line too long (111 > 110)
- `app/services/external_auth/access.py:353` - Line too long (113 > 110)
- `app/services/external_auth/access.py:374` - Line too long (114 > 110)
- `app/services/external_auth/access.py:380` - Line too long (111 > 110)
- `app/services/external_auth/signin.py:15` - Line too long (114 > 110)
- `app/services/external_auth/signin.py:268` - Line too long (114 > 110)
- `app/services/external_auth/signin.py:271` - Line too long (115 > 110)
- `app/services/external_auth/signin.py:283` - Line too long (111 > 110)
- `app/services/external_auth/signin.py:284` - Line too long (111 > 110)
- `app/services/files_erasure.py:46` - Line too long (122 > 110)
- `app/services/files_erasure.py:53` - Line too long (114 > 110)
- `app/services/files_erasure.py:83` - Line too long (120 > 110)
- `app/services/files_erasure.py:91` - Line too long (114 > 110)
- `app/services/files_erasure.py:93` - Line too long (124 > 110)
- `app/services/files_erasure.py:94` - Line too long (112 > 110)
- `app/services/files_erasure.py:112` - Line too long (121 > 110)
- `app/services/files_erasure.py:118` - Line too long (120 > 110)
- `app/services/find_workspace_service.py:45` - Line too long (112 > 110)
- `app/services/golive/owners.py:43` - Line too long (112 > 110)
- `app/services/golive/owners.py:44` - Line too long (116 > 110)
- `app/services/golive/owners.py:77` - Line too long (111 > 110)
- `app/services/golive/service.py:342` - Line too long (112 > 110)
- `app/services/golive/service.py:412` - Line too long (111 > 110)
- `app/services/golive/service.py:433` - Line too long (116 > 110)
- `app/services/identity_bridge.py:106` - Line too long (111 > 110)
- `app/services/imports_service.py:1` - Line too long (112 > 110)
- `app/services/imports_service.py:103` - Line too long (124 > 110)
- `app/services/imports_service.py:116` - Line too long (116 > 110)
- `app/services/imports_service.py:131` - Line too long (128 > 110)
- `app/services/imports_service.py:135` - Line too long (130 > 110)
- `app/services/imports_service.py:147` - Line too long (123 > 110)
- `app/services/interviews/common.py:63` - Line too long (113 > 110)
- `app/services/interviews/invites.py:10` - Line too long (114 > 110)
- `app/services/interviews/invites.py:14` - Line too long (113 > 110)
- `app/services/interviews/invites.py:15` - Line too long (119 > 110)
- `app/services/interviews/invites.py:18` - Line too long (115 > 110)
- `app/services/interviews/invites.py:55` - Line too long (113 > 110)
- `app/services/interviews/invites.py:56` - Line too long (111 > 110)
- `app/services/interviews/invites.py:175` - Line too long (128 > 110)
- `app/services/interviews/invites.py:189` - Line too long (130 > 110)
- `app/services/interviews/invites.py:202` - Line too long (124 > 110)
- `app/services/interviews/invites.py:218` - Line too long (112 > 110)
- `app/services/interviews/invites.py:390` - Line too long (128 > 110)
- `app/services/interviews/invites.py:597` - Line too long (112 > 110)
- `app/services/interviews/invites.py:610` - Line too long (112 > 110)
- `app/services/interviews/invites.py:681` - Line too long (111 > 110)
- `app/services/interviews/invites.py:715` - Line too long (139 > 110)
- `app/services/interviews/outcomes.py:6` - Line too long (111 > 110)
- `app/services/interviews/plan.py:30` - Line too long (114 > 110)
- `app/services/interviews/portal.py:3` - Line too long (114 > 110)
- `app/services/interviews/scheduling.py:4` - Line too long (111 > 110)
- `app/services/interviews/scheduling.py:93` - Line too long (111 > 110)
- `app/services/interviews/scheduling.py:739` - Line too long (112 > 110)
- `app/services/interviews/timeutil.py:93` - Line too long (111 > 110)
- `app/services/interviews_service.py:16` - Line too long (111 > 110)
- `app/services/legal_entities/__init__.py:2` - Line too long (112 > 110)
- `app/services/legal_entities/fx.py:4` - Line too long (112 > 110)
- `app/services/legal_entities/fx.py:51` - Line too long (113 > 110)
- `app/services/legal_entities/fx.py:130` - Line too long (112 > 110)
- `app/services/legal_entities/fx.py:131` - Line too long (113 > 110)
- `app/services/legal_entities/gstin.py:33` - Line too long (113 > 110)
- `app/services/legal_entities/gstin.py:36` - Line too long (111 > 110)
- `app/services/legal_entities/numbering.py:9` - Line too long (112 > 110)
- `app/services/legal_entities/numbering.py:75` - Line too long (112 > 110)
- `app/services/legal_entities/numbering.py:81` - Line too long (115 > 110)
- `app/services/legal_entities/numbering.py:139` - Line too long (113 > 110)
- `app/services/legal_entities/numbering.py:150` - Line too long (116 > 110)
- `app/services/legal_entities/periods.py:3` - Line too long (111 > 110)
- `app/services/legal_entities/periods.py:4` - Line too long (111 > 110)
- `app/services/legal_entities/periods.py:6` - Line too long (114 > 110)
- `app/services/legal_entities/periods.py:67` - Line too long (111 > 110)
- `app/services/legal_entities/periods.py:78` - Line too long (111 > 110)
- `app/services/legal_entities/periods.py:87` - Line too long (117 > 110)
- `app/services/legal_entities/periods.py:136` - Line too long (118 > 110)
- `app/services/legal_entities/periods.py:140` - Line too long (121 > 110)
- `app/services/legal_entities/periods.py:161` - Line too long (112 > 110)
- `app/services/legal_entities/periods.py:167` - Line too long (112 > 110)
- `app/services/legal_entities/periods.py:212` - Line too long (111 > 110)
- `app/services/legal_entities/service.py:920` - Line too long (115 > 110)
- `app/services/mailbox_service.py:4` - Line too long (111 > 110)
- `app/services/matching/runs.py:119` - Line too long (111 > 110)
- `app/services/matching/service.py:322` - Line too long (112 > 110)
- `app/services/module_rbac.py:132` - Line too long (116 > 110)
- `app/services/module_rbac.py:140` - Line too long (111 > 110)
- `app/services/notifications_service.py:130` - Line too long (115 > 110)
- `app/services/notifications_service.py:442` - Line too long (118 > 110)
- `app/services/notifications_service.py:459` - Line too long (141 > 110)
- `app/services/notifications_service.py:468` - Line too long (141 > 110)
- `app/services/notifications_service.py:555` - Line too long (120 > 110)
- `app/services/notifications_service.py:710` - Line too long (117 > 110)
- `app/services/notifications_service.py:807` - Line too long (117 > 110)
- `app/services/notifications_service.py:859` - Line too long (121 > 110)
- `app/services/notifications_service.py:930` - Line too long (113 > 110)
- `app/services/notifications_service.py:1223` - Line too long (113 > 110)
- `app/services/offers/__init__.py:93` - Line too long (112 > 110)
- `app/services/offers/common.py:24` - Line too long (121 > 110)
- `app/services/offers/offers.py:1` - Line too long (113 > 110)
- `app/services/offers/response.py:9` - Line too long (112 > 110)
- `app/services/offers/response.py:10` - Line too long (112 > 110)
- `app/services/offers/response.py:341` - Line too long (112 > 110)
- `app/services/offers/status.py:4` - Line too long (111 > 110)
- `app/services/offers/status.py:78` - Line too long (114 > 110)
- `app/services/offers/status.py:153` - Line too long (111 > 110)
- `app/services/offers/templates.py:3` - Line too long (111 > 110)
- `app/services/offers/templates.py:245` - Line too long (111 > 110)
- `app/services/payroll/earnings.py:38` - Line too long (113 > 110)
- `app/services/payroll/exports.py:875` - Line too long (115 > 110)
- `app/services/pipeline/applications.py:77` - Line too long (114 > 110)
- `app/services/pipeline/applications.py:525` - Line too long (116 > 110)
- `app/services/pipeline/applications.py:532` - Line too long (111 > 110)
- `app/services/pipeline/common.py:22` - Line too long (121 > 110)
- `app/services/pipeline/common.py:154` - Line too long (111 > 110)
- `app/services/pipeline/common.py:182` - Line too long (111 > 110)
- `app/services/pipeline/common.py:202` - Line too long (111 > 110)
- `app/services/pipeline/portal.py:7` - Line too long (112 > 110)
- `app/services/pipeline/portal.py:9` - Line too long (111 > 110)
- `app/services/pipeline/portal.py:10` - Line too long (111 > 110)
- `app/services/placement_exit_erasure.py:4` - Line too long (112 > 110)
- `app/services/placements/__init__.py:3` - Line too long (111 > 110)
- `app/services/placements/creation.py:3` - Line too long (113 > 110)
- `app/services/placements/creation.py:42` - Line too long (114 > 110)
- `app/services/placements/events.py:1` - Line too long (112 > 110)
- `app/services/placements/gates.py:3` - Line too long (111 > 110)
- `app/services/placements/gates.py:5` - Line too long (115 > 110)
- `app/services/placements/gates.py:8` - Line too long (114 > 110)
- `app/services/placements/gates.py:9` - Line too long (116 > 110)
- `app/services/placements/gates.py:60` - Line too long (111 > 110)
- `app/services/placements/gates.py:367` - Line too long (113 > 110)
- `app/services/placements/hooks.py:4` - Line too long (111 > 110)
- `app/services/placements/hooks.py:8` - Line too long (115 > 110)
- `app/services/placements/hooks.py:11` - Line too long (116 > 110)
- `app/services/placements/hooks.py:13` - Line too long (111 > 110)
- `app/services/placements/hooks.py:216` - Line too long (126 > 110)
- `app/services/placements/hooks.py:233` - Line too long (119 > 110)
- `app/services/placements/hooks.py:251` - Line too long (119 > 110)
- `app/services/placements/hooks.py:469` - Line too long (112 > 110)
- `app/services/placements/joining.py:4` - Line too long (115 > 110)
- `app/services/placements/joining.py:7` - Line too long (113 > 110)
- `app/services/placements/lists.py:1` - Line too long (115 > 110)
- `app/services/placements/margin.py:3` - Line too long (114 > 110)
- `app/services/placements/margin.py:5` - Line too long (112 > 110)
- `app/services/placements/merge_source.py:4` - Line too long (115 > 110)
- `app/services/placements/portal.py:1` - Line too long (111 > 110)
- `app/services/placements/portal.py:195` - Line too long (111 > 110)
- `app/services/placements/portal.py:196` - Line too long (117 > 110)
- `app/services/placements/portal.py:220` - Line too long (120 > 110)
- `app/services/placements/portal.py:225` - Line too long (118 > 110)
- `app/services/placements/portal.py:249` - Line too long (124 > 110)
- `app/services/placements/portal.py:250` - Line too long (115 > 110)
- `app/services/placements/portal.py:257` - Line too long (125 > 110)
- `app/services/placements/portal.py:277` - Line too long (112 > 110)
- `app/services/placements/prejoining.py:5` - Line too long (116 > 110)
- `app/services/placements/prejoining.py:8` - Line too long (112 > 110)
- `app/services/placements/rate_segments.py:3` - Line too long (115 > 110)
- `app/services/placements/rate_segments.py:7` - Line too long (111 > 110)
- `app/services/placements/rate_segments.py:9` - Line too long (114 > 110)
- `app/services/placements/rate_segments.py:98` - Line too long (117 > 110)
- `app/services/placements/registry.py:61` - Line too long (113 > 110)
- `app/services/placements/service.py:3` - Line too long (116 > 110)
- `app/services/placements/service.py:203` - Line too long (111 > 110)
- `app/services/placements/service.py:204` - Line too long (114 > 110)
- `app/services/placements/service.py:226` - Line too long (114 > 110)
- `app/services/placements/service.py:236` - Line too long (121 > 110)
- `app/services/placements/settings.py:1` - Line too long (116 > 110)
- `app/services/placements/status.py:1` - Line too long (115 > 110)
- `app/services/placements/workers.py:1` - Line too long (113 > 110)
- `app/services/placements/workers.py:4` - Line too long (112 > 110)
- `app/services/placements/workers.py:6` - Line too long (113 > 110)
- `app/services/placements/workers.py:7` - Line too long (111 > 110)
- `app/services/placements_erasure.py:6` - Line too long (113 > 110)
- `app/services/placements_erasure.py:11` - Line too long (113 > 110)
- `app/services/placements_service.py:1` - Line too long (118 > 110)
- `app/services/platform_service.py:269` - Line too long (114 > 110)
- `app/services/platform_service.py:473` - Line too long (115 > 110)
- `app/services/platform_service.py:479` - Line too long (120 > 110)
- `app/services/portal/__init__.py:9` - Line too long (111 > 110)
- `app/services/portal/__init__.py:10` - Line too long (113 > 110)
- `app/services/portal/apply.py:56` - Line too long (116 > 110)
- `app/services/portal/apply.py:76` - Line too long (114 > 110)
- `app/services/portal/apply.py:505` - Line too long (126 > 110)
- `app/services/portal/candidate_auth.py:125` - Line too long (116 > 110)
- `app/services/portal/candidate_auth.py:195` - Line too long (118 > 110)
- `app/services/portal/candidate_auth.py:196` - Line too long (115 > 110)
- `app/services/portal/careers.py:47` - Line too long (125 > 110)
- `app/services/portal/common.py:63` - Line too long (111 > 110)
- `app/services/portal/handlers.py:1` - Line too long (113 > 110)
- `app/services/portal/privacy_self_service.py:371` - Line too long (120 > 110)
- `app/services/portal/privacy_self_service.py:372` - Line too long (117 > 110)
- `app/services/portal/privacy_self_service.py:373` - Line too long (124 > 110)
- `app/services/portal/privacy_self_service.py:509` - Line too long (121 > 110)
- `app/services/portal/projection.py:50` - Line too long (111 > 110)
- `app/services/portal/self_service.py:1` - Line too long (111 > 110)
- `app/services/portal_notifications_service.py:186` - Line too long (117 > 110)
- `app/services/postings.py:8` - Line too long (112 > 110)
- `app/services/postings.py:9` - Line too long (112 > 110)
- `app/services/postings.py:14` - Line too long (112 > 110)
- `app/services/postings.py:16` - Line too long (111 > 110)
- `app/services/postings.py:200` - Line too long (116 > 110)
- `app/services/postings.py:218` - Line too long (134 > 110)
- `app/services/postings.py:266` - Line too long (122 > 110)
- `app/services/postings.py:278` - Line too long (111 > 110)
- `app/services/postings.py:331` - Line too long (116 > 110)
- `app/services/postings.py:341` - Line too long (116 > 110)
- `app/services/postings.py:358` - Line too long (113 > 110)
- `app/services/postings.py:389` - Line too long (111 > 110)
- `app/services/privacy/class_expiry.py:3` - Line too long (113 > 110)
- `app/services/privacy/class_expiry.py:4` - Line too long (111 > 110)
- `app/services/privacy/class_expiry.py:5` - Line too long (111 > 110)
- `app/services/privacy/class_expiry.py:8` - Line too long (112 > 110)
- `app/services/privacy/common.py:42` - Line too long (117 > 110)
- `app/services/privacy/consent.py:47` - Line too long (117 > 110)
- `app/services/privacy/consent.py:66` - Line too long (117 > 110)
- `app/services/privacy/consent.py:70` - Line too long (114 > 110)
- `app/services/privacy/consent.py:79` - Line too long (115 > 110)
- `app/services/privacy/consent.py:84` - Line too long (114 > 110)
- `app/services/privacy/consent.py:89` - Line too long (114 > 110)
- `app/services/privacy/consent.py:95` - Line too long (113 > 110)
- `app/services/privacy/consent.py:98` - Line too long (123 > 110)
- `app/services/privacy/consent.py:172` - Line too long (118 > 110)
- `app/services/privacy/consent.py:267` - Line too long (112 > 110)
- `app/services/privacy/consent.py:333` - Line too long (115 > 110)
- `app/services/privacy/consent.py:456` - Line too long (111 > 110)
- `app/services/privacy/consent.py:466` - Line too long (111 > 110)
- `app/services/privacy/consent.py:467` - Line too long (114 > 110)
- `app/services/privacy/consent.py:480` - Line too long (115 > 110)
- `app/services/privacy/consent.py:517` - Line too long (114 > 110)
- `app/services/privacy/consent.py:593` - Line too long (118 > 110)
- `app/services/privacy/consent.py:601` - Line too long (112 > 110)
- `app/services/privacy/consent.py:629` - Line too long (113 > 110)
- `app/services/privacy/consent.py:653` - Line too long (114 > 110)
- `app/services/privacy/consent.py:662` - Line too long (113 > 110)
- `app/services/privacy/consent.py:674` - Line too long (123 > 110)
- `app/services/privacy/consent.py:679` - Line too long (117 > 110)
- `app/services/privacy/consent.py:682` - Line too long (112 > 110)
- `app/services/privacy/dsr.py:115` - Line too long (114 > 110)
- `app/services/privacy/dsr.py:125` - Line too long (129 > 110)
- `app/services/privacy/dsr.py:173` - Line too long (116 > 110)
- `app/services/privacy/dsr.py:177` - Line too long (119 > 110)
- `app/services/privacy/dsr.py:182` - Line too long (115 > 110)
- `app/services/privacy/dsr.py:186` - Line too long (113 > 110)
- `app/services/privacy/dsr.py:196` - Line too long (113 > 110)
- `app/services/privacy/dsr.py:209` - Line too long (112 > 110)
- `app/services/privacy/dsr.py:281` - Line too long (123 > 110)
- `app/services/privacy/dsr.py:297` - Line too long (116 > 110)
- `app/services/privacy/dsr.py:313` - Line too long (112 > 110)
- `app/services/privacy/dsr.py:329` - Line too long (112 > 110)
- `app/services/privacy/dsr.py:339` - Line too long (112 > 110)
- `app/services/privacy/dsr.py:340` - Line too long (113 > 110)
- `app/services/privacy/dsr.py:348` - Line too long (118 > 110)
- `app/services/privacy/dsr.py:368` - Line too long (118 > 110)
- `app/services/privacy/dsr.py:377` - Line too long (114 > 110)
- `app/services/privacy/dsr.py:380` - Line too long (113 > 110)
- `app/services/privacy/erasure.py:188` - Line too long (116 > 110)
- `app/services/privacy/erasure.py:239` - Line too long (121 > 110)
- `app/services/privacy/erasure.py:267` - Line too long (119 > 110)
- `app/services/privacy/export.py:50` - Line too long (115 > 110)
- `app/services/privacy/export.py:99` - Line too long (114 > 110)
- `app/services/privacy/handlers.py:58` - Line too long (117 > 110)
- `app/services/privacy/registry.py:4` - Line too long (111 > 110)
- `app/services/privacy/registry.py:18` - Line too long (112 > 110)
- `app/services/privacy/registry.py:136` - Line too long (111 > 110)
- `app/services/privacy/retention.py:10` - Line too long (111 > 110)
- `app/services/privacy/retention.py:63` - Line too long (114 > 110)
- `app/services/privacy/retention.py:113` - Line too long (128 > 110)
- `app/services/privacy/retention.py:140` - Line too long (117 > 110)
- `app/services/privacy/retention.py:148` - Line too long (126 > 110)
- `app/services/privacy/retention.py:155` - Line too long (117 > 110)
- `app/services/privacy/retention.py:157` - Line too long (117 > 110)
- `app/services/privacy/retention.py:184` - Line too long (119 > 110)
- `app/services/privacy/retention.py:215` - Line too long (127 > 110)
- `app/services/privacy/retention.py:271` - Line too long (123 > 110)
- `app/services/privacy/retention.py:302` - Line too long (119 > 110)
- `app/services/privacy/retention_bindings.py:3` - Line too long (111 > 110)
- `app/services/privacy/retention_bindings.py:9` - Line too long (112 > 110)
- `app/services/privacy/retention_bindings.py:10` - Line too long (112 > 110)
- `app/services/privacy/retention_bindings.py:11` - Line too long (114 > 110)
- `app/services/privacy/retention_bindings.py:13` - Line too long (111 > 110)
- `app/services/privacy/retention_bindings.py:18` - Line too long (133 > 110)
- `app/services/privacy/retention_classes.py:30` - Line too long (112 > 110)
- `app/services/privacy/retention_classes.py:286` - Line too long (111 > 110)
- `app/services/privacy/retention_classes.py:287` - Line too long (112 > 110)
- `app/services/privacy/retention_overrides.py:1` - Line too long (112 > 110)
- `app/services/privacy/retention_overrides.py:48` - Line too long (111 > 110)
- `app/services/privacy_erasure.py:3` - Line too long (112 > 110)
- `app/services/privacy_erasure.py:4` - Line too long (111 > 110)
- `app/services/privacy_erasure.py:6` - Line too long (111 > 110)
- `app/services/privacy_erasure.py:10` - Line too long (114 > 110)
- `app/services/privacy_erasure.py:69` - Line too long (120 > 110)
- `app/services/privacy_erasure.py:94` - Line too long (112 > 110)
- `app/services/privacy_erasure.py:104` - Line too long (114 > 110)
- `app/services/privacy_erasure.py:107` - Line too long (114 > 110)
- `app/services/privacy_erasure.py:129` - Line too long (115 > 110)
- `app/services/public_links_service.py:27` - Line too long (116 > 110)
- `app/services/public_links_service.py:28` - Line too long (117 > 110)
- `app/services/public_links_service.py:31` - Line too long (128 > 110)
- `app/services/public_links_service.py:32` - Line too long (121 > 110)
- `app/services/public_links_service.py:33` - Line too long (117 > 110)
- `app/services/public_links_service.py:34` - Line too long (111 > 110)
- `app/services/public_links_service.py:36` - Line too long (119 > 110)
- `app/services/public_links_service.py:38` - Line too long (116 > 110)
- `app/services/public_links_service.py:39` - Line too long (122 > 110)
- `app/services/public_links_service.py:40` - Line too long (116 > 110)
- `app/services/public_links_service.py:93` - Line too long (128 > 110)
- `app/services/public_links_service.py:110` - Line too long (112 > 110)
- `app/services/public_links_service.py:208` - Line too long (113 > 110)
- `app/services/public_links_service.py:254` - Line too long (113 > 110)
- `app/services/public_links_service.py:303` - Line too long (113 > 110)
- `app/services/public_links_service.py:345` - Line too long (119 > 110)
- `app/services/public_links_service.py:369` - Line too long (111 > 110)
- `app/services/public_links_service.py:383` - Line too long (118 > 110)
- `app/services/public_links_service.py:385` - Line too long (111 > 110)
- `app/services/public_links_service.py:386` - Line too long (115 > 110)
- `app/services/rbac_service.py:801` - Line too long (113 > 110)
- `app/services/sandbox/__init__.py:1` - Line too long (116 > 110)
- `app/services/sandbox/__init__.py:2` - Line too long (113 > 110)
- `app/services/sandbox/__init__.py:3` - Line too long (118 > 110)
- `app/services/sandbox/__init__.py:4` - Line too long (119 > 110)
- `app/services/sandbox/seed.py:3` - Line too long (117 > 110)
- `app/services/sandbox/seed.py:4` - Line too long (121 > 110)
- `app/services/sandbox/seed.py:6` - Line too long (121 > 110)
- `app/services/sandbox/seed.py:13` - Line too long (114 > 110)
- `app/services/sandbox/seed.py:14` - Line too long (117 > 110)
- `app/services/sandbox/seed.py:17` - Line too long (115 > 110)
- `app/services/sandbox/seed.py:18` - Line too long (120 > 110)
- `app/services/sandbox/seed.py:19` - Line too long (118 > 110)
- `app/services/sandbox/seed.py:20` - Line too long (113 > 110)
- `app/services/sandbox/seed.py:305` - Line too long (116 > 110)
- `app/services/sandbox/seed.py:671` - Line too long (119 > 110)
- `app/services/sandbox/tenants.py:3` - Line too long (115 > 110)
- `app/services/sandbox/tenants.py:4` - Line too long (113 > 110)
- `app/services/sandbox/tenants.py:5` - Line too long (121 > 110)
- `app/services/sandbox/tenants.py:48` - Line too long (114 > 110)
- `app/services/sandbox/tenants.py:50` - Line too long (133 > 110)
- `app/services/sandbox/tenants.py:69` - Line too long (119 > 110)
- `app/services/sandbox/tenants.py:129` - Line too long (115 > 110)
- `app/services/sandbox_service.py:3` - Line too long (118 > 110)
- `app/services/sandbox_service.py:4` - Line too long (119 > 110)
- `app/services/sandbox_service.py:5` - Line too long (118 > 110)
- `app/services/sandbox_service.py:6` - Line too long (121 > 110)
- `app/services/sandbox_service.py:8` - Line too long (114 > 110)
- `app/services/sandbox_service.py:45` - Line too long (120 > 110)
- `app/services/settings_service.py:328` - Line too long (119 > 110)
- `app/services/settings_service.py:333` - Line too long (114 > 110)
- `app/services/settings_service.py:336` - Line too long (116 > 110)
- `app/services/sla/__init__.py:1` - Line too long (111 > 110)
- `app/services/sla/service.py:4` - Line too long (112 > 110)
- `app/services/sla/service.py:182` - Line too long (124 > 110)
- `app/services/sla/service.py:206` - Line too long (124 > 110)
- `app/services/start_gates_erasure.py:5` - Line too long (111 > 110)
- `app/services/support_access_service.py:151` - Line too long (120 > 110)
- `app/services/tasks_service.py:42` - Line too long (115 > 110)
- `app/services/tasks_service.py:48` - Line too long (113 > 110)
- `app/services/tasks_service.py:266` - Line too long (111 > 110)
- `app/services/tasks_service.py:301` - Line too long (114 > 110)
- `app/services/tasks_service.py:370` - Line too long (121 > 110)
- `app/services/tenant_lifecycle_service.py:223` - Line too long (112 > 110)
- `app/services/tenant_lifecycle_service.py:281` - Line too long (129 > 110)
- `app/services/tenant_lifecycle_service.py:425` - Line too long (127 > 110)
- `app/services/tenant_lifecycle_service.py:426` - Line too long (124 > 110)
- `app/services/workflow/actions.py:633` - Line too long (111 > 110)
- `app/services/workflow/engine.py:275` - Line too long (116 > 110)
- `app/services/workflow/registry.py:14` - Line too long (113 > 110)
- `app/services/workflow/registry.py:15` - Line too long (115 > 110)
- `app/services/workflow/registry.py:49` - Line too long (116 > 110)
- `app/services/workflow_erasure.py:3` - Line too long (111 > 110)
- `app/services/workforce/__init__.py:1` - Line too long (136 > 110)
- `app/tasks/client_portal.py:3` - Line too long (117 > 110)
- `app/tasks/connectors.py:5` - Line too long (111 > 110)
- `app/tasks/connectors.py:29` - Line too long (118 > 110)
- `app/tasks/connectors.py:62` - Line too long (115 > 110)
- `app/tasks/documents.py:5` - Line too long (113 > 110)
- `app/tasks/documents.py:28` - Line too long (112 > 110)
- `app/tasks/interviews.py:1` - Line too long (111 > 110)
- `app/tasks/legal_entities.py:3` - Line too long (112 > 110)
- `app/tasks/legal_entities.py:24` - Line too long (123 > 110)
- `app/tasks/maintenance.py:4` - Line too long (113 > 110)
- `app/tasks/maintenance.py:6` - Line too long (114 > 110)
- `app/tasks/maintenance.py:7` - Line too long (114 > 110)
- `app/tasks/maintenance.py:8` - Line too long (114 > 110)
- `app/tasks/maintenance.py:9` - Line too long (116 > 110)
- `app/tasks/maintenance.py:10` - Line too long (114 > 110)
- `app/tasks/maintenance.py:47` - Line too long (112 > 110)
- `app/tasks/maintenance.py:107` - Line too long (112 > 110)
- `app/tasks/placements.py:3` - Line too long (111 > 110)
- `app/tasks/platform.py:64` - Line too long (130 > 110)
- `app/tasks/platform.py:73` - Line too long (116 > 110)
- `app/tasks/platform.py:77` - Line too long (114 > 110)
- `app/tasks/platform.py:105` - Line too long (123 > 110)
- `app/tasks/privacy.py:8` - Line too long (111 > 110)
- `app/tasks/time.py:103` - Line too long (112 > 110)
- `app/tasks/time.py:106` - Line too long (113 > 110)

</details>

---

## 2. mypy - type-check errors (strict)

_Counts below are the **current** run (**818** errors in 340 files), after Phase 1 (528 `unused-ignore` deleted), Phase 2 (61 `arg-type` via the ErasureHandler fix) and Tier A (`type-arg`, `no-untyped-def`, `no-untyped-call` cleared to 0). The original pre-cleanup run was 1431._

| Count | Category | Meaning |
|---:|---|---|
| 258 | `arg-type` | argument type mismatch |
| 139 | `attr-defined` | attribute not defined on type |
| 84 | `valid-type` | invalid type annotation |
| 84 | `no-any-return` | returning Any from a typed function |
| 65 | `assignment` | incompatible assignment |
| 50 | `misc` | miscellaneous |
| 47 | `var-annotated` | variable needs a type annotation |
| 21 | `unreachable` | unreachable code |
| 16 | `union-attr` | attr on Optional/Union |
| 11 | `str` | str-format / str-bytes-safe |
| 10 | `operator` | bad operator operands |
| 10 | `index` | bad index type |
| 7 | `return-value` | incompatible return value |
| 5 | `import-untyped` | import of untyped module |
| 4 | `list-item` | bad list element type |
| 4 | `has-type` | cannot determine type |
| 3 | `call-overload` | no matching overload |
| 2 | `no-redef` | name redefined |
| 2 | `name-defined` | name not defined |
| 2 | `call-arg` | bad call arguments |
| 1 | `typeddict-item` | bad TypedDict item |
| 1 | `type-var` | type-var constraint |
| 1 | `func-returns-value` | using value of None-returning func |
| 1 | `dict-item` | bad dict item type |
| 1 | `comparison-overlap` | non-overlapping comparison |

**Total: 818 errors in 340 files.** `arg-type` (258) and `attr-defined` (139) are the biggest substantive buckets. Tier A cleared `no-untyped-def`/`no-untyped-call`/`type-arg` to 0. The original top bucket, `unused-ignore` (528 stale `# type: ignore`), was deleted in Phase 1 — the historical detail list is retained below for reference but **no longer applies**.

<details><summary><b>unused-ignore</b> - needless `# type: ignore` (safe to delete) (528)</summary>

- `app/core/registry.py:60` - Unused "type: ignore" comment
- `app/services/ai/vector_index.py:135` - Unused "type: ignore" comment
- `app/services/metering/partitions.py:164` - Unused "type: ignore" comment
- `app/core/money.py:137` - Unused "type: ignore" comment
- `app/core/idempotency.py:248` - Unused "type: ignore" comment
- `app/core/actors.py:96` - Unused "type: ignore" comment
- `app/services/postings.py:306` - Unused "type: ignore" comment
- `app/services/postings.py:328` - Unused "type: ignore" comment
- `app/services/messaging/opt_outs.py:140` - Unused "type: ignore" comment
- `app/services/sla/clocks.py:32` - Unused "type: ignore" comment
- `app/services/marketplace/checks/__init__.py:203` - Unused "type: ignore" comment
- `app/services/connectors/stt/azure_speech.py:116` - Unused "type: ignore" comment
- `app/services/billing/rates.py:69` - Unused "type: ignore" comment
- `app/services/assessments/items.py:238` - Unused "type: ignore" comment
- `app/services/agent_tools/portal_common/tooling.py:119` - Unused "type: ignore" comment
- `app/services/identity_bridge.py:117` - Unused "type: ignore" comment
- `app/services/identity_bridge.py:126` - Unused "type: ignore" comment
- `app/services/sso/service.py:98` - Unused "type: ignore" comment
- `app/services/sso/service.py:99` - Unused "type: ignore" comment
- `app/services/sso/service.py:103` - Unused "type: ignore" comment
- `app/services/skills/graph.py:329` - Unused "type: ignore" comment
- `app/services/skills/graph.py:332` - Unused "type: ignore" comment
- `app/services/projects/budgets.py:24` - Unused "type: ignore" comment
- `app/services/connectors/stt/fake.py:24` - Unused "type: ignore" comment
- `app/services/connectors/hrms/keka.py:96` - Unused "type: ignore" comment
- `app/services/campaigns/settings.py:139` - Unused "type: ignore" comment
- `app/services/approvals/service.py:87` - Unused "type: ignore" comment
- `app/services/approvals/service.py:306` - Unused "type: ignore" comment
- `app/services/approvals/service.py:1115` - Unused "type: ignore" comment
- `app/services/ai_governance/evals.py:87` - Unused "type: ignore" comment
- `app/schemas/screening_agent.py:102` - Unused "type: ignore" comment
- `app/core/secure_links.py:230` - Unused "type: ignore" comment
- `app/core/secure_links.py:244` - Unused "type: ignore" comment
- `app/services/tasks_service.py:478` - Unused "type: ignore" comment
- `app/services/custom_domains.py:180` - Unused "type: ignore" comment
- `app/services/portal/projection.py:408` - Unused "type: ignore" comment
- `app/services/interviews/configuration.py:77` - Unused "type: ignore" comment
- `app/services/interviews/configuration.py:78` - Unused "type: ignore" comment
- `app/services/interviews/configuration.py:80` - Unused "type: ignore" comment
- `app/services/interviews/configuration.py:213` - Unused "type: ignore" comment
- `app/services/connectors/payroll/keka.py:20` - Unused "type: ignore" comment
- `app/services/connectors/hrms/darwinbox.py:64` - Unused "type: ignore" comment
- `app/services/collection_rules/rules.py:247` - Unused "type: ignore" comment
- `app/services/workflow_erasure.py:69` - Unused "type: ignore" comment
- `app/services/vendors_erasure.py:87` - Unused "type: ignore" comment
- `app/services/time_erasure.py:59` - Unused "type: ignore" comment
- `app/services/start_gates_erasure.py:43` - Unused "type: ignore" comment
- `app/services/skills_erasure.py:29` - Unused "type: ignore" comment
- `app/services/search_erasure.py:25` - Unused "type: ignore" comment
- `app/services/projects_erasure.py:33` - Unused "type: ignore" comment
- `app/services/portal_erasure.py:88` - Unused "type: ignore" comment
- `app/services/portal_copilot_erasure.py:55` - Unused "type: ignore" comment
- `app/services/placement_exit_erasure.py:49` - Unused "type: ignore" comment
- `app/services/placement_exit_erasure.py:57` - Unused "type: ignore" comment
- `app/services/mobility_erasure.py:73` - Unused "type: ignore" comment
- `app/services/leave_erasure.py:56` - Unused "type: ignore" comment
- `app/services/leave_erasure.py:58` - Unused "type: ignore" comment
- `app/services/leave_erasure.py:59` - Unused "type: ignore" comment
- `app/services/leave_erasure.py:60` - Unused "type: ignore" comment
- `app/services/campaigns_erasure.py:54` - Unused "type: ignore" comment
- `app/services/campaigns_erasure.py:95` - Unused "type: ignore" comment
- `app/services/bench_erasure.py:31` - Unused "type: ignore" comment
- `app/services/timesheet_flags/nudges.py:77` - Unused "type: ignore" comment
- `app/services/reporting/builder/sources_p1p2.py:92` - Unused "type: ignore" comment
- `app/services/reporting/builder/sources_p1p2.py:98` - Unused "type: ignore" comment
- `app/services/public_api/engagement.py:136` - Unused "type: ignore" comment
- `app/services/privacy/retention_classes.py:223` - Unused "type: ignore" comment
- `app/services/privacy/retention_classes.py:224` - Unused "type: ignore" comment
- `app/services/oauth/principals.py:59` - Unused "type: ignore" comment
- `app/services/connectors/payroll/darwinbox.py:27` - Unused "type: ignore" comment
- `app/tasks/maintenance.py:48` - Unused "type: ignore" comment
- `app/tasks/maintenance.py:100` - Unused "type: ignore" comment
- `app/services/connections_service.py:168` - Unused "type: ignore" comment
- `app/services/connections_service.py:189` - Unused "type: ignore" comment
- `app/services/oauth/authorizations.py:95` - Unused "type: ignore" comment
- `app/services/developer_portal/accounts.py:115` - Unused "type: ignore" comment
- `app/services/developer_portal/accounts.py:380` - Unused "type: ignore" comment
- `app/services/connectors/registry.py:167` - Unused "type: ignore" comment
- `app/services/connectors/registry.py:170` - Unused "type: ignore" comment
- `app/services/commercials/rate_cards.py:636` - Unused "type: ignore" comment
- `app/services/commercials/rate_cards.py:637` - Unused "type: ignore" comment
- `app/services/commercials/rate_cards.py:679` - Unused "type: ignore" comment
- `app/services/campus/institutions.py:84` - Unused "type: ignore" comment
- `app/services/campus/institutions.py:87` - Unused "type: ignore" comment
- `app/services/campus/institutions.py:131` - Unused "type: ignore" comment
- `app/services/campus/institutions.py:132` - Unused "type: ignore" comment
- `app/tasks/webhooks.py:51` - Unused "type: ignore" comment
- `app/services/webhooks/common.py:203` - Unused "type: ignore" comment
- `app/services/webhooks/common.py:205` - Unused "type: ignore" comment
- `app/services/webhooks/log.py:33` - Unused "type: ignore" comment
- `app/services/webhooks/log.py:34` - Unused "type: ignore" comment
- `app/services/webhooks/scheduler.py:202` - Unused "type: ignore" comment
- `app/services/oauth/authorize.py:246` - Unused "type: ignore" comment
- `app/services/oauth/apps.py:66` - Unused "type: ignore" comment
- `app/services/immigration/permits.py:108` - Unused "type: ignore" comment
- `app/services/external_auth/signin.py:690` - Unused "type: ignore" comment
- `app/services/developer_portal/orgs.py:92` - Unused "type: ignore" comment
- `app/services/developer_portal/orgs.py:132` - Unused "type: ignore" comment
- `app/services/developer_portal/orgs.py:223` - Unused "type: ignore" comment
- `app/services/analytics/governance.py:282` - Unused "type: ignore" comment
- `app/services/analytics/governance.py:285` - Unused "type: ignore" comment
- `app/services/knowledge/sources.py:40` - Unused "type: ignore" comment
- `app/services/immigration/visa_types.py:286` - Unused "type: ignore" comment
- `app/services/external_auth/access.py:78` - Unused "type: ignore" comment
- `app/services/external_auth/access.py:89` - Unused "type: ignore" comment
- `app/services/external_auth/access.py:399` - Unused "type: ignore" comment
- `app/services/external_auth/access.py:413` - Unused "type: ignore" comment
- `app/services/ecosystem_governance/console.py:92` - Unused "type: ignore" comment
- `app/services/campus/drives.py:102` - Unused "type: ignore" comment
- `app/services/campus/drives.py:125` - Unused "type: ignore" comment
- `app/services/campus/drives.py:165` - Unused "type: ignore" comment
- `app/services/notifications_service.py:1016` - Unused "type: ignore" comment
- `app/services/notifications_service.py:1017` - Unused "type: ignore" comment
- `app/services/notifications_service.py:1207` - Unused "type: ignore" comment
- `app/services/search/global_search.py:158` - Unused "type: ignore" comment
- `app/services/pipeline/settings.py:31` - Unused "type: ignore" comment
- `app/services/matching/handlers.py:240` - Unused "type: ignore" comment
- `app/services/matching/handlers.py:253` - Unused "type: ignore" comment
- `app/services/interviews/ports.py:62` - Unused "type: ignore" comment
- `app/services/candidates/records.py:540` - Unused "type: ignore" comment
- `app/services/sla/escalations.py:164` - Unused "type: ignore" comment
- `app/services/sla/escalations.py:528` - Unused "type: ignore" comment
- `app/services/search/projection.py:112` - Unused "type: ignore" comment
- `app/services/search/projection.py:133` - Unused "type: ignore" comment
- `app/services/search/candidate_search.py:712` - Unused "type: ignore" comment
- `app/services/search/candidate_search.py:821` - Unused "type: ignore" comment
- `app/services/pipeline/submissions.py:148` - Unused "type: ignore" comment
- `app/services/pipeline/submissions.py:199` - Unused "type: ignore" comment
- `app/services/pipeline/submissions.py:202` - Unused "type: ignore" comment
- `app/services/pipeline/submissions.py:209` - Unused "type: ignore" comment
- `app/services/pipeline/submissions.py:224` - Unused "type: ignore" comment
- `app/services/pipeline/submissions.py:872` - Unused "type: ignore" comment
- `app/services/pipeline/applications.py:619` - Unused "type: ignore" comment
- `app/services/pipeline/applications.py:627` - Unused "type: ignore" comment
- `app/services/pipeline/applications.py:632` - Unused "type: ignore" comment
- `app/services/pipeline/applications.py:633` - Unused "type: ignore" comment
- `app/services/pipeline/applications.py:744` - Unused "type: ignore" comment
- `app/services/pipeline/applications.py:745` - Unused "type: ignore" comment
- `app/services/pipeline/applications.py:844` - Unused "type: ignore" comment
- `app/services/pipeline/applications.py:847` - Unused "type: ignore" comment
- `app/services/interviews/plan.py:91` - Unused "type: ignore" comment
- `app/services/matching/runs.py:437` - Unused "type: ignore" comment
- `app/services/matching/runs.py:510` - Unused "type: ignore" comment
- `app/services/interviews/scheduling.py:184` - Unused "type: ignore" comment
- `app/services/interviews/scheduling.py:188` - Unused "type: ignore" comment
- `app/services/interviews/scheduling.py:198` - Unused "type: ignore" comment
- `app/services/interviews/scheduling.py:206` - Unused "type: ignore" comment
- `app/services/platform_service.py:64` - Unused "type: ignore[union-attr]" comment
- `app/services/workflow/rules.py:458` - Unused "type: ignore" comment
- `app/services/privacy/consent.py:109` - Unused "type: ignore" comment
- `app/services/pipeline/screening.py:418` - Unused "type: ignore" comment
- `app/services/pipeline/screening.py:423` - Unused "type: ignore" comment
- `app/services/pipeline/screening.py:425` - Unused "type: ignore" comment
- `app/services/pipeline/screening.py:627` - Unused "type: ignore" comment
- `app/services/matching/service.py:81` - Unused "type: ignore" comment
- `app/services/matching/service.py:153` - Unused "type: ignore" comment
- `app/services/matching/service.py:157` - Unused "type: ignore" comment
- `app/services/interviews/scorecards.py:77` - Unused "type: ignore" comment
- `app/services/interviews/scorecards.py:82` - Unused "type: ignore" comment
- `app/services/interviews/scorecards.py:84` - Unused "type: ignore" comment
- `app/services/interviews/scorecards.py:228` - Unused "type: ignore" comment
- `app/services/interviews/scorecards.py:361` - Unused "type: ignore" comment
- `app/services/interviews/scorecards.py:370` - Unused "type: ignore" comment
- `app/services/privacy/dsr.py:78` - Unused "type: ignore" comment
- `app/services/privacy/dsr.py:83` - Unused "type: ignore" comment
- `app/services/privacy/dsr.py:84` - Unused "type: ignore" comment
- `app/services/privacy/dsr.py:86` - Unused "type: ignore" comment
- `app/services/interviews/outcomes.py:68` - Unused "type: ignore" comment
- `app/services/voice/common.py:194` - Unused "type: ignore" comment
- `app/services/voice/common.py:195` - Unused "type: ignore" comment
- `app/services/vendors/review.py:469` - Unused "type: ignore" comment
- `app/services/transcripts/service.py:45` - Unused "type: ignore" comment
- `app/services/transcripts/service.py:51` - Unused "type: ignore" comment
- `app/services/transcripts/service.py:63` - Unused "type: ignore" comment
- `app/services/transcripts/service.py:252` - Unused "type: ignore" comment
- `app/services/submission_packs/rules.py:81` - Unused "type: ignore" comment
- `app/services/sso/client_admin.py:205` - Unused "type: ignore" comment
- `app/services/sso/client_admin.py:206` - Unused "type: ignore" comment
- `app/services/skills/evidence.py:45` - Unused "type: ignore" comment
- `app/services/skills/evidence.py:227` - Unused "type: ignore" comment
- `app/services/skills/inference.py:236` - Unused "type: ignore" comment
- `app/services/skills/inference.py:290` - Unused "type: ignore" comment
- `app/services/skills/inference.py:292` - Unused "type: ignore" comment
- `app/services/skills/inference.py:355` - Unused "type: ignore" comment
- `app/services/screening_agent/questionnaires.py:205` - Unused "type: ignore" comment
- `app/services/offers/common.py:58` - Unused "type: ignore" comment
- `app/services/immigration/service.py:101` - Unused "type: ignore" comment
- `app/services/demand_intake/settings.py:114` - Unused "type: ignore" comment
- `app/services/demand_intake/settings.py:115` - Unused "type: ignore" comment
- `app/services/demand_insights/suggestions.py:43` - Unused "type: ignore" comment
- `app/services/demand_insights/suggestions.py:125` - Unused "type: ignore" comment
- `app/services/demand_insights/suggestions.py:202` - Unused "type: ignore" comment
- `app/services/demand_insights/jd.py:81` - Unused "type: ignore" comment
- `app/services/crm/opportunities.py:183` - Unused "type: ignore" comment
- `app/services/crm/opportunities.py:194` - Unused "type: ignore" comment
- `app/services/crm/opportunities.py:300` - Unused "type: ignore" comment
- `app/services/crm/opportunities.py:352` - Unused "type: ignore" comment
- `app/services/crm/duplicates.py:57` - Unused "type: ignore" comment
- `app/services/campus/registration.py:150` - Unused "type: ignore" comment
- `app/services/campus/registration.py:151` - Unused "type: ignore" comment
- `app/services/assessments/taking.py:251` - Unused "type: ignore" comment
- `app/services/assessments/taking.py:622` - Unused "type: ignore" comment
- `app/services/voice/service.py:23` - Unused "type: ignore" comment
- `app/services/voice/policy_admin.py:28` - Unused "type: ignore" comment
- `app/services/voice/policy_admin.py:135` - Unused "type: ignore" comment
- `app/services/voice/policy_admin.py:140` - Unused "type: ignore" comment
- `app/services/telephony/user_phones.py:81` - Unused "type: ignore" comment
- `app/services/sso/login.py:344` - Unused "type: ignore" comment
- `app/services/sso/client.py:310` - Unused "type: ignore" comment
- `app/services/skills/service.py:143` - Unused "type: ignore" comment
- `app/services/public_api/imports.py:203` - Unused "type: ignore" comment
- `app/services/public_api/imports.py:232` - Unused "type: ignore" comment
- `app/services/projects/roles.py:99` - Unused "type: ignore" comment
- `app/services/portal/privacy_self_service.py:160` - Unused "type: ignore" comment
- `app/services/portal/candidate_auth.py:171` - Unused "type: ignore" comment
- `app/services/portal/candidate_auth.py:172` - Unused "type: ignore" comment
- `app/tasks/portal.py:62` - Unused "type: ignore" comment
- `app/tasks/portal.py:71` - Unused "type: ignore" comment
- `app/services/mobility/employees.py:248` - Unused "type: ignore" comment
- `app/services/mobility/employees.py:290` - Unused "type: ignore" comment
- `app/services/mobility/employees.py:359` - Unused "type: ignore" comment
- `app/services/documents/repository.py:474` - Unused "type: ignore" comment
- `app/services/demand_intake/items.py:65` - Unused "type: ignore" comment
- `app/services/demand_intake/items.py:66` - Unused "type: ignore" comment
- `app/services/demand_intake/items.py:86` - Unused "type: ignore" comment
- `app/services/demand_intake/items.py:457` - Unused "type: ignore" comment
- `app/services/crm/won.py:122` - Unused "type: ignore" comment
- `app/services/crm/leads.py:138` - Unused "type: ignore" comment
- `app/services/connectors/service.py:36` - Unused "type: ignore" comment
- `app/services/connectors/service.py:41` - Unused "type: ignore" comment
- `app/services/campus/events.py:87` - Unused "type: ignore" comment
- `app/services/campus/events.py:90` - Unused "type: ignore" comment
- `app/services/campus/events.py:91` - Unused "type: ignore" comment
- `app/services/campus/events.py:207` - Unused "type: ignore" comment
- `app/services/campus/events.py:290` - Unused "type: ignore" comment
- `app/services/campus/events.py:292` - Unused "type: ignore" comment
- `app/services/campus/eligibility.py:131` - Unused "type: ignore" comment
- `app/services/bgv/retention.py:39` - Unused "type: ignore" comment
- `app/services/bgv/retention.py:104` - Unused "type: ignore" comment
- `app/services/bgv/retention.py:117` - Unused "type: ignore" comment
- `app/services/analytics/hooks.py:90` - Unused "type: ignore" comment
- `app/services/sandbox_service.py:63` - Unused "type: ignore" comment
- `app/services/sandbox_service.py:66` - Unused "type: ignore" comment
- `app/services/screening_agent/turns.py:97` - Unused "type: ignore" comment
- `app/services/screening_agent/turns.py:125` - Unused "type: ignore" comment
- `app/services/screening_agent/review.py:66` - Unused "type: ignore" comment
- `app/services/screening_agent/review.py:69` - Unused "type: ignore" comment
- `app/services/referrals/tracking.py:181` - Unused "type: ignore" comment
- `app/services/referrals/tracking.py:227` - Unused "type: ignore" comment
- `app/services/referrals/tracking.py:320` - Unused "type: ignore" comment
- `app/services/referrals/tracking.py:322` - Unused "type: ignore" comment
- `app/services/referrals/posting_source.py:57` - Unused "type: ignore" comment
- `app/services/legal_entities/fx_feed.py:79` - Unused "type: ignore" comment
- `app/services/ecosystem_governance/erasure_fanout.py:117` - Unused "type: ignore" comment
- `app/services/ecosystem_governance/erasure_fanout.py:121` - Unused "type: ignore" comment
- `app/services/documents/esign.py:1117` - Unused "type: ignore" comment
- `app/services/demand_insights/flags.py:79` - Unused "type: ignore" comment
- `app/services/chasing/bgv_follow_ups.py:448` - Unused "type: ignore" comment
- `app/services/assessments/invitations.py:72` - Unused "type: ignore" comment
- `app/services/assessments/invitations.py:394` - Unused "type: ignore" comment
- `app/services/assessments/invitations.py:395` - Unused "type: ignore" comment
- `app/services/assessments/invitations.py:563` - Unused "type: ignore" comment
- `app/services/assessments/invitations.py:697` - Unused "type: ignore" comment
- `app/services/agent_runtime/executor.py:237` - Unused "type: ignore" comment
- `app/services/documents_erasure.py:134` - Unused "type: ignore" comment
- `app/services/referrals/acceptance.py:410` - Unused "type: ignore" comment
- `app/services/receivables/tds_recon/runs.py:532` - Unused "type: ignore" comment
- `app/services/receivables/tds_recon/runs.py:533` - Unused "type: ignore" comment
- `app/services/receivables/tds_recon/runs.py:534` - Unused "type: ignore" comment
- `app/services/receivables/tds_recon/runs.py:535` - Unused "type: ignore" comment
- `app/services/receivables/tds_recon/runs.py:583` - Unused "type: ignore" comment
- `app/services/receivables/tds_recon/runs.py:633` - Unused "type: ignore" comment
- `app/services/messaging/sending.py:536` - Unused "type: ignore" comment
- `app/services/messaging/sending.py:594` - Unused "type: ignore" comment
- `app/services/messaging/sending.py:595` - Unused "type: ignore" comment
- `app/services/immigration/checklist.py:73` - Unused "type: ignore" comment
- `app/services/fairness/reports.py:60` - Unused "type: ignore" comment
- `app/services/documents/verify.py:67` - Unused "type: ignore" comment
- `app/services/agent_runtime/runs.py:214` - Unused "type: ignore" comment
- `app/services/agent_runtime/runs.py:238` - Unused "type: ignore" comment
- `app/services/agent_runtime/runs.py:239` - Unused "type: ignore" comment
- `app/services/agent_runtime/external.py:199` - Unused "type: ignore" comment
- `app/services/agent_runtime/external.py:349` - Unused "type: ignore" comment
- `app/services/receivables/tds_recon/exceptions.py:107` - Unused "type: ignore" comment
- `app/services/offers/versions.py:267` - Unused "type: ignore" comment
- `app/services/offers/versions.py:269` - Unused "type: ignore" comment
- `app/services/offers/versions.py:271` - Unused "type: ignore" comment
- `app/services/offers/versions.py:282` - Unused "type: ignore" comment
- `app/services/offers/versions.py:289` - Unused "type: ignore" comment
- `app/services/offers/versions.py:300` - Unused "type: ignore" comment
- `app/services/offers/response.py:91` - Unused "type: ignore" comment
- `app/services/offers/response.py:96` - Unused "type: ignore" comment
- `app/services/offers/response.py:98` - Unused "type: ignore" comment
- `app/services/offers/response.py:104` - Unused "type: ignore" comment
- `app/services/offers/response.py:112` - Unused "type: ignore" comment
- `app/services/offers/offers.py:125` - Unused "type: ignore" comment
- `app/services/offers/offers.py:131` - Unused "type: ignore" comment
- `app/services/offers/offers.py:158` - Unused "type: ignore" comment
- `app/services/mobility/board.py:263` - Unused "type: ignore" comment
- `app/services/mobility/board.py:271` - Unused "type: ignore" comment
- `app/services/marketplace/installs.py:130` - Unused "type: ignore" comment
- `app/services/marketplace/installs.py:925` - Unused "type: ignore" comment
- `app/services/vendors/records.py:90` - Unused "type: ignore" comment
- `app/services/vendors/records.py:573` - Unused "type: ignore" comment
- `app/services/sourcing_agent/contactability.py:59` - Unused "type: ignore" comment
- `app/services/receivables/tds_recon/status.py:106` - Unused "type: ignore" comment
- `app/services/receivables/tds_recon/status.py:107` - Unused "type: ignore" comment
- `app/services/projects/burn.py:302` - Unused "type: ignore" comment
- `app/services/marketplace/catalog.py:117` - Unused "type: ignore" comment
- `app/services/marketplace/catalog.py:120` - Unused "type: ignore" comment
- `app/services/marketplace/catalog.py:135` - Unused "type: ignore" comment
- `app/services/marketplace/catalog.py:136` - Unused "type: ignore" comment
- `app/services/marketplace/catalog.py:233` - Unused "type: ignore[arg-type, index]" comment
- `app/services/ecosystem_governance/search.py:82` - Unused "type: ignore" comment
- `app/services/commercials/rate_checks.py:88` - Unused "type: ignore" comment
- `app/services/commercials/contracts.py:167` - Unused "type: ignore" comment
- `app/services/commercials/contracts.py:879` - Unused "type: ignore" comment
- `app/services/commercials/billing_profiles.py:77` - Unused "type: ignore" comment
- `app/services/commercials/billing_profiles.py:402` - Unused "type: ignore" comment
- `app/services/commercials/purchase_orders.py:390` - Unused "type: ignore" comment
- `app/services/commercials/milestones.py:43` - Unused "type: ignore" comment
- `app/services/commercials/gates.py:66` - Unused "type: ignore" comment
- `app/services/campus/batches.py:83` - Unused "type: ignore" comment
- `app/services/campus/batches.py:243` - Unused "type: ignore" comment
- `app/services/campus/batches.py:255` - Unused "type: ignore" comment
- `app/services/campus/batches.py:256` - Unused "type: ignore" comment
- `app/services/campaigns/audiences.py:303` - Unused "type: ignore" comment
- `app/services/analytics/reads.py:110` - Unused "type: ignore" comment
- `app/services/analytics/reads.py:120` - Unused "type: ignore" comment
- `app/services/analytics/reads.py:122` - Unused "type: ignore" comment
- `app/services/analytics/reads.py:130` - Unused "type: ignore" comment
- `app/services/analytics/reads.py:138` - Unused "type: ignore" comment
- `app/services/analytics/reads.py:139` - Unused "type: ignore" comment
- `app/services/analytics/reads.py:186` - Unused "type: ignore" comment
- `app/services/analytics/reads.py:189` - Unused "type: ignore" comment
- `app/services/analytics/reads.py:228` - Unused "type: ignore" comment
- `app/services/analytics/reads.py:230` - Unused "type: ignore" comment
- `app/services/agent_runtime/triggers.py:41` - Unused "type: ignore" comment
- `app/services/vendors/portal.py:124` - Unused "type: ignore" comment
- `app/services/vendors/portal.py:751` - Unused "type: ignore" comment
- `app/services/submission_packs/checks.py:99` - Unused "type: ignore" comment
- `app/services/submission_packs/checks.py:252` - Unused "type: ignore" comment
- `app/services/sourcing_agent/shortlists.py:73` - Unused "type: ignore" comment
- `app/services/sourcing_agent/shortlists.py:74` - Unused "type: ignore" comment
- `app/services/sourcing_agent/shortlists.py:76` - Unused "type: ignore" comment
- `app/services/sourcing_agent/shortlists.py:77` - Unused "type: ignore" comment
- `app/services/sourcing_agent/shortlists.py:102` - Unused "type: ignore" comment
- `app/services/sourcing_agent/shortlists.py:103` - Unused "type: ignore" comment
- `app/services/sourcing_agent/shortlists.py:163` - Unused "type: ignore" comment
- `app/services/expenses/approval.py:368` - Unused "type: ignore" comment
- `app/services/expenses/approval.py:369` - Unused "type: ignore" comment
- `app/services/demand_insights/sla_risk.py:255` - Unused "type: ignore" comment
- `app/services/demand_insights/drafts.py:82` - Unused "type: ignore" comment
- `app/services/demand_insights/drafts.py:184` - Unused "type: ignore" comment
- `app/services/demand_insights/drafts.py:225` - Unused "type: ignore" comment
- `app/services/demand_insights/drafts.py:250` - Unused "type: ignore" comment
- `app/services/campus/queue.py:74` - Unused "type: ignore" comment
- `app/services/campus/queue.py:100` - Unused "type: ignore" comment
- `app/tasks/mcp.py:45` - Unused "type: ignore" comment
- `app/tasks/mcp.py:55` - Unused "type: ignore" comment
- `app/services/mcp_service.py:539` - Unused "type: ignore" comment
- `app/services/submission_packs/service.py:177` - Unused "type: ignore" comment
- `app/services/submission_packs/service.py:347` - Unused "type: ignore" comment
- `app/services/submission_packs/service.py:348` - Unused "type: ignore" comment
- `app/services/submission_packs/service.py:351` - Unused "type: ignore" comment
- `app/services/submission_packs/service.py:354` - Unused "type: ignore" comment
- `app/services/submission_packs/service.py:439` - Unused "type: ignore" comment
- `app/services/submission_packs/service.py:440` - Unused "type: ignore" comment
- `app/services/submission_packs/service.py:737` - Unused "type: ignore" comment
- `app/services/submission_packs/service.py:744` - Unused "type: ignore" comment
- `app/services/sourcing_agent/service.py:69` - Unused "type: ignore" comment
- `app/services/screening_agent/conversations.py:84` - Unused "type: ignore" comment
- `app/services/screening_agent/conversations.py:272` - Unused "type: ignore" comment
- `app/services/screening_agent/conversations.py:798` - Unused "type: ignore" comment
- `app/services/projects/deliverables.py:115` - Unused "type: ignore" comment
- `app/services/placements/service.py:37` - Unused "type: ignore" comment
- `app/services/placements/service.py:38` - Unused "type: ignore" comment
- `app/services/placements/service.py:51` - Unused "type: ignore" comment
- `app/services/placements/service.py:73` - Unused "type: ignore" comment
- `app/services/placements/service.py:122` - Unused "type: ignore" comment
- `app/services/placements/service.py:144` - Unused "type: ignore" comment
- `app/services/placements/service.py:146` - Unused "type: ignore" comment
- `app/services/placements/service.py:149` - Unused "type: ignore" comment
- `app/services/knowledge/qa.py:196` - Unused "type: ignore" comment
- `app/services/knowledge/qa.py:198` - Unused "type: ignore" comment
- `app/services/knowledge/qa.py:220` - Unused "type: ignore" comment
- `app/services/knowledge/qa.py:252` - Unused "type: ignore" comment
- `app/services/knowledge/qa.py:820` - Unused "type: ignore" comment
- `app/services/knowledge/qa.py:822` - Unused "type: ignore" comment
- `app/services/knowledge/qa.py:834` - Unused "type: ignore" comment
- `app/services/interview_captures/withdrawal.py:113` - Unused "type: ignore" comment
- `app/services/interview_captures/consent.py:369` - Unused "type: ignore" comment
- `app/services/developer_portal/sandboxes.py:62` - Unused "type: ignore" comment
- `app/services/campaign_copy/drafts.py:53` - Unused "type: ignore" comment
- `app/services/campaign_copy/drafts.py:131` - Unused "type: ignore" comment
- `app/services/screening_agent/agent_ops.py:108` - Unused "type: ignore" comment
- `app/services/screening_agent/agent_ops.py:109` - Unused "type: ignore" comment
- `app/services/portal_copilot/store.py:228` - Unused "type: ignore" comment
- `app/services/placements/extension_checks.py:89` - Unused "type: ignore" comment
- `app/services/placements/bench.py:120` - Unused "type: ignore" comment
- `app/services/placements/bench.py:192` - Unused "type: ignore" comment
- `app/services/placements/bench.py:193` - Unused "type: ignore" comment
- `app/services/placements/bench.py:210` - Unused "type: ignore" comment
- `app/services/placements/bench.py:695` - Unused "type: ignore" comment
- `app/services/placements/bench.py:696` - Unused "type: ignore" comment
- `app/services/interview_captures/uploads.py:411` - Unused "type: ignore" comment
- `app/services/campus/offers.py:139` - Unused "type: ignore" comment
- `app/services/campaign_copy/requests.py:44` - Unused "type: ignore" comment
- `app/services/campaign_copy/apply.py:125` - Unused "type: ignore" comment
- `app/services/campaign_copy/apply.py:156` - Unused "type: ignore" comment
- `app/services/billing/dsc.py:443` - Unused "type: ignore" comment
- `app/services/billing/dsc.py:447` - Unused "type: ignore" comment
- `app/services/billing/dsc.py:559` - Unused "type: ignore" comment
- `app/services/billing/dsc.py:564` - Unused "type: ignore" comment
- `app/services/agent_tools/skills/tools_e55.py:71` - Unused "type: ignore" comment
- `app/services/agent_tools/portal_vendor/exposure_e77.py:15` - Unused "type: ignore" comment
- `app/services/agent_tools/demands/tools_e54.py:84` - Unused "type: ignore" comment
- `app/router/client_portal_bgv.py:26` - Unused "type: ignore" comment
- `app/services/public_links_service.py:311` - Unused "type: ignore" comment
- `app/services/public_links_service.py:314` - Unused "type: ignore" comment
- `app/services/public_links_service.py:315` - Unused "type: ignore" comment
- `app/services/public_links_service.py:316` - Unused "type: ignore" comment
- `app/services/time/timesheets.py:756` - Unused "type: ignore" comment
- `app/services/time/timesheets.py:765` - Unused "type: ignore" comment
- `app/services/time/timesheets.py:885` - Unused "type: ignore" comment
- `app/services/time/approval.py:240` - Unused "type: ignore" comment
- `app/services/screening_agent/portal_chat.py:108` - Unused "type: ignore" comment
- `app/services/screening_agent/portal_chat.py:112` - Unused "type: ignore" comment
- `app/services/screening_agent/portal_chat.py:135` - Unused "type: ignore" comment
- `app/services/scheduling_agent/requests.py:699` - Unused "type: ignore" comment
- `app/services/projects/assignments.py:85` - Unused "type: ignore" comment
- `app/services/placements/extensions.py:517` - Unused "type: ignore" comment
- `app/services/placements/bench_reporting.py:121` - Unused "type: ignore" comment
- `app/services/placements/bench_matching.py:195` - Unused "type: ignore" comment
- `app/services/placements/bench_matching.py:211` - Unused "type: ignore" comment
- `app/services/placements/bench_matching.py:218` - Unused "type: ignore" comment
- `app/services/copilot/turns.py:335` - Unused "type: ignore" comment
- `app/services/compliance_erasure.py:55` - Unused "type: ignore" comment
- `app/services/time/imports.py:262` - Unused "type: ignore" comment
- `app/services/time/leave_types.py:82` - Unused "type: ignore" comment
- `app/services/time/leave_types.py:285` - Unused "type: ignore" comment
- `app/services/time/holidays.py:94` - Unused "type: ignore" comment
- `app/services/time/holidays.py:112` - Unused "type: ignore" comment
- `app/services/time/holidays.py:427` - Unused "type: ignore" comment
- `app/services/time/holidays.py:572` - Unused "type: ignore" comment
- `app/services/time/leave_requests.py:752` - Unused "type: ignore" comment
- `app/services/time/leave_requests.py:823` - Unused "type: ignore" comment
- `app/services/scheduling_agent/settings_api.py:154` - Unused "type: ignore" comment
- `app/services/scheduling_agent/offers.py:318` - Unused "type: ignore" comment
- `app/services/projects/projects.py:85` - Unused "type: ignore" comment
- `app/services/projects/overview.py:204` - Unused "type: ignore" comment
- `app/services/placements/offboarding.py:721` - Unused "type: ignore" comment
- `app/services/placements/offboarding.py:731` - Unused "type: ignore" comment
- `app/services/onboarding/templates.py:267` - Unused "type: ignore" comment
- `app/services/onboarding/templates.py:271` - Unused "type: ignore" comment
- `app/services/onboarding/templates.py:272` - Unused "type: ignore" comment
- `app/services/onboarding/cases.py:580` - Unused "type: ignore" comment
- `app/services/golive/p3_imports.py:148` - Unused "type: ignore" comment
- `app/services/client_portal/approvals.py:307` - Unused "type: ignore" comment
- `app/services/portal_copilot_service.py:83` - Unused "type: ignore" comment
- `app/services/portal_copilot_service.py:397` - Unused "type: ignore" comment
- `app/services/voice/sessions.py:282` - Unused "type: ignore" comment
- `app/services/voice/consent.py:250` - Unused "type: ignore" comment
- `app/services/voice/agent_ops.py:74` - Unused "type: ignore" comment
- `app/services/voice/agent_ops.py:140` - Unused "type: ignore" comment
- `app/services/time/leave_settings.py:15` - Unused "type: ignore" comment
- `app/services/mobility/transfers.py:49` - Unused "type: ignore" comment
- `app/services/mobility/transfers.py:163` - Unused "type: ignore" comment
- `app/services/mobility/transfers.py:164` - Unused "type: ignore" comment
- `app/services/mobility/transfers.py:291` - Unused "type: ignore" comment
- `app/services/mobility/applications.py:230` - Unused "type: ignore" comment
- `app/services/mobility/applications.py:304` - Unused "type: ignore" comment
- `app/services/mobility/applications.py:307` - Unused "type: ignore" comment
- `app/services/client_portal/links.py:165` - Unused "type: ignore" comment
- `app/router/developer_portal.py:100` - Unused "type: ignore" comment
- `app/router/client_portal_approvals.py:85` - Unused "type: ignore" comment
- `app/services/voice/uploads.py:96` - Unused "type: ignore" comment
- `app/services/payroll/vendor_statements.py:535` - Unused "type: ignore" comment
- `app/services/agent_tools/portal_client/exposure_e77.py:15` - Unused "type: ignore" comment
- `app/services/agent_tools/portal_candidate/tools_e77.py:239` - Unused "type: ignore" comment
- `app/services/workforce/plans.py:160` - Unused "type: ignore" comment
- `app/services/workforce/plans.py:161` - Unused "type: ignore" comment
- `app/services/workforce/lines.py:101` - Unused "type: ignore" comment
- `app/services/workforce/lines.py:103` - Unused "type: ignore" comment
- `app/services/workforce/lines.py:109` - Unused "type: ignore" comment
- `app/services/workforce/lines.py:113` - Unused "type: ignore" comment
- `app/services/telephony/calls.py:52` - Unused "type: ignore" comment
- `app/services/telephony/calls.py:54` - Unused "type: ignore" comment
- `app/services/telephony/calls.py:55` - Unused "type: ignore" comment
- `app/services/telephony/calls.py:211` - Unused "type: ignore" comment
- `app/services/telephony/calls.py:212` - Unused "type: ignore" comment
- `app/services/telephony/calls.py:233` - Unused "type: ignore" comment
- `app/services/agent_tools/scheduling/tools_e59.py:93` - Unused "type: ignore" comment
- `app/services/agent_tools/scheduling/tools_e59.py:188` - Unused "type: ignore" comment
- `app/services/agent_tools/scheduling/tools_e59.py:389` - Unused "type: ignore" comment
- `app/services/agent_tools/portal_candidate/exposure_e77.py:18` - Unused "type: ignore" comment
- `app/services/workforce/revisions.py:115` - Unused "type: ignore" comment
- `app/services/calendar_sync/settings.py:56` - Unused "type: ignore" comment
- `app/services/calendar_sync/settings.py:101` - Unused "type: ignore" comment
- `app/services/calendar_sync/settings.py:177` - Unused "type: ignore" comment
- `app/services/calendar_sync/settings.py:190` - Unused "type: ignore" comment
- `app/services/calendar_sync/settings.py:194` - Unused "type: ignore" comment
- `app/services/calendar_sync/settings.py:242` - Unused "type: ignore" comment
- `app/services/telephony_service.py:64` - Unused "type: ignore" comment
- `app/services/billing/preview.py:159` - Unused "type: ignore" comment
- `app/services/billing/hooks.py:99` - Unused "type: ignore" comment
- `app/services/receivables/aging.py:87` - Unused "type: ignore" comment
- `app/services/reporting/export.py:417` - Unused "type: ignore" comment
- `app/services/reporting/export.py:418` - Unused "type: ignore" comment
- `app/services/reporting/export.py:419` - Unused "type: ignore" comment
- `app/services/reporting/export.py:420` - Unused "type: ignore" comment
- `app/services/reporting/__init__.py:235` - Unused "type: ignore" comment
- `app/services/collection_rules/holds.py:116` - Unused "type: ignore" comment
- `app/services/collection_rules/facts.py:133` - Unused "type: ignore" comment
- `app/services/reporting/builder/runs.py:66` - Unused "type: ignore" comment
- `app/services/reporting/builder/runs.py:87` - Unused "type: ignore" comment
- `app/services/reporting/builder/runs.py:133` - Unused "type: ignore" comment
- `app/services/reporting/builder/runs.py:135` - Unused "type: ignore" comment
- `app/services/reporting/builder/runs.py:138` - Unused "type: ignore" comment
- `app/router/reporting.py:182` - Unused "type: ignore" comment
- `app/router/oauth.py:122` - Unused "type: ignore[arg-type, union-attr]" comment
- `app/router/marketplace.py:61` - Unused "type: ignore" comment
- `app/router/marketplace.py:92` - Unused "type: ignore" comment
- `app/router/ecosystem_console.py:72` - Unused "type: ignore" comment
- `app/router/demand_intake.py:76` - Unused "type: ignore" comment
- `app/router/demand_insights.py:40` - Unused "type: ignore" comment
- `app/services/analytics_answers/service.py:218` - Unused "type: ignore" comment
- `app/services/analytics_answers/service.py:377` - Unused "type: ignore" comment

</details>

<details><summary><b>arg-type</b> - argument type mismatch (319)</summary>

- `app/services/connectors/voice/bridge.py:65` - Argument 1 to "BridgedCallRequest" has incompatible type "**dict[str, Any | None]"; expected "UUID"
- `app/services/connectors/voice/bridge.py:65` - Argument 1 to "BridgedCallRequest" has incompatible type "**dict[str, Any | None]"; expected "str"
- `app/services/connectors/voice/bridge.py:65` - Argument 1 to "BridgedCallRequest" has incompatible type "**dict[str, Any | None]"; expected "bool"
- `app/services/connectors/voice/bridge.py:65` - Argument 1 to "BridgedCallRequest" has incompatible type "**dict[str, Any | None]"; expected "int"
- `app/services/assessments/scoring.py:266` - Argument 2 to "_choice" has incompatible type "Any | dict[Any, Any] | None"; expected "Mapping[str, Any]"
- `app/services/assessments/scoring.py:268` - Argument 2 to "_multi" has incompatible type "Any | dict[Any, Any] | None"; expected "Mapping[str, Any]"
- `app/services/assessments/scoring.py:270` - Argument 2 to "_numeric" has incompatible type "Any | dict[Any, Any] | None"; expected "Mapping[str, Any]"
- `app/services/assessments/scoring.py:272` - Argument 2 to "_short_text" has incompatible type "Any | dict[Any, Any] | None"; expected "Mapping[str, Any]"
- `app/services/assessments/scoring.py:274` - Argument 2 to "_code_output" has incompatible type "Any | dict[Any, Any] | None"; expected "Mapping[str, Any]"
- `app/services/assessments/scoring.py:276` - Argument 2 to "_code_fill" has incompatible type "Any | dict[Any, Any] | None"; expected "Mapping[str, Any]"
- `app/services/assessments/scoring.py:278` - Argument 2 to "_code_order" has incompatible type "Any | dict[Any, Any] | None"; expected "Mapping[str, Any]"
- `app/services/assessments/scoring.py:280` - Argument 2 to "_find_bug" has incompatible type "Any | dict[Any, Any] | None"; expected "Mapping[str, Any]"
- `app/services/webhooks_erasure.py:57` - Argument 1 to "register_erasure_handler" has incompatible type "WebhooksHandler"; expected "ErasureHandler"
- `app/services/collection_rules_erasure.py:65` - Argument 1 to "register_erasure_handler" has incompatible type "CollectionLogHandler"; expected "ErasureHandler"
- `app/schemas/webhooks.py:175` - Argument "default_factory" to "Field" has incompatible type "Callable[[], list[str]]"; expected "Callable[[], list[Literal['failed', 'held']]] | Callable[[dict[str, Any]], list[Literal['failed', 'held']]]"
- `app/schemas/oauth.py:59` - Argument "default_factory" to "Field" has incompatible type "Callable[[], list[str]]"; expected "Callable[[], list[Literal['authorization_code', 'client_credentials', 'refresh_token']]] | Callable[[dict[str, Any]], list[Literal['authorization_code', 'client_credentials', 'refresh_token']]]"
- `app/schemas/oauth.py:61` - Argument "default_factory" to "Field" has incompatible type "Callable[[], list[str]]"; expected "Callable[[], list[Literal['api', 'mcp']]] | Callable[[dict[str, Any]], list[Literal['api', 'mcp']]]"
- `app/schemas/metering.py:44` - Argument "default_factory" to "Field" has incompatible type "Callable[[], list[str]]"; expected "Callable[[], list[Literal['day', 'unit', 'installation', 'api_key', 'route_group']]] | Callable[[dict[str, Any]], list[Literal['day', 'unit', 'installation', 'api_key', 'route_group']]]"
- `app/schemas/fairness.py:164` - Argument "default_factory" to "Field" has incompatible type "Callable[[], list[str]]"; expected "Callable[[], list[Literal['gender�age_band', 'gender�disability']]] | Callable[[dict[str, Any]], list[Literal['gender�age_band', 'gender�disability']]]"
- `app/schemas/ecosystem.py:16` - Argument "default_factory" to "Field" has incompatible type "Callable[[], list[str]]"; expected "Callable[[], list[Literal['platform', 'partner', 'tenant']]] | Callable[[dict[str, Any]], list[Literal['platform', 'partner', 'tenant']]]"
- `app/services/central_token.py:44` - Argument 2 to "import_key" of "RSAKey" has incompatible type "dict[str, str]"; expected "KeyParameters | None"
- `app/core/rate_tiers.py:59` - Argument 2 to "replace" of "RateTier" has incompatible type "**dict[str, int]"; expected "str"
- `app/services/postings.py:338` - Argument 1 to "_out" has incompatible type "_PostingColumns"; expected "BillingPosting | PayablePosting"
- `app/services/postings.py:348` - Argument 1 to "_out" has incompatible type "_PostingColumns"; expected "BillingPosting | PayablePosting"
- `app/services/postings.py:384` - Argument 1 to "_out" has incompatible type "_PostingColumns"; expected "BillingPosting | PayablePosting"
- `app/services/campaign_copy/checks.py:332` - Argument 1 to "len" has incompatible type "Decimal"; expected "Sized"
- `app/services/campaign_copy/checks.py:333` - Argument 1 to "len" has incompatible type "Decimal"; expected "Sized"
- `app/services/campaign_copy/checks.py:337` - Argument 1 to "len" has incompatible type "Decimal"; expected "Sized"
- `app/services/campaign_copy/checks.py:338` - Argument 1 to "len" has incompatible type "Decimal"; expected "Sized"
- `app/schemas/vendors_portal.py:22` - Argument 1 to "register_portal_schema" has incompatible type "str"; expected "Literal['client_user', 'vendor_user', 'worker'] | Iterable[Literal['client_user', 'vendor_user', 'worker']]"
- `app/schemas/vendors_portal.py:46` - Argument 1 to "register_portal_schema" has incompatible type "str"; expected "Literal['client_user', 'vendor_user', 'worker'] | Iterable[Literal['client_user', 'vendor_user', 'worker']]"
- `app/schemas/vendors_portal.py:52` - Argument 1 to "register_portal_schema" has incompatible type "str"; expected "Literal['client_user', 'vendor_user', 'worker'] | Iterable[Literal['client_user', 'vendor_user', 'worker']]"
- `app/schemas/vendors_portal.py:79` - Argument 1 to "register_portal_schema" has incompatible type "str"; expected "Literal['client_user', 'vendor_user', 'worker'] | Iterable[Literal['client_user', 'vendor_user', 'worker']]"
- `app/schemas/vendors_portal.py:90` - Argument 1 to "register_portal_schema" has incompatible type "str"; expected "Literal['client_user', 'vendor_user', 'worker'] | Iterable[Literal['client_user', 'vendor_user', 'worker']]"
- `app/schemas/vendors_portal.py:102` - Argument 1 to "register_portal_schema" has incompatible type "str"; expected "Literal['client_user', 'vendor_user', 'worker'] | Iterable[Literal['client_user', 'vendor_user', 'worker']]"
- `app/schemas/vendors_portal.py:114` - Argument 1 to "register_portal_schema" has incompatible type "str"; expected "Literal['client_user', 'vendor_user', 'worker'] | Iterable[Literal['client_user', 'vendor_user', 'worker']]"
- `app/schemas/vendors_portal.py:120` - Argument 1 to "register_portal_schema" has incompatible type "str"; expected "Literal['client_user', 'vendor_user', 'worker'] | Iterable[Literal['client_user', 'vendor_user', 'worker']]"
- `app/schemas/vendors_portal.py:127` - Argument 1 to "register_portal_schema" has incompatible type "str"; expected "Literal['client_user', 'vendor_user', 'worker'] | Iterable[Literal['client_user', 'vendor_user', 'worker']]"
- `app/schemas/vendors_portal.py:138` - Argument 1 to "register_portal_schema" has incompatible type "str"; expected "Literal['client_user', 'vendor_user', 'worker'] | Iterable[Literal['client_user', 'vendor_user', 'worker']]"
- `app/schemas/vendors_portal.py:152` - Argument 1 to "register_portal_schema" has incompatible type "str"; expected "Literal['client_user', 'vendor_user', 'worker'] | Iterable[Literal['client_user', 'vendor_user', 'worker']]"
- `app/services/campaigns/analytics.py:64` - Argument 1 to "list" has incompatible type "Sequence[Row[Any]]"; expected "Iterable[tuple[Any, ...]]"
- `app/services/campaigns/analytics.py:73` - Argument 1 to "list" has incompatible type "Sequence[Row[Any]]"; expected "Iterable[tuple[Any, ...]]"
- `app/services/billing/rates.py:68` - Argument 1 to "any" of "PropComparator" has incompatible type "str"; expected "ColumnElement[bool] | _HasClauseElement[bool] | SQLCoreOperations[bool] | ExpressionElementRole[bool] | TypedColumnsClauseRole[bool] | Callable[[], ColumnElement[bool]] | LambdaElement | None"
- `app/services/agent_tools/portal_common/tooling.py:115` - Argument "type" to "Principal" has incompatible type "str"; expected "Literal['user', 'candidate', 'platform', 'support', 'api_key', 'client_user', 'vendor_user', 'agent', 'app']"
- `app/core/public_api.py:230` - Argument 2 to "tuple_" has incompatible type "UUID"; expected "ColumnElement[Any] | _HasClauseElement[Any] | SQLCoreOperations[Any] | ExpressionElementRole[Any] | TypedColumnsClauseRole[Any] | Callable[[], ColumnElement[Any]] | LambdaElement"
- `app/core/semantic/compiler.py:405` - Argument 1 to "append" of "list" has incompatible type "Condition | AndNode | OrNode"; expected "Condition"
- `app/services/mail_settings_service.py:86` - Argument 1 to "encrypt_str" has incompatible type "str"; expected "Literal['audit', 'mfa', 'identity', 'connector', 'sso', 'demographic', 'agent_payload', 'vault']"
- `app/services/public_api/_core.py:136` - Argument 2 to "tuple_" has incompatible type "UUID"; expected "ColumnElement[Any] | _HasClauseElement[Any] | SQLCoreOperations[Any] | ExpressionElementRole[Any] | TypedColumnsClauseRole[Any] | Callable[[], ColumnElement[Any]] | LambdaElement"
- `app/services/ecosystem/controls.py:168` - Argument 3 to "record" has incompatible type "int"; expected "UUID | str | None"
- `app/services/connectors/hrms/fake.py:94` - Argument "status" to "HrmsStatusDTO" has incompatible type "str | Any"; expected "Literal['active', 'exited', 'pending', 'unknown']"
- `app/router/semantic.py:36` - Argument 1 to "get" of "Mapping" has incompatible type "Any | None"; expected "str"
- `app/services/tenant_lifecycle_service.py:221` - Argument 1 to "list" has incompatible type "Result[Any]"; expected "Iterable[AuditLog]"
- `app/services/candidates_erasure.py:182` - Argument 1 to "register_erasure_handler" has incompatible type "CandidateProfileHandler"; expected "ErasureHandler"
- `app/services/candidates_erasure.py:183` - Argument 1 to "register_erasure_handler" has incompatible type "CandidateMergesHandler"; expected "ErasureHandler"
- `app/services/auth_service.py:919` - Argument 1 to "_active_tenant_by_slug" has incompatible type "str | None"; expected "str"
- `app/services/fairness/crypto.py:16` - Argument 2 to "seal_json" has incompatible type "str"; expected "Literal['identity', 'demographic', 'agent_payload', 'vault']"
- `app/services/fairness/crypto.py:20` - Argument 2 to "open_json" has incompatible type "str"; expected "Literal['identity', 'demographic', 'agent_payload', 'vault']"
- `app/services/connections/manifests.py:108` - Argument 2 to "sign" of "MLDSA44PrivateKey" has incompatible type "PKCS1v15"; expected "bytes | bytearray | memoryview[int] | None"
- `app/services/connections/manifests.py:108` - Argument 2 to "sign" of "MLDSA65PrivateKey" has incompatible type "PKCS1v15"; expected "bytes | bytearray | memoryview[int] | None"
- `app/services/connections/manifests.py:108` - Argument 2 to "sign" of "MLDSA87PrivateKey" has incompatible type "PKCS1v15"; expected "bytes | bytearray | memoryview[int] | None"
- `app/services/connections/manifests.py:108` - Argument 2 to "sign" of "DSAPrivateKey" has incompatible type "PKCS1v15"; expected "Prehashed | HashAlgorithm"
- `app/services/connections/manifests.py:108` - Argument 2 to "sign" of "EllipticCurvePrivateKey" has incompatible type "PKCS1v15"; expected "EllipticCurveSignatureAlgorithm"
- `app/services/collection_rules/log.py:164` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, str]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/agent_runtime/principals.py:154` - Argument 3 to "owner_principal" has incompatible type "UUID | None"; expected "UUID"
- `app/services/workflow_erasure.py:90` - Argument 1 to "register_erasure_handler" has incompatible type "WorkflowWaitsHandler"; expected "ErasureHandler"
- `app/services/voice_erasure.py:95` - Argument 1 to "register_erasure_handler" has incompatible type "VoiceSessionsHandler"; expected "ErasureHandler"
- `app/services/vendors_erasure.py:123` - Argument 1 to "register_erasure_handler" has incompatible type "VendorSubmissionsHandler"; expected "ErasureHandler"
- `app/services/timesheet_flags_erasure.py:65` - Argument 1 to "register_erasure_handler" has incompatible type "TimesheetFlagsHandler"; expected "ErasureHandler"
- `app/services/time_erasure.py:88` - Argument 1 to "register_erasure_handler" has incompatible type "TimeHandler"; expected "ErasureHandler"
- `app/services/telephony_erasure.py:78` - Argument 1 to "register_erasure_handler" has incompatible type "TelephonyCallsHandler"; expected "ErasureHandler"
- `app/services/tasks_erasure.py:63` - Argument 1 to "register_erasure_handler" has incompatible type "TaskTextsHandler"; expected "ErasureHandler"
- `app/services/talent_pools_erasure.py:76` - Argument 1 to "register_erasure_handler" has incompatible type "TalentPoolMembersHandler"; expected "ErasureHandler"
- `app/services/submission_packs_erasure.py:97` - Argument 1 to "register_erasure_handler" has incompatible type "SubmissionPacksHandler"; expected "ErasureHandler"
- `app/services/start_gates_erasure.py:58` - Argument 1 to "register_erasure_handler" has incompatible type "StartGatesHandler"; expected "ErasureHandler"
- `app/services/sourcing_agent_erasure.py:98` - Argument 1 to "register_erasure_handler" has incompatible type "SourcingShortlistsHandler"; expected "ErasureHandler"
- `app/services/skills_erasure.py:86` - Argument 1 to "register_erasure_handler" has incompatible type "SkillsHandler"; expected "ErasureHandler"
- `app/services/search_erasure.py:99` - Argument 1 to "register_erasure_handler" has incompatible type "SearchIndexHandler"; expected "ErasureHandler"
- `app/services/screening_agent_erasure.py:94` - Argument 1 to "register_erasure_handler" has incompatible type "ScreeningConversationsHandler"; expected "ErasureHandler"
- `app/services/scheduling_agent_erasure.py:108` - Argument 1 to "register_erasure_handler" has incompatible type "SchedulingRequestsHandler"; expected "ErasureHandler"
- `app/services/projects_erasure.py:68` - Argument 1 to "register_erasure_handler" has incompatible type "ProjectsHandler"; expected "ErasureHandler"
- `app/services/privacy_erasure.py:152` - Argument 1 to "dict" has incompatible type "Sequence[tuple[UUID, datetime | None]]"; expected "Iterable[tuple[UUID, datetime]]"
- `app/services/privacy_erasure.py:155` - Argument 1 to "register_erasure_handler" has incompatible type "ConsentsHandler"; expected "ErasureHandler"
- `app/services/privacy_erasure.py:156` - Argument 1 to "register_erasure_handler" has incompatible type "PrivacyRecordsHandler"; expected "ErasureHandler"
- `app/services/privacy_erasure.py:157` - Argument 1 to "register_erasure_handler" has incompatible type "AuditShredHandler"; expected "ErasureHandler"
- `app/services/portal_erasure.py:141` - Argument 1 to "register_erasure_handler" has incompatible type "PortalAccountHandler"; expected "ErasureHandler"
- `app/services/portal_copilot_erasure.py:74` - Argument 1 to "register_erasure_handler" has incompatible type "PortalCopilotHandler"; expected "ErasureHandler"
- `app/services/placements_erasure.py:172` - Argument 1 to "register_erasure_handler" has incompatible type "PlacementsHandler"; expected "ErasureHandler"
- `app/services/placement_exit_erasure.py:95` - Argument 1 to "register_erasure_handler" has incompatible type "PlacementExitHandler"; expected "ErasureHandler"
- `app/services/pipeline_erasure.py:269` - Argument 1 to "register_erasure_handler" has incompatible type "ScreeningsHandler"; expected "ErasureHandler"
- `app/services/pipeline_erasure.py:270` - Argument 1 to "register_erasure_handler" has incompatible type "SubmissionsHandler"; expected "ErasureHandler"
- `app/services/onboarding_erasure.py:130` - Argument 1 to "register_erasure_handler" has incompatible type "OnboardingHandler"; expected "ErasureHandler"
- `app/services/offers_erasure.py:152` - Argument 1 to "register_erasure_handler" has incompatible type "OffersHandler"; expected "ErasureHandler"
- `app/services/notifications_erasure.py:95` - Argument 1 to "register_erasure_handler" has incompatible type "NotificationTextsHandler"; expected "ErasureHandler"
- `app/services/messaging_erasure.py:121` - Argument 1 to "register_erasure_handler" has incompatible type "CommsChannelsHandler"; expected "ErasureHandler"
- `app/services/leave_erasure.py:98` - Argument 1 to "register_erasure_handler" has incompatible type "LeaveHandler"; expected "ErasureHandler"
- `app/services/interviews_erasure.py:110` - Argument 1 to "register_erasure_handler" has incompatible type "InterviewsHandler"; expected "ErasureHandler"
- `app/services/interview_captures_erasure.py:142` - Argument 1 to "register_erasure_handler" has incompatible type "InterviewCaptureRecordsHandler"; expected "ErasureHandler"
- `app/services/internal_integration_service.py:203` - Argument 1 to "decrypt_str" has incompatible type "str"; expected "Literal['audit', 'mfa', 'identity', 'connector', 'sso', 'demographic', 'agent_payload', 'vault']"
- `app/services/internal_integration_service.py:215` - Argument 1 to "encrypt_str" has incompatible type "str"; expected "Literal['audit', 'mfa', 'identity', 'connector', 'sso', 'demographic', 'agent_payload', 'vault']"
- `app/services/integrations_erasure.py:45` - Argument 1 to "register_erasure_handler" has incompatible type "IntegrationLinksHandler"; expected "ErasureHandler"
- `app/services/immigration_erasure.py:113` - Argument 1 to "register_erasure_handler" has incompatible type "ImmigrationHandler"; expected "ErasureHandler"
- `app/services/hrms_sync_erasure.py:57` - Argument 1 to "register_erasure_handler" has incompatible type "HrmsSyncRecordsHandler"; expected "ErasureHandler"
- `app/services/files_erasure.py:126` - Argument 1 to "register_erasure_handler" has incompatible type "CandidateFilesHandler"; expected "ErasureHandler"
- `app/services/expenses_erasure.py:73` - Argument 1 to "register_erasure_handler" has incompatible type "ExpensesHandler"; expected "ErasureHandler"
- `app/services/crm_erasure.py:68` - Argument 1 to "register_erasure_handler" has incompatible type "CrmLeadsHandler"; expected "ErasureHandler"
- `app/services/copilot_erasure.py:85` - Argument 1 to "register_erasure_handler" has incompatible type "CopilotMessagesHandler"; expected "ErasureHandler"
- `app/services/comms_erasure.py:101` - Argument 1 to "register_erasure_handler" has incompatible type "CommsEmailsHandler"; expected "ErasureHandler"
- `app/services/chasing_erasure.py:57` - Argument 1 to "register_erasure_handler" has incompatible type "ChasingHandler"; expected "ErasureHandler"
- `app/services/campaigns_erasure.py:101` - Argument 1 to "register_erasure_handler" has incompatible type "CampaignsMarketingHandler"; expected "ErasureHandler"
- `app/services/bgv_erasure.py:216` - Argument 1 to "register_erasure_handler" has incompatible type "BgvHandler"; expected "ErasureHandler"
- `app/services/bench_erasure.py:66` - Argument 1 to "register_erasure_handler" has incompatible type "BenchHandler"; expected "ErasureHandler"
- `app/services/assessments_erasure.py:138` - Argument 1 to "register_erasure_handler" has incompatible type "AssessmentsHandler"; expected "ErasureHandler"
- `app/services/analytics_answers_erasure.py:166` - Argument 1 to "register_erasure_handler" has incompatible type "AnalyticsAnswersHandler"; expected "ErasureHandler"
- `app/services/agent_runtime_erasure.py:118` - Argument 1 to "register_erasure_handler" has incompatible type "AgentRunsHandler"; expected "ErasureHandler"
- `app/services/activity_erasure.py:69` - Argument 1 to "register_erasure_handler" has incompatible type "ActivityEntityHandler"; expected "ErasureHandler"
- `app/services/timesheet_flags/nudges.py:141` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, datetime]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/timesheet_flags/nudges.py:231` - Argument 1 to "set" has incompatible type "ScalarResult[UUID | None]"; expected "Iterable[UUID]"
- `app/services/reporting/builder/sources_p1p2.py:68` - Argument 5 to "FieldDef" has incompatible type "frozenset[str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/reporting/builder/sources_p1p2.py:81` - Argument 5 to "FieldDef" has incompatible type "frozenset[str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/reporting/builder/sources_p1p2.py:90` - Argument 3 to "FieldDef" has incompatible type "str"; expected "Literal['none', 'personal', 'compensation', 'identity', 'protected']"
- `app/services/reporting/builder/sources_p1p2.py:90` - Argument 5 to "FieldDef" has incompatible type "frozenset[str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/reporting/builder/sources_p1p2.py:97` - Argument 1 to "FieldDef" has incompatible type "str"; expected "Literal['string', 'int', 'decimal', 'bool', 'date', 'datetime', 'uuid', 'enum', 'string_array', 'skill']"
- `app/services/reporting/builder/sources_p1p2.py:97` - Argument 5 to "FieldDef" has incompatible type "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match'] | str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/reporting/builder/sources_p1p2.py:97` - Argument "aggs" to "FieldDef" has incompatible type "frozenset[str]"; expected "frozenset[Literal['count', 'count_distinct', 'sum', 'avg', 'min', 'max']]"
- `app/services/reporting/builder/sources_p1p2.py:183` - Argument 5 to "FieldDef" has incompatible type "frozenset[str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/reporting/builder/sources_p1p2.py:247` - Argument 5 to "FieldDef" has incompatible type "frozenset[str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/reporting/builder/sources_p1p2.py:292` - Argument 5 to "FieldDef" has incompatible type "frozenset[str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/reporting/builder/sources_p1p2.py:332` - Argument 5 to "FieldDef" has incompatible type "frozenset[str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/reporting/builder/sources_p1p2.py:361` - Argument 5 to "FieldDef" has incompatible type "frozenset[str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/reporting/builder/sources_p1p2.py:402` - Argument 5 to "FieldDef" has incompatible type "frozenset[str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/reporting/builder/sources_p1p2.py:498` - Argument 5 to "FieldDef" has incompatible type "frozenset[str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/reporting/builder/sources_p1p2.py:554` - Argument 5 to "FieldDef" has incompatible type "frozenset[str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/reporting/builder/sources_p1p2.py:567` - Argument 5 to "FieldDef" has incompatible type "frozenset[str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/projects/timeline.py:146` - Argument 2 to "where" of "Select" has incompatible type "object"; expected "ColumnElement[bool] | _HasClauseElement[bool] | SQLCoreOperations[bool] | ExpressionElementRole[bool] | TypedColumnsClauseRole[bool] | Callable[[], ColumnElement[bool]] | LambdaElement"
- `app/services/projects/timeline.py:183` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, UUID]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/projects/timeline.py:194` - Argument 2 to "where" of "Select" has incompatible type "object"; expected "ColumnElement[bool] | _HasClauseElement[bool] | SQLCoreOperations[bool] | ExpressionElementRole[bool] | TypedColumnsClauseRole[bool] | Callable[[], ColumnElement[bool]] | LambdaElement"
- `app/services/fairness/selection_points.py:184` - Argument 1 to "Point" has incompatible type "UUID | None"; expected "UUID"
- `app/services/fairness/selection_points.py:202` - Argument 1 to "update" of "MutableMapping" has incompatible type "dict[str, Callable[[AsyncSession, dict[str, Any], datetime, datetime, dict[str, Any]], Awaitable[list[Any]]]]"; expected "SupportsKeysAndGetItem[str, Callable[[AsyncSession, dict[str, Any], datetime, datetime, dict[str, Any]], Coroutine[Any, Any, list[Point]]]]"
- `app/services/candidates/semantic_source.py:97` - Argument 5 has incompatible type "frozenset[str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/candidates/semantic_source.py:99` - Argument 5 has incompatible type "frozenset[str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/candidates/semantic_source.py:102` - Argument 5 has incompatible type "frozenset[str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/candidates/semantic_source.py:157` - Argument 5 has incompatible type "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match'] | str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/calendar_sync/common.py:93` - Argument "entity_type" to "model_construct" of "TaskCreate" has incompatible type "Literal['calendar_change']"; expected "Literal['client', 'contact', 'demand', 'candidate', 'application', 'lead', 'opportunity'] | None"
- `app/services/agent_tools/portal_common/handoff.py:64` - Argument "entity_type" to "model_construct" of "TaskCreate" has incompatible type "str | None"; expected "Literal['client', 'contact', 'demand', 'candidate', 'application', 'lead', 'opportunity'] | None"
- `app/services/agent_runtime/fallback.py:54` - Argument "entity_type" to "model_construct" of "TaskCreate" has incompatible type "str"; expected "Literal['client', 'contact', 'demand', 'candidate', 'application', 'lead', 'opportunity'] | None"
- `app/services/job_distribution_erasure.py:74` - Argument 1 to "register_erasure_handler" has incompatible type "JobBoardIngestsHandler"; expected "ErasureHandler"
- `app/services/placements/semantic_source.py:25` - Argument 5 to "FieldDef" has incompatible type "frozenset[str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/placements/semantic_source.py:30` - Argument 5 to "FieldDef" has incompatible type "frozenset[str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/placements/semantic_source.py:35` - Argument 5 to "FieldDef" has incompatible type "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match'] | str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/placements/semantic_source.py:40` - Argument 5 to "FieldDef" has incompatible type "frozenset[str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/placements/semantic_source.py:41` - Argument 5 to "FieldDef" has incompatible type "frozenset[str]"; expected "frozenset[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]"
- `app/services/placements/lists.py:237` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, datetime | None]]]"; expected "Iterable[tuple[UUID, Any]]"
- `app/services/expenses/policies.py:134` - Argument 1 to "Decimal" has incompatible type "str | None"; expected "Decimal | float | str | tuple[int, Sequence[int], int]"
- `app/services/knowledge_erasure.py:76` - Argument 1 to "register_erasure_handler" has incompatible type "OnboardingQaHandler"; expected "ErasureHandler"
- `app/services/audit_timeline_service.py:196` - Argument 1 to "tuple_" has incompatible type "datetime"; expected "ColumnElement[Any] | _HasClauseElement[Any] | SQLCoreOperations[Any] | ExpressionElementRole[Any] | TypedColumnsClauseRole[Any] | Callable[[], ColumnElement[Any]] | LambdaElement"
- `app/services/audit_timeline_service.py:196` - Argument 2 to "tuple_" has incompatible type "UUID"; expected "ColumnElement[Any] | _HasClauseElement[Any] | SQLCoreOperations[Any] | ExpressionElementRole[Any] | TypedColumnsClauseRole[Any] | Callable[[], ColumnElement[Any]] | LambdaElement"
- `app/services/webhooks/log.py:66` - Argument 1 to "tuple_" has incompatible type "datetime"; expected "ColumnElement[Any] | _HasClauseElement[Any] | SQLCoreOperations[Any] | ExpressionElementRole[Any] | TypedColumnsClauseRole[Any] | Callable[[], ColumnElement[Any]] | LambdaElement"
- `app/services/webhooks/log.py:66` - Argument 2 to "tuple_" has incompatible type "UUID"; expected "ColumnElement[Any] | _HasClauseElement[Any] | SQLCoreOperations[Any] | ExpressionElementRole[Any] | TypedColumnsClauseRole[Any] | Callable[[], ColumnElement[Any]] | LambdaElement"
- `app/services/webhooks/subscriptions.py:157` - Argument 1 to "tuple_" has incompatible type "datetime"; expected "ColumnElement[Any] | _HasClauseElement[Any] | SQLCoreOperations[Any] | ExpressionElementRole[Any] | TypedColumnsClauseRole[Any] | Callable[[], ColumnElement[Any]] | LambdaElement"
- `app/services/webhooks/subscriptions.py:157` - Argument 2 to "tuple_" has incompatible type "UUID"; expected "ColumnElement[Any] | _HasClauseElement[Any] | SQLCoreOperations[Any] | ExpressionElementRole[Any] | TypedColumnsClauseRole[Any] | Callable[[], ColumnElement[Any]] | LambdaElement"
- `app/services/webhooks/scheduler.py:96` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, int]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/oauth/apps.py:573` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID | None, datetime | None]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/knowledge/sources.py:88` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[int, int]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/immigration/visa_types.py:52` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[str | None, UUID]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/immigration/visa_types.py:237` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[str, date]]]"; expected "Iterable[tuple[str, Any]]"
- `app/services/immigration/visa_types.py:272` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, int]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/workflow/actions.py:648` - Argument 1 to "get" of "Mapping" has incompatible type "Any | None"; expected "str"
- `app/services/matching/explain.py:76` - Argument 1 to "update" has incompatible type "FromClause"; expected "TableClause | Join | Alias | CTE | type[Any] | Inspectable[_HasClauseElement[Any]] | _HasClauseElement[Any]"
- `app/services/interviews/ports.py:61` - Argument 5 to "transition_application" has incompatible type "str"; expected "Literal['user', 'candidate', 'system', 'workflow', 'ai', 'api_key', 'agent', 'app']"
- `app/services/pipeline/applications.py:105` - Argument 2 to "stage_by_key" has incompatible type "UUID | str"; expected "str"
- `app/services/pipeline/applications.py:436` - Argument "owner_id" to "create_application" has incompatible type "UUID | None"; expected "UUID"
- `app/services/pipeline/applications.py:487` - Argument "owner_id" to "create_application" has incompatible type "UUID | None"; expected "UUID"
- `app/services/pipeline/applications.py:519` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, Decimal]]]"; expected "Iterable[tuple[UUID, Any]]"
- `app/services/matching/runs.py:282` - Argument 1 to "insert" has incompatible type "FromClause"; expected "TableClause | Join | Alias | CTE | type[Any] | Inspectable[_HasClauseElement[Any]] | _HasClauseElement[Any]"
- `app/services/platform_service.py:445` - Argument 1 to "get" of "dict" has incompatible type "UUID | None"; expected "UUID"
- `app/services/interviews/scorecards.py:430` - Argument 1 to "__exit__" of "_GeneratorContextManager" has incompatible type "*tuple[object, ...]"; expected "type[BaseException] | None"
- `app/services/interviews/scorecards.py:430` - Argument 1 to "__exit__" of "_GeneratorContextManager" has incompatible type "*tuple[object, ...]"; expected "BaseException | None"
- `app/services/interviews/scorecards.py:430` - Argument 1 to "__exit__" of "_GeneratorContextManager" has incompatible type "*tuple[object, ...]"; expected "TracebackType | None"
- `app/services/privacy/dsr.py:416` - Argument 1 to "set" has incompatible type "ScalarResult[UUID | None]"; expected "Iterable[UUID]"
- `app/services/ai_erasure.py:72` - Argument 1 to "register_erasure_handler" has incompatible type "AIActionsHandler"; expected "ErasureHandler"
- `app/services/skills/evidence.py:203` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, int]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/screening_agent/questionnaires.py:167` - Argument 1 to "float" has incompatible type "Any | None"; expected "str | Buffer | SupportsFloat | SupportsIndex"
- `app/services/sandbox/seed.py:646` - Argument 1 to "get" of "dict" has incompatible type "UUID | None"; expected "UUID"
- `app/services/sandbox/seed.py:647` - Argument 1 to "get" of "dict" has incompatible type "UUID | None"; expected "UUID"
- `app/services/public_api/candidates.py:82` - Argument "source" to "model_construct" of "CandidateFilters" has incompatible type "str | None"; expected "Literal['manual', 'resume_upload', 'bulk_import', 'career_portal', 'referral', 'job_board', 'other', 'campus', 'event', 'internal', 'job_network', 'public_api'] | None"
- `app/services/ledger_sync/common.py:181` - Argument "entity_type" to "model_construct" of "TaskCreate" has incompatible type "str"; expected "Literal['client', 'contact', 'demand', 'candidate', 'application', 'lead', 'opportunity'] | None"
- `app/services/interviews/portal.py:156` - Argument 2 to "public_submit" has incompatible type "object"; expected "PublicScorecardIn"
- `app/services/hrms_sync/common.py:180` - Argument "entity_type" to "model_construct" of "TaskCreate" has incompatible type "str"; expected "Literal['client', 'contact', 'demand', 'candidate', 'application', 'lead', 'opportunity'] | None"
- `app/services/demand_intake/receive.py:95` - Argument "policy" to "message_from_bytes" has incompatible type "EmailPolicy[EmailMessage]"; expected "Policy[Message[str, str]]"
- `app/services/demand_intake/receive.py:244` - Argument 1 to "append" of "list" has incompatible type "None"; expected "Literal['clean', 'infected', 'skipped_dev', 'error']"
- `app/services/crm/duplicates.py:64` - Argument 1 to "append" of "list" has incompatible type "bool"; expected "ColumnElement[bool]"
- `app/services/comms/hooks.py:60` - Argument 5 to "NotificationType" has incompatible type "Callable[[AsyncSession, dict[str, Any]], Coroutine[Any, Any, set[UUID]]]"; expected "Callable[[AsyncSession, Mapping[str, Any]], Awaitable[set[UUID] | set[Recipient]]]"
- `app/services/comms/hooks.py:60` - Argument 6 to "NotificationType" has incompatible type "Callable[[AsyncSession, dict[str, Any]], Coroutine[Any, Any, Content]]"; expected "Callable[[AsyncSession, Mapping[str, Any]], Awaitable[Content]]"
- `app/services/transcripts_erasure.py:149` - Argument 1 to "register_erasure_handler" has incompatible type "TranscriptRecordsHandler"; expected "ErasureHandler"
- `app/services/transcripts_erasure.py:150` - Argument 1 to "register_erasure_handler" has incompatible type "RecordingFilesHandler"; expected "ErasureHandler"
- `app/services/telephony/costs.py:57` - Argument 1 to "int" has incompatible type "int | None"; expected "str | Buffer | SupportsInt | SupportsIndex | SupportsTrunc"
- `app/services/sso/hooks.py:52` - Argument "recipients_resolver" to "NotificationType" has incompatible type "Callable[[AsyncSession, dict[str, Any]], Coroutine[Any, Any, set[UUID]]]"; expected "Callable[[AsyncSession, Mapping[str, Any]], Awaitable[set[UUID] | set[Recipient]]]"
- `app/services/sso/hooks.py:53` - Argument "render" to "NotificationType" has incompatible type "Callable[[AsyncSession, dict[str, Any]], Coroutine[Any, Any, Content]]"; expected "Callable[[AsyncSession, Mapping[str, Any]], Awaitable[Content]]"
- `app/services/sso/hooks.py:135` - Argument "recipients_resolver" to "NotificationType" has incompatible type "Callable[[AsyncSession, dict[str, Any]], Coroutine[Any, Any, set[UUID]]]"; expected "Callable[[AsyncSession, Mapping[str, Any]], Awaitable[set[UUID] | set[Recipient]]]"
- `app/services/sso/hooks.py:136` - Argument "render" to "NotificationType" has incompatible type "Callable[[AsyncSession, dict[str, Any]], Coroutine[Any, Any, Content]]"; expected "Callable[[AsyncSession, Mapping[str, Any]], Awaitable[Content]]"
- `app/services/sso/hooks.py:147` - Argument "recipients_resolver" to "NotificationType" has incompatible type "Callable[[AsyncSession, dict[str, Any]], Coroutine[Any, Any, set[UUID]]]"; expected "Callable[[AsyncSession, Mapping[str, Any]], Awaitable[set[UUID] | set[Recipient]]]"
- `app/services/sso/hooks.py:148` - Argument "render" to "NotificationType" has incompatible type "Callable[[AsyncSession, dict[str, Any]], Coroutine[Any, Any, Content]]"; expected "Callable[[AsyncSession, Mapping[str, Any]], Awaitable[Content]]"
- `app/services/reporting/lists.py:808` - Argument 2 to "_json_value" has incompatible type "RowMapping"; expected "Mapping[str, Any]"
- `app/services/portal/privacy_self_service.py:77` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, int]]]"; expected "Iterable[tuple[UUID, int]]"
- `app/services/crm/won.py:266` - Argument 4 to "_link" has incompatible type "UUID | None"; expected "UUID"
- `app/services/campus/events.py:198` - Argument 2 to "revoke" has incompatible type "UUID | None"; expected "UUID"
- `app/services/agent_runtime/proposals.py:323` - Argument 1 to "append" of "list" has incompatible type "str | None"; expected "str"
- `app/services/agent_runtime/proposals.py:433` - Argument "owner_user_id" to "ToolCtx" has incompatible type "UUID | None"; expected "UUID"
- `app/services/demand_intake_erasure.py:81` - Argument 1 to "register_erasure_handler" has incompatible type "DemandIntakeMessagesHandler"; expected "ErasureHandler"
- `app/services/telephony/gates.py:214` - Argument "purpose" to "check" has incompatible type "str"; expected "Literal['call_recording', 'interview_recording']"
- `app/services/job_distribution/ingest.py:62` - Argument "entity_type" to "model_construct" of "TaskCreate" has incompatible type "Literal['board_posting']"; expected "Literal['client', 'contact', 'demand', 'candidate', 'application', 'lead', 'opportunity'] | None"
- `app/services/ecosystem_governance/erasure_fanout.py:117` - Argument 3 to "find_link" has incompatible type "str | None"; expected "str"
- `app/services/ecosystem_governance/erasure_fanout.py:121` - Argument 1 to "adapter_for" has incompatible type "str | None"; expected "RequestContext"
- `app/services/assessments/invitations.py:695` - Argument "candidate_id" to "AssessmentReminderDue" has incompatible type "UUID | None"; expected "UUID"
- `app/services/agent_runtime/executor.py:287` - Argument "owner_user_id" to "AgentRunFailed" has incompatible type "UUID | None"; expected "UUID"
- `app/services/agent_runtime/executor.py:293` - Argument "owner_user_id" to "AgentRunFinished" has incompatible type "UUID | None"; expected "UUID"
- `app/services/agent_runtime/executor.py:631` - Argument "owner_user_id" to "ToolCtx" has incompatible type "UUID | None"; expected "UUID"
- `app/services/agent_runtime/executor.py:691` - Argument "owner_user_id" to "ToolCtx" has incompatible type "UUID | None"; expected "UUID"
- `app/services/fairness_erasure.py:57` - Argument 1 to "register_erasure_handler" has incompatible type "FairnessHandler"; expected "ErasureHandler"
- `app/services/documents_erasure.py:238` - Argument 1 to "register_erasure_handler" has incompatible type "DocumentsHandler"; expected "ErasureHandler"
- `app/services/receivables/tds_recon/runs.py:468` - Argument 1 to "set" has incompatible type "ScalarResult[UUID | None]"; expected "Iterable[UUID]"
- `app/services/ledger_sync/documents.py:235` - Argument 1 to "get" of "dict" has incompatible type "UUID | None"; expected "UUID"
- `app/services/ledger_sync/documents.py:236` - Argument 1 to "get" of "dict" has incompatible type "UUID | None"; expected "UUID"
- `app/services/job_distribution/sync.py:137` - Argument "entity_type" to "model_construct" of "TaskCreate" has incompatible type "Literal['board_posting']"; expected "Literal['client', 'contact', 'demand', 'candidate', 'application', 'lead', 'opportunity'] | None"
- `app/services/job_distribution/expiry.py:57` - Argument "entity_type" to "model_construct" of "TaskCreate" has incompatible type "Literal['board_posting']"; expected "Literal['client', 'contact', 'demand', 'candidate', 'application', 'lead', 'opportunity'] | None"
- `app/services/agent_runtime/runs.py:130` - Argument "entity_type" to "model_construct" of "TaskCreate" has incompatible type "str | None"; expected "Literal['client', 'contact', 'demand', 'candidate', 'application', 'lead', 'opportunity'] | None"
- `app/services/agent_runtime/runs.py:552` - Argument 1 to "get" of "dict" has incompatible type "UUID | None"; expected "UUID"
- `app/services/agent_runtime/external.py:249` - Argument 1 to "get" of "dict" has incompatible type "UUID | None"; expected "UUID"
- `app/services/marketplace/installs.py:1011` - Argument 2 to "tuple_" has incompatible type "UUID"; expected "ColumnElement[Any] | _HasClauseElement[Any] | SQLCoreOperations[Any] | ExpressionElementRole[Any] | TypedColumnsClauseRole[Any] | Callable[[], ColumnElement[Any]] | LambdaElement"
- `app/services/messaging/hooks.py:154` - Argument 5 to "NotificationType" has incompatible type "Callable[[AsyncSession, dict[str, Any]], Coroutine[Any, Any, set[UUID]]]"; expected "Callable[[AsyncSession, Mapping[str, Any]], Awaitable[set[UUID] | set[Recipient]]]"
- `app/services/messaging/hooks.py:154` - Argument 6 to "NotificationType" has incompatible type "Callable[[AsyncSession, dict[str, Any]], Coroutine[Any, Any, Content]]"; expected "Callable[[AsyncSession, Mapping[str, Any]], Awaitable[Content]]"
- `app/services/ledger_sync/push.py:188` - Argument 5 to "link" has incompatible type "str | None"; expected "str"
- `app/services/job_distribution/hooks.py:36` - Argument 2 to "register_webhook_handler" has incompatible type "Callable[[RequestContext, AsyncSession, dict[str, Any], Any], Coroutine[Any, Any, None]]"; expected "Callable[[RequestContext, AsyncSession, Mapping[str, Any], ConnectorConfig], Awaitable[None]]"
- `app/services/chasing/chases.py:227` - Argument 1 to "get" of "dict" has incompatible type "tuple[str, UUID]"; expected "tuple[Literal['checklist_item', 'credential', 'permit'], UUID]"
- `app/services/chasing/chases.py:379` - Argument "entity_type" to "model_construct" of "TaskCreate" has incompatible type "str"; expected "Literal['client', 'contact', 'demand', 'candidate', 'application', 'lead', 'opportunity'] | None"
- `app/services/campaigns/audiences.py:144` - Argument 1 to "get" of "dict" has incompatible type "UUID | None"; expected "UUID"
- `app/services/vendors/portal.py:245` - Argument 1 to "tenant_session" has incompatible type "UUID | None"; expected "UUID | str"
- `app/services/submission_packs/checks.py:156` - Argument 5 to "_d" has incompatible type "**dict[str, object]"; expected "PackLink | None"
- `app/services/receivables/settings.py:19` - Argument 1 to "insert" has incompatible type "FromClause"; expected "TableClause | Join | Alias | CTE | type[Any] | Inspectable[_HasClauseElement[Any]] | _HasClauseElement[Any]"
- `app/services/receivables/periods_close.py:177` - Argument 4 to "select" has incompatible type "Any | None"; expected "TypedColumnsClauseRole[Any] | SQLCoreOperations[Any] | type[Any]"
- `app/services/agent_runtime/hooks.py:200` - Argument 1 to <set> has incompatible type "UUID | None"; expected "UUID"
- `app/services/agent_runtime/governance/console.py:339` - Argument 1 to "int" has incompatible type "int | None"; expected "str | Buffer | SupportsInt | SupportsIndex | SupportsTrunc"
- `app/services/submission_packs/service.py:812` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, datetime]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/projects/deliverables.py:77` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, str]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/placements/gates.py:344` - Argument 2 to "GateState" has incompatible type "str"; expected "Literal['pending', 'met', 'failed', 'waived', 'not_required']"
- `app/services/placements/gates.py:392` - Argument 2 to "GateState" has incompatible type "str"; expected "Literal['pending', 'met', 'failed', 'waived', 'not_required']"
- `app/services/placements/gates.py:418` - Argument 2 to "GateState" has incompatible type "str"; expected "Literal['pending', 'met', 'failed', 'waived', 'not_required']"
- `app/services/placements/gates.py:433` - Argument 2 to "GateState" has incompatible type "str"; expected "Literal['pending', 'met', 'failed', 'waived', 'not_required']"
- `app/services/ledger_sync/records.py:77` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, str]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/ledger_sync/records.py:174` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, str]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/interview_captures/withdrawal.py:110` - Argument 3 to "cancel_run" has incompatible type "UUID | None"; expected "UUID"
- `app/services/bgv/cases.py:503` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, UUID]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/projects/portal.py:86` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, int]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/projects/portal.py:157` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, str]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/developer_portal/conformance/runner.py:182` - Argument 1 to "sample_envelope" has incompatible type "str | None"; expected "str"
- `app/services/developer_portal/conformance/runner.py:183` - Argument "event_type" to "headers" has incompatible type "str | None"; expected "str"
- `app/services/developer_portal/conformance/runner.py:215` - Argument "event_id" to "headers" has incompatible type "object"; expected "UUID | str"
- `app/services/campus/offers.py:140` - Argument 1 to "get" of "dict" has incompatible type "UUID | None"; expected "UUID"
- `app/services/time/timesheets.py:823` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, str]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/time/timesheets.py:829` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, str]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/time/timesheets.py:835` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, str]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/time/timesheets.py:849` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, Decimal]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/time/timesheets.py:859` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, Decimal]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/time/timesheets.py:859` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, Decimal]]]"; expected "Iterable[tuple[Any, Any]]"
- `app/services/time/monitoring.py:190` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, str]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/time/monitoring.py:196` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, str]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/submission_packs/hooks.py:130` - Argument 1 to "add" of "set" has incompatible type "UUID | None"; expected "UUID"
- `app/services/projects/assignments.py:47` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, str]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/projects/assignments.py:56` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, str]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/projects/assignments.py:67` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, str]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/compliance/checklist.py:559` - Argument 2 to "GateState" has incompatible type "str"; expected "Literal['pending', 'met', 'failed', 'waived', 'not_required']"
- `app/services/agent_tools/pipeline/tools_e61.py:258` - Argument "version" to "ScopeRule" has incompatible type "Callable[[Any], Coroutine[Any, Any, str | None]]"; expected "Callable[[Any], str] | None"
- `app/services/agent_tools/demands/tools_e55.py:458` - Argument "version" to "ScopeRule" has incompatible type "Callable[[Any], Coroutine[Any, Any, str | None]]"; expected "Callable[[Any], str] | None"
- `app/services/agent_tools/core/tools_e53.py:112` - Argument "entity_type" to "model_construct" of "TaskCreate" has incompatible type "Literal['candidate', 'application', 'demand', 'client', 'placement', 'interview', 'timesheet', 'invoice']"; expected "Literal['client', 'contact', 'demand', 'candidate', 'application', 'lead', 'opportunity'] | None"
- `app/services/compliance_erasure.py:105` - Argument 1 to "register_erasure_handler" has incompatible type "ComplianceHandler"; expected "ErasureHandler"
- `app/services/screening_agent/hooks.py:179` - Argument 2 to "subscribe" has incompatible type "Callable[[AgentRunStarted], None] | Callable[[AgentRunFailed], None] | Callable[[ScreeningRunNeeded], None]"; expected "Callable[[object], None]"
- `app/services/projects/projects.py:53` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, str]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/projects/projects.py:60` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, str]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/projects/projects.py:244` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, Decimal | None]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/projects/overview.py:246` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, str]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/placements/offboarding.py:719` - Argument 1 to "get" of "dict" has incompatible type "UUID | None"; expected "UUID"
- `app/services/onboarding/cases.py:579` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, str]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/bgv/hooks.py:382` - Argument 2 to "register_webhook_handler" has incompatible type "Callable[[RequestContext, AsyncSession, dict[str, Any], Any], Coroutine[Any, Any, None]]"; expected "Callable[[RequestContext, AsyncSession, Mapping[str, Any], ConnectorConfig], Awaitable[None]]"
- `app/services/voice/consent.py:249` - Argument "purpose" to "check" has incompatible type "str"; expected "Literal['call_recording', 'interview_recording']"
- `app/services/voice/agent_ops.py:71` - Argument "purpose" to "check" has incompatible type "str"; expected "Literal['call_recording', 'interview_recording']"
- `app/services/scheduling_agent/hooks.py:207` - Argument 2 to "subscribe" has incompatible type "Callable[[SchedulingRunNeeded], None] | Callable[[Any], None] | Callable[[InterviewCancelled], None] | Callable[[InterviewCompleted], None] | Callable[[ScorecardSubmitted], None] | Callable[[ApplicationStageChanged], None] | Callable[[AgentRunFailed], None]"; expected "Callable[[object], None]"
- `app/services/mobility/transfers.py:76` - Argument 1 to "get" of "dict" has incompatible type "UUID | None"; expected "UUID"
- `app/services/agent_tools/sourcing/tools_e56.py:457` - Argument "version" to "ScopeRule" has incompatible type "Callable[[Any], Coroutine[Any, Any, str | None]]"; expected "Callable[[Any], str] | None"
- `app/services/agent_tools/screening/tools_e57.py:497` - Argument "version" to "ScopeRule" has incompatible type "Callable[[Any], Coroutine[Any, Any, str]]"; expected "Callable[[Any], str] | None"
- `app/services/agent_tools/pipeline/tools_e56.py:82` - Argument "version" to "ScopeRule" has incompatible type "Callable[[Any], Coroutine[Any, Any, str]]"; expected "Callable[[Any], str] | None"
- `app/services/voice/uploads.py:91` - Argument "purpose" to "check" has incompatible type "str"; expected "Literal['call_recording', 'interview_recording']"
- `app/services/timesheet_flags/actions.py:93` - Argument "entity_type" to "model_construct" of "TaskCreate" has incompatible type "Literal['placement']"; expected "Literal['client', 'contact', 'demand', 'candidate', 'application', 'lead', 'opportunity'] | None"
- `app/services/timesheet_flags/actions.py:113` - Argument "entity_type" to "model_construct" of "TaskCreate" has incompatible type "Literal['placement']"; expected "Literal['client', 'contact', 'demand', 'candidate', 'application', 'lead', 'opportunity'] | None"
- `app/services/hrms_sync/records.py:49` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, str]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/hrms_sync/records.py:50` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, str]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/workforce/validation.py:175` - Argument 1 to "add" of "set" has incompatible type "Any | str | None"; expected "str"
- `app/services/client_portal/hooks.py:87` - Argument 2 to "where" of "Select" has incompatible type "bool | Any"; expected "ColumnElement[bool] | _HasClauseElement[bool] | SQLCoreOperations[bool] | ExpressionElementRole[bool] | TypedColumnsClauseRole[bool] | Callable[[], ColumnElement[bool]] | LambdaElement"
- `app/services/agent_tools/voice/tools_e58.py:299` - Argument "version" to "ScopeRule" has incompatible type "Callable[[Any], Coroutine[Any, Any, str]]"; expected "Callable[[Any], str] | None"
- `app/services/billing/runs.py:215` - Argument 1 to "Decimal" has incompatible type "Decimal | None"; expected "Decimal | float | str | tuple[int, Sequence[int], int]"
- `app/services/billing/preview.py:166` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, str]]]"; expected "Iterable[tuple[Never, Never]]"
- `app/services/receivables/settlement.py:180` - Argument 1 to "insert" has incompatible type "FromClause"; expected "TableClause | Join | Alias | CTE | type[Any] | Inspectable[_HasClauseElement[Any]] | _HasClauseElement[Any]"
- `app/services/agent_tools/billing/tools_e63.py:311` - Argument "version" to "ScopeRule" has incompatible type "Callable[[Any], Coroutine[Any, Any, str | None]]"; expected "Callable[[Any], str] | None"
- `app/services/receivables/payments.py:153` - Argument 1 to "set" has incompatible type "list[tuple[str, UUID]]"; expected "Iterable[tuple[Literal['invoice', 'debit_note'], UUID]]"
- `app/services/receivables/collections.py:67` - Argument 1 to "insert" has incompatible type "FromClause"; expected "TableClause | Join | Alias | CTE | type[Any] | Inspectable[_HasClauseElement[Any]] | _HasClauseElement[Any]"
- `app/services/receivables/statements.py:75` - Argument 1 to "append" of "list" has incompatible type "tuple[DocRow, int, Any, str | None, str, Decimal]"; expected "tuple[date, int, str, str | None, str, Decimal]"
- `app/services/reporting/p2tables.py:33` - Argument 2 to "column" has incompatible type "TypeEngineMixin"; expected "type[TypeEngine[Never]] | TypeEngine[Never] | None"
- `app/services/reporting/p2tables.py:33` - Argument 2 to "column" has incompatible type "TypeEngineMixin"; expected "type[TypeEngine[Any]] | TypeEngine[Any] | None"
- `app/services/ledger_sync/payments.py:67` - Argument 1 to "_dt" has incompatible type "Any | None"; expected "str"
- `app/services/ledger_sync/payments.py:134` - Argument 1 to "dict" has incompatible type "Sequence[Row[tuple[UUID, str]]]"; expected "Iterable[tuple[UUID, str]]"
- `app/router/placements.py:173` - Argument 2 to "preview_margin" has incompatible type "UUID | None"; expected "UUID"
- `app/router/modules.py:210` - Argument "params" to "request" of "AsyncClient" has incompatible type "list[tuple[str, str]]"; expected "QueryParams | Mapping[str, str | int | float | bool | None | Sequence[str | int | float | bool | None]] | list[tuple[str, str | int | float | bool | None]] | tuple[tuple[str, str | int | float | bool | None], ...] | str | bytes | None"
- `app/router/ecosystem_console.py:106` - Argument 2 to "get_settings" has incompatible type "UUID | None"; expected "UUID"
- `app/router/ecosystem_console.py:121` - Argument 2 to "update_settings" has incompatible type "UUID | None"; expected "UUID"
- `app/router/ecosystem_console.py:139` - Argument 2 to "list_reviews" has incompatible type "UUID | None"; expected "UUID"
- `app/router/ecosystem_console.py:158` - Argument 2 to "list_items" has incompatible type "UUID | None"; expected "UUID"
- `app/router/ecosystem_console.py:173` - Argument 2 to "decide_item" has incompatible type "UUID | None"; expected "UUID"
- `app/router/ecosystem_console.py:185` - Argument 2 to "complete_review" has incompatible type "UUID | None"; expected "UUID"
- `app/router/crm.py:170` - Argument "status" to "list_leads" has incompatible type "list[Literal['new', 'contacted', 'qualified', 'converted', 'disqualified']] | None"; expected "list[str] | None"
- `app/services/agent_tools/receivables/tools_e63.py:417` - Argument "version" to "ScopeRule" has incompatible type "Callable[[Any], Coroutine[Any, Any, str]]"; expected "Callable[[Any], str] | None"

</details>

<details><summary><b>attr-defined</b> - attribute not defined on type (139)</summary>

- `app/services/documents/merge.py:20` - Module "jinja2.sandbox" does not explicitly export attribute "SecurityError"
- `app/core/public_events.py:108` - "type?[pydantic.main.BaseModel]" has no attribute "model_validate"
- `app/core/public_events.py:288` - "type?[pydantic.main.BaseModel]" has no attribute "model_config"
- `app/core/public_api.py:84` - "BaseRoute" has no attribute "include_in_schema"
- `app/services/ecosystem/controls.py:164` - "PlatformEcosystemControls" has no attribute "platform_suspended_at"
- `app/services/ecosystem/controls.py:165` - "PlatformEcosystemControls" has no attribute "platform_suspended_by"
- `app/services/ecosystem/controls.py:166` - "PlatformEcosystemControls" has no attribute "platform_suspended_reason"; maybe "global_suspended_reason"?
- `app/services/tenant_lifecycle_service.py:228` - "AuditLog" has no attribute "_mapping"
- `app/services/custom_domains.py:62` - "Rdata" has no attribute "strings"
- `app/services/bgv_erasure.py:91` - "BgvCaseEvent" has no attribute "file_id"
- `app/services/webhooks/common.py:116` - "type?[pydantic.main.BaseModel]" has no attribute "model_fields"
- `app/services/webhooks/subscriptions.py:194` - "type?[pydantic.main.BaseModel]" has no attribute "model_json_schema"
- `app/services/interviews/ports.py:31` - Module "app.services.pipeline.applications" does not explicitly export attribute "application_for"
- `app/services/interviews/ports.py:69` - Module "app.services.demands" does not explicitly export attribute "get_for"
- `app/services/interviews/ports.py:69` - Module "app.services.demands" does not explicitly export attribute "READ"
- `app/services/offers/templates.py:19` - Module "jinja2.sandbox" does not explicitly export attribute "SecurityError"
- `app/services/offers/templates.py:110` - "Expr" has no attribute "node"
- `app/services/job_network/listings.py:186` - Module "app.services.portal.projection" does not explicitly export attribute "render_markdown"
- `app/services/documents/service.py:57` - Module "app.services.documents.repository" does not explicitly export attribute "tenant_of"
- `app/services/demand_intake/hooks.py:57` - Module "app.services.workflow.registry" does not explicitly export attribute "FieldDef"
- `app/services/offers/__init__.py:53` - Module "app.services.offers.response" does not explicitly export attribute "normalize_name"
- `app/services/receivables/common.py:180` - "Base" has no attribute "status"
- `app/services/receivables/common.py:180` - "Base" has no attribute "doc_date"
- `app/services/messaging/hooks.py:58` - Module "app.services.messaging.sending" does not explicitly export attribute "RecipientRef"
- `app/services/messaging/hooks.py:66` - Module "app.services.messaging.sending" does not explicitly export attribute "RecipientRef"
- `app/services/ledger_sync/push.py:225` - "type[Base]" has no attribute "legal_entity_id"
- `app/services/ledger_sync/push.py:225` - "type[Base]" has no attribute "status"
- `app/services/ledger_sync/push.py:225` - "type[Base]" has no attribute "number"
- `app/services/ledger_sync/push.py:227` - "type[Base]" has no attribute "doc_date"
- `app/services/ledger_sync/push.py:229` - "type[Base]" has no attribute "doc_date"
- `app/services/ledger_sync/push.py:231` - "type[Base]" has no attribute "id"
- `app/services/ledger_sync/push.py:232` - "Base" has no attribute "status"
- `app/services/ledger_sync/push.py:256` - "type[Base]" has no attribute "id"
- `app/services/ledger_sync/push.py:258` - "type[Base]" has no attribute "status"
- `app/services/billing/service.py:82` - "Base" has no attribute "status"
- `app/services/billing/service.py:82` - "Base" has no attribute "doc_date"
- `app/services/billing/service.py:86` - "Base" has no attribute "id"
- `app/services/billing/service.py:87` - "Base" has no attribute "client_id"
- `app/services/billing/service.py:88` - "Base" has no attribute "legal_entity_id"
- `app/services/billing/service.py:89` - "Base" has no attribute "currency"
- `app/services/billing/service.py:90` - "Base" has no attribute "document_total"
- `app/services/billing/service.py:91` - "Base" has no attribute "doc_date"
- `app/services/billing/service.py:92` - "Base" has no attribute "due_date"
- `app/services/billing/service.py:93` - "Base" has no attribute "status"
- `app/services/billing/service.py:94` - "Base" has no attribute "fx_rate"
- `app/services/billing/service.py:95` - "Base" has no attribute "id"
- `app/services/billing/service.py:96` - "Base" has no attribute "inr_document_total"
- `app/services/billing/service.py:97` - "Base" has no attribute "taxable_total"
- `app/services/billing/service.py:109` - "Base" has no attribute "number"
- `app/services/billing/service.py:109` - "Base" has no attribute "doc_date"
- `app/services/billing/service.py:113` - "Base" has no attribute "id"
- `app/services/billing/service.py:114` - "Base" has no attribute "number"
- `app/services/billing/service.py:115` - "Base" has no attribute "client_id"
- `app/services/billing/service.py:116` - "Base" has no attribute "legal_entity_id"
- `app/services/billing/service.py:117` - "Base" has no attribute "currency"
- `app/services/billing/service.py:118` - "Base" has no attribute "document_total"
- `app/services/billing/service.py:119` - "Base" has no attribute "doc_date"
- `app/services/billing/service.py:120` - "Base" has no attribute "due_date"
- `app/services/billing/service.py:121` - "Base" has no attribute "status"
- `app/services/billing/service.py:122` - "Base" has no attribute "fx_rate"
- `app/services/billing/service.py:123` - "Base" has no attribute "inr_document_total"
- `app/services/billing/service.py:124` - "Base" has no attribute "taxable_total"
- `app/services/receivables/accounting_export.py:66` - "Base" has no attribute "id"
- `app/services/receivables/accounting_export.py:66` - "Base" has no attribute "number"
- `app/services/receivables/accounting_export.py:67` - "Base" has no attribute "doc_date"
- `app/services/receivables/accounting_export.py:67` - "Base" has no attribute "id"
- `app/services/receivables/accounting_export.py:67` - "Base" has no attribute "number"
- `app/services/receivables/accounting_export.py:67` - "Base" has no attribute "client_id"
- `app/services/receivables/accounting_export.py:67` - "Base" has no attribute "currency"
- `app/services/receivables/accounting_export.py:68` - "Base" has no attribute "document_total"
- `app/services/receivables/accounting_export.py:68` - "Base" has no attribute "inr_document_total"
- `app/services/receivables/accounting_export.py:68` - "Base" has no attribute "charged_tax_total"
- `app/services/ledger_sync/tally.py:79` - "ScalarResult[Any]" has no attribute "append"
- `app/services/demand_insights/hooks.py:33` - Module "app.services.workflow.registry" does not explicitly export attribute "FieldDef"
- `app/services/vendors/hooks.py:193` - Module "app.services.workflow.registry" does not explicitly export attribute "FieldDef"
- `app/services/placements/rate_segments.py:210` - "RateCheckResult" has no attribute "request_id"
- `app/services/placements/rate_segments.py:211` - "RateCheckResult" has no attribute "request_id"
- `app/services/ledger_sync/records.py:69` - "type[Base]" has no attribute "id"
- `app/services/ledger_sync/records.py:70` - "Base" has no attribute "id"
- `app/services/ledger_sync/records.py:71` - "Base" has no attribute "client_id"
- `app/services/developer_portal/sandboxes.py:257` - "FromClause" has no attribute "delete"
- `app/services/scheduling_agent/reminders.py:396` - "QueueResult" has no attribute "results"
- `app/services/developer_portal/conformance/runner.py:174` - Module "app.services.webhooks.test_events" does not explicitly export attribute "common"
- `app/services/commercials/hooks.py:308` - "type[Base]" has no attribute "id"
- `app/services/billing/invoices.py:512` - "type[Base]" has no attribute "status"
- `app/services/billing/invoices.py:514` - "type[Base]" has no attribute "einvoice_status"
- `app/services/billing/invoices.py:516` - "type[Base]" has no attribute "delivery_status"
- `app/services/billing/invoices.py:518` - "type[Base]" has no attribute "client_id"
- `app/services/billing/invoices.py:522` - "type[Base]" has no attribute "original_invoice_id"
- `app/services/billing/invoices.py:524` - "type[Base]" has no attribute "doc_date"
- `app/services/billing/invoices.py:524` - "type[Base]" has no attribute "preview_date"
- `app/services/billing/invoices.py:526` - "type[Base]" has no attribute "doc_date"
- `app/services/billing/invoices.py:526` - "type[Base]" has no attribute "preview_date"
- `app/services/billing/invoices.py:527` - "type[Base]" has no attribute "created_at"
- `app/services/billing/dsc.py:555` - "Base" has no attribute "recipient_snapshot"
- `app/services/billing/dsc.py:561` - "Base" has no attribute "number"
- `app/services/billing/dsc.py:562` - "Base" has no attribute "client_id"
- `app/services/billing/dsc.py:580` - "type[Base]" has no attribute "id"
- `app/services/billing/dsc.py:581` - "type[Base]" has no attribute "dsc_status"
- `app/services/billing/dsc.py:582` - "type[Base]" has no attribute "status"
- `app/services/billing/dsc.py:583` - "type[Base]" has no attribute "id"
- `app/services/agent_tools/demands/tools_e54.py:108` - "dict[str, Any]" has no attribute "id"
- `app/services/submission_packs/hooks.py:193` - Module "app.services.workflow.registry" does not explicitly export attribute "FieldDef"
- `app/services/scheduling_agent/requests.py:68` - Module "app.services.pipeline.applications" does not explicitly export attribute "application_for"
- `app/services/time/imports.py:268` - Module "app.services.time.timesheets" does not explicitly export attribute "get_timesheet"
- `app/services/mobility/transfers.py:224` - Module "app.services.placements.creation" does not explicitly export attribute "get_placement"
- `app/router/portal_copilot_routes.py:107` - User? has no attribute "tenant_id"
- `app/router/portal_copilot_routes.py:107` - User? has no attribute "id"
- `app/services/billing/notes.py:129` - "Base" has no attribute "id"
- `app/services/billing/notes.py:134` - "Base" has no attribute "line_id"
- `app/services/billing/notes.py:136` - "Base" has no attribute "original_invoice_line_id"
- `app/services/billing/notes.py:139` - "Base" has no attribute "taxable_value"
- `app/services/billing/notes.py:140` - "Base" has no attribute "quantity"
- `app/services/billing/notes.py:141` - "Base" has no attribute "inr_taxable_value"
- `app/services/billing/notes.py:142` - "Base" has no attribute "id"
- `app/services/billing/notes.py:143` - "Base" has no attribute "component"
- `app/services/billing/notes.py:143` - "Base" has no attribute "amount"
- `app/services/billing/notes.py:144` - "Base" has no attribute "component"
- `app/services/billing/notes.py:145` - "Base" has no attribute "inr_amount"
- `app/services/billing/notes.py:319` - "Base" has no attribute "original_invoice_line_id"
- `app/services/billing/notes.py:322` - "Base" has no attribute "taxable_value"
- `app/services/billing/notes.py:323` - "Base" has no attribute "is_manual"
- `app/services/billing/notes.py:330` - "Base" has no attribute "inr_taxable_value"
- `app/services/billing/notes.py:331` - "Base" has no attribute "outside_taxable_value"
- `app/services/billing/notes.py:337` - "Base" has no attribute "id"
- `app/services/timesheet_flags/hooks.py:120` - Module "app.services.workflow.registry" does not explicitly export attribute "FieldDef"
- `app/services/reporting/lists_p4.py:26` - Module "app.services.reporting.p4tables" does not explicitly export attribute "_t"
- `app/services/reporting/metrics/skills.py:11` - Module "app.services.reporting.metrics.p3common" does not explicitly export attribute "need"
- `app/services/reporting/metrics/referrals.py:13` - Module "app.services.reporting.metrics.p3common" does not explicitly export attribute "need"
- `app/services/reporting/metrics/projects.py:12` - Module "app.services.reporting.metrics.p3common" does not explicitly export attribute "need"
- `app/services/reporting/metrics/predictions.py:17` - Module "app.services.reporting.metrics.p3common" does not explicitly export attribute "need"
- `app/services/reporting/metrics/mobility.py:8` - Module "app.services.reporting.metrics.p3common" does not explicitly export attribute "need"
- `app/services/reporting/metrics/marketing.py:12` - Module "app.services.reporting.metrics.p3common" does not explicitly export attribute "need"
- `app/services/reporting/metrics/immigration.py:10` - Module "app.services.reporting.metrics.p3common" does not explicitly export attribute "need"
- `app/services/reporting/metrics/campus.py:11` - Module "app.services.reporting.metrics.p3common" does not explicitly export attribute "need"
- `app/services/collection_rules/facts.py:122` - Module "app.services.collection_rules.plan" does not explicitly export attribute "time"
- `app/services/collection_rules/facts.py:123` - Module "app.services.collection_rules.plan" does not explicitly export attribute "time"
- `app/router/public_v1/events.py:28` - "type?[pydantic.main.BaseModel]" has no attribute "model_json_schema"
- `app/services/collection_rules/hooks.py:168` - Module "app.services.workflow.registry" does not explicitly export attribute "FieldDef"

</details>

<details><summary><b>valid-type</b> - invalid type annotation (84)</summary>

- `app/core/public_events.py:47` - Variable "app.core.public_events.PublicEventSpec.type" is not valid as a type
- `app/core/public_events.py:50` - Variable "app.core.public_events.PublicEventSpec.type" is not valid as a type
- `app/services/documents/repository.py:560` - Function "app.services.documents.repository.AttachmentFacade.list" is not valid as a type
- `app/router/portal_copilot_routes.py:75` - Variable "User" is not valid as a type
- `app/router/portal_copilot_routes.py:82` - Variable "User" is not valid as a type
- `app/router/portal_copilot_routes.py:86` - Variable "User" is not valid as a type
- `app/router/portal_copilot_routes.py:90` - Variable "User" is not valid as a type
- `app/router/portal_copilot_routes.py:95` - Variable "User" is not valid as a type
- `app/router/portal_copilot_routes.py:102` - Variable "User" is not valid as a type
- `app/router/portal_copilot_routes.py:119` - Variable "User" is not valid as a type
- `app/router/portal_copilot_routes.py:128` - Variable "User" is not valid as a type
- `app/router/portal_copilot_routes.py:133` - Variable "User" is not valid as a type
- `app/router/portal_copilot_routes.py:139` - Variable "User" is not valid as a type
- `app/router/portal_copilot_routes.py:146` - Variable "User" is not valid as a type
- `app/services/agent_tools/receivables/tools_e63.py:125` - Variable "app.services.agent_tools.receivables.tools_e63.ActivityRow.date" is not valid as a type
- `app/router/public_v1/workers.py:19` - Variable "app.router.public_v1.workers.Read" is not valid as a type
- `app/router/public_v1/workers.py:24` - Variable "app.router.public_v1.workers.Read" is not valid as a type
- `app/router/public_v1/webhooks.py:33` - Variable "app.router.public_v1.webhooks.Manage" is not valid as a type
- `app/router/public_v1/webhooks.py:39` - Variable "app.router.public_v1.webhooks.Manage" is not valid as a type
- `app/router/public_v1/webhooks.py:47` - Variable "app.router.public_v1.webhooks.Manage" is not valid as a type
- `app/router/public_v1/webhooks.py:54` - Variable "app.router.public_v1.webhooks.Manage" is not valid as a type
- `app/router/public_v1/webhooks.py:65` - Variable "app.router.public_v1.webhooks.Manage" is not valid as a type
- `app/router/public_v1/webhooks.py:71` - Variable "app.router.public_v1.webhooks.Manage" is not valid as a type
- `app/router/public_v1/webhooks.py:77` - Variable "app.router.public_v1.webhooks.Manage" is not valid as a type
- `app/router/public_v1/webhooks.py:86` - Variable "app.router.public_v1.webhooks.Manage" is not valid as a type
- `app/router/public_v1/webhooks.py:93` - Variable "app.router.public_v1.webhooks.Manage" is not valid as a type
- `app/router/public_v1/users.py:19` - Variable "app.router.public_v1.users.Read" is not valid as a type
- `app/router/public_v1/users.py:24` - Variable "app.router.public_v1.users.Read" is not valid as a type
- `app/router/public_v1/timesheets.py:19` - Variable "app.router.public_v1.timesheets.Read" is not valid as a type
- `app/router/public_v1/timesheets.py:24` - Variable "app.router.public_v1.timesheets.Read" is not valid as a type
- `app/router/public_v1/tasks.py:24` - Variable "app.router.public_v1.tasks.Read" is not valid as a type
- `app/router/public_v1/tasks.py:32` - Variable "app.router.public_v1.tasks.Read" is not valid as a type
- `app/router/public_v1/tasks.py:37` - Variable "app.router.public_v1.tasks.Write" is not valid as a type
- `app/router/public_v1/tasks.py:42` - Variable "app.router.public_v1.tasks.Write" is not valid as a type
- `app/router/public_v1/submissions.py:19` - Variable "app.router.public_v1.submissions.Read" is not valid as a type
- `app/router/public_v1/submissions.py:24` - Variable "app.router.public_v1.submissions.Read" is not valid as a type
- `app/router/public_v1/skills.py:19` - Invalid type comment or annotation
- `app/router/public_v1/postings.py:20` - Variable "app.router.public_v1.postings.Read" is not valid as a type
- `app/router/public_v1/postings.py:25` - Variable "app.router.public_v1.postings.Read" is not valid as a type
- `app/router/public_v1/postings.py:30` - Variable "app.router.public_v1.postings.Write" is not valid as a type
- `app/router/public_v1/postings.py:35` - Variable "app.router.public_v1.postings.Write" is not valid as a type
- `app/router/public_v1/pools.py:19` - Variable "app.router.public_v1.pools.Read" is not valid as a type
- `app/router/public_v1/pools.py:24` - Variable "app.router.public_v1.pools.Read" is not valid as a type
- `app/router/public_v1/pools.py:29` - Variable "app.router.public_v1.pools.Read" is not valid as a type
- `app/router/public_v1/placements.py:19` - Variable "app.router.public_v1.placements.Read" is not valid as a type
- `app/router/public_v1/placements.py:24` - Variable "app.router.public_v1.placements.Read" is not valid as a type
- `app/router/public_v1/payments.py:19` - Variable "app.router.public_v1.payments.Read" is not valid as a type
- `app/router/public_v1/payments.py:24` - Variable "app.router.public_v1.payments.Read" is not valid as a type
- `app/router/public_v1/offers.py:19` - Variable "app.router.public_v1.offers.Read" is not valid as a type
- `app/router/public_v1/offers.py:24` - Variable "app.router.public_v1.offers.Read" is not valid as a type
- `app/router/public_v1/notes.py:21` - Invalid type comment or annotation
- `app/router/public_v1/notes.py:28` - Invalid type comment or annotation
- `app/router/public_v1/invoices.py:19` - Variable "app.router.public_v1.invoices.Read" is not valid as a type
- `app/router/public_v1/invoices.py:24` - Variable "app.router.public_v1.invoices.Read" is not valid as a type
- `app/router/public_v1/interviews.py:19` - Variable "app.router.public_v1.interviews.Read" is not valid as a type
- `app/router/public_v1/interviews.py:24` - Variable "app.router.public_v1.interviews.Read" is not valid as a type
- `app/router/public_v1/ingest.py:26` - Variable "app.router.public_v1.ingest.Write" is not valid as a type
- `app/router/public_v1/ingest.py:31` - Variable "app.router.public_v1.ingest.Write" is not valid as a type
- `app/router/public_v1/imports.py:23` - Variable "app.router.public_v1.imports.Write" is not valid as a type
- `app/router/public_v1/imports.py:35` - Variable "app.router.public_v1.imports.Write" is not valid as a type
- `app/router/public_v1/imports.py:40` - Variable "app.router.public_v1.imports.Write" is not valid as a type
- `app/router/public_v1/imports.py:45` - Variable "app.router.public_v1.imports.Write" is not valid as a type
- `app/router/public_v1/expenses.py:19` - Variable "app.router.public_v1.expenses.Read" is not valid as a type
- `app/router/public_v1/expenses.py:24` - Variable "app.router.public_v1.expenses.Read" is not valid as a type
- `app/router/public_v1/demands.py:23` - Variable "app.router.public_v1.demands.Read" is not valid as a type
- `app/router/public_v1/demands.py:30` - Variable "app.router.public_v1.demands.Read" is not valid as a type
- `app/router/public_v1/demands.py:35` - Variable "app.router.public_v1.demands.Write" is not valid as a type
- `app/router/public_v1/credit_notes.py:19` - Variable "app.router.public_v1.credit_notes.Read" is not valid as a type
- `app/router/public_v1/credit_notes.py:24` - Variable "app.router.public_v1.credit_notes.Read" is not valid as a type
- `app/router/public_v1/contacts.py:19` - Variable "app.router.public_v1.contacts.Read" is not valid as a type
- `app/router/public_v1/contacts.py:24` - Variable "app.router.public_v1.contacts.Read" is not valid as a type
- `app/router/public_v1/clients.py:19` - Variable "app.router.public_v1.clients.Read" is not valid as a type
- `app/router/public_v1/clients.py:24` - Variable "app.router.public_v1.clients.Read" is not valid as a type
- `app/router/public_v1/candidates.py:31` - Variable "app.router.public_v1.candidates.Read" is not valid as a type
- `app/router/public_v1/candidates.py:39` - Variable "app.router.public_v1.candidates.Read" is not valid as a type
- `app/router/public_v1/candidates.py:46` - Variable "app.router.public_v1.candidates.Write" is not valid as a type
- `app/router/public_v1/candidates.py:51` - Variable "app.router.public_v1.candidates.Write" is not valid as a type
- `app/router/public_v1/candidates.py:57` - Variable "app.router.public_v1.candidates.Write" is not valid as a type
- `app/router/public_v1/applications.py:24` - Variable "app.router.public_v1.applications.Read" is not valid as a type
- `app/router/public_v1/applications.py:31` - Variable "app.router.public_v1.applications.Read" is not valid as a type
- `app/router/public_v1/applications.py:38` - Variable "app.router.public_v1.applications.Write" is not valid as a type
- `app/router/public_v1/applications.py:43` - Variable "app.router.public_v1.applications.Write" is not valid as a type
- `app/router/public_v1/activities.py:21` - Invalid type comment or annotation
- `app/router/public_v1/activities.py:28` - Invalid type comment or annotation

</details>

<details><summary><b>no-any-return</b> - returning Any from a typed function (83)</summary>

- `app/services/workflow/conditions.py:242` - Returning Any from function declared to return "bool"
- `app/services/sso/oidc.py:47` - Returning Any from function declared to return "str"
- `app/services/sso/oidc.py:51` - Returning Any from function declared to return "str"
- `app/core/money.py:137` - Returning Any from function declared to return "Money"
- `app/services/placements/conversion_fee.py:38` - Returning Any from function declared to return "int"
- `app/services/mcp_server/limits.py:117` - Returning Any from function declared to return "int"
- `app/services/mcp_server/limits.py:121` - Returning Any from function declared to return "int"
- `app/services/mcp_server/limits.py:126` - Returning Any from function declared to return "int"
- `app/core/semantic/operators.py:227` - Returning Any from function declared to return "ColumnElement[bool]"
- `app/core/semantic/operators.py:229` - Returning Any from function declared to return "ColumnElement[bool]"
- `app/core/semantic/operators.py:231` - Returning Any from function declared to return "ColumnElement[bool]"
- `app/core/semantic/operators.py:235` - Returning Any from function declared to return "ColumnElement[bool]"
- `app/core/semantic/operators.py:237` - Returning Any from function declared to return "ColumnElement[bool]"
- `app/core/semantic/operators.py:239` - Returning Any from function declared to return "ColumnElement[bool]"
- `app/core/semantic/operators.py:241` - Returning Any from function declared to return "ColumnElement[bool]"
- `app/core/semantic/operators.py:243` - Returning Any from function declared to return "ColumnElement[bool]"
- `app/core/semantic/operators.py:245` - Returning Any from function declared to return "ColumnElement[bool]"
- `app/core/semantic/operators.py:247` - Returning Any from function declared to return "ColumnElement[bool]"
- `app/core/semantic/operators.py:252` - Returning Any from function declared to return "ColumnElement[bool]"
- `app/services/connectors/accounting/zoho_books.py:83` - Returning Any from function declared to return "Mapping[str, Any]"
- `app/core/semantic/compiler.py:468` - Returning Any from function declared to return "Principal | None"
- `app/services/sso/service.py:126` - Returning Any from function declared to return "dict[str, Any]"
- `app/services/sso/service.py:131` - Returning Any from function declared to return "dict[str, Any]"
- `app/services/interviews/ics.py:96` - Returning Any from function declared to return "bytes"
- `app/services/tasks_service.py:88` - Returning Any from function declared to return "Task"
- `app/services/custom_domains.py:196` - Returning Any from function declared to return "CustomDomain | None"
- `app/services/public_api/engagement.py:122` - Returning Any from function declared to return "Task"
- `app/services/job_distribution/mapping.py:154` - Returning Any from function declared to return "FieldSchemaDTO"
- `app/services/connections_service.py:243` - Returning Any from function declared to return "bool"
- `app/services/privacy/retention_bindings.py:94` - Returning Any from function declared to return "date | None"
- `app/services/placements/lists.py:70` - Returning Any from function declared to return "Placement"
- `app/services/commercials/rate_cards.py:259` - Returning Any from function declared to return "RateCard | None"
- `app/services/analytics/features/__init__.py:19` - Returning Any from function declared to return "list[TrainingRow]"
- `app/services/analytics/features/__init__.py:34` - Returning Any from function declared to return "list[ScoringRow]"
- `app/services/webhooks/sink.py:27` - Returning Any from function declared to return "UUID | None"
- `app/services/immigration/cases.py:98` - Returning Any from function declared to return "ImmigrationCase"
- `app/services/immigration/cases.py:280` - Returning Any from function declared to return "UUID | None"
- `app/services/analytics/training.py:215` - Returning Any from function declared to return "dict[Any, Any]"
- `app/services/search/candidate_search.py:256` - Returning Any from function declared to return "ColumnElement[bool] | None"
- `app/services/screening_agent/questionnaires.py:184` - Returning Any from function declared to return "bool"
- `app/services/public_api/candidates.py:96` - Returning Any from function declared to return "Candidate"
- `app/services/public_api/applications.py:80` - Returning Any from function declared to return "Application"
- `app/services/offers/common.py:58` - Returning Any from function declared to return "Literal['user', 'candidate', 'system', 'workflow', 'ai', 'api_key', 'agent', 'app']"
- `app/services/offers/common.py:159` - Returning Any from function declared to return "Offer"
- `app/services/crm/opportunities.py:116` - Returning Any from function declared to return "Opportunity"
- `app/services/reporting/lists.py:415` - Returning Any from function declared to return "ColumnElement[bool] | None"
- `app/services/portal/self_service.py:340` - Returning Any from function declared to return "UUID"
- `app/services/portal/apply.py:71` - Returning Any from function declared to return "str | None"
- `app/tasks/portal.py:60` - Returning Any from function declared to return "dict[str, object]"
- `app/tasks/portal.py:67` - Returning Any from function declared to return "dict[str, object]"
- `app/services/marketplace/review.py:59` - Returning Any from function declared to return "UUID"
- `app/services/crm/leads.py:89` - Returning Any from function declared to return "Lead"
- `app/services/marketplace/installs.py:86` - Returning Any from function declared to return "UUID"
- `app/services/ecosystem_governance/search.py:87` - Returning Any from function declared to return "list[QuickHit]"
- `app/services/job_network/moderation.py:37` - Returning Any from function declared to return "UUID"
- `app/services/placements/creation.py:93` - Returning Any from function declared to return "str | None"
- `app/services/developer_portal/sandboxes.py:84` - Returning Any from function declared to return "DeveloperSandbox"
- `app/services/agent_tools/interview_capture/common_e60.py:34` - Returning Any from function declared to return "tuple[str, UUID] | None"
- `app/services/projects/portal.py:58` - Returning Any from function declared to return "Project"
- `app/services/portal_copilot/store.py:78` - Returning Any from function declared to return "PortalCopilotConversation"
- `app/services/campaign_copy/apply.py:102` - Returning Any from function declared to return "UUID"
- `app/tasks/campaigns.py:197` - Returning Any from function declared to return "str | None"
- `app/services/agent_tools/core/tools_e53.py:73` - Returning Any from function declared to return "str"
- `app/services/time/rate_adjustments.py:38` - Returning Any from function declared to return "Decimal | None"
- `app/services/time/rate_adjustments.py:39` - Returning Any from function declared to return "Decimal | None"
- `app/services/placements/offboarding.py:744` - Returning Any from function declared to return "OffboardingCaseDetail"
- `app/router/public.py:174` - Returning Any from function declared to return "CandidateOfferView"
- `app/router/public.py:179` - Returning Any from function declared to return "OfferActionOut"
- `app/router/public.py:194` - Returning Any from function declared to return "OfferActionOut"
- `app/services/agent_tools/sourcing/tools_e56.py:369` - Returning Any from function declared to return "str | None"
- `app/router/portal_interview_consent.py:22` - Returning Any from function declared to return "UUID"
- `app/services/agent_tools/scheduling/common_e59.py:31` - Returning Any from function declared to return "UUID | None"
- `app/router/portal_scheduling.py:31` - Returning Any from function declared to return "UUID"
- `app/services/agent_tools/interviews/tools_e59.py:199` - Returning Any from function declared to return "UUID | None"
- `app/services/receivables/portal.py:21` - Returning Any from function declared to return "UUID"
- `app/services/reporting/metrics/p2common.py:52` - Returning Any from function declared to return "ColumnElement[bool] | None"
- `app/services/reporting/metrics/p3common.py:48` - Returning Any from function declared to return "float"
- `app/router/privacy.py:260` - Returning Any from function declared to return "RetentionClassOut"
- `app/router/privacy.py:267` - Returning Any from function declared to return "RetentionClassOut"
- `app/router/privacy.py:272` - Returning Any from function declared to return "RetentionClassOut"
- `app/router/oauth.py:64` - Returning Any from function declared to return "Response"
- `app/router/marketplace.py:217` - Returning Any from function declared to return "PolicyOut"
- `app/services/agent_tools/receivables/tools_e63.py:86` - Returning Any from function declared to return "str"

</details>

<details><summary><b>assignment</b> - incompatible assignment (65)</summary>

- `app/services/job_network/checks.py:64` - Incompatible types in assignment (expression has type "Match[str] | None", variable has type "Match[str]")
- `app/services/job_network/checks.py:67` - Incompatible types in assignment (expression has type "Match[str] | None", variable has type "Match[str]")
- `app/services/fairness/suppression.py:63` - Incompatible types in assignment (expression has type "list[tuple[tuple[str, str], Cell]]", variable has type "dict[str, Cell]")
- `app/models/skills_graph.py:42` - Incompatible types in assignment (expression has type "tuple[str, str, str, str, str, str, str, str]", variable has type "tuple[str, str, str, str, str, str]")
- `app/models/reporting.py:62` - Incompatible types in assignment (expression has type tuple[str, str, ... <29 more items>], variable has type tuple[str, str, str, str, str, str])
- `app/schemas/analytics.py:46` - Incompatible types in assignment (expression has type "str", variable has type "Literal['Supporting signal']")
- `app/services/campaign_copy/checks.py:329` - Incompatible types in assignment (expression has type "str", variable has type "Decimal")
- `app/services/billing/tax.py:125` - Incompatible types in assignment (expression has type "str | None", variable has type "str")
- `app/core/semantic/compiler.py:523` - Incompatible types in assignment (expression has type "str", variable has type "Literal['string', 'int', 'decimal', 'bool', 'date', 'datetime', 'uuid', 'enum', 'string_array', 'skill']")
- `app/services/ecosystem/controls.py:163` - Incompatible types in assignment (expression has type "EcosystemSettings", variable has type "PlatformEcosystemControls")
- `app/services/campaigns/settings.py:196` - Incompatible types in assignment (expression has type "str", variable has type "Literal['email', 'sms', 'whatsapp']")
- `app/services/tenant_lifecycle_service.py:404` - Incompatible types in assignment (expression has type "Tenant | None", variable has type "Tenant")
- `app/models/fairness.py:56` - Incompatible types in assignment (expression has type "tuple[str, str, str, str, str, str, str]", variable has type "tuple[str, str, str, str]")
- `app/models/fairness.py:61` - Incompatible types in assignment (expression has type "tuple[str, str, str, str, str, str, str, str]", variable has type "tuple[str, str, str, str]")
- `app/services/vendors_erasure.py:40` - Incompatible types in assignment (expression has type "ColumnElement[bool]", variable has type "BinaryExpression[bool]")
- `app/services/start_gates_erasure.py:25` - Incompatible types in assignment (expression has type "BooleanClauseList", variable has type "BinaryExpression[bool]")
- `app/services/mobility_erasure.py:42` - Incompatible types in assignment (expression has type "ColumnElement[bool]", variable has type "BooleanClauseList")
- `app/services/bgv_erasure.py:88` - Incompatible types in assignment (expression has type "BgvEvidence", variable has type "BgvCaseEvent")
- `app/services/placements/common.py:110` - Incompatible default for argument "candidate_ids" (default has type "frozenset[Never]", argument has type "set[UUID]")
- `app/services/knowledge/sealing.py:89` - Incompatible types in assignment (expression has type "str", variable has type "None")
- `app/services/knowledge/sealing.py:92` - Incompatible types in assignment (expression has type "str", variable has type "None")
- `app/services/knowledge/sealing.py:95` - Incompatible types in assignment (expression has type "str", variable has type "None")
- `app/services/knowledge/sealing.py:97` - Incompatible types in assignment (expression has type "str", variable has type "None")
- `app/services/webhooks/delivery.py:107` - Incompatible types in assignment (expression has type "str | None", variable has type "str")
- `app/services/external_auth/access.py:133` - Incompatible types in assignment (expression has type "tuple[Literal['demand', 'placement'], UUID]", variable has type "ClientUserScopeIn")
- `app/services/notifications_service.py:689` - Incompatible types in assignment (expression has type "UUID", target has type "None")
- `app/services/matching/handlers.py:252` - Incompatible types in assignment (expression has type "type[MatchDismissed] | type[MatchRestored] | type[MatchAddedToPipeline]", variable has type "type[CandidateCreated] | type[CandidateUpdated] | type[CandidateSkillsChanged] | type[CandidateResumeParsed] | type[AIConsentGranted]")
- `app/services/search/candidate_search.py:265` - Incompatible types in assignment (expression has type "ColumnElement[Any]", variable has type "Function[Any]")
- `app/services/pipeline/submissions.py:164` - Incompatible types in assignment (expression has type "str | None", variable has type "Literal['hour', 'day', 'month', 'year'] | None")
- `app/services/pipeline/applications.py:604` - Incompatible types in assignment (expression has type "Submission | None", variable has type "Submission")
- `app/services/interviews/scheduling.py:271` - Incompatible types in assignment (expression has type "str | None", variable has type "str")
- `app/services/platform_service.py:180` - Incompatible types in assignment (expression has type "list[PlatformNotice]", variable has type "ScalarResult[PlatformNotice]")
- `app/services/privacy/retention.py:268` - Incompatible types in assignment (expression has type "RetentionRunItem | None", variable has type "RetentionRunItem")
- `app/services/sandbox/seed.py:241` - Incompatible types in assignment (expression has type "Any | Literal[40]", variable has type "list[int]")
- `app/services/analytics/workflow_policy.py:45` - Incompatible types in assignment (expression has type "Row[tuple[UUID | None, UUID | None]] | None", variable has type "Row[tuple[UUID, UUID]] | None")
- `app/services/workforce/conversion.py:60` - Incompatible types in assignment (expression has type "str", variable has type "SkillKey")
- `app/services/telephony/costs.py:98` - Incompatible types in assignment (expression has type "InstrumentedAttribute[UUID | None]", variable has type "InstrumentedAttribute[UUID]")
- `app/services/telephony/costs.py:100` - Incompatible types in assignment (expression has type "Function[Any]", variable has type "InstrumentedAttribute[UUID]")
- `app/services/mobility/employees.py:227` - Incompatible types in assignment (expression has type "str | None", variable has type "str")
- `app/services/integrations/runner.py:461` - Incompatible types in assignment (expression has type "SyncJobSpec | None", variable has type "SyncJobSpec")
- `app/services/connectors/jobs.py:203` - Incompatible types in assignment (expression has type "ConnectorAuthError | ConnectorPermanentError", variable has type "ConnectorRateLimited | None")
- `app/services/connectors/jobs.py:210` - Incompatible types in assignment (expression has type "Exception", variable has type "ConnectorRateLimited | None")
- `app/services/documents/esign.py:367` - Incompatible types in assignment (expression has type "DocumentVersion | None", variable has type "DocumentVersion")
- `app/services/documents/esign.py:368` - Incompatible types in assignment (expression has type "Document | None", variable has type "Document")
- `app/services/crm/conversion.py:145` - Incompatible types in assignment (expression has type "Select[tuple[UUID]]", variable has type "Select[tuple[UUID | None]]")
- `app/services/agent_runtime/executor.py:938` - Incompatible types in assignment (expression has type "str | None", variable has type "str")
- `app/services/agent_runtime/executor.py:962` - Incompatible types in assignment (expression has type "str | None", variable has type "str")
- `app/services/agent_runtime/executor.py:962` - Incompatible types in assignment (expression has type "dict[str, Any] | None", variable has type "dict[str, Any]")
- `app/services/receivables/tds_recon/runs.py:624` - Incompatible types in assignment (expression has type "TdsReconMatch | None", variable has type "TdsReconMatch")
- `app/services/agent_runtime/runs.py:217` - Incompatible types in assignment (expression has type "Principal", variable has type "OwnerRef | None")
- `app/services/ledger_sync/push.py:237` - Incompatible types in assignment (expression has type "Select[tuple[Payment]]", variable has type "Select[tuple[Base]]")
- `app/services/vendors/hooks.py:339` - Incompatible types in assignment (expression has type "type[PlacementCreated] | type[PlacementStatusChanged] | type[PlacementDropped]", variable has type "type[InterviewScheduled] | type[InterviewRescheduled] | type[InterviewCancelled] | type[InterviewCompleted]")
- `app/services/screening_agent/conversations.py:293` - Incompatible types in assignment (expression has type "Application | None", variable has type "Application")
- `app/services/placements/rate_segments.py:207` - Incompatible types in assignment (expression has type "StartResult", variable has type "RateCheckResult")
- `app/services/ledger_sync/records.py:79` - Incompatible types in assignment (expression has type "object", variable has type "Base")
- `app/services/knowledge/qa.py:311` - Incompatible types in assignment (expression has type "str", variable has type "None")
- `app/services/scheduling_agent/reminders.py:391` - Incompatible types in assignment (expression has type "SendMessagesOut", variable has type "QueueResult")
- `app/services/agent_tools/pipeline/tools_e54.py:79` - Incompatible types in assignment (expression has type "CandidateApplicationOut", variable has type "ApplicationOut")
- `app/services/time/timesheets.py:321` - Incompatible types in assignment (expression has type "tuple[date, str]", variable has type "tuple[date, Literal['regular', 'ot', 'double_time', 'holiday', 'leave_paid', 'leave_unpaid']]")
- `app/services/time/timesheets.py:327` - Incompatible types in assignment (expression has type "tuple[date, str]", variable has type "tuple[date, Literal['regular', 'ot', 'double_time', 'holiday', 'leave_paid', 'leave_unpaid']]")
- `app/services/time/adjustments.py:79` - Incompatible types in assignment (expression has type "tuple[date, str]", variable has type "tuple[date, Literal['regular', 'ot', 'double_time', 'holiday', 'leave_paid', 'leave_unpaid']]")
- `app/services/client_portal/approvals.py:176` - Incompatible types in assignment (expression has type "dict[str, Any]", variable has type "ClientTimesheetSummary")
- `app/services/telephony/calls.py:208` - Incompatible types in assignment (expression has type "UUID | None", variable has type "UUID")
- `app/services/billing/runs.py:507` - Incompatible types in assignment (expression has type "UUID | None", variable has type "UUID")
- `app/services/receivables/statements.py:84` - Incompatible types in assignment (expression has type "date", variable has type "DocRow")

</details>

<details><summary><b>misc</b> - miscellaneous (50)</summary>

- `app/services/fairness/suppression.py:64` - Unpacking a string is disallowed
- `app/services/fairness/suppression.py:68` - Unpacking a string is disallowed
- `app/services/fairness/suppression.py:77` - Unpacking a string is disallowed
- `app/services/documents/render.py:132` - Class cannot subclass "Canvas" (has type "Any")
- `app/services/copilot/streaming.py:89` - List comprehension has incompatible type List[tuple[bytes | str | Any | None, bytes | str | Any, dict[str, Any]]]; expected List[tuple[str, str, dict[str, Any]]]
- `app/services/copilot/streaming.py:147` - Unpacking a string is disallowed
- `app/services/public_events/catalogue_v1.py:310` - Cannot infer type of lambda
- `app/services/tasks_service.py:134` - Set comprehension has incompatible type Set[UUID | None]; expected Set[UUID]
- `app/tasks/maintenance.py:118` - Untyped decorator makes function "purge_sessions" untyped
- `app/tasks/maintenance.py:123` - Untyped decorator makes function "purge_password_resets" untyped
- `app/tasks/maintenance.py:128` - Untyped decorator makes function "purge_invitations" untyped
- `app/tasks/maintenance.py:133` - Untyped decorator makes function "purge_secure_links" untyped
- `app/tasks/maintenance.py:138` - Untyped decorator makes function "purge_api_key_request_log" untyped
- `app/tasks/maintenance.py:154` - Untyped decorator makes function "purge_idempotency_keys" untyped
- `app/tasks/maintenance.py:194` - Untyped decorator makes function "purge_draft_resumes" untyped
- `app/tasks/external_auth.py:16` - Untyped decorator makes function "revoke_vendor_sessions" untyped
- `app/tasks/platform.py:24` - Untyped decorator makes function "expire_support_grants" untyped
- `app/tasks/platform.py:35` - Untyped decorator makes function "export_tenant" untyped
- `app/tasks/platform.py:43` - Untyped decorator makes function "process_tenant_deletions" untyped
- `app/tasks/platform.py:50` - Untyped decorator makes function "notify_budget_alert" untyped
- `app/tasks/platform.py:83` - Untyped decorator makes function "resume_budget_paused" untyped
- `app/tasks/platform.py:97` - Untyped decorator makes function "send_incident_emails" untyped
- `app/tasks/platform.py:114` - Untyped decorator makes function "notify_sub_processor" untyped
- `app/tasks/matching.py:174` - Cannot infer type of lambda
- `app/services/privacy/class_expiry.py:208` - "record" gets multiple values for keyword argument "action"
- `app/tasks/privacy.py:31` - Untyped decorator makes function "purge_ai_derived" untyped
- `app/tasks/privacy.py:42` - Untyped decorator makes function "consent_withdrawal_sweep" untyped
- `app/tasks/privacy.py:54` - Untyped decorator makes function "recompute_ai_consent_state" untyped
- `app/tasks/privacy.py:65` - Untyped decorator makes function "run_erasure" untyped
- `app/tasks/privacy.py:71` - Untyped decorator makes function "build_subject_export" untyped
- `app/tasks/privacy.py:77` - Untyped decorator makes function "dsr_due_scan" untyped
- `app/tasks/privacy.py:86` - Untyped decorator makes function "execute_retention_run" untyped
- `app/tasks/privacy.py:122` - Untyped decorator makes function "retention_tick" untyped
- `app/tasks/privacy.py:128` - Untyped decorator makes function "retention_expiry_scan" untyped
- `app/tasks/privacy.py:138` - Untyped decorator makes function "record_engagement_consent" untyped
- `app/tasks/portal.py:22` - Untyped decorator makes function "refresh_posting_search" untyped
- `app/tasks/portal.py:34` - Untyped decorator makes function "send_apply_confirmation" untyped
- `app/tasks/portal.py:40` - Untyped decorator makes function "send_candidate_sign_in_link" untyped
- `app/tasks/portal.py:46` - Untyped decorator makes function "build_candidate_export" untyped
- `app/tasks/portal.py:52` - Untyped decorator makes function "notify_privacy_contact" untyped
- `app/tasks/portal.py:58` - Untyped decorator makes function "purge_apply_sessions" untyped
- `app/tasks/portal.py:65` - Untyped decorator makes function "expire_exports" untyped
- `app/tasks/portal.py:74` - Untyped decorator makes function "alert_stale_exports" untyped
- `app/services/ecosystem_governance/erasure_fanout.py:117` - Too many positional arguments for "find_link"
- `app/services/agent_runtime/governance/search.py:26` - List comprehension has incompatible type List[str | None]; expected List[str]
- `app/tasks/messaging.py:19` - Untyped decorator makes function "sync_replies" untyped
- `app/tasks/messaging.py:30` - Untyped decorator makes function "release_deferred" untyped
- `app/tasks/messaging.py:41` - Untyped decorator makes function "job_failed" untyped
- `app/tasks/messaging.py:56` - Untyped decorator makes function "check_custom_domains" untyped
- `app/services/agent_tools/demands/tools_e54.py:105` - List comprehension has incompatible type List[DemandRow]; expected List[dict[str, Any]]

</details>

<details><summary><b>var-annotated</b> - variable needs a type annotation (47)</summary>

- `app/services/billing/words.py:104` - Need type annotation for "groups" (hint: "groups: list[<type>] = ...")
- `app/core/semantic/compiler.py:85` - Need type annotation for "_TRUNC"
- `app/core/semantic/compiler.py:91` - Need type annotation for "_FY_SHIFT"
- `app/core/semantic/compiler.py:503` - Need type annotation for "group_ordinals" (hint: "group_ordinals: list[<type>] = ...")
- `app/services/collection_rules/log.py:163` - Need type annotation for "human" (hint: "human: dict[<type>, <type>] = ...")
- `app/services/timesheet_flags/nudges.py:140` - Need type annotation for "last" (hint: "last: dict[<type>, <type>] = ...")
- `app/services/projects/timeline.py:182` - Need type annotation for "cand" (hint: "cand: dict[<type>, <type>] = ...")
- `app/services/webhooks/scheduler.py:95` - Need type annotation for "inflight" (hint: "inflight: dict[<type>, <type>] = ...")
- `app/services/oauth/apps.py:572` - Need type annotation for "last_used" (hint: "last_used: dict[<type>, <type>] = ...")
- `app/services/knowledge/sources.py:87` - Need type annotation for "counts" (hint: "counts: dict[<type>, <type>] = ...")
- `app/services/immigration/visa_types.py:51` - Need type annotation for "adopted" (hint: "adopted: dict[<type>, <type>] = ...")
- `app/services/immigration/visa_types.py:270` - Need type annotation for "versions" (hint: "versions: dict[<type>, <type>] = ...")
- `app/services/search/projection.py:25` - Need type annotation for "CFG"
- `app/services/search/candidate_search.py:73` - Need type annotation for "CFG"
- `app/services/vendors/review.py:182` - Need type annotation for "owners"
- `app/services/skills/evidence.py:202` - Need type annotation for "counts" (hint: "counts: dict[<type>, <type>] = ...")
- `app/services/interviews/portal.py:79` - Need type annotation for "titles" (hint: "titles: dict[<type>, <type>] = ...")
- `app/services/demand_intake/receive.py:241` - Need type annotation for "scans" (hint: "scans: list[<type>] = ...")
- `app/services/submission_packs/service.py:811` - Need type annotation for "entered" (hint: "entered: dict[<type>, <type>] = ...")
- `app/services/projects/deliverables.py:75` - Need type annotation for "names" (hint: "names: dict[<type>, <type>] = ...")
- `app/services/ledger_sync/records.py:77` - Need type annotation for "names" (hint: "names: dict[<type>, <type>] = ...")
- `app/services/ledger_sync/records.py:174` - Need type annotation for "names" (hint: "names: dict[<type>, <type>] = ...")
- `app/services/bgv/cases.py:501` - Need type annotation for "clients" (hint: "clients: dict[<type>, <type>] = ...")
- `app/services/projects/portal.py:85` - Need type annotation for "waiting" (hint: "waiting: dict[<type>, <type>] = ...")
- `app/services/projects/portal.py:155` - Need type annotation for "names" (hint: "names: dict[<type>, <type>] = ...")
- `app/services/time/timesheets.py:821` - Need type annotation for "cn" (hint: "cn: dict[<type>, <type>] = ...")
- `app/services/time/timesheets.py:828` - Need type annotation for "cl" (hint: "cl: dict[<type>, <type>] = ...")
- `app/services/time/timesheets.py:833` - Need type annotation for "pl" (hint: "pl: dict[<type>, <type>] = ...")
- `app/services/time/timesheets.py:848` - Need type annotation for "hours" (hint: "hours: dict[<type>, <type>] = ...")
- `app/services/time/monitoring.py:188` - Need type annotation for "cn" (hint: "cn: dict[<type>, <type>] = ...")
- `app/services/time/monitoring.py:195` - Need type annotation for "cl" (hint: "cl: dict[<type>, <type>] = ...")
- `app/services/projects/assignments.py:46` - Need type annotation for "role_titles" (hint: "role_titles: dict[<type>, <type>] = ...")
- `app/services/projects/assignments.py:55` - Need type annotation for "placements" (hint: "placements: dict[<type>, <type>] = ...")
- `app/services/projects/assignments.py:65` - Need type annotation for "names" (hint: "names: dict[<type>, <type>] = ...")
- `app/services/projects/projects.py:52` - Need type annotation for "clients" (hint: "clients: dict[<type>, <type>] = ...")
- `app/services/projects/projects.py:58` - Need type annotation for "contracts" (hint: "contracts: dict[<type>, <type>] = ...")
- `app/services/projects/projects.py:243` - Need type annotation for "burn" (hint: "burn: dict[<type>, <type>] = ...")
- `app/services/projects/overview.py:244` - Need type annotation for "names" (hint: "names: dict[<type>, <type>] = ...")
- `app/services/onboarding/cases.py:577` - Need type annotation for "cands" (hint: "cands: dict[<type>, <type>] = ...")
- `app/services/onboarding/cases.py:584` - Need type annotation for "clients" (hint: "clients: dict[<type>, <type>] = ...")
- `app/services/onboarding/cases.py:761` - Need type annotation for "counts" (hint: "counts: dict[<type>, <type>] = ...")
- `app/services/hrms_sync/records.py:49` - Need type annotation for "names" (hint: "names: dict[<type>, <type>] = ...")
- `app/services/hrms_sync/records.py:50` - Need type annotation for "hids" (hint: "hids: dict[<type>, <type>] = ...")
- `app/services/billing/notes.py:324` - Need type annotation for "comps" (hint: "comps: list[<type>] = ...")
- `app/services/billing/preview.py:165` - Need type annotation for "names" (hint: "names: dict[<type>, <type>] = ...")
- `app/services/reporting/p2tables.py:30` - Need type annotation for "cols" (hint: "cols: list[<type>] = ...")
- `app/services/reporting/metrics/delivery.py:108` - Need type annotation for "rows"

</details>

<details><summary><b>unreachable</b> - unreachable code (21)</summary>

- `app/core/database.py:301` - Statement is unreachable
- `app/core/money.py:58` - Statement is unreachable
- `app/services/postings.py:160` - Statement is unreachable
- `app/services/postings.py:207` - Statement is unreachable
- `app/core/kms.py:134` - Statement is unreachable
- `app/core/integrations/registry.py:103` - Statement is unreachable
- `app/core/integrations/registry.py:189` - Statement is unreachable
- `app/services/approvals/service.py:475` - Right operand of "and" is never evaluated
- `app/services/approvals/service.py:478` - Right operand of "and" is never evaluated
- `app/core/encrypted.py:154` - Statement is unreachable
- `app/services/connections/microsoft.py:425` - Statement is unreachable
- `app/services/knowledge/sealing.py:87` - Statement is unreachable
- `app/services/webhooks/delivery.py:67` - Statement is unreachable
- `app/services/notifications_service.py:651` - Statement is unreachable
- `app/services/pipeline/links.py:48` - Statement is unreachable
- `app/services/demand_intake/receive.py:262` - Right operand of "or" is never evaluated
- `app/services/agent_runtime/runs.py:223` - Statement is unreachable
- `app/services/submission_packs/service.py:763` - Statement is unreachable
- `app/services/time/leave_balances.py:100` - Subclass of "Decimal" and "float" cannot exist: would have incompatible method signatures
- `app/services/time/leave_balances.py:101` - Statement is unreachable
- `app/services/time/leave_balances.py:286` - Statement is unreachable

</details>

<details><summary><b>union-attr</b> - attr on Optional/Union (16)</summary>

- `app/services/assessments/scoring.py:220` - Item "None" of "Any | dict[Any, Any] | None" has no attribute "get"
- `app/services/assessments/scoring.py:240` - Item "None" of "Any | list[Any] | None" has no attribute "__iter__" (not iterable)
- `app/services/copilot/streaming.py:89` - Item "None" of "dict[bytes | str, bytes | str] | Any | None" has no attribute "get"
- `app/services/copilot/streaming.py:89` - Item "None" of "list[tuple[bytes | str | None, dict[bytes | str, bytes | str] | None]] | None" has no attribute "__iter__" (not iterable)
- `app/services/copilot/streaming.py:148` - Item "int" of "Any | int" has no attribute "__iter__" (not iterable)
- `app/services/assessments/assembly.py:58` - Item "float" of "Decimal | float" has no attribute "quantize"
- `app/services/job_distribution/mapping.py:128` - Item "None" of "LocationDTO | None" has no attribute "city"
- `app/services/bgv/retention.py:38` - Item "None" of "date | None" has no attribute "isoformat"
- `app/services/analytics/hooks.py:89` - Item "None" of "Any | None" has no attribute "task_always_eager"
- `app/services/job_network/listings.py:158` - Item "None" of "Demand | None" has no attribute "status"
- `app/services/job_network/listings.py:159` - Item "None" of "Demand | None" has no attribute "archived_at"
- `app/services/job_network/listings.py:160` - Item "None" of "Demand | None" has no attribute "visibility"
- `app/services/job_network/listings.py:163` - Item "None" of "Demand | None" has no attribute "visibility"
- `app/services/job_network/listings.py:163` - Item "None" of "Demand | None" has no attribute "client_id"
- `app/services/campaigns/audiences.py:310` - Item "None" of "Any | None" has no attribute "strip"
- `app/services/agent_tools/placements/tools_e54.py:52` - Item "None" of "date | None" has no attribute "isoformat"

</details>

<details><summary><b>index</b> - bad index type (10)</summary>

- `app/services/matching/ports.py:90` - Invalid index type "str | Any | None" for "dict[str, Callable[[AsyncSession, Sequence[tuple[UUID, UUID]]], Awaitable[Mapping[tuple[UUID, UUID], str] | AbstractSet[tuple[UUID, UUID]]]]]"; expected type "str"
- `app/services/assessments/scoring.py:242` - Value of type "Any | list[Any] | None" is not indexable
- `app/services/external_auth/access.py:140` - Value of type "ClientUserScopeIn" is not indexable
- `app/services/external_auth/access.py:141` - Value of type "ClientUserScopeIn" is not indexable
- `app/services/pipeline/screening.py:624` - Invalid index type "str" for "dict[Literal['expected_ctc', 'notice_period_days', 'current_location', 'work_authorization', 'willing_to_relocate'], Any]"; expected type "Literal['expected_ctc', 'notice_period_days', 'current_location', 'work_authorization', 'willing_to_relocate']"
- `app/services/sandbox/seed.py:222` - Value of type "int" is not indexable
- `app/services/sandbox/seed.py:224` - Value of type "int" is not indexable
- `app/services/workforce/conversion.py:62` - Invalid index type "SkillKey" for "dict[str, Any]"; expected type "str"
- `app/services/campus/queue.py:173` - Value of type "str | None" is not indexable
- `app/services/client_portal/approvals.py:179` - Value of type "ClientTimesheetSummary" is not indexable

</details>

<details><summary><b>operator</b> - bad operator operands (10)</summary>

- `app/services/agent_runtime/locks.py:86` - Unsupported operand types for >= ("list[Any]" and "int")
- `app/core/fairness.py:62` - Unsupported operand types for >= ("list[Any]" and "int")
- `app/services/campaign_copy/checks.py:339` - Unsupported right operand type for in ("Decimal")
- `app/core/semantic/registry.py:122` - Unsupported operand types for - ("set[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]" and "frozenset[str]")
- `app/core/semantic/registry.py:125` - Unsupported operand types for - ("set[Literal['eq', 'ne', 'in', 'not_in', 'lt', 'lte', 'gt', 'gte', 'between', 'contains', 'starts_with', 'is_null', 'within_days', 'skill_match']]" and "frozenset[str]")
- `app/core/semantic/registry.py:128` - Unsupported operand types for - ("set[Literal['count', 'count_distinct', 'sum', 'avg', 'min', 'max']]" and "frozenset[str]")
- `app/core/agentic/exposure.py:115` - Unsupported operand types for & ("frozenset[Literal['mcp', 'portal_client', 'portal_vendor', 'portal_candidate']]" and "frozenset[str]")
- `app/services/chasing/bgv_follow_ups.py:135` - No overload variant of "__sub__" of "date" matches argument type "None"
- `app/services/chasing/bgv_follow_ups.py:450` - No overload variant of "__sub__" of "date" matches argument type "None"
- `app/services/developer_portal/sandboxes.py:53` - Cannot call function of unknown type

</details>

<details><summary><b>no-untyped-call</b> - call to untyped function (9)</summary>

- `app/core/dirty_sets.py:41` - Call to untyped function "_redis" in typed context
- `app/core/dirty_sets.py:53` - Call to untyped function "_redis" in typed context
- `app/core/dirty_sets.py:64` - Call to untyped function "_redis" in typed context
- `app/services/exposure/record.py:115` - Call to untyped function "_redis" in typed context
- `app/services/exposure/record.py:129` - Call to untyped function "_redis" in typed context
- `app/services/public_events/dispatch.py:118` - Call to untyped function "_redis" in typed context
- `app/tasks/analytics.py:52` - Call to untyped function "_redis" in typed context
- `app/services/analytics/hooks.py:67` - Call to untyped function "_redis" in typed context
- `app/services/job_network/sink.py:45` - Call to untyped function "_redis" in typed context

</details>

<details><summary><b>no-untyped-def</b> - function missing type annotations (9)</summary>

- `app/services/screening_agent/agent_settings.py:13` - Function is missing a type annotation for one or more arguments
- `app/services/job_network_service.py:41` - Function is missing a type annotation for one or more arguments
- `app/services/job_network_service.py:45` - Function is missing a type annotation for one or more arguments
- `app/services/job_network_service.py:54` - Function is missing a type annotation for one or more arguments
- `app/services/job_network_service.py:60` - Function is missing a type annotation for one or more arguments
- `app/services/screening_agent/reminders.py:48` - Function is missing a return type annotation
- `app/services/reporting/dashboards_p5.py:25` - Function is missing a type annotation for one or more arguments
- `app/services/reporting/dashboards_p5.py:38` - Function is missing a type annotation for one or more arguments
- `app/services/reporting/dashboards_p5.py:47` - Function is missing a type annotation for one or more arguments

</details>

<details><summary><b>type-arg</b> - missing/extra type parameter (7)</summary>

- `app/schemas/job_network.py:157` - Missing type parameters for generic type "dict"
- `app/services/reporting/dashboards_p5.py:25` - Missing type parameters for generic type "dict"
- `app/services/reporting/dashboards_p5.py:38` - Missing type parameters for generic type "dict"
- `app/services/reporting/dashboards_p5.py:47` - Missing type parameters for generic type "dict"
- `app/router/ecosystem_console.py:61` - Missing type parameters for generic type "dict"
- `app/router/ecosystem_console.py:98` - Missing type parameters for generic type "dict"
- `app/router/ecosystem_console.py:193` - Missing type parameters for generic type "dict"

</details>

<details><summary><b>return-value</b> -  (7)</summary>

- `app/services/connectors/hrms/keka.py:96` - Incompatible return value type (got "BaseModel", expected "KekaConfig")
- `app/services/connectors/hrms/darwinbox.py:64` - Incompatible return value type (got "BaseModel", expected "DarwinboxConfig")
- `app/services/connections/__init__.py:36` - Incompatible return value type (got "GoogleProvider", expected "CalendarProvider | None")
- `app/services/connections/__init__.py:39` - Incompatible return value type (got "MicrosoftProvider", expected "CalendarProvider | None")
- `app/services/connections/__init__.py:41` - Incompatible return value type (got "FakeProvider", expected "CalendarProvider | None")
- `app/services/external_auth/signin.py:688` - Incompatible return value type (got "Base | None", expected "ClientUser | VendorUser | None")
- `app/services/developer_portal/conformance/runner.py:185` - Incompatible return value type (got "tuple[int | None, int | None, list[bytes], str | None]", expected "tuple[int | None, int | None, list[bytes], str]")

</details>

<details><summary><b>import-untyped</b> -  (5)</summary>

- `app/router/public_v1/changelog.py:8` - Library stubs not installed for "yaml"
- `app/services/privacy/retention_bindings.py:28` - Library stubs not installed for "dateutil.relativedelta"
- `app/services/ai/fake_llm.py:28` - Library stubs not installed for "yaml"
- `app/services/privacy/retention.py:20` - Library stubs not installed for "dateutil.relativedelta"
- `app/services/submission_packs/writeup.py:11` - Library stubs not installed for "yaml"

</details>

<details><summary><b>has-type</b> -  (4)</summary>

- `app/services/fairness/suppression.py:64` - Cannot determine type of "c"
- `app/services/fairness/suppression.py:68` - Cannot determine type of "r"
- `app/services/fairness/suppression.py:68` - Cannot determine type of "c"
- `app/services/fairness/suppression.py:78` - Cannot determine type of "c"

</details>

<details><summary><b>list-item</b> -  (4)</summary>

- `app/services/integrations/runner.py:427` - List item 0 has incompatible type "None"; expected "UUID"
- `app/services/documents/templates.py:225` - List item 0 has incompatible type "None"; expected "DocumentTemplate"
- `app/services/agent_runtime/runs.py:533` - List item 0 has incompatible type "UUID | None"; expected "UUID"
- `app/services/agent_runtime/external.py:248` - List item 0 has incompatible type "UUID | None"; expected "UUID"

</details>

<details><summary><b>call-overload</b> -  (3)</summary>

- `app/services/campaign_copy/checks.py:334` - No overload variant of "search" of "Pattern" matches argument type "Decimal"
- `app/services/workforce/conversion.py:61` - No overload variant of "get" of "dict" matches argument type "SkillKey"
- `app/services/billing/compute.py:106` - No overload variant of "get" of "dict" matches argument types "str", "bool"

</details>

<details><summary><b>no-redef</b> -  (2)</summary>

- `app/services/mcp_server/results.py:183` - Name "structured" already defined on line 178
- `app/services/ledger_sync/tally.py:59` - Name "rows" already defined on line 40

</details>

<details><summary><b>call-arg</b> -  (2)</summary>

- `app/services/ecosystem_governance/console.py:92` - Missing positional argument "q" in call to "list_subscriptions"
- `app/services/ecosystem_governance/erasure_fanout.py:121` - Missing positional arguments "db", "installation_id" in call to "adapter_for"

</details>

<details><summary><b>name-defined</b> -  (2)</summary>

- `app/services/messaging/hooks.py:52` - Name "sending.RecipientRef" is not defined
- `app/services/projects/overview.py:233` - Name "timeline.TimelineOut" is not defined

</details>

<details><summary><b>typeddict-item</b> -  (1)</summary>

- `app/services/immigration/definition.py:44` - Missing keys ("type", "loc", "msg", "input") for TypedDict "ErrorDetails"

</details>

<details><summary><b>comparison-overlap</b> -  (1)</summary>

- `app/core/semantic/registry.py:100` - Non-overlapping equality check (left operand type: "Literal['none', 'personal', 'compensation', 'identity', 'protected']", right operand type: "Literal['']")

</details>

<details><summary><b>dict-item</b> -  (1)</summary>

- `app/services/crm/settings.py:102` - Unpacked dict entry 1 has incompatible type "dict[Literal['qualification', 'proposal', 'negotiation', 'won', 'lost'], int]"; expected "SupportsKeysAndGetItem[str, Any]"

</details>

<details><summary><b>func-returns-value</b> -  (1)</summary>

- `app/services/pipeline/links.py:45` - Function does not return a value (it only ever returns None)

</details>

<details><summary><b>type-var</b> -  (1)</summary>

- `app/services/onboarding/cases.py:82` - Value of type variable "T" of "select" cannot be "OnboardingTemplate"

</details>

---

## 3. Bandit - security findings

| Count | Test | Name |
|---:|---|---|
| 243 | `B101` | assert_used |
| 26 | `B105` | hardcoded_password_string |
| 14 | `B608` | hardcoded_sql_expressions |
| 7 | `B112` | try_except_continue |
| 6 | `B406` | blacklist |
| 5 | `B311` | blacklist |
| 4 | `B113` | request_without_timeout |
| 4 | `B110` | try_except_pass |
| 2 | `B106` | hardcoded_password_funcarg |
| 1 | `B613` | trojansource |
| 1 | `B701` | jinja2_autoescape_false |
| 1 | `B404` | blacklist |
| 1 | `B603` | subprocess_without_shell_equals_true |

### HIGH severity (2)

- **`B613` trojansource** _(confidence MEDIUM)_ - `app/services/submission_packs/writeup.py:19`
  - A Python source file contains bidirectional control characters ('\u200f').
- **`B701` jinja2_autoescape_false** _(confidence HIGH)_ - `app/services/platform_service.py:52`
  - Using jinja2 templates with autoescape=False is dangerous and can lead to XSS. Use autoescape=True or use the select_autoescape function to mitigate XSS vulnerabilities.

### MEDIUM severity (18)

- **`B113` request_without_timeout** _(confidence LOW)_ - `app/services/expenses/portal.py:402`
  - Call to requests without timeout
- **`B113` request_without_timeout** _(confidence LOW)_ - `app/services/time/monitoring.py:250`
  - Call to requests without timeout
- **`B113` request_without_timeout** _(confidence LOW)_ - `app/services/time/portal.py:395`
  - Call to requests without timeout
- **`B113` request_without_timeout** _(confidence LOW)_ - `app/services/timesheet_flags/nudges.py:182`
  - Call to requests without timeout
- **`B608` hardcoded_sql_expressions** _(confidence LOW)_ - `app/core/migration_helpers.py:97`
  - Possible SQL injection vector through string-based query construction.
- **`B608` hardcoded_sql_expressions** _(confidence LOW)_ - `app/core/migration_helpers.py:139`
  - Possible SQL injection vector through string-based query construction.
- **`B608` hardcoded_sql_expressions** _(confidence LOW)_ - `app/core/migration_helpers.py:673`
  - Possible SQL injection vector through string-based query construction.
- **`B608` hardcoded_sql_expressions** _(confidence LOW)_ - `app/services/ai/vector_index.py:122`
  - Possible SQL injection vector through string-based query construction.
- **`B608` hardcoded_sql_expressions** _(confidence LOW)_ - `app/services/ai/vector_index.py:274`
  - Possible SQL injection vector through string-based query construction.
- **`B608` hardcoded_sql_expressions** _(confidence LOW)_ - `app/services/job_network/stats.py:26`
  - Possible SQL injection vector through string-based query construction.
- **`B608` hardcoded_sql_expressions** _(confidence LOW)_ - `app/services/knowledge/sealing.py:203`
  - Possible SQL injection vector through string-based query construction.
- **`B608` hardcoded_sql_expressions** _(confidence LOW)_ - `app/services/privacy/audit_purge.py:68`
  - Possible SQL injection vector through string-based query construction.
- **`B608` hardcoded_sql_expressions** _(confidence LOW)_ - `app/services/privacy/audit_purge.py:72`
  - Possible SQL injection vector through string-based query construction.
- **`B608` hardcoded_sql_expressions** _(confidence LOW)_ - `app/services/privacy/audit_purge.py:75`
  - Possible SQL injection vector through string-based query construction.
- **`B608` hardcoded_sql_expressions** _(confidence LOW)_ - `app/services/skills/graph.py:259`
  - Possible SQL injection vector through string-based query construction.
- **`B608` hardcoded_sql_expressions** _(confidence LOW)_ - `app/services/vault/legacy_move.py:270`
  - Possible SQL injection vector through string-based query construction.
- **`B608` hardcoded_sql_expressions** _(confidence LOW)_ - `app/services/vault/qa_seal_move.py:98`
  - Possible SQL injection vector through string-based query construction.
- **`B608` hardcoded_sql_expressions** _(confidence LOW)_ - `app/services/vault/qa_seal_move.py:108`
  - Possible SQL injection vector through string-based query construction.

### LOW severity (295) - by rule

| Count | Test | Name |
|---:|---|---|
| 243 | `B101` | assert_used |
| 26 | `B105` | hardcoded_password_string |
| 7 | `B112` | try_except_continue |
| 6 | `B406` | blacklist |
| 5 | `B311` | blacklist |
| 4 | `B110` | try_except_pass |
| 2 | `B106` | hardcoded_password_funcarg |
| 1 | `B404` | blacklist |
| 1 | `B603` | subprocess_without_shell_equals_true |

Most LOW findings are `B101 assert_used` (asserts, largely in tests/guards) and `B105 hardcoded_password_string` (almost all **false positives**: field names/constants containing the word "password", not real secrets).

<details><summary>Show all LOW findings</summary>

- `B101` assert_used - `app/core/database.py:157` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/core/errors.py:146` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/core/errors.py:157` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/core/errors.py:170` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/core/integrations/context.py:97` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/core/public_api_contract.py:40` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/core/semantic/compiler.py:371` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/core/semantic/compiler.py:399` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/core/semantic/executor.py:130` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/core/semantic/executor.py:139` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/dependencies/public.py:55` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/router/admin.py:262` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/router/admin.py:278` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/router/client_portal_approvals.py:94` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/router/client_portal_approvals.py:120` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/router/client_portal_approvals.py:129` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/router/client_portal_expenses.py:30` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/router/client_portal_leave.py:30` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/router/client_portal_leave.py:31` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/router/client_portal_placement_exit.py:30` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/router/client_portal_timesheets.py:36` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/router/public_v1/meta.py:39` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/router/sso.py:160` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/agent_runtime/admin.py:80` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/agent_runtime/admin.py:167` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/agent_runtime/admin.py:199` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/agent_runtime/admin.py:234` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/agent_runtime/admin.py:294` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/agent_runtime/executor.py:486` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/agent_runtime/external.py:315` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/agent_runtime/external.py:324` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/agent_runtime/governance/burn_in.py:52` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/agent_runtime/proposals.py:132` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/agent_runtime/proposals.py:154` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/agent_runtime/proposals.py:187` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/agent_runtime/proposals.py:460` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/agent_runtime/runs.py:207` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/agent_tools/pipeline/tools_e54.py:78` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/agent_tools/portal_common/tooling.py:87` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/ai/admin.py:67` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/ai/admin.py:83` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/ai_governance/checks.py:186` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/analytics_answers/describe.py:152` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/api_keys_service.py:131` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/api_keys_service.py:165` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/api_keys_service.py:295` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/approvals/service.py:923` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/approvals/service.py:996` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/assessments/taking.py:496` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/campaign_copy/checks.py:207` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/campaign_copy/drafts.py:44` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/campaigns/sending.py:321` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/campus/events.py:362` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/campus/queue.py:166` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/campus/registration.py:288` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/campus/registration.py:327` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/candidates/parsing.py:80` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/central_token.py:97` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/commercials/contracts.py:935` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/commercials/rate_cards.py:279` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/commercials/rate_checks.py:99` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/commercials/rate_checks.py:186` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/compliance/credentials.py:298` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/copilot_service.py:369` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/copilot_service.py:415` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/crm/imports.py:166` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/crm/won.py:105` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/crm/won.py:200` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/demand_insights/drafts.py:127` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/demand_insights/drafts.py:204` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/demand_intake/items.py:296` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/demand_intake/receive.py:217` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/developer_portal/conformance/runner.py:84` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/developer_portal/conformance/runner.py:124` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/developer_portal/conformance/runner.py:148` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/developer_portal/conformance/runner.py:194` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/developer_portal/conformance/runner.py:219` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/developer_portal/conformance/runner.py:237` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/developer_portal/conformance/runner.py:259` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/developer_portal/conformance/runner.py:286` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/developer_portal/orgs.py:131` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/developer_portal/orgs.py:204` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/developer_portal/playground.py:21` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/developer_portal/sandboxes.py:118` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/developer_portal/sandboxes.py:151` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/developer_portal/sandboxes.py:198` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/developer_portal/sandboxes.py:312` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/developer_portal/sandboxes.py:324` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/developer_portal/sandboxes.py:351` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/developer_portal/sandboxes.py:353` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/developer_portal/verification.py:176` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/developer_portal/verification.py:206` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/documents/esign.py:858` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/documents/repository.py:469` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/ecosystem/controls.py:43` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/external_auth/signin.py:785` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/files_service.py:360` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/golive/owners.py:291` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/golive/owners.py:349` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/golive/owners.py:374` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/golive/owners.py:409` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/golive/owners.py:437` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/golive/owners.py:442` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/golive/p3_imports.py:167` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/hrms_sync/push.py:211` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/immigration/cases.py:84` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/immigration/checklist.py:404` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/immigration/permits.py:143` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/immigration/visa_types.py:303` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/integrations/runner.py:264` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/job_distribution/expiry.py:41` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/job_distribution/ingest.py:110` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/job_distribution/ingest.py:135` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/job_network/listings.py:69` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/job_network/listings.py:219` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/job_network/membership.py:42` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/job_network/moderation.py:36` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/job_network/stats.py:22` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/job_network/stats.py:74` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/knowledge/qa.py:724` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/ledger_sync/tally.py:33` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/legal_entities/fx_feed.py:74` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/legal_entities/fx_feed.py:98` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/legal_entities/fx_feed.py:161` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/mail_settings_service.py:71` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/marketplace/catalog.py:80` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/marketplace/catalog.py:219` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/marketplace/catalog.py:312` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/marketplace/installs.py:79` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/marketplace/installs.py:85` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/marketplace/policy.py:85` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/marketplace/review.py:58` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/marketplace/review.py:163` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/marketplace/review.py:434` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/marketplace/review.py:468` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/marketplace/review.py:500` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/marketplace/review.py:522` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/marketplace/submissions.py:204` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/marketplace/submissions.py:243` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/mcp_service.py:155` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/mcp_service.py:186` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/mcp_service.py:577` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/mcp_service.py:594` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/mcp_service.py:610` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/messaging/opt_outs.py:185` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/messaging/sending.py:451` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/mobility/applications.py:95` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/mobility/applications.py:168` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/mobility/employees.py:58` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/mobility/employees.py:188` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/mobility/employees.py:365` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/mobility/transfers.py:274` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/oauth/authorize.py:219` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/oauth/authorize.py:330` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/oauth/authorize.py:427` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/oauth/refresh.py:105` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/offers/response.py:83` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/offers/versions.py:262` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/onboarding/cases.py:340` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/onboarding/cases.py:416` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/onboarding/portal.py:254` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/payroll/files.py:118` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/payroll/mappings.py:64` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/payroll/vendor_statements.py:358` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/pipeline/applications.py:169` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/pipeline/feedback.py:178` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/pipeline/screening.py:741` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/pipeline/screening.py:822` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/pipeline/screening.py:869` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/placements/end_alerts.py:47` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/placements/end_alerts.py:107` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/placements/end_alerts.py:143` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/placements/prejoining.py:130` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/platform_service.py:204` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/platform_service.py:360` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/portal/candidate_auth.py:167` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/portal/candidate_auth.py:173` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/portal/privacy_self_service.py:332` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/portal/privacy_self_service.py:474` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/portal_copilot/principals.py:89` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/portal_copilot_service.py:301` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/portal_copilot_service.py:313` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/privacy/erasure.py:253` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/public_api/engagement.py:66` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/public_api/imports.py:103` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/public_api/imports.py:276` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/rbac_service.py:365` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/rbac_service.py:426` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/rbac_service.py:501` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/rbac_service.py:716` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/rbac_service.py:842` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/rbac_service.py:994` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/rbac_service.py:1053` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/receivables/tds_recon/parser_26as.py:119` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/receivables/tds_recon/parser_ais.py:117` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/referrals/tracking.py:55` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/referrals/tracking.py:64` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/reporting/__init__.py:214` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/reporting/builder/execute.py:124` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/reporting/builder/execute.py:188` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/reporting/builder/runs.py:113` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/reporting/builder/schedules.py:389` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/reporting/builder/schedules.py:492` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/reporting/hooks.py:36` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/screening_agent/conversations.py:306` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/search/candidate_search.py:771` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/search/global_search.py:52` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/search/global_search.py:190` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/segments/service.py:126` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/skills/graph.py:358` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/skills/graph.py:433` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/skills/graph.py:454` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/skills/service.py:222` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/sso/client.py:119` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/sso/client.py:253` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/sso/client_admin.py:89` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/sso/client_admin.py:200` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/sso/client_admin.py:419` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/sso/login.py:376` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/sso/login.py:450` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/sso/service.py:456` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/submission_packs/common.py:108` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/submission_packs/service.py:625` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/support_access_service.py:202` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/talent_pools/legacy_lists.py:151` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/talent_pools/pools.py:178` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/talent_pools/settings.py:35` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/talent_pools/settings.py:40` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/telephony/calls.py:170` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/telephony/calls.py:278` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/tenant_lifecycle_service.py:268` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/time/imports.py:229` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/transcripts/parsers.py:150` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/webhooks/echo.py:48` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/webhooks/replay.py:50` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/webhooks/replay.py:78` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/webhooks/sink.py:139` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/webhooks/subscriptions.py:130` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/webhooks/subscriptions.py:165` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/webhooks/subscriptions.py:207` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/webhooks_service.py:77` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/workflow/executor.py:169` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B101` assert_used - `app/services/workflow/executor.py:363` - Use of assert detected. The enclosed code will be removed when compiling to optimised byte code.
- `B105` hardcoded_password_string - `app/core/mailer.py:279` - Possible hardcoded password: ''
- `B105` hardcoded_password_string - `app/core/settings.py:11` - Possible hardcoded password: 'talent_app_dev'
- `B105` hardcoded_password_string - `app/router/auth.py:133` - Possible hardcoded password: 'Bearer'
- `B105` hardcoded_password_string - `app/services/agent_tools/billing/tools_e63.py:149` - Possible hardcoded password: 'within_card'
- `B105` hardcoded_password_string - `app/services/calendar_sync/common.py:16` - Possible hardcoded password: 'channel_token'
- `B105` hardcoded_password_string - `app/services/connections/google.py:37` - Possible hardcoded password: 'https://oauth2.googleapis.com/token'
- `B105` hardcoded_password_string - `app/services/connections_service.py:328` - Possible hardcoded password: 'calendar_connection'
- `B105` hardcoded_password_string - `app/services/connectors/accounting/zoho_books.py:40` - Possible hardcoded password: 'https://accounts.zoho.in/oauth/v2/token'
- `B105` hardcoded_password_string - `app/services/connectors/hrms/keka.py:36` - Possible hardcoded password: 'https://login.keka.com/connect/token'
- `B105` hardcoded_password_string - `app/services/connectors/job_board/indeed.py:54` - Possible hardcoded password: 'https://apis.indeed.com/oauth/v2/tokens'
- `B105` hardcoded_password_string - `app/services/connectors/job_board/linkedin.py:46` - Possible hardcoded password: 'https://www.linkedin.com/oauth/v2/accessToken'
- `B105` hardcoded_password_string - `app/services/demand_intake/settings.py:45` - Possible hardcoded password: 'abcdefghijklmnopqrstuvwxyz234567'
- `B105` hardcoded_password_string - `app/services/developer_portal/conformance/fault_injection.py:71` - Possible hardcoded password: 'sbx:conf:tok:'
- `B105` hardcoded_password_string - `app/services/developer_portal/erasure.py:29` - Possible hardcoded password: '!'
- `B105` hardcoded_password_string - `app/services/external_auth/signin.py:79` - Possible hardcoded password: 'vendor_password_reset'
- `B105` hardcoded_password_string - `app/services/fairness/pack.py:35` - Possible hardcoded password: 'AI Screening pass'
- `B105` hardcoded_password_string - `app/services/fairness/pack.py:36` - Possible hardcoded password: 'Assessment pass'
- `B105` hardcoded_password_string - `app/services/fairness/selection_points.py:42` - Possible hardcoded password: 'Applicants are AI screenings completed in the date range; selected applicants have the outcome 'proceed'.'
- `B105` hardcoded_password_string - `app/services/fairness/selection_points.py:43` - Possible hardcoded password: 'Applicants are assessment invitations decided (passed or failed) in the date range; selected applicants passed. Scoring rate: share scoring above the sample median.'
- `B105` hardcoded_password_string - `app/services/fairness/selection_points.py:221` - Possible hardcoded password: 'AI Screening pass'
- `B105` hardcoded_password_string - `app/services/fairness/selection_points.py:222` - Possible hardcoded password: 'Assessment pass'
- `B105` hardcoded_password_string - `app/services/mail_settings_service.py:23` - Possible hardcoded password: 'connector'
- `B105` hardcoded_password_string - `app/services/marketplace/conformance_report.py:19` - Possible hardcoded password: 'conformance_report_signing'
- `B105` hardcoded_password_string - `app/services/oauth/tokens.py:34` - Possible hardcoded password: 'tos_cs_'
- `B105` hardcoded_password_string - `app/services/portal/apply.py:50` - Possible hardcoded password: 'X-Apply-Token'
- `B105` hardcoded_password_string - `app/services/webhooks/common.py:37` - Possible hardcoded password: 'signing_secret'
- `B106` hardcoded_password_funcarg - `app/services/marketplace/platform_catalog.py:76` - Possible hardcoded password: ''
- `B106` hardcoded_password_funcarg - `app/services/sandbox/seed.py:294` - Possible hardcoded password: '!sandbox-seed-no-login'
- `B110` try_except_pass - `app/services/agent_tools/demands/tools_e55.py:300` - Try, Except, Pass detected.
- `B110` try_except_pass - `app/services/central_token.py:86` - Try, Except, Pass detected.
- `B110` try_except_pass - `app/services/commercials/rate_checks.py:304` - Try, Except, Pass detected.
- `B110` try_except_pass - `app/services/placements/exit_hooks.py:287` - Try, Except, Pass detected.
- `B112` try_except_continue - `app/services/exposure/record.py:139` - Try, Except, Continue detected.
- `B112` try_except_continue - `app/services/golive/service.py:361` - Try, Except, Continue detected.
- `B112` try_except_continue - `app/services/interview_captures/validators.py:76` - Try, Except, Continue detected.
- `B112` try_except_continue - `app/services/metering/flush.py:55` - Try, Except, Continue detected.
- `B112` try_except_continue - `app/services/mobility/matches.py:91` - Try, Except, Continue detected.
- `B112` try_except_continue - `app/services/mobility/matches.py:100` - Try, Except, Continue detected.
- `B112` try_except_continue - `app/services/scheduling_agent/availability.py:298` - Try, Except, Continue detected.
- `B311` blacklist - `app/core/fairness.py:31` - Standard pseudo-random generators are not suitable for security/cryptographic purposes.
- `B311` blacklist - `app/services/assessments/draw.py:22` - Standard pseudo-random generators are not suitable for security/cryptographic purposes.
- `B311` blacklist - `app/services/knowledge/sealing.py:237` - Standard pseudo-random generators are not suitable for security/cryptographic purposes.
- `B311` blacklist - `app/services/sandbox/seed.py:139` - Standard pseudo-random generators are not suitable for security/cryptographic purposes.
- `B311` blacklist - `app/tasks/campaigns.py:70` - Standard pseudo-random generators are not suitable for security/cryptographic purposes.
- `B404` blacklist - `app/tasks/ai_governance.py:16` - Consider possible security implications associated with the subprocess module.
- `B406` blacklist - `app/services/connectors/accounting/tally_xml.py:24` - Using escape to parse untrusted XML data is known to be vulnerable to XML attacks. Replace escape with the equivalent defusedxml package, or make sure defusedxml.defuse_stdlib() is called.
- `B406` blacklist - `app/services/documents/certificate.py:15` - Using escape to parse untrusted XML data is known to be vulnerable to XML attacks. Replace escape with the equivalent defusedxml package, or make sure defusedxml.defuse_stdlib() is called.
- `B406` blacklist - `app/services/documents/render.py:14` - Using escape to parse untrusted XML data is known to be vulnerable to XML attacks. Replace escape with the equivalent defusedxml package, or make sure defusedxml.defuse_stdlib() is called.
- `B406` blacklist - `app/services/job_distribution/feed.py:16` - Using escape to parse untrusted XML data is known to be vulnerable to XML attacks. Replace escape with the equivalent defusedxml package, or make sure defusedxml.defuse_stdlib() is called.
- `B406` blacklist - `app/services/pipeline/resume_pdf.py:12` - Using escape to parse untrusted XML data is known to be vulnerable to XML attacks. Replace escape with the equivalent defusedxml package, or make sure defusedxml.defuse_stdlib() is called.
- `B406` blacklist - `app/services/telephony/twiml.py:6` - Using escape to parse untrusted XML data is known to be vulnerable to XML attacks. Replace escape with the equivalent defusedxml package, or make sure defusedxml.defuse_stdlib() is called.
- `B603` subprocess_without_shell_equals_true - `app/tasks/ai_governance.py:79` - subprocess call - check for execution of untrusted input.

</details>

---

## 4. Recommended priorities

1. **`B701` Jinja2 `autoescape=False`** (`app/services/platform_service.py:52`) - the only real HIGH; use `autoescape=True` / `select_autoescape` if the template renders any user-controlled data (XSS).
1b. **mypy `unused-ignore` (528)** - delete the stale `# type: ignore` comments (mechanical, big win on the error count).
2. **`B113` requests without timeout** (4) - add `timeout=` to each outbound HTTP call (avoids hangs/resource leaks).
3. **`B608` hardcoded SQL** (14) - all in migration/vault/stats code using dynamic identifiers; confirm interpolated values are server-controlled (schema/table names), not user input.
4. **`B613` bidirectional unicode** (`app/services/submission_packs/writeup.py:19`) - remove the stray RTL control char.
5. **Ruff non-E501** cleanups - mostly docstring/typing/unused-arg; safe to batch-fix.
6. **`E501`** (line-too-long) - cosmetic; raise the line length or wrap, low priority.

---

## 5. Roadmap to "close all issues" (behavior-preserving boundary)

Goal: drive every issue to closed. The hard boundary is **no functionality change** — so issues split into three tiers by whether they *can* be closed silently.

**Tier A — DONE ✅ (type/style only, no behavior risk):**
- ruff `E501` + interface/style rules (`ARG001/2`, `D401/400/415`, `E402`, `RUF001/2`, `N801/2/6/12/18`, `E731/741`, `B903`, `ARG005`, `F403`, `C416`) ignored with justification in `pyproject.toml`; 65 intentional side-effect/re-export imports `# noqa: F401`; dead RUF028 comments removed; D301/isort fixed. **Ruff app → 0.**
- bandit `B101`/`B105`/`B106` skipped in `bandit.yml` with justification (asserts are guards; B105/B106 hits are field/URL/label constants). **Bandit app → 43** (Tier C residual).
- mypy clean structural fixes: `type-arg` (7), `no-untyped-def` (9), `no-untyped-call` (9 via `_redis()->Any`) → **0**; `RUF012`→`ClassVar`. `var-annotated` (47) left for a later pass (many are coupled to the `dict(Row…)` `arg-type` cluster, so not a clean standalone fix). **mypy 843 → 818.**

**Tier B — DONE ✅ (−31 arg-type, type-only):**
- `model_construct` entity-type `Literal` — resolved: the valid set **is** the DB CHECK (`TASK_ENTITY_TYPES`), so `TaskEntity` was synced to it (7→28). Closed the DB-valid cases; surfaced `interview`/`timesheet`/`invoice` as a real latent bug (see below).
- FieldDef `frozenset[str]` vs `frozenset[Literal]` — resolved: the operator set is the closed `Op`/`Agg` Literals; annotated the constants `frozenset[Op]`/`frozenset[Agg]`.
- **Still Tier C:** `select(union)` collapsing to `Base` (`attr-defined`/`arg-type`) — needs a per-site `cast()` (band-aid, no safety gain) or a query-shape change.

**Tier B leftover decision:** `tools_e53.py` passes task `entity_type` values `interview`/`timesheet`/`invoice` that are **not** in `TASK_ENTITY_TYPES` → they would fail the DB CHECK on insert. Either fix the callers (bug) or add those three to the constraint via migration (scope/behavior change). Not changed here.

**Tier C — cannot be closed without a per-item choice (behavior-sensitive):**
- `union-attr` (16) + the `UUID | None → UUID` subset of `arg-type` — these are **real latent None-paths**. Each closes only as either (a) a deliberate None-guard bug-fix (behavior change, needs sign-off) or (b) a documented `# type: ignore[union-attr]`. No silent close.
- bandit `B608` hardcoded SQL (14) + `B113` requests-timeout (4) — already reviewed: identifiers are server-controlled / the `requests` call is a local dict lookup, not HTTP. Close as **documented accepted-risk**, not code change.

**Suggested order:** Tier A (no questions) → Tier B (one batch of questions, then apply) → Tier C (per-item sign-off). After each tier, re-run mypy/ruff/bandit and update the Summary counts above.
