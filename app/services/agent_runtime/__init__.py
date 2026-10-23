# ruff: file-ignore[line-too-long]
"""Agent runtime domain (Epic 53, AD-58..AD-63): runs, steps, policy, Effective Rights, Proposals, payload store,
send policy, budgets, kill switches, triggers and waits. Public API: the facade ``app.services.agent_runtime_service``.
It never imports a P1-P3 business domain (contract ``p4-runtime-no-domains``): tools reach domains, the runtime does not.
"""
