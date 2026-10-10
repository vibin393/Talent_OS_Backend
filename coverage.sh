#!/bin/bash
set -e  # ⛑️ Exit on first failure

pytest . --cov=app --cov-report=term-missing --cov-report=html --tb=short -q -ra