# ruff: noqa: E501
"""Exposure hooks (Epic 66): nothing to register yet -- the buffer is flushed by the ``exposure.flush`` beat task
(``app.tasks.ecosystem``); readers (the D-11 ``subject.erased`` fan-out of Epic 68) call the facade."""
