"""26AS / AIS TDS reconciliation (Phase 3 Epic 51, Story 51.2, FR-254): pure parsers (``parser_26as``,
``parser_ais``) and matcher (``matching``), runs / decisions / TAN mappings (``runs``), exception lists
(``exceptions``) and per Client x quarter status (``status``). Writes only ``tds_recon_runs``,
``tds_statement_lines``, ``tds_recon_matches`` and ``tds_deductor_tans``; never ``tds_deductions`` or their
reversals."""
