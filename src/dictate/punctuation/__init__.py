"""Spoken punctuation: "hello comma world" -> "hello, world".

A stage of its own, deliberately NOT part of the cleanup pass. Cleanup is
guaranteed to only ever delete words; this stage substitutes, so it could not
live there without taking that guarantee apart. See `engine.py` for the
narrower guarantee this stage carries instead, and `docs/DESIGN.md`.
"""
