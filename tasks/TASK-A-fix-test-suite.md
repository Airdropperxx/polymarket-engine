# TASK-A: Fix test_engines.py import error
## Status: PENDING | Priority: Medium | Time: 15 min

## Bug
tests/test_engines.py line 24 imports _seconds_until which was renamed to _parse_iso_to_ts.
All tests in the file silently skip.

## Fix
1. Open tests/test_engines.py
2. Line 24: change _seconds_until to _parse_iso_to_ts in the import
3. Find all calls to _seconds_until( in the file (~3 places) and replace with _parse_iso_to_ts(
4. The function signature is identical — takes ISO string, returns float timestamp

## Verify
GHA test workflow should show 0 skipped tests in test_engines.py
