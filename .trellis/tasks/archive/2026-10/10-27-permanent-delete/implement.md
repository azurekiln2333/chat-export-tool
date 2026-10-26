# Execution and verification

1. Delegate store/calendar, Vue controls, independent tests and deployment docs
   to existing pi agents with explicit file ownership. Root implements API.
2. Integrate contracts and run focused tests; no delivery-order skips.
3. Run production build and full Node suite. Independently inspect SQL tables
   after single/batch deletes, after reopen/changed/forced import and with only
   the SQLite file available. Verify calendar/day pagination and count refresh.
4. Test destructive actions only on synthetic exports/copies. Compare original
   user data counts/hashes; restart the existing local Node server and check it
   serves the new app and API. No computer-use/browser automation.
5. Update backend/frontend specs, package the Node runtime, verify a relocated
   production-only installation, then commit scoped work, archive and journal.
6. Final handoff in Chinese: exact local reader.sqlite, data/attachments,
   Vue source/build and Node entry paths; Linux deployment layout and commands;
   deletion boundaries and new calendar/batch functionality.

## Verified results

- Four existing pi owners delivered and froze store, Vue source, independent
  tests and documentation. Root integrated HTTP routes, version and packaging.
- Production build passed; full Node suite 140/140, zero skips; full Python
  suite 108/108. Compile checks, doctor and diff whitespace checks passed.
- Snapshot-only migration preserved 65,724 exact raw rows, 75 conversations
  and 413 recorded dates. Independent date comparisons covered 21
  account-months and 530 account-days; isolated 500-item SQL deletion passed.
- Live reader restarted at port 8765. Original source hashes and all raw rows
  are unchanged; zero user edits/deletions. Calendar/day/pagination and current
  Chinese built assets passed read-only HTTP checks.
- 0.2.0 runtime zip inspected and extracted to a new directory. Production-only
  dependency installation and nine synthetic runtime smoke checks passed.
- No browser/desktop automation or real-record destructive test was used.
- Final path/deployment handoff follows the scoped commit and task/journal
  wrap-up. Private reports are outside the repository.
