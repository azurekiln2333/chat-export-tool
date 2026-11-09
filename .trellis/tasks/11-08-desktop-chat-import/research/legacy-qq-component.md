# Classic QQ local component verification, 2026-11-09

The user supplied QQ 9.7.23.29348. Authenticode validation identified Tencent's
signed installer; it was extracted as an archive and never executed. This is
not the 29368 version mentioned in older research.

KernelUtil.dll is x86, SHA-256
`46f2cd17c647ed0b2aad1c0c448135fbd4485eebb6d14392e9223257e1aee9c2`.
Its recursive local import closure contains 22 DLLs, including msvcr100 from
the installer's second archive. Each local copy was SHA-256 checked. No binary,
installer, actual key, account identifier or chat record belongs in Git.

Independent disassembly of exported CppSQLite3DB wrappers identified the native
open/key/rekey/close/exec functions. The public sample's alleged exec pattern
`558BEC8B45088B40505DC3` is the changes-counter getter (RVA 0x35ce1), not exec.
The five-argument call from execDML targets RVA 0x35990. The wrapper close target
is 0x3596e. The helper pins the whole component hash before calling any RVA.
No upstream hook/scanner source was copied into the implementation.

An original .NET Framework x86 helper loads only the explicit local bundle and
Windows system DLLs. It creates an exclusive private copy, validates the key
and SQLite integrity, rekeys only that copy, then validates again. Python strips
the QQ wrapper and independently verifies normal SQLite before publication.
The 1 GiB shifted-lock layout uses the existing isolated QQ normalization path.
Keys go through stdin, not argv; untrusted native errors are never displayed.
Nonempty classic WAL/journal sidecars are rejected, not silently ignored.

Observed synthetic validation: actual DLL-produced encrypted plain/wrapped
fixtures round-trip exact Chinese text, NULL and binary values. Wrong keys and
corruption fail without publishing output. Cancellation retains inputs and
partials. A real encrypted classic fixture and plaintext NT fixture merge into
one reader account: first partial with no classic key, then completed with the
key, with both histories and direction preserved and subsequent import unchanged.
This validates implementation, not the user's real old history.

Validation: the full Python suite passed 338 tests without skips; after the
copy-hash/error-code hardening and one additional mixed-credential case, all
17 focused native/bridge/key tests passed without skips. Compileall and doctor
passed. Retained logs are `python-legacy-full-20261109-1.log` and
`python-legacy-native-final-20261109-2.log` under the external validation root.
The prior 169-test Node baseline remains unchanged; real Node reader merges
were exercised again through the Python integration tests. No web source changed.

The four requested external Pi agents received tasks in the correct Herdr
workspace. Their provider first returned rate limits, then rejected the client;
all four stopped before making code changes. Root continued implementation and
used a separate read-only review. Do not attribute root's changes to those Pi.

External remaining condition: a matching real classic account key is not
available. No QQ process was running at this checkpoint. The public classic key
method intercepts client calls using injection, outside the established read-only
process-access boundary. Automatic classic key acquisition is not implemented.
Keep the real old-history requirement open; never infer migration into NT.
Read-only discovery found five real Msg3.0 databases; all had the classic wrapper
and encrypted payload. None was presented as decrypted or imported by this step.
