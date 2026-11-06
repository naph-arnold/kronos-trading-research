"""Deterministic market-calendar intelligence: holidays, broker closures, economic events.

Only verified, structured data from identified providers enters these stores.
AI agents may *read* the calendar; nothing they produce can open a closed market
or shorten a blackout (closures are unioned; the most restrictive source wins).
"""
