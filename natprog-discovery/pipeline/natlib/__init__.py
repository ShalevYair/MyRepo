"""Shared layer for the NATPROG pipeline.

Every module here ports logic that already exists and is validated in
natprog-discovery/app.js (a browser tool checked against a real 770 MB
scan — see natprog-discovery/README.md "Findings so far"). The rule for
this package: when app.js already does something, port it; do not
re-derive it from scratch. See MERGE-PLAN.md and WORKPLAN.md stage 0.
"""
