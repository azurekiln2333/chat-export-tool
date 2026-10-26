# Permanent deletion, bulk actions and calendar browsing

The user explicitly requests pi delegation and implementation: permanently
delete one or several messages, delete one or several whole conversations,
paginate queries, filter by day, and provide a calendar showing recorded days.
The final handoff must give exact database/frontend/backend/media paths and
server deployment commands. Chinese interface; English files and directories.

## Acceptance

- Message and conversation records are SQL-deleted from the reader database,
  including exact raw JSON, annotations and derived aggregates as applicable.
  There is no recycle-bin copy of message content. Hidden is a separate action.
- Single and batch deletion have explicit Chinese confirmation. Invalid/stale
  batches fail atomically. Scope is one account and at most 500 selected items.
- Restart, unchanged/changed import, forced reindex and database-only migration
  preserve permanent deletions. Identifier hashes prevent source reimport from
  resurrecting deleted records, without retaining deleted contents or names.
- Original backup/JSONL/account archives and shared media are preserved; the
  UI and docs identify reader.sqlite as the database affected by deletion.
- Existing edit/restore works independently. A stale old restore request must
  never become an accidental permanent delete.
- Message pagination supports a selected calendar day. The calendar displays
  month navigation, daily counts, recorded dates, selected date and clear date.
  Counts follow account, optional conversation, search and hidden scope.
- Day calculations use the export timezone, not the viewer computer timezone.
- Build and independent tests prove SQL absence, batch atomicity, reimport
  durability, selection state, calendar counts/filter/pagination and auth/CSRF.
- Final runtime is restarted and health checked. Deployment instructions list
  absolute local paths, files to upload, Node requirements and server commands.

Do not delete actual user records for testing: use independent synthetic data
or an isolated copy. Do not resume browser/desktop automation stopped earlier.
