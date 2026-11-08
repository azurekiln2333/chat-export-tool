# Design

Independent desktop discovery module and apps/desktop parser adapter; new app identifiers wechat_pc and qq_pc avoid Android collisions. Stable ASCII account UIDs derived from canonical account IDs; preserve original identifiers in metadata. Discovery source dict adds account, uid, format, status. Each desktop source represents one account root.

SHGetKnownFolderPath/registry discovers redirected Documents. Read-only discovery checks known DB locations without traversing received attachments. GUI asynchronously discovers, retains per-row check state and passes only selected sources. Transient key inputs never saved/logged.

All parsing uses verified private database/WAL snapshots in configured work under E:/analysis for this task. Supported cipher profiles require integrity/schema validation. No fake empty success for unsupported encryption. Reader gains separate wxpc/qqpc source labels and prefixes through existing interchange; existing three adapters stay unchanged.

Mixed QQ accounts snapshot NT and classic databases together, with per-database
schema/crypto dispatch. Decrypted classic records use independent IDs because
migration equivalence is unproven. Unread encrypted classic data yields explicit
partial coverage and retryable source IDs, while valid NT rows remain usable.

QQ local media is recovered from bounded account caches using recorded paths,
MD5 and resource identifiers. Content fingerprints trigger cache-only updates.
Stable media reference IDs preserve originals across later cache eviction and
replace earlier thumbnails; browser audio derivatives retain downloadable originals.
QQ's shifted lock-byte databases are normalized through an isolated SQLite reader
process into ordinary SQLite without changing the GUI process global settings.
