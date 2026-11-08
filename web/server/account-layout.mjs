import fs from 'node:fs';
import path from 'node:path';

const LEGACY = /^(?:[0-9]+|[0-9a-f]{16,64})$/;
const PREFIXED = /^(wx|xhs|dy|wxpc|qqpc)_([0-9a-f]{6,64})$/;
export const APP_PREFIX = { wechat: 'wx', xhs: 'xhs', duoshan: 'dy', wechat_pc: 'wxpc', qq_pc: 'qqpc' };

/** Only explicit single-app metadata can identify an account's source. */
export function canonicalSourceApp(value) {
  const label = typeof value === 'string' ? value.trim().toLowerCase() : '';
  if (['wechat_pc', 'wechat pc', 'pc 微信'].includes(label)) return 'wechat_pc';
  if (['qq_pc', 'qq pc', 'pc qq'].includes(label)) return 'qq_pc';
  if (['wechat', 'com.tencent.mm', '微信'].includes(label)) return 'wechat';
  if (['xhs', 'xiaohongshu', 'com.xingin.xhs', '小红书'].includes(label)) return 'xhs';
  if (['duoshan', 'douyin', 'my.maya.android', '多闪', '抖音', '多闪/抖音'].includes(label)) return 'duoshan';
  return '';
}

/** Folder labels are independent of the account IDs used by messages/edits. */
export function accountDirectories(root) {
  const seen = new Set();
  return fs.readdirSync(root, { withFileTypes: true })
    .filter(entry => entry.isDirectory())
    .map(entry => {
      const match = PREFIXED.exec(entry.name);
      if (!match && !LEGACY.test(entry.name)) return null;
      return { uid: match ? match[2] : entry.name, directory: entry.name,
        app: match ? ({ wx: 'wechat', xhs: 'xhs', dy: 'duoshan', wxpc: 'wechat_pc', qqpc: 'qq_pc' })[match[1]] : '' };
    }).filter(Boolean).sort((a, b) => a.uid.localeCompare(b.uid))
    .map(account => {
      if (seen.has(account.uid)) throw new Error(`Duplicate account directories for ${account.uid}`);
      seen.add(account.uid);
      return account;
    });
}

export function accountDirectory(root, uid) {
  const found = accountDirectories(root).find(account => account.uid === uid);
  return path.join(root, found ? found.directory : uid);
}
