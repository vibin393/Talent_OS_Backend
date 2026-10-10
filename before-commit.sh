#!/bin/bash
set -e  # ⛑️ Exit on first failure

echo "------Format (Ruff)-----"
ruff format .

echo "------Lint Check (Ruff)-----" 
ruff check . --fix 

echo "------Type Checking (mypy)-----"
mypy --pretty --show-error-codes .

echo "------Unit Tests (pytest)-----"
pytest -q --tb=short --disable-warnings
