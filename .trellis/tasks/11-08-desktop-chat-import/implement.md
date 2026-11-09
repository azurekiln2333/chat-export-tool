# Implementation

1. Read-only source inventory and primary-source research.
2. Desktop discovery and snapshot/parser adapter with synthetic tests.
3. GUI checkbox/account selection and automatic local discovery.
4. Root integrates library/CLI and reader formats, then validates incremental imports.
5. Required Python suite, Node build/tests, source retention checks and real-data format validation. Preserve all test artifacts under E:/analysis.
6. Document observed limitations and restart idle GUI when ready.
7. User clarification: continue legacy QQ decryption/schema work and local QQ
   media recovery before delivery. Compare recovered legacy/NT identities and
   time ranges only after real decryption; no assumption of complete migration.

Implementation and independent review are complete for the current code
checkpoint. Validation passed: 322 Python tests, 169 Node tests, reader build,
compileall and doctor. Test artifacts remain outside the repository on E.

Actual NT inventory contains 6,473,385 records across five nonempty supported
message tables; 368 sampled rows retained exact raw values and unique IDs.
QQ media and crypto research notes record separate real-data validation.

The local classic QQ codec bridge is now implemented and independently reviewed.
Real DLL-generated wrapped/unwrapped fixtures and encrypted mixed-format reader
imports pass. Independent review added a full hash check before native decryption;
classic-only explicit credentials intentionally do not enable NT memory scanning,
and that behavior has a dedicated mixed-import test and documentation.

Remaining real-history work requires a matching classic QQ key or decrypted copy.
Do not infer classic-to-NT migration or mark full legacy recovery complete.
No more computer-use actions after user Escape; no original or temporary files
were deleted. All component binaries stay in ignored local storage on E.
