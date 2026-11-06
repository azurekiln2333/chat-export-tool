/** Decode a newline-delimited batch of local SILK files without network access. */
import fs from 'node:fs';
import readline from 'node:readline';
import { decode } from 'silk-wasm';

for await (const line of readline.createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  if (!line.trim()) continue;
  const job = JSON.parse(line);
  try {
    const { data } = await decode(fs.readFileSync(job.source), 24000);
    const header = Buffer.alloc(44);
    header.write('RIFF', 0); header.writeUInt32LE(36 + data.length, 4);
    header.write('WAVEfmt ', 8); header.writeUInt32LE(16, 16);
    header.writeUInt16LE(1, 20); header.writeUInt16LE(1, 22);
    header.writeUInt32LE(24000, 24); header.writeUInt32LE(48000, 28);
    header.writeUInt16LE(2, 32); header.writeUInt16LE(16, 34);
    header.write('data', 36); header.writeUInt32LE(data.length, 40);
    fs.writeFileSync(job.output, Buffer.concat([header, Buffer.from(data)]), { flag: 'wx' });
    console.log(JSON.stringify({ ok: true }));
  } catch (error) { console.log(JSON.stringify({ ok: false, error: error.message })); }
}
