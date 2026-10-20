#!/usr/bin/env python3
"""Serve a PCL custom-homepage XAML listing Minecraft Java versions and loaders.

The page mirrors PCL's own 下载 → 原版游戏 layout:

    最新版本   latest release + latest snapshot
    正式版     collapsible list of every release
    预览版     collapsible list of snapshots, collapsed by default

Each version row is followed by one button per loader. A button downloads the
loader's installer that matches that exact Minecraft version:

    原版       Mojang client.jar for that version
    Forge      maven.minecraftforge.net, version from promotions_slim.json
    NeoForge   maven.neoforged.net, highest build whose prefix matches the MC version
    Fabric     fabric-installer.jar (version independent, latest installer)
    OptiFine   no public API, button opens optifine.net instead

PCL renders its custom homepage from XAML and can refresh it from a URL
(设置 → 个性化 → 主页 → 联网更新). The XAML has no loops, so the list is built
here and handed to PCL already expanded.

Endpoints
    GET /                  HTML preview, for checking without PCL
    GET /homepage.xaml     the PCL custom homepage
    GET /homepage.xaml.ini version probe PCL reads before re-downloading
    GET /api/versions      the same data as JSON
    GET /healthz           liveness

Query parameters (XAML and JSON)
    releases=<n>    release rows, default 30, max 500
    snapshots=<n>   snapshot rows, default 10, max 500
    tz=<hours>      UTC offset used for 发布于 times, default 8
    loaders=0       omit the per-version loader button row

Data sources: Mojang version manifest, Fabric meta, Forge promotions, NeoForge
maven metadata. Nothing is scraped; all are public machine-readable endpoints.

Run:
    python server.py                       # 127.0.0.1:8087
    python server.py --host 0.0.0.0        # expose on the network
    python server.py --export out.xaml     # write one static snapshot and exit
"""
import argparse
import datetime
import hashlib
import html
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# This host has no IPv6 route. getaddrinfo returns AAAA records first, so every
# request pays a failed connect (up to a full timeout) before falling back to
# IPv4, and one upstream fails outright with ENETUNREACH. Force IPv4.
_getaddrinfo = socket.getaddrinfo


def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    return _getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_only

MANIFEST_URL = 'https://piston-meta.mojang.com/mc/game/version_manifest_v2.json'
FORGE_PROMOS_URL = 'https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json'
NEOFORGE_META_URL = 'https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml'
# maven.neoforged.net is not reachable from the panel host, but BMCLAPI mirrors
# the metadata. The installer link still points at the official maven, because
# the mirror does not carry the jars and the client downloads on its own network.
NEOFORGE_META_URLS = (
    NEOFORGE_META_URL,
    'https://bmclapi2.bangbang93.com/maven/net/neoforged/neoforge/maven-metadata.xml',
)
FABRIC_INSTALLER_URL = 'https://meta.fabricmc.net/v2/versions/installer'
OPTIFINE_PAGE = 'https://optifine.net/downloads'

USER_AGENT = 'pcl-version-homepage/1.2 (+https://piston-meta.mojang.com)'

DEFAULT_RELEASES = 30
DEFAULT_SNAPSHOTS = 10
MAX_ROWS = 500
DEFAULT_TZ = 8
MANIFEST_TTL = 600          # seconds
DETAIL_TTL = 86400 * 7      # a version's JSON never changes once published
LOADER_TTL = 3600           # loader metadata: hourly is plenty

GRASS = 'pack://application:,,,/images/Blocks/Grass.png'

_cache_lock = threading.Lock()
_manifest = {'at': 0.0, 'data': None}
_details = {}
_loader_cache = {'at': 0.0, 'data': None}

# A version's JSON never changes once published, so persist the resolved
# downloads to disk. Without this every restart pays a full round of slow
# piston-meta.mojang.com fetches (about 40 s from this host).
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.cache')
DETAIL_FILE = os.path.join(CACHE_DIR, 'version_downloads.json')
LOADER_FILE = os.path.join(CACHE_DIR, 'loader_meta.json')


def _load_loader_cache():
    try:
        with open(LOADER_FILE, encoding='utf-8') as f:
            raw = json.load(f)
        if time.time() - raw.get('at', 0) < LOADER_TTL:
            _loader_cache['data'] = raw['data']
            _loader_cache['at'] = raw['at']
            sys.stderr.write('loaded cached loader metadata\n')
    except FileNotFoundError:
        pass
    except Exception as exc:
        sys.stderr.write('loader cache load failed: %s\n' % exc)


def _save_loader_cache(meta):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = LOADER_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'at': time.time(), 'data': meta}, f)
        os.replace(tmp, LOADER_FILE)
    except Exception as exc:
        sys.stderr.write('loader cache save failed: %s\n' % exc)


def _load_details():
    try:
        with open(DETAIL_FILE, encoding='utf-8') as f:
            raw = json.load(f)
        now = time.time()
        for url, downloads in raw.items():
            _details[url] = (now, downloads)
        sys.stderr.write('loaded %d cached version entries\n' % len(_details))
    except FileNotFoundError:
        pass
    except Exception as exc:
        sys.stderr.write('cache load failed: %s\n' % exc)


def _save_details():
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with _cache_lock:
            raw = {url: downloads for url, (_, downloads) in _details.items()}
        tmp = DETAIL_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(raw, f)
        os.replace(tmp, DETAIL_FILE)
    except Exception as exc:
        sys.stderr.write('cache save failed: %s\n' % exc)



# --------------------------------------------------------------------------- fetch

def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', 'replace')


def _get_json(url, timeout=30):
    return json.loads(_get(url, timeout))


def manifest():
    with _cache_lock:
        if _manifest['data'] and time.time() - _manifest['at'] < MANIFEST_TTL:
            return _manifest['data']
    data = _get_json(MANIFEST_URL)
    with _cache_lock:
        _manifest['data'] = data
        _manifest['at'] = time.time()
    return data


def client_jar(version_url):
    with _cache_lock:
        hit = _details.get(version_url)
        if hit and time.time() - hit[0] < DETAIL_TTL:
            return (hit[1].get('client') or {}).get('url')
    try:
        data = _get_json(version_url)
    except Exception:
        return None
    downloads = data.get('downloads') or {}
    with _cache_lock:
        _details[version_url] = (time.time(), downloads)
    return (downloads.get('client') or {}).get('url')


def loader_meta():
    """Forge promotions, NeoForge version list, Fabric installer. Cached.

    Fetched in parallel: these upstreams are slow from this host, and doing
    them one after another dominated the first request.
    """
    with _cache_lock:
        if _loader_cache['data'] and time.time() - _loader_cache['at'] < LOADER_TTL:
            return _loader_cache['data']

    def forge():
        return (_get_json(FORGE_PROMOS_URL, timeout=10).get('promos') or {})

    def neoforge():
        error = None
        for url in NEOFORGE_META_URLS:
            try:
                return re.findall(r'<version>([^<]+)</version>', _get(url, timeout=10))
            except Exception as exc:
                error = exc
                sys.stderr.write('neoforge metadata %s failed: %s\n'
                                 % (urlparse(url).hostname, exc))
        raise error

    def fabric():
        data = _get_json(FABRIC_INSTALLER_URL, timeout=10)
        return {'version': data[0]['version'], 'url': data[0]['url']} if data else None

    results = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {'forge': pool.submit(forge),
                   'neoforge': pool.submit(neoforge),
                   'fabric': pool.submit(fabric)}
        for name, fut in futures.items():
            try:
                results[name] = fut.result()
            except Exception as exc:
                # None means "could not check", which is different from "has no
                # build". A failed fetch must not render as 暂无构建.
                sys.stderr.write('%s metadata failed: %s\n' % (name, exc))
                results[name] = None

    meta = {'forge': results['forge'] if results['forge'] else None,
            'neoforge': results['neoforge'],
            'fabric': results['fabric']}
    with _cache_lock:
        _loader_cache['data'] = meta
        _loader_cache['at'] = time.time()
    _save_loader_cache(meta)
    return meta


def _neoforge_for(mc, versions):
    """NeoForge dropped the leading 1.: MC 1.21.8 -> 21.8.x, MC 26.3 -> 26.3.x."""
    key = mc[2:] if mc.startswith('1.') else mc
    prefix = key + '.'
    best = None
    for v in versions:
        if not v.startswith(prefix):
            continue
        tail = v[len(prefix):]
        if not tail.replace('.', '').isdigit():
            continue
        parts = tuple(int(x) for x in tail.split('.'))
        if best is None or parts > best[0]:
            best = (parts, v)
    return best[1] if best else None


def resolve_loaders(mc):
    """Buttons for one Minecraft version."""
    meta = loader_meta()
    out = []

    forge_ver = None
    if meta['forge'] is not None:
        forge_ver = (meta['forge'].get(mc + '-latest')
                     or meta['forge'].get(mc + '-recommended'))
    if forge_ver:
        url = ('https://maven.minecraftforge.net/net/minecraftforge/forge/%s-%s/'
               'forge-%s-%s-installer.jar' % (mc, forge_ver, mc, forge_ver))
        out.append({'key': 'forge', 'label': 'Forge', 'tip': 'Forge %s 安装器' % forge_ver,
                    'event': '下载文件', 'data': '%s|%s-forge-%s-installer.jar' % (url, mc, forge_ver)})
    elif meta['forge'] is None:
        out.append({'key': 'forge', 'label': 'Forge', 'tip': 'Forge 元数据暂时取不到，按钮改为打开官网',
                    'event': '打开网页', 'data': 'https://files.minecraftforge.net/net/minecraftforge/forge/'})
    else:
        out.append({'key': 'forge', 'label': 'Forge', 'tip': '该版本还没有 Forge 构建',
                    'event': '弹出提示', 'data': '%s 暂无 Forge 构建|Red' % mc})

    neo_ver = _neoforge_for(mc, meta['neoforge'] or [])
    if neo_ver:
        url = ('https://maven.neoforged.net/releases/net/neoforged/neoforge/%s/'
               'neoforge-%s-installer.jar' % (neo_ver, neo_ver))
        out.append({'key': 'neoforge', 'label': 'NeoForge', 'tip': 'NeoForge %s 安装器' % neo_ver,
                    'event': '下载文件', 'data': '%s|%s-neoforge-%s-installer.jar' % (url, mc, neo_ver)})
    elif meta['neoforge'] is None:
        out.append({'key': 'neoforge', 'label': 'NeoForge',
                    'tip': '这台机器连不上 maven.neoforged.net，按钮改为打开官网',
                    'event': '打开网页', 'data': 'https://neoforged.net/'})
    else:
        out.append({'key': 'neoforge', 'label': 'NeoForge', 'tip': '该版本还没有 NeoForge 构建',
                    'event': '弹出提示', 'data': '%s 暂无 NeoForge 构建|Red' % mc})

    if meta['fabric']:
        fv = meta['fabric']['version']
        out.append({'key': 'fabric', 'label': 'Fabric',
                    'tip': 'Fabric 安装器 %s（安装器与游戏版本无关，装的时候再选版本）' % fv,
                    'event': '下载文件',
                    'data': '%s|fabric-installer-%s.jar' % (meta['fabric']['url'], fv)})
    else:
        out.append({'key': 'fabric', 'label': 'Fabric', 'tip': 'Fabric 元数据暂时取不到',
                    'event': '打开网页', 'data': 'https://fabricmc.net/use/installer/'})

    out.append({'key': 'optifine', 'label': 'OptiFine',
                'tip': 'OptiFine 没有公开的直链接口，按钮改为打开官网下载页',
                'event': '打开网页', 'data': OPTIFINE_PAGE})

    return out


# --------------------------------------------------------------------------- data

def fmt_time(iso, tz_hours):
    if not iso:
        return ''
    try:
        dt = datetime.datetime.fromisoformat(iso.replace('Z', '+00:00'))
    except ValueError:
        return iso[:16].replace('T', ' ')
    tz = datetime.timezone(datetime.timedelta(hours=tz_hours))
    return dt.astimezone(tz).strftime('%Y/%m/%d %H:%M')


def prefetch_jars(versions):
    """Warm the client.jar cache in parallel; sequential fetches are too slow."""
    urls = [v.get('url') for v in versions if v and v.get('url')]
    if not urls:
        return
    before = len(_details)
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(client_jar, urls))
    if len(_details) != before:
        _save_details()


def collect(releases_n, snapshots_n, tz_hours, with_loaders=True):
    t0 = time.time()
    data = manifest()
    t1 = time.time()
    rows = data.get('versions') or []
    by_id = {v.get('id'): v for v in rows}

    releases = [v for v in rows if v.get('type') == 'release']
    snapshots = [v for v in rows if v.get('type') == 'snapshot']
    latest = data.get('latest') or {}

    prefetch_jars([by_id.get(latest.get('release')), by_id.get(latest.get('snapshot'))]
                  + releases[:releases_n] + snapshots[:snapshots_n])
    t2 = time.time()
    if with_loaders:
        loader_meta()
    t3 = time.time()
    sys.stderr.write('timing: manifest %.1fs, jars %.1fs, loaders %.1fs\n'
                     % (t1 - t0, t2 - t1, t3 - t2))

    def entry(v):
        if not v:
            return None
        jar = client_jar(v.get('url'))
        return {
            'id': v.get('id'),
            'type': v.get('type'),
            'released': fmt_time(v.get('releaseTime'), tz_hours),
            'url': v.get('url'),
            'client': jar,
            'loaders': resolve_loaders(v.get('id')) if with_loaders else [],
        }

    return {
        'tz': tz_hours,
        'loaders': with_loaders,
        'latest': {
            'release': entry(by_id.get(latest.get('release'))),
            'snapshot': entry(by_id.get(latest.get('snapshot'))),
        },
        'releases': [entry(v) for v in releases[:releases_n]],
        'snapshots': [entry(v) for v in snapshots[:snapshots_n]],
        'release_total': len(releases),
        'snapshot_total': len(snapshots),
        'releases_n': releases_n,
        'snapshots_n': snapshots_n,
        'fetched_at': time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime()),
    }


# --------------------------------------------------------------------------- xaml

def esc(text):
    return (str(text).replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))


def _item(v, info, with_loaders, indent='        '):
    """A version row plus, under it, one button per loader."""
    jar_tip = ('下载 %s 的原版 client.jar' % v['id']) if v.get('client') \
        else ('打开 %s 的官方版本 JSON' % v['id'])
    jar_event = ('下载文件', '%s|%s-client.jar' % (v['client'], v['id'])) if v.get('client') \
        else ('打开网页', v['url'])

    lines = [
        '%s<local:MyListItem Margin="-5,0,-5,4"' % indent,
        '%s    Logo="%s"' % (indent, GRASS),
        '%s    Title="%s"' % (indent, esc(v['id'])),
        '%s    Info="%s"' % (indent, esc(info)),
        '%s    Type="Clickable"' % indent,
        '%s    ToolTip="%s"' % (indent, esc(jar_tip)),
        '%s    EventType="%s" EventData="%s" />' % (indent, jar_event[0], esc(jar_event[1])),
    ]

    if with_loaders and v.get('loaders'):
        n = len(v['loaders'])
        lines.append('%s<Grid Margin="-5,0,-5,12">' % indent)
        lines.append('%s    <Grid.ColumnDefinitions>' % indent)
        for _ in range(n):
            lines.append('%s        <ColumnDefinition Width="1*" />' % indent)
        lines.append('%s    </Grid.ColumnDefinitions>' % indent)
        for i, ld in enumerate(v['loaders']):
            margin = '0,0,0,0' if i == n - 1 else '0,0,6,0'
            lines.append('%s    <local:MyButton Grid.Column="%d" Margin="%s" Height="30"'
                         % (indent, i, margin))
            lines.append('%s        Text="%s"' % (indent, esc(ld['label'])))
            lines.append('%s        ToolTip="%s"' % (indent, esc(ld['tip'])))
            lines.append('%s        EventType="%s" EventData="%s" />'
                         % (indent, ld['event'], esc(ld['data'])))
        lines.append('%s</Grid>' % indent)
    return lines


def build_xaml(p):
    out = []

    out.append('<local:MyCard Title="最新版本" Margin="0,0,0,15">')
    out.append('    <StackPanel Margin="25,40,23,15">')
    for key, label in (('release', '最新正式版'), ('snapshot', '最新预览版')):
        v = p['latest'].get(key)
        if v:
            out.extend(_item(v, '%s，发布于 %s' % (label, v['released']), p['loaders']))
    out.append('    </StackPanel>')
    out.append('</local:MyCard>')

    out.append('<local:MyCard Title="正式版" Margin="0,0,0,15" CanSwap="True">')
    out.append('    <StackPanel Margin="25,40,23,15">')
    if p['releases']:
        for v in p['releases']:
            out.extend(_item(v, v['released'], p['loaders']))
    else:
        out.append('        <TextBlock Text="没有取到正式版。" />')
    out.append('    </StackPanel>')
    out.append('</local:MyCard>')

    out.append('<local:MyCard Title="预览版" Margin="0,0,0,15" CanSwap="True" IsSwapped="True">')
    out.append('    <StackPanel Margin="25,40,23,15">')
    if p['snapshots']:
        for v in p['snapshots']:
            out.extend(_item(v, v['released'], p['loaders']))
    else:
        out.append('        <TextBlock Text="没有取到预览版。" />')
    out.append('    </StackPanel>')
    out.append('</local:MyCard>')

    out.append('<local:MyCard Title="说明" Margin="0,0,0,15" CanSwap="True" IsSwapped="True">')
    out.append('    <StackPanel Margin="25,40,23,15">')
    out.append('        <TextBlock TextWrapping="Wrap" Margin="0,0,0,6"')
    out.append('            Text="点版本行下载原版 client.jar。下面四个按钮下载对应加载器的安装器：'
               'Forge 和 NeoForge 会锁定到该游戏版本，Fabric 的安装器与游戏版本无关。'
               'OptiFine 没有公开直链，按钮打开官网。" />')
    out.append('        <TextBlock TextWrapping="Wrap" FontSize="11" Foreground="{DynamicResource ColorBrush2}" Margin="0,0,0,10"')
    out.append('            Text="共 %d 个正式版、%d 个预览版。数据更新于 %s。" />'
               % (p['release_total'], p['snapshot_total'], esc(p['fetched_at'])))
    out.append('        <local:MyButton Width="230" Height="35" HorizontalAlignment="Left" Padding="20,0,20,0" ColorType="Highlight"')
    out.append('            Text="打开 PCL 原版下载页"')
    out.append('            ToolTip="等同于点顶栏的「下载 → 原版游戏」。想改安装目录、内存、Java，走这条路。"')
    out.append('            EventType="切换页面" EventData="Download|DownloadInstall" />')
    out.append('    </StackPanel>')
    out.append('</local:MyCard>')
    return '\n'.join(out) + '\n'


# --------------------------------------------------------------------------- html

def build_html(p):
    def rows(items):
        out = []
        for v in items:
            jar = ('<a href="%s">原版</a>' % html.escape(v['client'])) if v.get('client') else '—'
            buttons = []
            for ld in v.get('loaders') or []:
                if ld['event'] == '下载文件':
                    url = ld['data'].split('|')[0]
                    buttons.append('<a href="%s">%s</a>' % (html.escape(url), html.escape(ld['label'])))
                else:
                    buttons.append('<span title="%s">%s</span>'
                                   % (html.escape(ld['tip']), html.escape(ld['label'])))
            out.append('<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td></tr>'
                       % (html.escape(str(v['id'])), html.escape(v['released']),
                          jar, ' · '.join(buttons) or '—'))
        return '\n'.join(out)

    latest_rows = '\n'.join(
        '<tr><th>%s</th><td><code>%s</code></td><td>%s</td></tr>'
        % (html.escape(label), html.escape(str(v['id'])), html.escape(v['released']))
        for label, v in (('最新正式版', p['latest']['release']),
                         ('最新预览版', p['latest']['snapshot'])) if v)

    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Minecraft Java 版本下载</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:980px;margin:40px auto;padding:0 16px;line-height:1.6}
 table{border-collapse:collapse;width:100%%}th,td{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left}
 code{background:#f2f2f2;padding:1px 4px;border-radius:3px}
 .muted{color:#666;font-size:14px}
</style></head><body>
<h1>Minecraft Java 版本下载</h1>
<p class="muted">来自 Mojang / Fabric / Forge / NeoForge 官方数据，清单缓存 %d 分钟，加载器元数据缓存 %d 分钟。更新于 %s，时区 UTC%+d。</p>
<p>PCL 自定义主页地址：<code>%s</code><br>填到 <b>设置 → 个性化 → 主页 → 联网更新</b>。</p>
<h2>最新版本</h2>
<table><tbody>%s</tbody></table>
<h2>正式版（%d）</h2>
<table><thead><tr><th>版本</th><th>发布于</th><th>原版</th><th>加载器</th></tr></thead><tbody>%s</tbody></table>
<h2>预览版（%d）</h2>
<table><thead><tr><th>版本</th><th>发布于</th><th>原版</th><th>加载器</th></tr></thead><tbody>%s</tbody></table>
</body></html>
""" % (MANIFEST_TTL // 60, LOADER_TTL // 60, html.escape(p['fetched_at']), p['tz'],
       html.escape(p['self_url']), latest_rows,
       p['release_total'], rows(p['releases']),
       p['snapshot_total'], rows(p['snapshots']))


# --------------------------------------------------------------------------- misc

def version_token(p):
    """Stable token for PCL's <url>.xaml.ini probe."""
    parts = [str(p['tz']), str(p['releases_n']), str(p['snapshots_n']),
             str(p['release_total']), str(p['snapshot_total']), str(p['loaders'])]
    for v in (p['latest']['release'], p['latest']['snapshot']):
        parts.append('%s@%s' % (v['id'], v['released']) if v else '-')
    for v in p['releases'][:3]:
        parts.append('%s@%s' % (v['id'], v['released']))
        for ld in v.get('loaders') or []:
            parts.append(ld['data'])
    return hashlib.sha1('|'.join(parts).encode('utf-8')).hexdigest()[:16]


# --------------------------------------------------------------------------- render cache

RENDER_TTL = 300
_render = {}
_render_lock = threading.Lock()


def rendered(key):
    """Render once per TTL and serve the same string afterwards.

    Upstream calls (Mojang, Forge, NeoForge, Fabric) take tens of seconds from
    this host, so a request must never be the thing that pays for them.
    """
    with _render_lock:
        hit = _render.get(key)
        if hit and time.time() - hit['at'] < RENDER_TTL:
            return hit
    payload = collect(*key)
    rec = {'at': time.time(), 'payload': payload,
           'xaml': build_xaml(payload), 'token': version_token(payload)}
    with _render_lock:
        _render[key] = rec
    return rec


def warm_forever(key):
    while True:
        try:
            rendered(key)
        except Exception as exc:
            sys.stderr.write('warm failed: %s\n' % exc)
        time.sleep(RENDER_TTL)


# --------------------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = 'pcl-version-homepage/1.2'
    public_base = '127.0.0.1:8087'

    def log_message(self, fmt, *args):
        sys.stderr.write('%s %s\n' % (time.strftime('%H:%M:%S'), fmt % args))

    def _send(self, body, ctype, status=200):
        raw = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(raw)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/') or '/'
        q = parse_qs(parsed.query)

        def num(name, default):
            try:
                return max(0, min(int(q.get(name, [default])[0]), MAX_ROWS))
            except (TypeError, ValueError):
                return default

        releases_n = num('releases', DEFAULT_RELEASES)
        snapshots_n = num('snapshots', DEFAULT_SNAPSHOTS)
        with_loaders = q.get('loaders', ['1'])[0] not in ('0', 'false', 'no')
        try:
            tz = float(q.get('tz', [DEFAULT_TZ])[0])
        except (TypeError, ValueError):
            tz = DEFAULT_TZ

        if path == '/healthz':
            self._send('ok\n', 'text/plain; charset=utf-8')
            return

        try:
            rec = rendered((releases_n, snapshots_n, tz, with_loaders))
        except Exception as exc:
            self._send('上游数据获取失败: %s\n' % exc, 'text/plain; charset=utf-8', 502)
            return

        payload = rec['payload']
        # Prefer the Host header so the printed URL is the one the caller used.
        payload['self_url'] = 'http://%s/homepage.xaml?releases=%d&snapshots=%d&tz=%g' % (
            self.headers.get('Host') or self.public_base, releases_n, snapshots_n, tz)

        if path in ('/homepage.xaml', '/index.xaml'):
            self._send(rec['xaml'], 'application/xml; charset=utf-8')
        elif path in ('/homepage.xaml.ini', '/index.xaml.ini'):
            self._send(rec['token'] + '\n', 'text/plain; charset=utf-8')
        elif path == '/api/versions':
            self._send(json.dumps(payload, ensure_ascii=False, indent=2),
                       'application/json; charset=utf-8')
        elif path == '/':
            self._send(build_html(payload), 'text/html; charset=utf-8')
        else:
            self._send('not found\n', 'text/plain; charset=utf-8', 404)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8087)
    ap.add_argument('--export', metavar='FILE',
                    help='write one static XAML snapshot to FILE and exit')
    ap.add_argument('--releases', type=int, default=DEFAULT_RELEASES)
    ap.add_argument('--snapshots', type=int, default=DEFAULT_SNAPSHOTS)
    ap.add_argument('--tz', type=float, default=DEFAULT_TZ)
    ap.add_argument('--no-loaders', action='store_true',
                    help='omit the per-version loader button row')
    args = ap.parse_args()

    if args.export:
        payload = collect(args.releases, args.snapshots, args.tz, not args.no_loaders)
        payload['self_url'] = '(static export)'
        text = build_xaml(payload)
        with open(args.export, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        print('wrote %s (%d bytes, %d releases + %d snapshots)'
              % (args.export, len(text.encode()), len(payload['releases']), len(payload['snapshots'])))
        return 0

    Handler.public_base = '%s:%d' % (args.host, args.port)
    _load_details()
    _load_loader_cache()
    # Warm the default page in the background so the first real request is fast.
    threading.Thread(target=warm_forever,
                     args=((DEFAULT_RELEASES, DEFAULT_SNAPSHOTS, DEFAULT_TZ, True),),
                     daemon=True).start()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print('PCL homepage XAML : http://%s:%d/homepage.xaml' % (args.host, args.port))
    print('HTML preview      : http://%s:%d/' % (args.host, args.port))
    print('JSON              : http://%s:%d/api/versions' % (args.host, args.port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nstopped')
    return 0


if __name__ == '__main__':
    sys.exit(main())
