"""Read Android WeChat backup snapshots without modifying their source files.

Verified format: EnMicroMsg, SQLCipher AES-CBC, 1024-byte pages, SHA1
PBKDF2/4000, no page HMAC. Unknown formats fail validation instead of being
reported as empty accounts. Stable identities are scoped to each account.
"""
from __future__ import annotations

import base64
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import sqlite3
import struct
import subprocess
import threading
import uuid
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from ..database import _load_sqlcipher
from ..errors import ExportError
from ..source import _open_tar, _safe_parts, _looks_like_archive_file

APP_PACKAGE = 'com.tencent.mm'
HASH = re.compile(r'^[0-9a-f]{32}$')
TYPE_LABELS = {1:'文本',3:'图片',34:'语音',42:'名片',43:'视频',47:'表情',48:'位置',49:'文件',62:'视频',10000:'系统消息',10002:'系统消息'}
MEDIA_DIRS = {'image2','image','video','voice2','voice','attachment','emoji','record'}


def _check(cancel):
    if cancel and cancel():
        raise ExportError('操作已取消；已完成的缓存和现有资料库保留。')


def _dump(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'))


def _raw(value):
    if isinstance(value, bytes):
        return {'__bytes_base64__': base64.b64encode(value).decode('ascii')}
    return value


def source_key(row):
    """Stable across snapshots; never deduplicate one account against another."""
    server = int(row.get('msgSvrId') or 0)
    talker = str(row.get('talker') or '')
    if server > 0:
        identity = ['server', talker, str(server)]
    else:
        identity = ['local', talker, str(row.get('msgId') or ''),
                    str(row.get('createTime') or ''), str(row.get('type') or ''),
                    str(row.get('isSend') or 0), str(row.get('content') or '')]
    return 'wechat:' + hashlib.sha256(_dump(identity).encode()).hexdigest()


def _category(parts, head):
    if any(part in {'voice','voice2'} for part in parts):
        category = 'audio'
    elif 'emoji' in parts:
        category = 'emoji'
    elif 'video' in parts:
        category = 'videos'
    elif any(part in {'image','image2'} for part in parts):
        category = 'images'
    else:
        category = 'files'
    if head.startswith(b'wxgf'):
        return ('emoji' if category=='emoji' else 'images'),'.wxgf'
    signatures = [(b'\xff\xd8\xff','.jpg'),(b'\x89PNG','.png'),(b'GIF8','.gif'),
                  (b'#!AMR','.amr'),(b'OggS','.ogg'),(b'ID3','.mp3'),
                  (b'%PDF','.pdf'),(b'PK\x03\x04','.zip'),(b'fLaC','.flac')]
    for magic, ext in signatures:
        if head.startswith(magic):
            if ext in {'.jpg','.png','.gif'} and category != 'emoji':category='images'
            return category, ext
    if b'SILK_V3' in head[:20]:return 'audio','.silk'
    if head[:4]==b'RIFF':
        if head[8:12]==b'WAVE':return 'audio','.wav'
        if head[8:12]==b'WEBP':return ('emoji' if category=='emoji' else 'images'),'.webp'
    if head[4:8]==b'ftyp':
        return ('images','.heic') if head[8:12] in (b'heic',b'heix',b'mif1') else ('videos','.mp4')
    suffix=Path(parts[-1]).suffix.lower()
    return category, suffix if re.fullmatch(r'\.[a-z0-9]{1,8}',suffix) else '.bin'


def _metadata_member(parts, size):
    base=parts[-1]
    return (base in {'EnMicroMsg.db','EnMicroMsg.db-wal','EnMicroMsg.db-journal'}
            or (size < 4*1024*1024 and ('shared_prefs' in parts or
                ('mmkv' in parts and base in {'mmkv.default','system_config_prefs','com.tencent.mm_preferences','auth_info_key_prefs'})))
            or base in {'systemInfo.cfg','CompatibleInfo.cfg'})


def _media_member(parts):
    return 'MicroMsg' in parts and any(p in MEDIA_DIRS for p in parts) and not parts[-1].endswith(('.tmp','.bak'))


def _sha(path, cancel=None):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(4*1024*1024),b''):
            _check(cancel);h.update(b)
    return h.hexdigest()


def _separate(a, b, label):
    a=Path(a).resolve();b=Path(b).resolve()
    if a==b or a.is_relative_to(b) or b.is_relative_to(a):
        raise ExportError(label)


def _verified_copy(source, target, digest, cancel=None):
    """Publish only a complete verified file; retain interrupted files for review."""
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists():
        if _sha(target,cancel)!=digest:raise ExportError('现有文件内容校验不符：'+str(target))
        return
    partial=target.with_name(target.name+'.partial-'+uuid.uuid4().hex[:12])
    h=hashlib.sha256()
    with Path(source).open('rb') as src,partial.open('xb') as dst:
        for block in iter(lambda:src.read(4*1024*1024),b''):
            _check(cancel);h.update(block);dst.write(block)
    if h.hexdigest()!=digest:raise ExportError('复制期间源文件已改变，请重新扫描备份。')
    partial.rename(target)


def _cached_metadata(cache, manifest, cancel=None):
    try:
        data=json.loads(manifest.read_text(encoding='utf-8'))
        entries=data['metadata']
        if not entries:return None
        for entry in entries:
            path=(cache/entry['path']).resolve()
            if not path.is_relative_to(cache) or not path.is_file() or _sha(path,cancel)!=entry['sha256']:
                return None
        return data
    except (OSError,KeyError,ValueError,TypeError):
        return None


def prepare_backup(source,work,*,progress=print,cancel=None,scan_media=False):
    source=Path(source).resolve();work=Path(work).resolve()
    _separate(work,source,'微信工作目录必须与原始备份分开。')
    work.mkdir(parents=True,exist_ok=True)
    archives=([source] if source.is_file() and _looks_like_archive_file(source) else
              sorted(p for p in source.glob('*') if p.is_file() and _looks_like_archive_file(p)
                     and not p.name.lower().startswith(('apk','icon','user_de'))))
    if archives:
        identity=hashlib.sha256()
        for p in archives:
            progress('校验微信备份：'+p.name);identity.update(p.name.encode());identity.update(_sha(p,cancel).encode())
        cache=work/('wechat-'+identity.hexdigest()[:20]);cache.mkdir(exist_ok=True)
        manifest=cache/('media-scan.json' if scan_media else 'database-scan.json')
        info=_cached_metadata(cache,manifest,cancel) if manifest.is_file() else None
        if info:
            info['archives']=archives;info['root']=cache
            return info
        previous=cache/'database-scan.json'
        prior=_cached_metadata(cache,previous,cancel) if previous.is_file() else None
        verified={entry['path']:entry for entry in prior['metadata']} if prior else {}
        media=[];metadata={}
        for p in archives:
            progress('扫描微信备份：'+p.name)
            with _open_tar(p) as archive:
                for member in archive:
                    _check(cancel)
                    parts=_safe_parts(member.name)
                    if not member.isfile() or not parts:continue
                    meta=_metadata_member(parts,member.size);is_media=scan_media and _media_member(parts)
                    if not meta and not is_media:continue
                    if member.size>8*1024**3:raise ExportError('备份成员超过 8 GiB 限制：'+member.name)
                    with archive.extractfile(member) as f:
                        if meta:
                            target=cache.joinpath(*parts);target.parent.mkdir(parents=True,exist_ok=True)
                            relative=target.relative_to(cache).as_posix()
                            if relative in verified:
                                metadata[relative]=verified[relative]
                                continue
                            if target.exists():target.rename(target.with_name(target.name+'.unverified-'+uuid.uuid4().hex[:12]))
                            partial=target.with_name(target.name+'.partial-'+uuid.uuid4().hex[:12])
                            h=hashlib.sha256();size=0
                            with partial.open('xb') as dest:
                                for block in iter(lambda:f.read(4*1024*1024),b''):
                                    _check(cancel);h.update(block);size+=len(block);dest.write(block)
                            if size!=member.size:raise ExportError('备份数据库提取不完整。')
                            partial.rename(target)
                            metadata[relative]={'path':relative,'size':size,'sha256':h.hexdigest()}
                        elif is_media:
                            h=hashlib.sha256();head=f.read(64);h.update(head)
                            for block in iter(lambda:f.read(4*1024*1024),b''):
                                _check(cancel);h.update(block)
                            category,ext=_category(parts,head)
                            media.append({'archive':str(p),'member':member.name,'size':member.size,
                                          'sha256':h.hexdigest(),'category':category,'ext':ext})
                    if len(media) and len(media)%25000==0:progress(f'已索引微信媒体 {len(media):,} 个')
        info={'media':media,'metadata':list(metadata.values()),'fingerprint':identity.hexdigest()}
        manifest.write_text(_dump(info),encoding='utf-8')
        info.update(root=cache,archives=archives)
        return info
    # Extracted sources are copied privately when a database is opened.
    roots=[source]
    if (source/APP_PACKAGE).is_dir():roots=[source/APP_PACKAGE]
    dbs=list(roots[0].rglob('EnMicroMsg.db'))
    if source.is_file() and source.name=='EnMicroMsg.db':dbs=[source];roots=[source.parent.parent.parent]
    if not dbs:raise ExportError('没有找到微信 EnMicroMsg.db 或 DataBackup 备份包。')
    media=[]
    if scan_media:
        for p in roots[0].rglob('*'):
            _check(cancel)
            if p.is_file() and _media_member(p.parts):
                with p.open('rb') as f:category,ext=_category(p.parts,f.read(64))
                media.append({'file':str(p),'member':p.relative_to(roots[0]).as_posix(),'size':p.stat().st_size,
                              'sha256':_sha(p,cancel),'category':category,'ext':ext})
    return {'root':roots[0],'archives':[],'media':media}


def _account_keys(root,accounts,*,imei=None,uin=None,password=None):
    candidates=set([str(uin)] if uin else []);devices=[str(imei)] if imei else []
    devices.append('1234567890ABCDEF')
    blobs=[]
    for p in Path(root).rglob('*'):
        if p.is_file() and p.stat().st_size<4*1024*1024 and (
                'shared_prefs' in p.parts or 'mmkv' in p.parts or p.name.endswith('.cfg')):
            b=p.read_bytes();blobs.append(b)
            candidates.update(v.decode() for v in re.findall(rb'-?\d{5,12}',b))
            if p.suffix=='.xml':
                try:
                    for e in ET.fromstring(b):
                        if any(x in e.get('name','').lower() for x in ('imei','deviceid','device_id')):
                            v=e.get('value',e.text or '')
                            if v and len(v)<128:devices.append(v)
                except ET.ParseError:pass
    mapped={hashlib.md5(('mm'+v).encode()).hexdigest():v for v in candidates}
    result={}
    for account in accounts:
        if password:result[account]=[str(password)];continue
        value=mapped.get(account)
        if not value:
            for b in blobs:
                if len(b)>1024*1024:continue
                for pos in range(len(b)-3):
                    for fmt in ('>i','<i'):
                        v=str(struct.unpack_from(fmt,b,pos)[0])
                        if hashlib.md5(('mm'+v).encode()).hexdigest()==account:value=v;break
                    if value:break
                if value:break
        result[account]=list(dict.fromkeys(hashlib.md5((d+value).encode()).hexdigest()[:7]
                                           for d in devices)) if value else []
    return result


@contextmanager
def open_account(path,work,keys,*,sqlcipher_module_path=None):
    path=Path(path);work=Path(work);work.mkdir(parents=True,exist_ok=True)
    h=hashlib.sha256();digests={}
    for suffix in ('','-wal','-journal'):
        p=Path(str(path)+suffix)
        if p.is_file():
            digests[suffix]=_sha(p);h.update(suffix.encode());h.update(digests[suffix].encode())
    snapshot=work/('db-'+h.hexdigest()[:24]);snapshot.mkdir(exist_ok=True)
    target=snapshot/path.name
    for suffix in ('','-wal','-journal'):
        src=Path(str(path)+suffix);dst=Path(str(target)+suffix)
        if suffix in digests:
            if dst.exists() and _sha(dst)!=digests[suffix]:
                dst.rename(dst.with_name(dst.name+'.invalid-'+uuid.uuid4().hex[:12]))
            _verified_copy(src,dst,digests[suffix])
    with target.open('rb') as f:plain=f.read(16)==b'SQLite format 3\0'
    db=None
    if plain:
        db=sqlite3.connect(target.as_uri()+'?mode=ro',uri=True)
    else:
        if not keys:raise ExportError('未能从备份定位账号密钥材料；请提供该备份的 UIN/IMEI 或数据库密码。')
        cipher=_load_sqlcipher(sqlcipher_module_path)
        for key in keys:
            candidate=cipher.connect(target.as_uri()+'?mode=ro',uri=True)
            try:
                candidate.execute("PRAGMA key='"+key.replace("'","''")+"'")
                for statement in ('cipher_page_size=1024','kdf_iter=4000','cipher_hmac_algorithm=HMAC_SHA1',
                                  'cipher_kdf_algorithm=PBKDF2_HMAC_SHA1','cipher_use_hmac=OFF'):
                    candidate.execute('PRAGMA '+statement)
                candidate.execute('SELECT count(*) FROM message').fetchone()
                db=candidate;break
            except Exception:candidate.close()
        if db is None:raise ExportError('微信数据库解密失败：密钥不匹配、文件损坏或数据库版本尚未支持。')
    try:
        db.execute('PRAGMA query_only=ON')
        cols={r[1] for r in db.execute('PRAGMA table_info(message)')}
        if not {'msgId','msgSvrId','talker','isSend','createTime','content'}.issubset(cols):
            raise ExportError('不支持的微信消息表结构。')
        yield db
    finally:db.close()


def _accounts(info,**key_options):
    dbs=sorted(p for p in Path(info['root']).rglob('EnMicroMsg.db') if HASH.fullmatch(p.parent.name))
    if not dbs:raise ExportError('备份中未发现受支持的微信账号数据库。')
    keys=_account_keys(info['root'],[p.parent.name for p in dbs],**key_options)
    return [(p,keys[p.parent.name]) for p in dbs]


def inspect_backup(source,work,*,progress=print,cancel=None,sqlcipher_module_path=None,password=None,imei=None,uin=None):
    info=prepare_backup(source,work,progress=progress,cancel=cancel)
    accounts=[]
    for path,keys in _accounts(info,password=password,imei=imei,uin=uin):
        _check(cancel)
        with open_account(path,Path(work)/'snapshots',keys,sqlcipher_module_path=sqlcipher_module_path) as db:
            count=db.execute('SELECT COUNT(*) FROM message').fetchone()[0]
            accounts.append({'uid':path.parent.name,'message_count':count,'status':'readable'})
    return {'app':'wechat','source':str(source),'accounts':accounts,'status':'ok'}


def _dict_rows(db,table):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'",(table,)).fetchone():return
    cur=db.execute('SELECT * FROM "'+table+'"');names=[c[0] for c in cur.description]
    for row in cur:yield dict(zip(names,row))


def _media_refs(db):
    refs=defaultdict(list)
    specs=[('ImgInfo2','msglocalid',['bigImgPath','midImgPath','thumbImgPath','hevcPath']),
           ('videoinfo2','msglocalid',['video_path','filename']),
           ('voiceinfo','MsgLocalId',['FileName']),('appattach','msgInfoId',['fileFullPath'])]
    for table,idcol,columns in specs:
        for row in _dict_rows(db,table):
            mid=int(row.get(idcol) or 0)
            if mid:
                refs[mid].extend(str(row[c]) for c in columns if row.get(c))
    return refs


def _media_index(media):
    out=defaultdict(list)
    for item in media:
        parts=item['member'].replace('\\','/').split('/');account=next((p for p in parts if HASH.fullmatch(p)),'')
        base=parts[-1]
        for key in sorted({base,Path(base).stem,base.removeprefix('th_'),Path(base).stem.removeprefix('th_'),Path(base).stem.removeprefix('msg_')}):
            out[(account,key)].append(item)
    return out


def _resolve_refs(account,refs,index):
    found={}
    for ref in refs:
        base=ref.replace('\\','/').split('/')[-1];base=base.split('://')[-1]
        for key in sorted({base,Path(base).stem,base.removeprefix('th_'),Path(base).stem.removeprefix('th_'),Path(base).stem.removeprefix('msg_')}):
            for item in index.get((account,key),[]):found[item['sha256']]=item
    return sorted(found.values(),key=lambda item:(item['category'],item['sha256']))


def _playable_images(items,output,work,progress,cancel=None):
    """Keep WeChat WXGF bytes and expose a PNG preview of the first HEVC frame."""
    replacements={};warnings=[];output=Path(output).resolve()
    work=Path(work)/'image-playback';work.mkdir(parents=True,exist_ok=True)
    for item in items:
        _check(cancel)
        if item['ext']!='.wxgf':continue
        original=item['export_path'];marker=work/(item['sha256']+'.json')
        if marker.is_file():
            try:
                cached=json.loads(marker.read_text(encoding='utf-8'));target=(output/cached['path']).resolve()
                if target.is_relative_to(output) and target.is_file() and _sha(target,cancel)==cached['sha256']:
                    replacements[original]=cached['path'];continue
            except (OSError,ValueError,KeyError):pass
        preview=work/(item['sha256']+'-'+uuid.uuid4().hex[:8]+'.png')
        try:
            options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
            result=subprocess.run(['ffmpeg','-v','error','-f','hevc','-i',str(output/original),
                '-frames:v','1','-update','1','-n',str(preview)],capture_output=True,timeout=120,**options)
            if result.returncode or not preview.is_file():raise ExportError('WXGF 预览转换失败')
            with preview.open('rb') as check:
                if check.read(8)!=b'\x89PNG\r\n\x1a\n':raise ExportError('WXGF 预览格式无效')
            digest=_sha(preview,cancel);relative='attachments/wechat/'+item['category']+'/'+digest+'.png'
            _verified_copy(preview,output/relative,digest,cancel)
            marker.write_text(_dump({'path':relative,'sha256':digest}),encoding='utf-8')
            replacements[original]=relative
        except (OSError,ValueError,ExportError,subprocess.SubprocessError) as error:
            _check(cancel)
            warnings.append('WXGF 图片保留原件，但预览转换未完成：'+type(error).__name__)
    return replacements,sorted(set(warnings))


def _playable_audio(items,output,work,progress,cancel=None):
    """Keep original voice bytes and add an offline WAV playback derivative."""
    replacements={};warnings=[];output=Path(output);work=Path(work)/'voice-playback';work.mkdir(parents=True,exist_ok=True)
    decoder=None;responses=None
    decoder_script=Path(__file__).resolve().parents[3]/'web/server/decode-voice.mjs'
    def stop_decoder():
        nonlocal decoder
        if decoder is None:return
        try:
            decoder.stdin.close()
            try:decoder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                decoder.kill();decoder.wait(timeout=5)
        finally:
            decoder.stdout.close();decoder=None
    def read_responses(process, inbox):
        try:
            for line in process.stdout:inbox.put(line)
        finally:inbox.put('')
    try:
        for item in items:
            _check(cancel)
            if item['ext'] not in ('.silk','.amr'):continue
            original=item['export_path'];src=output/original;marker=work/(item['sha256']+'.json')
            if marker.exists():
                cached=json.loads(marker.read_text(encoding='utf-8'));target=(output/cached['path']).resolve()
                if target.is_relative_to(output.resolve()) and target.is_file() and _sha(target,cancel)==cached['sha256']:
                    replacements[original]=cached['path'];continue
            wav_path=work/(item['sha256']+'-'+uuid.uuid4().hex[:8]+'.wav')
            try:
                if item['ext']=='.silk':
                    if decoder is None:
                        options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
                        decoder=subprocess.Popen(['node',str(decoder_script)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,text=True,encoding='utf-8',**options)
                        responses=queue.Queue()
                        threading.Thread(target=read_responses,args=(decoder,responses),daemon=True).start()
                    decoder.stdin.write(_dump({'source':str(src),'output':str(wav_path)})+'\n');decoder.stdin.flush()
                    try:response=responses.get(timeout=60)
                    except queue.Empty:raise ExportError('SILK 解码超时')
                    if not response or not json.loads(response).get('ok'):raise ExportError('SILK 解码失败')
                else:
                    options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
                    result=subprocess.run(['ffmpeg','-v','error','-i',str(src),'-ac','1','-ar','24000','-c:a','pcm_s16le','-y',str(wav_path)],capture_output=True,timeout=120,**options)
                    if result.returncode:raise ExportError('AMR 解码失败')
                digest=_sha(wav_path);relative='attachments/wechat/audio/'+digest+'.wav';target=output/relative
                _verified_copy(wav_path,target,digest,cancel)
                marker.write_text(_dump({'path':relative,'sha256':digest}),encoding='utf-8')
                replacements[original]=relative
            except (OSError,ValueError,ExportError,RuntimeError,subprocess.SubprocessError) as error:
                _check(cancel)
                stop_decoder()
                warnings.append('语音播放转换未完成：'+type(error).__name__)
    finally:stop_decoder()
    return replacements,sorted(set(warnings))


def _write_media(items,output,work,*,progress=print,cancel=None):
    """Second archive pass writes only referenced absent media, never all media twice."""
    output=Path(output);work=Path(work);mapping={};pending=defaultdict(dict)
    for item in items:
        _check(cancel)
        rel='attachments/wechat/'+item['category']+'/'+item['sha256']+item['ext'];dest=output/rel
        item['export_path']=rel
        if dest.is_file():
            if dest.stat().st_size!=item['size'] or _sha(dest,cancel)!=item['sha256']:
                raise ExportError('现有微信附件校验不符：'+rel)
            mapping[item['sha256']]=rel;continue
        if item.get('file'):
            _verified_copy(item['file'],dest,item['sha256'],cancel)
            mapping[item['sha256']]=rel
        else:pending[item['archive']][item['member']]=item
    for archive_name,wanted in pending.items():
        progress(f'提取已引用微信媒体：{Path(archive_name).name}（{len(wanted):,} 个）')
        with _open_tar(Path(archive_name)) as archive:
            for member in archive:
                _check(cancel)
                item=wanted.get(member.name)
                if not item:continue
                dest=output/item['export_path'];dest.parent.mkdir(parents=True,exist_ok=True)
                partial=dest.with_name(dest.name+'.partial-'+uuid.uuid4().hex[:12])
                h=hashlib.sha256();size=0
                with archive.extractfile(member) as source,partial.open('xb') as target:
                    for block in iter(lambda:source.read(4*1024*1024),b''):
                        _check(cancel);h.update(block);size+=len(block);target.write(block)
                if size!=item['size'] or h.hexdigest()!=item['sha256']:
                    raise ExportError('微信附件提取校验失败：'+member.name)
                partial.rename(dest)
                mapping[item['sha256']]=item['export_path']
        if any(item['sha256'] not in mapping for item in wanted.values()):
            raise ExportError('备份中缺少已索引的微信附件，请重新扫描备份。')
    return mapping


def export_backup(source,output,work,*,timezone='Asia/Shanghai',progress=print,cancel=None,
                  sqlcipher_module_path=None,password=None,imei=None,uin=None,media_output=None):
    output=Path(output).resolve();work=Path(work).resolve();source=Path(source).resolve()
    _separate(output,source,'输出目录必须与微信原始备份分开。')
    _separate(work,source,'微信工作目录必须与原始备份分开。')
    if work==output or output.is_relative_to(work):raise ExportError('微信工作目录不能等于或包含导出目录。')
    if media_output:
        _separate(media_output,source,'微信媒体目录必须与原始备份分开。')
    output.mkdir(parents=True,exist_ok=True);tz=ZoneInfo(timezone)
    info=prepare_backup(source,work,progress=progress,cancel=cancel,scan_media=True)
    index=_media_index(info['media']);used={};results=[]
    for path,keys in _accounts(info,password=password,imei=imei,uin=uin):
        _check(cancel);uid=path.parent.name;directory=output/uid;directory.mkdir(exist_ok=True)
        progress('导出微信账号 '+uid[:8])
        with open_account(path,work/'snapshots',keys,sqlcipher_module_path=sqlcipher_module_path) as db:
            contacts={str(r.get('username') or ''):str(r.get('conRemark') or r.get('nickname') or r.get('username') or '') for r in _dict_rows(db,'rcontact')}
            refs=_media_refs(db);count=0;missing=0;seen=set()
            with (directory/'messages.jsonl').open('w',encoding='utf-8',newline='\n') as f:
                for raw in _dict_rows(db,'message'):
                    _check(cancel);key=source_key(raw)
                    if key in seen:continue
                    seen.add(key);mid=int(raw.get('msgId') or 0);talker=str(raw.get('talker') or '');own=raw.get('isSend')==1
                    text=str(raw.get('content') or '');sender=uid if own else talker
                    if talker.endswith('@chatroom') and not own and ':\n' in text:
                        sender,text=text.split(':\n',1)
                    typ=int(raw.get('type') or 0);base=typ & 0xffff;label=TYPE_LABELS.get(base,'其他')
                    ts=int(raw.get('createTime') or 0);time=datetime.fromtimestamp(ts/1000,tz).strftime('%Y-%m-%d %H:%M:%S') if ts else ''
                    candidates=refs.get(mid,[])+[str(raw.get('imgPath') or '')]
                    candidates+=re.findall(r'\bmd5=["\x27]([a-f0-9]{32})',text,re.I)
                    assets=_resolve_refs(uid,candidates,index)
                    for asset in assets:used[asset['sha256']]=asset
                    local=['attachments/wechat/'+a['category']+'/'+a['sha256']+a['ext'] for a in assets]
                    if base in (3,34,43,47,49,62) and not local:missing+=1
                    shown=text
                    if text.lstrip().startswith('<'):
                        try:
                            tree=ET.fromstring(text);title=tree.findtext('.//title')
                            shown='['+label+(': '+title if title else '')+']'
                        except ET.ParseError:pass
                    conv=talker+'@'+uid
                    rec={'msg_uuid':key,'conversation_id':conv,'conversation_name':contacts.get(talker,talker),
                         'sender':sender,'created_time':ts,'type':base,'msg_server_id':str(raw.get('msgSvrId') or 0),
                         '_wechat_raw':{k:_raw(v) for k,v in raw.items()},
                         '_export':{'app':'WeChat','source_key':key,'source_conversation':talker,'account_uid':uid,
                                    'text':shown,'time':time,'type_name':label,'sender_name':('我' if own else contacts.get(sender,sender)),
                                    'conversation_name':contacts.get(talker,talker),'conversation_id':conv,
                                    'is_group':talker.endswith('@chatroom'),'local_media':local,
                                    'media_status':'available' if local else ('missing' if base in (3,34,43,47,49,62) else 'none')}}
                    f.write(_dump(rec)+'\n');count+=1
                    if count%100000==0:progress(f'微信 {uid[:8]}：{count:,} 条')
            stats={'app':'WeChat','account_uid':uid,'nickname':'微信 · '+uid[:8],'message_count':count,'media_missing_messages':missing}
            (directory/'statistics.json').write_text(_dump(stats),encoding='utf-8')
            (directory/'contacts.json').write_text(_dump([{'user_id':k,'nickname':v} for k,v in contacts.items()]),encoding='utf-8')
            results.append(stats)
    _write_media(list(used.values()),media_output or output,work,progress=progress,cancel=cancel)
    previews,image_warnings=_playable_images(list(used.values()),media_output or output,work,progress,cancel)
    playback,warnings=_playable_audio(list(used.values()),media_output or output,work,progress,cancel)
    playback.update(previews);warnings+=image_warnings
    if playback:
        for stats in results:
            directory=output/stats['account_uid'];original=directory/'messages.jsonl';saved=directory/'messages.before-audio.jsonl'
            original.rename(saved)
            with saved.open(encoding='utf-8') as inp,original.open('x',encoding='utf-8',newline='\n') as out:
                for line in inp:
                    row=json.loads(line);meta=row['_export'];paths=meta['local_media']
                    originals=[p for p in paths if p in playback]
                    if originals:
                        meta['original_media']=originals
                        meta['local_media']=[playback.get(p,p) for p in paths]
                        if any(p in previews for p in originals):
                            meta['image_previews']=[previews[p] for p in originals if p in previews]
                    out.write(_dump(row)+'\n')
    summary={'app':'WeChat','timezone':timezone,'accounts':results,'media_assets':len(used),'status':'completed','warnings':warnings}
    (output/'export_summary.json').write_text(_dump(summary),encoding='utf-8')
    return {'status':'completed','app':'wechat','output':str(output),'summary':summary,
            'external_media_root':str(media_output) if media_output else None}
