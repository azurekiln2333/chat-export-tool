# Local classic QQ codec host

This directory contains original bridge source, not Tencent binaries or copied
upstream hook code. Build with Windows .NET Framework 4's C# compiler:

```powershell
powershell -ExecutionPolicy Bypass -File native\legacy_qq\build.ps1
```

The default local bundle is `.work/legacy-qq`; set
`CHAT_ARCHIVE_LEGACY_QQ_DIR` to select another directory. Keep the generated
`legacy-qq-helper.exe` and the locally extracted dependency closure together.
The helper is x86 even when the GUI uses 64-bit Python. It searches only the
explicit component directory and Windows system directory. It never loads the
codec inside the GUI, attaches to a client or modifies an input snapshot.

Only the supplied QQ **9.7.23.29348** KernelUtil.dll is verified, SHA-256:
`46f2cd17c647ed0b2aad1c0c448135fbd4485eebb6d14392e9223257e1aee9c2`.
Unknown versions are rejected before loading. Offsets are specific to that hash.
The installer is an input archive, not an application to execute.

The package's static dependency closure is KernelUtil, GF, xGraphic32, xImage,
Common, tinyxml, AsyncTask, jsonc, arkXML, zlib, arkImage, arkIOStub, pcre2,
libcrypto-1_1, arkFS, arkGraphic, libexpat, libimagequant, libjpegturbo, libpng,
sqlite, plus msvcr100 and the x86 Microsoft runtime. A lone KernelUtil.dll is
insufficient. Keep these proprietary dependencies local; do not add them to Git.

## Protocol and validation

`legacy-qq-helper.exe probe COMPONENT_DIRECTORY` returns protocol-1 JSON.
`decrypt COMPONENT_DIRECTORY` reads one UTF-8 JSON line from stdin containing
`source`, `output`, and `key` (32 hex characters). Keys never appear in argv or
diagnostic output. Both input and existing outputs are protected. The helper
copies into a new output, verifies full source/copy SHA-256 equality, verifies
the key and full SQLite integrity, disables
rollback journals on the disposable copy, rekeys that copy to plaintext, then
verifies again. Parent-side independent SQLite validation is required before
publication. Nonempty WAL/journals are rejected, not silently discarded.

`fixture COMPONENT_DIRECTORY` accepts `{ "output": "NEW_PATH", "wrapped": true }`.
It creates a small synthetic encrypted database using the public test key
`00112233445566778899aabbccddeeff`. It accepts no user SQL. This command exists
only to validate the codec bridge; it does not obtain a real account key.

Native entry points were inspected against the exported CppSQLite3DB wrappers.
`execDML` calls RVA `0x35990` with five C arguments; its other call to RVA
`0x35ce1` reads SQLite's changes counter and is **not** sqlite3_exec.
The native `close` wrapper calls `0x3596e`. Key/open/rekey RVAs and the header
initializer are pinned to the verified component. Synthetic wrapped/unwrapped
fixtures must round-trip exact Unicode, NULL and binary values; wrong keys and
corrupt files must not publish success.

Automatic classic key recovery is not implemented. The known public key hook
requires process injection, outside this tool's read-only acquisition boundary.
Supply the matching classic account key. An NT key or successful DLL load does
not establish recovery of the user's old chat database.
