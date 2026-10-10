#!/bin/bash
# Multi worker server
gunicorn -k uvicorn.workers.UvicornWorker --workers=2 --bind=0.0.0.0:8000 --reload --timeout=300 app.main:app

# Celery workers (Epic 13, AD-37) -- run in their own process / container:
#   celery -A app.core.celery worker -Q interactive,finance_runs,connectors,ai,maintenance,campaigns,analytics,reports,agents,ecosystem --loglevel=info
# Epic 53 (AD-68): Copilot and conversation turns on their own worker:
#   celery -A app.core.celery worker -Q conversations --concurrency 8 --loglevel=info
# Epic 66 (AD-92): outbox relay / webhook deliveries and telephony on their own workers:
#   celery -A app.core.celery worker -Q webhooks -n webhooks@%h --concurrency 16 --loglevel=info
#   celery -A app.core.celery worker -Q telephony -n telephony@%h --concurrency 4 --loglevel=info
# Celery beat (exactly one; LockedScheduler holds the Redis lock talent_os:beat_lock):
#   celery -A app.core.celery beat --loglevel=info
