#!/bin/zsh
# Double-click in Finder (or `open check_groww.command`) to run the Groww data-access check.
cd "$(dirname "$0")" && .venv/bin/python check_groww.py
