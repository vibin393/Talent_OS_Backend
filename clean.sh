# Find and remove all __pycache__ directories
find . -type d -name __pycache__ -exec rm -r {} +

# Find and remove mypy cache directory
find . -type d -name .mypy_cache -exec rm -r {} +

# Find and remove ruff cache directory
find . -type d -name .ruff_cache -exec rm -r {} +

# Find and remove history directory
find . -type d -name .history -exec rm -r {} +