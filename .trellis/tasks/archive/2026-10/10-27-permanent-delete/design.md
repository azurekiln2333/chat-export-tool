# Shared implementation contract

Owner files: pi p2 owns web/server/store.mjs; p4 owns web/src/**; p6 owns
web/tests/**; p5 owns docs/web-reader.md, docs/deployment.md and README files.
Root owns HTTP server, integration, release packaging, specifications and task
artifacts. No owner edits another owner's file without explicit coordination.

## Permanent storage deletion

Add `deleteMessage(uid, uuid, expectedRevision)` and
`deleteConversation(uid, id, expectedMessageCount)` as single wrappers around:

- `deleteMessages(uid, [{msg_uuid, expected_revision}, ...])`
- `deleteConversations(uid, [{id, expected_message_count}, ...])`

Result: `{deleted:true,account_uid,deleted_messages,deleted_conversations}`,
plus `msg_uuids` or `conversation_ids` arrays respectively. Message single
result additionally has msg_uuid; conversation single result has conversation_id.
Require nonempty arrays, at most 500 items, unique valid keys, revisions >=0,
conversation counts >=1. Validate all rows/conflicts inside one transaction
before mutation. Unknown item/account 404, stale value 409, malformed 400.
Delete message rows and all corresponding edits; update remaining conversation
count/first/last time, remove empty conversations. Conversation deletes include
hidden messages. Account rows may remain with zero counts.

Use a durable `deletions` table containing ONLY kind, SHA-256 key_hash and
deleted_at, with unique (kind,key_hash). Hash JSON.stringify([uid,key]) with a
kind domain; keys/content/names/raw JSON are not retained. Imports skip matching
message hashes and whole-conversation hashes. Keep this table through additive
migrations and reindex; avoid dropping index-only data during schema upgrade.
Deleting all messages must not trigger needless unchanged reimports on restart.
No source backups/account chat.sqlite/JSONL/shared attachments are modified.

## Calendar and day query

`listMessages` adds `{date:''}`: strict valid YYYY-MM-DD or empty. Calendar:
`getCalendar(uid,{month,conversationId='',q='',includeHidden=false})` returns
`{month,timezone,days:[{date,message_count}],total,first_date,last_date}`.
Month must be valid YYYY-MM; days array has recorded matching days only, sorted;
total is selected-month matching messages, first/last cover the entire matching
scope independent of month. Date-less rows remain queryable without a day.
Use exported local `time` date where valid, otherwise derive from created_time
using export_summary.json timezone (UTC only if missing); do not use browser
timezone. Add indexed day storage with additive upgrade/backfill from existing
rows, supporting reader.sqlite when JSONL has been removed. Day count changes
must reflect hide/edit/search/delete immediately.

## HTTP

GET api/messages adds optional `date=YYYY-MM-DD`.
GET api/calendar?account=UID&month=YYYY-MM&conversation=ID&q=&include_hidden=1
returns the calendar object above.
DELETE api/message?account=UID&uuid=UUID body
`{confirm_delete:true,expected_revision}` permanently deletes, returns result.
DELETE api/conversation?account=UID&conversation=ID body
`{confirm_delete:true,expected_message_count}` permanently deletes, returns result.
POST api/messages/delete?account=UID body `{confirm_delete:true,messages:[...]}`.
POST api/conversations/delete?account=UID body `{confirm_delete:true,conversations:[...]}`.
POST api/message/restore?account=UID&uuid=UUID body `{expected_revision}` restores
an annotation. Existing DELETE-without-confirm body is rejected (400), never
interpreted as permanent deletion. All writes use existing session/CSRF/Origin.

## Vue

Add Chinese per-item delete, message/conversation selection checkboxes, visible
selected count, current-page select-all and separate batch-delete controls.
Selections may span pages of the same query; clear on account/conversation/
search/date/hidden-scope change. Confirmation identifies selected count, database
scope and irreversible nature; disable double submissions. Failure leaves
selection for review; successful deletion reloads account/conversation/message/
calendar totals, clamps empty pages and clears selection. Guard asynchronous
results so account switching cannot delete or paint another scope's state.
Show a month calendar with recorded-day counts and navigation; clicking a date
filters the paginated messages and resetting date restores the broader view.
Chinese error messages; retain working raw view/media/bubbles/edit/restore.
