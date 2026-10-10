# ruff: noqa: E501
"""Employee referrals (Epic 42, FR-213..FR-216, AD-50).

Modules: ``programs`` (programs + settings), ``eligibility`` (pure rules + loaders), ``submit`` (openings, upload
channel, share links, withdraw), ``acceptance`` (public referee page, consent-first acceptance, duplicates,
expiry, link to Demand), ``status`` (pipeline / placement handlers), ``tracking`` (referrer-safe and internal
views), ``rewards`` (reward lifecycle + approval subject), ``posting_source`` (payroll ``referral_reward``),
``payouts`` (payout export, mark paid), ``portal`` (User / Worker self-service), ``semantic_source``, ``hooks``.

Kept import-light on purpose: ``hooks`` is autodiscovered and imports the modules it needs lazily.
"""
