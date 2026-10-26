"""Stable English names and legacy-path normalization for portable exports."""

from pathlib import Path
import hashlib
import re


ROOT_FILES = {
    'media_manifest': 'media_manifest.json',
    'decorations': 'decoration_manifest.json',
    'summary': 'export_summary.json',
    'data_validation': 'validation_results.json',
    'media_validation': 'media_validation_results.json',
    'reader_validation': 'reader_media_validation_results.json',
    'readme': 'README.txt',
}

ACCOUNT_FILES = {
    'database': 'chat.sqlite',
    'messages_jsonl': 'messages.jsonl',
    'messages_csv': 'messages.csv',
    'contacts_json': 'contacts.json',
    'contacts_csv': 'contacts.csv',
    'conversations': 'conversation_metadata.json',
    'statistics': 'statistics.json',
    'index_csv': 'conversation_index.csv',
}

DIRECTORIES = {
    'pages': 'conversations',
    'transcripts': 'transcripts',
    'attachments': 'attachments',
    'voice': 'voice',
    'stickers': 'stickers',
    'bubbles': 'bubbles',
    'inline_emoji': 'inline_emoji',
}

LEGACY_NAMES = {
    '多闪聊天记录': 'DuoshanChatExport',
    '媒体清单.json': ROOT_FILES['media_manifest'],
    '装饰资源清单.json': ROOT_FILES['decorations'],
    '提取统计.json': ROOT_FILES['summary'],
    '校验结果.json': ROOT_FILES['data_validation'],
    '媒体验证结果.json': ROOT_FILES['media_validation'],
    '页面媒体验证结果.json': ROOT_FILES['reader_validation'],
    '提取说明.txt': ROOT_FILES['readme'],
    '完整聊天数据库.sqlite': ACCOUNT_FILES['database'],
    '聊天记录.jsonl': ACCOUNT_FILES['messages_jsonl'],
    '聊天记录.csv': ACCOUNT_FILES['messages_csv'],
    '联系人.json': ACCOUNT_FILES['contacts_json'],
    '联系人.csv': ACCOUNT_FILES['contacts_csv'],
    '会话元数据.json': ACCOUNT_FILES['conversations'],
    '统计.json': ACCOUNT_FILES['statistics'],
    '会话索引.csv': ACCOUNT_FILES['index_csv'],
    '会话': DIRECTORIES['pages'],
    '会话TXT': DIRECTORIES['transcripts'],
    '附件': DIRECTORIES['attachments'],
    '语音': DIRECTORIES['voice'],
    '表情': DIRECTORIES['stickers'],
    '气泡': DIRECTORIES['bubbles'],
    '小表情': DIRECTORIES['inline_emoji'],
}


def conversation_basename(index):
    return f'conversation_{index:03d}'


def conversation_page(index, page):
    return f'{conversation_basename(index)}_page_{page:03d}.html'


def asset_filename(name):
    """Keep existing ASCII asset names; give other names a stable ASCII ID."""
    if name.isascii():
        return name
    suffix = Path(name).suffix.lower()
    if not suffix.isascii():
        suffix = '.bin'
    return 'asset_' + hashlib.sha256(name.encode('utf-8')).hexdigest()[:20] + suffix


def english_relative_path(value):
    """Convert only a supplied export path, never original message text or URLs."""
    if value.startswith(('http://', 'https://')):
        return value
    parts = [LEGACY_NAMES.get(part, part) for part in value.replace('\\', '/').split('/')]
    if len(parts) > 1:
        if parts[-2] == DIRECTORIES['pages']:
            match = re.fullmatch(r'(\d{3})_.*_(\d{3})\.html', parts[-1])
            if match:
                parts[-1] = conversation_page(int(match[1]), int(match[2]))
        elif parts[-2] == DIRECTORIES['transcripts']:
            match = re.fullmatch(r'(\d{3})_.*\.txt', parts[-1])
            if match:
                parts[-1] = conversation_basename(int(match[1])) + '.txt'
    if parts and not parts[-1].isascii():
        parts[-1] = asset_filename(parts[-1])
    return '/'.join(parts)
