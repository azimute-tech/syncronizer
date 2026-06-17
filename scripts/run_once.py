#!/usr/bin/env python
"""Convenience wrapper: run a single ETL+SEND cycle and exit.

Identical to ``python -m syncronizer run-once``. Useful for first-install validation
and debugging.
"""
import sys

from syncronizer.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main(["run-once"] + sys.argv[1:]))
