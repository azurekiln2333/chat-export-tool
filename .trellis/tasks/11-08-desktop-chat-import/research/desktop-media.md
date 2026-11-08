# QQ local media verification

## Boundary

Source database and media directories were read-only. Message decoding used the
verified E-drive SQLite copy. Matching inspected only the selected account's
known cache roots; image/audio decoding used copied files. No request was sent
to QQ's network services. Private paths, identities, contents and key material
are not included here.

## Observed results

- 121,924 cache files were indexed for the selected account.
- 32 selected recoverability samples produced 11 original images, 12 original
  voice recordings, 4 original videos and 5 thumbnails (one image and four video
  posters). These samples are not a recovery-rate estimate for all messages.
- Content-hash fallback traversed 16.33 GB of image/file candidates and 2.73 GB
  of video candidates. Three invalid or mismatching candidates were rejected
  before selecting a verified alternative. Long scans emitted byte progress.
- SHA-256 values of 100 sampled source files were identical before and after.
- All 12 copied SILK samples decoded to 24 kHz mono WAV, totaling 141.2 seconds.
  Their original bytes and copied-original SHA-256 values remained unchanged.
- Actual QQ NT media commonly uses binary MD5 field 45406, including images
  and voice; textual 45424 is not sufficient. The implementation handles both.
- Standard numbered QQ faces with no resource reference are not missing files.
  Quoted original-message payload 47423 is excluded from current-message media.

## Direction evidence

The 236,455 private-message rows contain 15 incoming messages with status 40013
equal to 1/2 and 232 own messages with status 0. It is therefore not a direction
flag. Positive sender QQ and UID-to-QQ mapping agreed in every comparable row.
UID mapping additionally identified 83 own messages where numeric sender was
absent. Unknown senders retain an explicit unknown-direction state.

## Regression contracts

Synthetic tests cover containment, wrong-content same-name rejection, lost-cache
index recovery, legacy media references, thumbnails, cancellation, corrupted
SILK and stable media reference IDs. Reader integration tests exercise later
original recovery and cache eviction without downgrading stored originals.
Original voice files remain downloadable beside playback derivatives.
