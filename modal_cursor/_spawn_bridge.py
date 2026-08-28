#!/usr/bin/env python3
"""Executable entry point invoked by Cursor's worker controller."""

from modal_cursor.spawn import main

raise SystemExit(main())
