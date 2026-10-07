#!/usr/bin/env python3
"""
wt_mine.py — локальный датамайнер War Thunder vromfs

Делает то же что и gszabi99 бот, но на твоём ПК:
 - находит *.vromfs.bin в папке War Thunder (LIVE и DEV)
 - распаковывает через wt-tools (vromfs_unpacker)
 - индексирует и сравнивает дампы
 - 🤖 САМ понимает, что изменилось (баффы/нерфы в .blk) и собирает готовый отчёт

Использование:
  # Замайнить LIVE установку
  python wt_mine.py --war-thunder "C:/Games/WarThunder" --out ./dump_live

  # Замайнить DEV установку
  python wt_mine.py --war-thunder "C:/Games/WarThunder DEV" --out ./dump_dev

  # Сравнить два дампа и получить ГОТОВЫЙ отчёт об изменениях:
  python wt_mine.py --compare ./dump_2.58.0.28 ./dump_2.59.0.16
  python wt_mine.py --compare ./dump_2.58.0.28 ./dump_2.59.0.16 --md report.md --html report.html
  python wt_mine.py --compare ./dump_2.58.0.28 ./dump_2.59.0.16 --json diff.json   # сырой diff как раньше

Требования:
  wt-tools: https://github.com/kotiq/wt-tools (скомпилировать или скачать бинарь)
  python 3.10+
"""
import argparse, os, sys, json, hashlib, subprocess, pathlib, shutil, difflib, re
from datetime import datetime

VROMFS_NAMES = [
    "aces.vromfs.bin",
    "char.vromfs.bin",
    "tex.vromfs.bin",
    "gui.vromfs.bin",
    "atlases.vromfs.bin",
    "images.vromfs.bin",
    "lang.vromfs.bin",
    "mis.vromfs.bin",
    "regional.vromfs.bin",
    "game.vromfs.bin",
]

def find_vromfs(wt_root: pathlib.Path):
    found = []
    for name in VROMFS_NAMES:
        # War Thunder хранит vromfs в корне или content/?
        candidates = [
            wt_root / name,
            wt_root / "content" / "pkg" / name,
            wt_root / "content" / name,
        ]
        for c in candidates:
            if c.exists():
                found.append(c)
                break
    return found

def unpack_vromfs(vromfs_path: pathlib.Path, out_root: pathlib.Path, wt_tools_bin: str = "vromfs_unpacker"):
    """Вызывает vromfs_unpacker из wt-tools"""
    out_dir = out_root / (vromfs_path.name + "_u")
    out_dir.mkdir(parents=True, exist_ok=True)
    # wt-tools команда: vromfs_unpacker input.bin -o output_dir
    # fallback — просто копируем если бинарь не найден (демо)
    bin_path = shutil.which(wt_tools_bin) or wt_tools_bin
    if not pathlib.Path(bin_path).exists() and not shutil.which(wt_tools_bin):
        print(f"[warn] {wt_tools_bin} не найден, делаю заглушку для {vromfs_path.name}")
        # заглушка: создаём фейковые blk для демо
        demo = out_dir / "gamedata" / "flightmodels" / "mig_35.blk"
        demo.parent.mkdir(parents=True, exist_ok=True)
        demo.write_text('mass { m: 15200.0 }\ndragCx: 0.0211\n')
        return out_dir
    cmd = [bin_path, str(vromfs_path), "-o", str(out_dir)]
    print(f"[unpack] {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)
    except subprocess.CalledProcessError as e:
        print(f"[error] unpack failed {vromfs_path}: {e.stderr[:500]}")
        raise
    return out_dir

def hash_file(p: pathlib.Path):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def index_dump(dump_root: pathlib.Path):
    """Индексирует дамп: {relative_path: {hash, size}}"""
    idx = {}
    for root, _, files in os.walk(dump_root):
        for fn in files:
            fp = pathlib.Path(root) / fn
            rel = str(fp.relative_to(dump_root)).replace(os.sep, "/")
            try:
                idx[rel] = {"hash": hash_file(fp)[:12], "size": fp.stat().st_size, "full": str(fp)}
            except: pass
    return idx

def compare_dumps(a_root: pathlib.Path, b_root: pathlib.Path):
    a_idx = index_dump(a_root)
    b_idx = index_dump(b_root)
    added, modified, deleted = [], [], []
    all_keys = set(a_idx.keys()) | set(b_idx.keys())
    for k in sorted(all_keys):
        in_a = k in a_idx
        in_b = k in b_idx
        if not in_a and in_b:
            added.append({"path": k, "size": b_idx[k]["size"], "vromfs": k.split("/")[0]})
        elif in_a and not in_b:
            deleted.append({"path": k, "size": a_idx[k]["size"], "vromfs": k.split("/")[0]})
        elif in_a and in_b and a_idx[k]["hash"] != b_idx[k]["hash"]:
            # try diff if text
            modified.append({"path": k, "size": b_idx[k]["size"], "prevSize": a_idx[k]["size"], "vromfs": k.split("/")[0]})
    return {"added": added, "modified": modified, "deleted": deleted, "stats": {"added": len(added), "modified": len(modified), "deleted": len(deleted), "total": len(added)+len(modified)+len(deleted)}}

# =====================================================================
# 🤖 Авто-анализ изменений (готовый отчёт). Приложение само понимает,
# что изменилось в .blk файлах, определяет баффы/нерфы и собирает отчёт.
# =====================================================================

# Словарь параметров BLK: ключ → (русское название, что лучше: 'up'/'down'/None)
PARAMS = {
    'dragCx': ('лобовое сопротивление (Cx)', 'down'),
    'dragCx0': ('Cx при нулевой подъёмной силе', 'down'),
    'dragCxS': ('Cx доп. коэффициент', 'down'),
    'dragCxFlaps': ('Cx с выпущенными закрылками', 'down'),
    'dragCxAirbrake': ('Cx воздушных тормозов', 'down'),
    'dragCxGears': ('Cx с выпущенным шасси', 'down'),
    'inducedDrag': ('индуктивное сопротивление', 'down'),
    'fuselageAoACd': ('сопротивление фюзеляжа', 'down'),
    'wingAoACd': ('сопротивление крыла', 'down'),
    'tailAoACd': ('сопротивление оперения', 'down'),
    'flapsAoACd': ('сопротивление закрылков', 'down'),
    'VyMax': ('скороподъёмность', 'up'),
    'climbRate': ('скороподъёмность', 'up'),
    'maxAltitude': ('практический потолок', 'up'),
    'turnTime': ('время виража', 'down'),
    'maxG': ('макс. перегрузка', 'up'),
    'criticalAoA': ('критический угол атаки', 'up'),
    'maxAoA': ('макс. угол атаки', 'up'),
    'maxSpeed': ('макс. скорость', 'up'),
    'wingArea': ('площадь крыла', None),
    'wingSpan': ('размах крыла', None),
    'maxPower': ('мощность двигателя', 'up'),
    'afterburnerPower': ('мощность форсажа', 'up'),
    'engine.maxPower': ('мощность двигателя', 'up'),
    'engine.afterburnerPower': ('мощность форсажа', 'up'),
    'fuelMass': ('запас топлива', 'up'),
    'fuelConsumption': ('расход топлива', 'down'),
    'brakeForce': ('тормозное усилие', 'up'),
    'mass': ('масса', None),
    'mass.m': ('масса', None),
    'reverseSpeed': ('скорость заднего хода', 'up'),
    'horsePower': ('мощность двигателя', 'up'),
    'power': ('мощность', 'up'),
    'crew': ('размер экипажа', None),
    'crewCount': ('размер экипажа', None),
    'cost': ('стоимость', None),
    'repairCost': ('стоимость ремонта', 'down'),
    'rotationSpeed': ('скорость поворота башни', 'up'),
    'maxVertSpeed': ('скорость вертикального наведения', 'up'),
    'maxHorSpeed': ('скорость горизонтального наведения', 'up'),
    'zoom': ('кратность прицела', 'up'),
    'thickness': ('толщина брони', 'up'),
    'maxArmorThickness': ('макс. толщина брони', 'up'),
    'armor.thickness': ('толщина брони', 'up'),
    'bullet.mass': ('масса снаряда', 'up'),
    'bullet.speed': ('начальная скорость снаряда', 'up'),
    'speed': ('скорость', 'up'),
    'startSpeed': ('начальная скорость', 'up'),
    'explosiveMass': ('масса ВВ', 'up'),
    'maxDist': ('дальность стрельбы', 'up'),
    'reloadTime': ('время перезарядки', 'down'),
    'shotFreq': ('скорострельность', 'up'),
    'rateOfFire': ('скорострельность', 'up'),
    'spread': ('разброс', 'down'),
    'recoil': ('отдача', 'down'),
    'ammo': ('боекомплект', 'up'),
    'ammoCount': ('боекомплект', 'up'),
    'penetration': ('пробитие', 'up'),
}

CATS = [
    ('fm',      re.compile(r'gamedata/flightmodels/'),               '✈️ Лётные модели (FM)', '✈️'),
    ('air',     re.compile(r'gamedata/units/(air|aircraft)'),        '🛩️ Авиация (юниты)', '🛩️'),
    ('tank',    re.compile(r'gamedata/units/(tank|ground|arty|aa)'),  '🪖 Наземная техника', '🪖'),
    ('ship',    re.compile(r'gamedata/units/(ship|naval|boat)'),     '⚓ Флот', '⚓'),
    ('units',   re.compile(r'gamedata/units/'),                      '🧩 Юниты (прочее)', '🧩'),
    ('weapons', re.compile(r'gamedata/weapons/'),                    '💥 Вооружение', '💥'),
    ('tex',     re.compile(r'tex\.vromfs\.bin_u/'),                 '🎨 Текстуры и скины', '🎨'),
    ('gui',     re.compile(r'gui\.vromfs\.bin_u/'),                 '🖥️ Интерфейс (GUI)', '🖥️'),
    ('mis',     re.compile(r'(/mis/|missions)'),                     '🗺️ Миссии', '🗺️'),
    ('lang',    re.compile(r'(lang|localization)'),                  '🌐 Локализация', '🌐'),
]
IMG_EXTS = {'png','jpg','jpeg','dds','tga','webp','avif','svg'}
UNIT_CAT_IDS = {'fm','air','tank','ship','units'}

def categorize(path, ext):
    for cid, rx, title, icon in CATS:
        if rx.search(path):
            return {'id': cid, 'title': title, 'icon': icon}
    if ext in IMG_EXTS:
        return {'id': 'img', 'title': '🖼️ Изображения', 'icon': '🖼️'}
    return {'id': 'other', 'title': '📦 Прочее', 'icon': '📦'}

NAME_FIX = {'mig':'MiG','su':'Su','yak':'Yak','la':'La','il':'Il','tu':'Tu','pe':'Pe','f':'F','a':'A','p':'P','t':'T','bf':'Bf','fw':'Fw','me':'Me','he':'He','ki':'Ki','h':'H','b':'B','i':'I','pz':'Pz','kv':'КВ','is':'ИС','bmp':'БМП','bmd':'БМД','t34':'T-34','t54':'T-54','t55':'T-55','t62':'T-62','t64':'T-64','t72':'T-72','t80':'T-80','t90':'T-90','leo':'Leo','type':'Type','amx':'AMX','cv':'CV','oto':'OTO','ztz':'ZTZ','pl':'PL'}
def prettify_name(base):
    """mig_35.blk → MiG-35, f_4e_phantom.blk → F-4E Phantom, tempest_mkv → Tempest Mk. V"""
    t = re.sub(r'\.(blk[csx]?|dds|png|jpe?g|webp|tga|svg)$', '', str(base), flags=re.I).replace('_', ' ').replace('-', ' ').strip()
    if not t:
        return str(base)
    toks = []
    for tok in t.split():
        low = tok.lower()
        if low in NAME_FIX:
            toks.append(NAME_FIX[low]); continue
        m = re.match(r'^mk(\d+|[ivx]+)([a-z]?)$', low)
        if m:
            v = m.group(1)
            v = ROMAN2AR.get(v, v) if re.fullmatch(r'[ivx]+', v) else v
            toks.append('Mk. ' + v + (m.group(2).upper() if m.group(2) else '')); continue
        m = re.match(r'^fb(\d+)$', low)
        if m: toks.append('F.B. ' + m.group(1)); continue
        m = re.match(r'^fz(\d+)$', low)
        if m: toks.append('Fz. ' + m.group(1)); continue
        toks.append(tok[:1].upper() + tok[1:])
    out = []
    for tok in toks:
        prev = out[-1] if out else None
        if prev and '-' not in prev and re.match(r'^[A-Za-z][A-Za-z0-9.]{0,9}$', prev) and re.match(r'^\d{1,3}[A-Za-z]{0,2}$', tok):
            out[-1] = prev + '-' + tok.upper()
        else:
            out.append(tok)
    return ' '.join(out)

def parse_blk_line(line):
    s = str(line or '').strip()
    if not s or s.startswith('//'):
        return None
    m = re.match(r'^([A-Za-z0-9_\-\.]+)\s*:\s*(.*?);?\s*$', s)
    if m:
        return {'kind': 'param', 'key': m.group(1), 'value': m.group(2).strip()}
    m = re.match(r'^([A-Za-z0-9_\-\.]+)\s*\{', s)
    if m:
        return {'kind': 'block', 'key': m.group(1)}
    m = re.match(r'^([A-Za-z0-9_\-\.]+)$', s)
    if m:
        return {'kind': 'bare', 'key': m.group(1)}
    return {'kind': 'raw', 'raw': s}

def nums(v):
    if v is None:
        return None
    m = re.findall(r'-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?', str(v))
    return [float(x) for x in m] if m else None

def fmt_num(n):
    return str(int(n)) if float(n).is_integer() else str(round(n, 6))

def param_info(key, ctx):
    if ctx:
        d = PARAMS.get(ctx[-1] + '.' + key)
        if d:
            return {'name': d[0], 'better': d[1]}
    p = PARAMS.get(key)
    if p:
        return {'name': p[0], 'better': p[1]}
    return {'name': key, 'better': None}

def parse_patch(patch_text):
    """Разбор unified diff в список изменений BLK (как в HTML-приложении)"""
    out = []
    ctx = []
    pending_del = []
    in_hunk = False

    def upd_ctx(line):
        s = str(line or '').strip()
        if not s:
            return
        opens = s.count('{')
        closes = s.count('}')
        if opens > closes:
            m = re.match(r'^([A-Za-z0-9_\-\.]+)', s)
            if m:
                ctx.append(m.group(1))
        elif closes > opens:
            for _ in range(closes - opens):
                if ctx:
                    ctx.pop()

    def change_from_parsed(direction, p):
        base = {'ctx': list(ctx)}
        if p['kind'] == 'param':
            base.update(kind='delParam' if direction == 'del' else 'addParam', key=p['key'], val=p['value'])
        elif p['kind'] == 'block':
            base.update(kind='delBlock' if direction == 'del' else 'addBlock', key=p['key'])
        else:
            base.update(kind='delRaw' if direction == 'del' else 'addRaw', raw=p.get('raw', p.get('key', '')))
        return base

    def make_change(del_line, add_line):
        d = parse_blk_line(del_line) if del_line is not None else None
        a = parse_blk_line(add_line) if add_line is not None else None
        if d and a:
            if d['kind'] == 'param' and a['kind'] == 'param' and d['key'] == a['key']:
                return [{'ctx': list(ctx), 'kind': 'value', 'key': d['key'], 'oldV': d['value'], 'newV': a['value']}]
            if d['kind'] == 'block' and a['kind'] == 'block' and d['key'] == a['key']:
                return [{'ctx': list(ctx), 'kind': 'value', 'key': d['key'], 'oldV': '', 'newV': ''}]
            res = []
            if d: res.append(change_from_parsed('del', d))
            if a: res.append(change_from_parsed('add', a))
            return res
        if d: return [change_from_parsed('del', d)]
        if a: return [change_from_parsed('add', a)]
        return []

    def flush_del():
        while pending_del:
            out.extend(make_change(pending_del.pop(0), None))

    for l in str(patch_text or '').split('\n'):
        if l.startswith('@@'):
            flush_del(); in_hunk = True; continue
        if l.startswith('diff ') or l.startswith('index '):
            flush_del(); in_hunk = False; continue
        if l.startswith('---') or l.startswith('+++'):
            continue
        if not in_hunk:
            continue
        if l.startswith('-'):
            pending_del.append(l[1:]); continue
        if l.startswith('+'):
            add_l = l[1:]
            add_p = parse_blk_line(add_l)
            pair_idx = 0
            # умное спаривание: ищем удалённую строку с тем же ключом параметра
            if pending_del and add_p and add_p['kind'] == 'param':
                for i, dl in enumerate(pending_del):
                    dp = parse_blk_line(dl)
                    if dp and dp['kind'] == 'param' and dp['key'] == add_p['key']:
                        pair_idx = i
                        break
            if pending_del:
                out.extend(make_change(pending_del.pop(pair_idx), add_l))
            else:
                out.extend(make_change(None, add_l))
            upd_ctx(add_l)
            continue
        flush_del()
        upd_ctx(l[1:] if l.startswith(' ') else l)
    flush_del()
    return out

def describe_change(ch):
    """Изменение → человекочитаемое описание + оценка бафф/нерф"""
    if ch['kind'] == 'value':
        if ch['oldV'] == '' and ch['newV'] == '':
            return None
        info = param_info(ch['key'], ch['ctx'])
        dn, nn = nums(ch['oldV']), nums(ch['newV'])
        if dn and nn and len(dn) == len(nn):
            if all(a == b for a, b in zip(dn, nn)):
                return None
            delta = [b - a for a, b in zip(dn, nn)]
            delta_str = (('+' if delta[0] > 0 else '') + fmt_num(delta[0])) if len(delta) == 1 else ' / '.join(('+' if d > 0 else '') + fmt_num(d) for d in delta)
            pct = pct_str = None
            if dn[0] != 0:
                pct = abs((nn[0] - dn[0]) / dn[0]) * 100
                pct_str = f" ({'+' if nn[0] > dn[0] else '-'}{pct:.1f}%)"
            score, tag = 0, ''
            if info['better']:
                good = nn[0] > dn[0] if info['better'] == 'up' else nn[0] < dn[0]
                score = 1 if good else -1
                tag = 'бафф' if good else 'нерф'
            delta_full = f"({delta_str}, {'+' if nn[0] > dn[0] else '-'}{pct:.1f}%)" if pct is not None else f"({delta_str})"
            text = f"{info['name']}: {ch['oldV']} → {ch['newV']} {delta_full}" + (f" • {tag}" if tag else '')
            return {'kind': 'value', 'name': info['name'], 'key': ch['key'], 'better': info['better'],
                    'oldV': ch['oldV'], 'newV': ch['newV'], 'deltaStr': delta_str, 'pct': pct, 'pctStr': pct_str or '',
                    'score': score, 'tag': tag, 'text': text}
        return {'kind': 'value', 'name': info['name'], 'key': ch['key'], 'better': None,
                'oldV': ch['oldV'], 'newV': ch['newV'], 'deltaStr': '', 'pct': None, 'pctStr': '',
                'score': 0, 'tag': '', 'text': f"{info['name']}: {ch['oldV']} → {ch['newV']}"}
    if ch['kind'] in ('addParam', 'addBlock'):
        info = param_info(ch['key'], ch['ctx'])
        if ch['kind'] == 'addBlock':
            text = f"добавлен блок «{info['name']}»"
        else:
            text = f"добавлен параметр {info['name']}" + (f" = {ch['val']}" if ch.get('val') is not None else '')
        return {'kind': 'add', 'name': info['name'], 'key': ch['key'], 'score': 0, 'tag': '', 'text': text}
    if ch['kind'] in ('delParam', 'delBlock'):
        info = param_info(ch['key'], ch['ctx'])
        if ch['kind'] == 'delBlock':
            text = f"удалён блок «{info['name']}»"
        else:
            text = f"удалён параметр {info['name']}" + (f" (было {ch['val']})" if ch.get('val') is not None else '')
        return {'kind': 'del', 'name': info['name'], 'key': ch['key'], 'score': 0, 'tag': '', 'text': text}
    text = ('удалена строка: ' if ch['kind'] == 'delRaw' else 'добавлена строка: ') + (ch.get('raw') or '')
    return {'kind': 'raw', 'name': '', 'key': None, 'score': 0, 'tag': '', 'text': text}

def analyze_file(f):
    """Анализ одного файла: список изменений + вердикт бафф/нерф"""
    res = {'changes': [], 'score': 0, 'verdict': 'change', 'rawCount': 0, 'note': None}
    if f.get('patch'):
        changes = parse_patch(str(f['patch'])[:300000])
        descs, score = [], 0
        for ch in changes:
            d = describe_change(ch)
            if not d:
                continue
            descs.append(d)
            score += d['score']
        res.update(changes=descs, score=score, rawCount=len(changes),
                   verdict='buff' if score > 0 else ('nerf' if score < 0 else 'change'))
        if not descs and changes:
            res['note'] = 'изменения не распознаны (не BLK-формат или бинарный diff)'
    else:
        res['note'] = 'diff недоступен (бинарный файл или лимит)'
    return res

def added_file_info(f):
    """Ключевые параметры нового файла из patch (для добавленных юнитов)"""
    if not f.get('patch'):
        return []
    want = ['mass','maxSpeed','maxAltitude','VyMax','turnTime','maxPower','afterburnerPower','crew','thickness','speed','reloadTime','ammo','maxDist']
    found, seen = [], set()
    for l in str(f['patch']).split('\n'):
        if len(found) >= 4:
            break
        if not l.startswith('+') or l.startswith('+++'):
            continue
        p = parse_blk_line(l[1:])
        if p and p['kind'] == 'param' and p['key'] in want and p['key'] not in seen:
            seen.add(p['key'])
            found.append(f"{param_info(p['key'], [])['name']}: {p['value']}")
    return found

def analyze_files(files, base_name, cmp_name):
    """Собирает готовый отчёт из списка файлов {filename, short_path, status, patch, ext, ...}"""
    rep = {
        'base': base_name, 'cmp': cmp_name,
        'generated': datetime.now().isoformat(),
        'stats': {'added': 0, 'modified': 0, 'deleted': 0, 'total': 0},
        'categories': {}, 'addedFiles': [], 'deletedFiles': [],
        'highlights': [], 'binary': {},
    }
    added = [f for f in files if f['status'] == 'added']
    modified = [f for f in files if f['status'] == 'modified']
    deleted = [f for f in files if f['status'] == 'deleted']
    rep['stats'] = {'added': len(added), 'modified': len(modified), 'deleted': len(deleted), 'total': len(files)}

    for f in added:
        cat = categorize(f['filename'], f.get('ext', ''))
        rep['addedFiles'].append({'file': f, 'cat': cat})
        if cat['id'] in UNIT_CAT_IDS and re.search(r'\.blk[csx]?$', f['filename'], re.I):
            nm = prettify_name(f['short_path'].split('/')[-1])
            info = added_file_info(f)
            rep['highlights'].append({'ico': '🆕', 'text': f"Новый юнит/FM: {nm}" + (f" — {', '.join(info)}" if info else ''), 'file': f['filename']})
        elif cat['id'] in ('tex', 'img'):
            rep['highlights'].append({'ico': '🎨', 'text': f"Новая текстура/скин: {f['short_path'].split('/')[-1]}", 'file': f['filename']})

    for f in deleted:
        cat = categorize(f['filename'], f.get('ext', ''))
        rep['deletedFiles'].append({'file': f, 'cat': cat})
        if cat['id'] in UNIT_CAT_IDS and re.search(r'\.blk[csx]?$', f['filename'], re.I):
            rep['highlights'].append({'ico': '🗑️', 'text': f"Удалён юнит/FM: {prettify_name(f['short_path'].split('/')[-1])}", 'file': f['filename']})

    for f in modified:
        cat = categorize(f['filename'], f.get('ext', ''))
        if not f.get('patch') and cat['id'] in ('tex', 'img', 'other'):
            rep['binary'][cat['id']] = rep['binary'].get(cat['id'], 0) + 1
            continue
        an = analyze_file(f)
        fr = {'file': f, 'cat': cat, 'changes': an['changes'], 'score': an['score'], 'verdict': an['verdict'], 'note': an['note']}
        rep['categories'].setdefault(cat['id'], {'cat': cat, 'files': []})['files'].append(fr)
        if an['verdict'] != 'change' and cat['id'] in UNIT_CAT_IDS:
            top = None
            for d in an['changes']:
                if d['pct'] is not None and (top is None or d['pct'] > top['pct']):
                    top = d
            if top and top['pct'] >= 5:
                rep['highlights'].append({'ico': '📈' if an['verdict'] == 'buff' else '📉',
                    'text': f"{prettify_name(f['short_path'].split('/')[-1])}: {top['name']}{top['pctStr']} ({top['oldV']} → {top['newV']})",
                    'file': f['filename']})

    for cid in list(rep['binary'].keys()):
        if cid not in rep['categories']:
            cat = next((c for c in CATS if c[0] == cid), None)
            rep['categories'][cid] = {'cat': {'id': cid, 'title': cat[2] if cat else '📦 Прочее', 'icon': cat[3] if cat else '📦'}, 'files': []}
    for crep in rep['categories'].values():
        crep['files'].sort(key=lambda fr: (-abs(fr['score']), fr['file']['filename']))
    return rep

VERDICT_LABEL = {'buff': '⬆️ бафф', 'nerf': '⬇️ нерф', 'change': '🔧 изменение'}

# =====================================================================
# 🗒️ CHANGELOG — семантический анализ (порт движка из приложения)
# Структурированный вывод как в патчноутах: юнит → тип изменения → детали,
# с группировкой одинаковых изменений (один пункт на много юнитов/карт).
# =====================================================================
COUNTRY_PREFIX = re.compile(r'^(uk|us|usa|ger|jp|ussr|fr|it|cn|sw|cz|pl|nl|fi|hu|ro|br|is|ko|au|ca)_', re.I)
BOMB_ABBR = {'mc':'M.C.', 'gp':'G.P.', 'sap':'S.A.P.', 'he':'H.E.', 'ap':'A.P.', 'apc':'A.P.C.', 'apcbc':'A.P.C.B.C.', 'apds':'A.P.D.S.', 'apcr':'A.P.C.R.', 'hvap':'H.V.A.P.', 'heat':'H.E.A.T.', 'heatfs':'H.E.A.T.-FS.', 'aphe':'A.P.H.E.', 'hefi':'H.E.F.I.', 'heiat':'H.E.I.A.T.', 'at':'A.T.', 'aa':'A.A.', 'frag':'Frag.', 'inc':'Inc.', 'apit':'A.P.I.T.'}
ROMAN = {'1':'I','2':'II','3':'III','4':'IV','5':'V','6':'VI','7':'VII','8':'VIII','9':'IX','10':'X','11':'XI','12':'XII'}
ROMAN2AR = {'i':'1','ii':'2','iii':'3','iv':'4','v':'5','vi':'6','vii':'7','viii':'8','ix':'9','x':'10'}
BOMB_NUM = {'50lb':'50 lb','100lb':'100 lb','250lb':'250 lb','500lb':'500 lb','500lbs':'500 lb','1000lb':'1000 lb','1000lbs':'1000 lb','2000lb':'2000 lb','4000lb':'4000 lb','50kg':'50 kg','100kg':'100 kg','250kg':'250 kg','500kg':'500 kg','1000kg':'1000 kg','1500kg':'1500 kg','3000kg':'3000 kg'}
BOMB_DROP_SUFFIX = re.compile(r'_(bomb|bombs|gun|guns|rocket|rockets|missile|missiles|torpedo|torpedoes|mine|mines|default|ap|long_tail|short_tail|with_tracer|tracer|practice|thin_wall|naval)$', re.I)

def prettify_bomb_path(p):
    """gameData/Weapons/BombGuns/uk_500lb_mc_mk1_mk4_long_tail_bomb.blk → '500 lb M.C. Mk. I'"""
    s = str(p or '').replace('\\', '/')
    s = s.split('/')[-1] or s
    s = re.sub(r'\.(blk|blkx)$', '', s, flags=re.I)
    s = COUNTRY_PREFIX.sub('', s)
    while BOMB_DROP_SUFFIX.search(s):
        s = BOMB_DROP_SUFFIX.sub('', s)
    out, mk_seen = [], 0
    for t in [x for x in s.split('_') if x]:
        low = t.lower()
        if low in BOMB_NUM: out.append(BOMB_NUM[low]); continue
        if low in BOMB_ABBR: out.append(BOMB_ABBR[low]); continue
        m = re.match(r'^mk(\d+|[ivx]+)$', low)
        if m:
            r = m.group(1)
            r = ROMAN.get(r, r) if r.isdigit() else r.upper()
            mk_seen += 1
            if mk_seen == 1: out.append('Mk. ' + r)
            continue
        m = re.match(r'^an/?m(\d+[a-z]?\d*)$', low)
        if m: out.append('AN/M' + m.group(1).upper()); continue
        out.append(t)
    dedup = []
    for t in out:
        if not dedup or dedup[-1] != t: dedup.append(t)
    return ' '.join(dedup) or s

PRESET_STOP = {'bombs','bomb','guns','gun','rockets','rocket','torpedoes','torpedo','mines','mine','smoke','smokes','flares','flare','chaff','countermeasures','presets','preset','loadout','loadouts','fuel','drop','tanks','tank','pods','pod','aa','aam','agm','atgm','mc','gp','sap','he','ap','heat','2x','4x','6x','8x','12x','14x'}

def _map_designation(tok):
    low = str(tok).lower()
    m = re.match(r'^mk(\d+|[ivx]+)([a-z]?)$', low)
    if m:
        v = m.group(1)
        v = ROMAN2AR.get(v, v) if re.fullmatch(r'[ivx]+', v) else v
        return 'Mk. ' + v + (m.group(2).upper() if m.group(2) else '')
    m = re.match(r'^fb(\d+)$', low)
    if m: return 'F.B. ' + m.group(1)
    m = re.match(r'^fz(\d+)$', low)
    if m: return 'Fz. ' + m.group(1)
    return tok[:1].upper() + tok[1:]

def prettify_aircraft_from_preset(fname):
    """tempest_mkv_500lbs_mc_bombs.blkx → 'Tempest Mk. 5'"""
    s = re.sub(r'\.blkx?$', '', str(fname or ''), flags=re.I)
    toks = [t for t in s.split('_') if t]
    ac = []
    for t in toks:
        low = t.lower()
        if re.match(r'^(mk|fb|fz)', low): ac.append(t); continue
        if low[0].isdigit(): break
        if low in PRESET_STOP: break
        if len(low) >= 5 and re.search(r'\d', low): break
        ac.append(t)
    if not ac and toks: ac.append(toks[0])
    return ' '.join(_map_designation(t) for t in ac)

ARMOR_MATERIAL = [
    ('dural_nikel','дюралево-никелевый сплав'), ('boron_carbide','карбид бора'),
    ('alum_alloy','алюминиевый сплав'), ('aluminum_armor','алюминиевая броня'), ('aluminium','алюминий'),
    ('space_composite','композитная броня'), ('fireproof_composite','огнеупорный композит'),
    ('tank_textolite','текстолит'), ('rubber_metal','резинометалл'), ('rubber_fabric','резиноткань'),
    ('armour_aramide','арамидная ткань'), ('dural','дюралюминий'), ('steel','сталь'), ('glass','стекло'),
    ('wood','дерево'), ('armor','броня'), ('kevlar','кевлар'), ('titan','титан'), ('composite','композит'),
    ('ceramic','керамика'), ('plexiglas','плексиглас'), ('fibreglass','стеклопластик'), ('aramid','арамид'),
    ('rubber','резина'), ('concrete','бетон'), ('brick','кирпич'), ('sand','песок'), ('graphite','графит'),
    ('RHA','RHA'), ('CHA','CHA'), ('ERA','ERA'), ('spaced_armor','разнесённая броня'), ('grille','решётка'),
]
ARMOR_PART = {'tail':'хвостовое оперение','fin':'киль и стабилизаторы','fuse':'фюзеляж','wing':'крыло','elevator':'рули, элероны и закрылки','cover':'обшивка','engine':'двигатель','cockpit':'кабина','spar':'лонжерон','turret':'башня','hull':'корпус','barrel':'ствол','track':'гусеница','wheel':'колесо','driver':'место мехвода','gunner':'пулемёт','commander':'командир','pilot':'пилот','fuel':'топливная система','cooling':'система охлаждения','ammo':'боеприпасы','optics':'оптика','antenna':'антенна','tank':'танк','tanks':'танки','jet':'реактивный'}
ARMOR_VARIANT_SKIP = {'na','nb','nbj','air','modern','light','heavy','s','m','l','mod','nbhl','screen','screens','fabric','vest','filing','shield','nb_2','c'}
ARMOR_PARAMS = {
    'explosionArmorQuality': {'name':'модификатор брони против фугасов','fmt':'pct'},
    'shatterArmorQuality': {'name':'модификатор брони против осколков','fmt':'pct'},
    'genericArmorQuality': {'name':'модификатор брони (общий)','fmt':'pct'},
    'explosionDamageMult': {'name':'множитель фугасного урона','fmt':'pct'},
    'shatterDamageMult': {'name':'множитель осколочного урона','fmt':'pct'},
    'genericDamageMult': {'name':'множитель урона (общий)','fmt':'pct'},
    'shatterEffectiveThicknessMax': {'name':'макс. эффективная толщина против осколков','fmt':'mmCap'},
    'armorThickness': {'name':'толщина брони','fmt':'mm'},
    'armorThrough': {'name':'пробитие брони','fmt':'mm'},
    'ricochetAngle': {'name':'угол рикошета','fmt':'deg'},
    'ricochetDamage': {'name':'урон при рикошете','fmt':'pct'},
    'ricochetCosinePower': {'name':'степень косинуса рикошета','fmt':'num'},
    'restrainDamage': {'name':'сдерживание урона','fmt':'pct'},
    'oneSided': {'name':'односторонняя броня','fmt':'bool'},
}

def decode_armor_class(name):
    s = str(name or '')
    composite = False
    if s.startswith('c_'):
        composite = True; s = s[2:]
    material = mat_key = None
    for key, label in ARMOR_MATERIAL:
        if s == key:
            material, mat_key, s = label, key, ''; break
        if s.startswith(key) and re.match(r'^[\d_]', s[len(key):]):
            material, mat_key, s = label, key, s[len(key):]; break
    if material is None:
        pretty = ' '.join(t[:1].upper() + t[1:] for t in s.split('_') if t)
        return {'material': pretty, 'matKey': s, 'thickness': None, 'part': '', 'label': pretty}
    thickness = None
    m = re.match(r'^(\d+(?:_\d+)?)', s)
    if m:
        thickness = m.group(1).replace('_', '.')
        s = s[len(m.group(1)):]
    parts = []
    for t in [x for x in s.split('_') if x]:
        low = t.lower()
        if low in ARMOR_VARIANT_SKIP: continue
        parts.append(ARMOR_PART.get(low, t))
    label = ''
    if thickness: label += f'{thickness} мм '
    label += material
    if parts: label += ', ' + ', '.join(parts)
    if composite: label += ' (композит)'
    return {'material': material, 'matKey': mat_key, 'thickness': thickness, 'part': ', '.join(parts), 'label': label}

def _nf(v):
    try: n = float(str(v).strip('"\''))
    except ValueError: return str(v)
    return str(int(n)) if n == int(n) else str(n)

def _fmt_armor_val(key, v, pi):
    if v is None: return '—'
    try: n = float(str(v).strip('"\''))
    except ValueError: return str(v)
    fmt = pi['fmt'] if pi else None
    if fmt == 'pct': return f'{round(n * 100)}%'
    if fmt == 'mm': return f'{_nf(v)} мм'
    if fmt == 'deg': return f'{_nf(v)}°'
    if fmt == 'bool': return 'да' if n else 'нет'
    return _nf(v)

def analyze_armor_classes_patch(patch):
    """Разбор patch файла armor_classes.blkx → изменения по классам брони"""
    out, cur, in_hunk = [], None, False
    def flush():
        nonlocal cur
        if not cur: return
        cur['delParams'] += cur['pendingDels']; cur['pendingDels'] = []
        by_key = {c['key']: c for c in cur['changes']}
        for d in cur['delParams']:
            by_key.setdefault(d['key'], {'key': d['key'], 'kind': 'del', 'oldV': d['val']})
        for a in cur['addParams']:
            by_key.setdefault(a['key'], {'key': a['key'], 'kind': 'add', 'newV': a['val']})
        out.append({'name': cur['name'], 'added': cur['added'], 'removed': cur['removed'], 'changes': list(by_key.values())})
        cur = None
    for line in str(patch or '').split('\n'):
        if line.startswith('@@'): in_hunk = True; continue
        if line.startswith('diff ') or line.startswith('index '): flush(); in_hunk = False; continue
        if line.startswith('---') or line.startswith('+++'): continue
        if not in_hunk: continue
        is_del, is_add = line.startswith('-'), line.startswith('+')
        raw = line[1:] if (is_del or is_add) else (line[1:] if line.startswith(' ') else line)
        trimmed = raw.strip()
        m = re.match(r'^"([^"]+)"\s*:\s*\{$', trimmed)
        if m:
            flush()
            cur = {'name': m.group(1), 'added': is_add, 'removed': is_del, 'changes': [], 'delParams': [], 'addParams': [], 'pendingDels': []}
            continue
        if not cur: continue
        if trimmed in ('}', '},'): flush(); continue
        kv = re.match(r'^\s*"([^"]+)"\s*:\s*(.*?)\s*,?\s*$', raw)
        if not kv: continue
        if is_add:
            idx = next((i for i, d in enumerate(cur['pendingDels']) if d['key'] == kv.group(1)), None)
            if idx is not None:
                d = cur['pendingDels'].pop(idx)
                cur['changes'].append({'key': kv.group(1), 'kind': 'value', 'oldV': d['val'], 'newV': kv.group(2)})
            else:
                cur['addParams'].append({'key': kv.group(1), 'val': kv.group(2)})
            continue
        if is_del:
            cur['pendingDels'].append({'key': kv.group(1), 'val': kv.group(2)}); continue
        cur['delParams'] += cur['pendingDels']; cur['pendingDels'] = []
    flush()
    res = []
    for c in out:
        dec = decode_armor_class(c['name'])
        base = {'name': c['name'], 'matKey': dec['matKey'], 'material': dec['material'],
                'thickness': dec['thickness'], 'part': dec['part'], 'label': dec['label']}
        if c['removed']:
            res.append(dict(base, removed=True, added=False, lines=[f"класс брони удалён: {dec['label']}"], changes=[])); continue
        if c['added']:
            res.append(dict(base, removed=False, added=True, lines=[f"класс брони добавлен: {dec['label']}"], changes=[])); continue
        lines = []
        for ch in c['changes']:
            pi = ARMOR_PARAMS.get(ch['key'])
            pname = pi['name'] if pi else ch['key']
            if ch['kind'] == 'value':
                if ch['oldV'] == ch['newV']: continue
                if ch['key'] == 'shatterEffectiveThicknessMax':
                    lines.append(f"макс. эффективная толщина против осколков: {_nf(ch['oldV'])} → {_nf(ch['newV'])} мм")
                else:
                    lines.append(f"{pname}: {_fmt_armor_val(ch['key'], ch['oldV'], pi)} → {_fmt_armor_val(ch['key'], ch['newV'], pi)}")
            elif ch['kind'] == 'add':
                if ch['key'] == 'shatterEffectiveThicknessMax':
                    lines.append(f"макс. эффективная толщина против осколков теперь ограничена {_nf(ch['newV'])} мм")
                else:
                    lines.append(f"{pname}: добавлено {_fmt_armor_val(ch['key'], ch['newV'], pi)}")
            else:
                lines.append(f"{pname}: удалено (было {_fmt_armor_val(ch['key'], ch['oldV'], pi)})")
        res.append(dict(base, removed=False, added=False, lines=lines, changes=c['changes']))
    return res

def analyze_weapon_preset_patch(patch):
    """weaponpresets: 'separate': true у бомб → 'бомбы теперь сбрасываются по одной'"""
    recs, ctx, pending, in_hunk = [], {'blk': None, 'trigger': None}, None, False
    lines = str(patch or '').split('\n')
    def upd(raw):
        m = re.search(r'"blk"\s*:\s*"([^"]*)"', raw)
        if m: ctx['blk'] = m.group(1)
        m = re.search(r'"trigger"\s*:\s*"([^"]*)"', raw)
        if m: ctx['trigger'] = m.group(1)
    def kv(raw):
        m = re.match(r'^\s*"([^"]+)"\s*:\s*(.*?)\s*,?\s*$', raw)
        return (m.group(1), m.group(2)) if m else None
    def flush_del():
        nonlocal pending
        if pending:
            recs.append({'type': 'del', 'key': pending[0], 'val': pending[1], 'ctx': dict(ctx)})
            pending = None
    i = 0
    while i < len(lines):
        l = lines[i]
        if l.startswith('@@'): flush_del(); in_hunk = True; i += 1; continue
        if l.startswith('diff ') or l.startswith('index '): flush_del(); in_hunk = False; i += 1; continue
        if l.startswith('---') or l.startswith('+++'): i += 1; continue
        if not in_hunk: i += 1; continue
        is_del, is_add = l.startswith('-'), l.startswith('+')
        raw = l[1:] if (is_del or is_add) else (l[1:] if l.startswith(' ') else l)
        upd(raw)
        trimmed = raw.strip()
        if (is_add or is_del) and trimmed == '{':
            depth, j, buf = 1, i + 1, [raw]
            while j < len(lines) and depth > 0:
                nl = lines[j]
                nraw = nl[1:] if (nl.startswith('+') or nl.startswith('-')) else (nl[1:] if nl.startswith(' ') else nl)
                buf.append(nraw)
                depth += nraw.count('{') - nraw.count('}')
                j += 1
            text = '\n'.join(buf)
            if re.search(r'"blk"\s*:', text):
                bm = re.search(r'"blk"\s*:\s*"([^"]*)"', text)
                tm = re.search(r'"trigger"\s*:\s*"([^"]*)"', text)
                recs.append({'type': 'weaponAdd' if is_add else 'weaponDel', 'bomb': bm.group(1) if bm else None,
                             'trigger': tm.group(1) if tm else None, 'ctx': dict(ctx)})
            i = j; continue
        if is_add:
            p = kv(raw)
            if pending and p and pending[0] == p[0]:
                recs.append({'type': 'value', 'key': p[0], 'oldV': pending[1], 'newV': p[1], 'ctx': dict(ctx)})
                pending = None
            else:
                recs.append({'type': 'add', 'key': p[0] if p else None, 'val': p[1] if p else None, 'ctx': dict(ctx)})
            i += 1; continue
        if is_del:
            p = kv(raw)
            pending = p; i += 1; continue
        flush_del(); i += 1
    flush_del()
    def bomb_type(c):
        trig = (c.get('trigger') or '').lower(); blk = (c.get('blk') or '').lower()
        if trig == 'bombs' or 'bomb' in blk: return 'bombs'
        if trig in ('rockets', 'missiles') or 'rocket' in blk or 'missile' in blk: return 'rockets'
        return 'other'
    PH_ADD = {'bombs': 'бомбы теперь сбрасываются по одной', 'rockets': 'теперь пускаются по одной', 'other': 'теперь сбрасываются по одной'}
    PH_DEL = {'bombs': 'бомбы больше не сбрасываются по одной', 'rockets': 'больше не пускаются по одной', 'other': 'больше не сбрасываются по одной'}
    lines_out, sep = [], {}
    def sep_add(c):
        e = sep.setdefault(c.get('blk'), {'n': 0, 'type': bomb_type(c)})
        e['n'] += 1
    for r in recs:
        if r['type'] == 'value' and r['key'] == 'separate':
            if r['newV'] == 'true': sep_add(r['ctx']); continue
            if r['oldV'] == 'true': lines_out.append(f"{prettify_bomb_path(r['ctx'].get('blk'))}: {PH_DEL[bomb_type(r['ctx'])]}"); continue
        if r['type'] == 'add' and r['key'] == 'separate' and r['val'] == 'true': sep_add(r['ctx']); continue
        if r['type'] == 'del' and r['key'] == 'separate' and r['val'] == 'true':
            lines_out.append(f"{prettify_bomb_path(r['ctx'].get('blk'))}: {PH_DEL[bomb_type(r['ctx'])]}"); continue
        if r['type'] == 'value' and r['key'] == 'bullets':
            if r['oldV'] != r['newV']: lines_out.append(f"{prettify_bomb_path(r['ctx'].get('blk'))}: боезапас {r['oldV']} → {r['newV']}")
            continue
        if r['type'] == 'value' and r['key'] == 'blk':
            lines_out.append(f"заменено оружие: {prettify_bomb_path(r['oldV'])} → {prettify_bomb_path(r['newV'])}"); continue
        if r['type'] == 'value' and r['key'] == 'trigger':
            lines_out.append(f"{prettify_bomb_path(r['ctx'].get('blk'))}: триггер {r['oldV']} → {r['newV']}"); continue
        if r['type'] == 'weaponAdd': lines_out.append(f"добавлена подвеска: {prettify_bomb_path(r['bomb'])}"); continue
        if r['type'] == 'weaponDel': lines_out.append(f"убрана подвеска: {prettify_bomb_path(r['bomb'])}"); continue
    for bomb, e in sep.items():
        lines_out.insert(0, f"{e['n']}x {prettify_bomb_path(bomb)}: {PH_ADD[e['type']]}")
    return {'lines': lines_out}

UNIT_CAT = [
    (re.compile(r'ammo', re.I), ('склад боеприпасов', 'склады боеприпасов')),
    (re.compile(r'assembly_area|assembly', re.I), ('сборочная площадка', 'сборочные площадки')),
    (re.compile(r'stronghold', re.I), ('опорный пункт', 'опорные пункты')),
    (re.compile(r'mlrs', re.I), ('РСЗО', 'РСЗО')),
    (re.compile(r'aew|awacs', re.I), ('РЛС ДРЛО', 'РЛС ДРЛО')),
    (re.compile(r'radar', re.I), ('РЛС', 'РЛС')),
    (re.compile(r'aircraftcarrier|carrier', re.I), ('авианосец', 'авианосцы')),
    (re.compile(r'destroyer', re.I), ('эсминец', 'эсминцы')),
    (re.compile(r'cruiser', re.I), ('крейсер', 'крейсеры')),
    (re.compile(r'battleship', re.I), ('линкор', 'линкоры')),
    (re.compile(r'submarine', re.I), ('подлодка', 'подлодки')),
    (re.compile(r'anti_aircraft|spaa|aa_gun', re.I), ('ЗСУ', 'ЗСУ')),
    (re.compile(r'artillery|howitzer|self_propelled', re.I), ('САУ', 'САУ')),
    (re.compile(r'tank_destroyer', re.I), ('ПТ-САУ', 'ПТ-САУ')),
    (re.compile(r'light_tank', re.I), ('лёгкий танк', 'лёгкие танки')),
    (re.compile(r'medium_tank', re.I), ('средний танк', 'средние танки')),
    (re.compile(r'heavy_tank', re.I), ('тяжёлый танк', 'тяжёлые танки')),
    (re.compile(r'helicopter', re.I), ('вертолёт', 'вертолёты')),
    (re.compile(r'bomber', re.I), ('бомбардировщик', 'бомбардировщики')),
    (re.compile(r'attacker|assault', re.I), ('штурмовик', 'штурмовики')),
    (re.compile(r'fighter', re.I), ('истребитель', 'истребители')),
    (re.compile(r'recon', re.I), ('разведчик', 'разведчики')),
    (re.compile(r'ifv', re.I), ('БМП', 'БМП')),
    (re.compile(r'apc', re.I), ('БТР', 'БТР')),
    (re.compile(r'truck', re.I), ('грузовик', 'грузовики')),
    (re.compile(r'bunker', re.I), ('бункер', 'бункеры')),
    (re.compile(r'tank', re.I), ('танк', 'танки')),
]
UNIT_ABBR = {'mlrs':'MLRS','aew':'AAEW','aa':'AA','apc':'APC','ifv':'IFV','mbt':'MBT','spaa':'SPAA','rha':'RHA','era':'ERA','heat':'HEAT','ap':'AP','he':'HE','tps':'TPS','sam':'SAM','aam':'AAM','agm':'AGM','atgm':'ATGM','radar':'РЛС','gps':'GPS','ircm':'IRCM','smerch':'«Смерч»'}
UNIT_NAME_DROP = [
    (re.compile(r'aew|awacs|radar', re.I), {'aew','awacs','radar'}),
    (re.compile(r'mlrs', re.I), {'mlrs'}),
    (re.compile(r'ammo', re.I), {'ammo','storage','factory','depot'}),
    (re.compile(r'assembly', re.I), {'assembly','area'}),
    (re.compile(r'stronghold', re.I), {'stronghold'}),
    (re.compile(r'aircraftcarrier|carrier', re.I), {'aircraftcarrier','carrier'}),
    (re.compile(r'destroyer', re.I), {'destroyer'}),
    (re.compile(r'cruiser', re.I), {'cruiser'}),
    (re.compile(r'battleship', re.I), {'battleship'}),
    (re.compile(r'submarine', re.I), {'submarine'}),
    (re.compile(r'anti_aircraft|spaa', re.I), {'anti_aircraft','spaa','aa_gun'}),
    (re.compile(r'artillery|howitzer', re.I), {'artillery','howitzer','self_propelled'}),
]

def decode_unit_class(cls):
    s = str(cls or '')
    cat = None
    for rx, forms in UNIT_CAT:
        if rx.search(s): cat = forms; break
    name = re.sub(r'^(us|uk|ger|jp|ussr|fr|it|cn|nt|ai)_', '', s, flags=re.I)
    name = re.sub(r'^(us|uk|ger|jp|ussr|fr|it|cn|nt|ai)_', '', name, flags=re.I)
    name = re.sub(r'_ai$', '', name, flags=re.I)
    all_toks = [t for t in name.split('_') if t]
    raw = all_toks
    if cat:
        for rx, drop in UNIT_NAME_DROP:
            if rx.search(s): raw = [t for t in raw if t.lower() not in drop]; break
    if not raw: raw = all_toks
    toks = []
    for t in raw:
        low = t.lower()
        if low in UNIT_ABBR: toks.append(UNIT_ABBR[low]); continue
        if low[0].isdigit(): toks.append(low.upper()); continue
        toks.append(low[:1].upper() + low[1:])
    out = []
    for t in toks:
        prev = out[-1] if out else None
        if prev and re.search(r'\d$', prev) and re.fullmatch(r'\d+', t) and len(prev) <= 6:
            out[-1] = prev + '-' + t
        else:
            out.append(t)
    return {'name': ' '.join(out), 'cat': cat[0] if cat else None, 'catPl': cat[1] if cat else None}

def mission_label(path):
    seg = str(path).split('/')
    if 'missions' not in seg: return None
    mi = seg.index('missions')
    mode = seg[mi + 3] if mi + 3 < len(seg) else ''
    if '/carriers/' in str(path):
        ci = seg.index('carriers') if 'carriers' in seg else -1
        mapn = seg[ci - 1] if ci > 0 else ''
    else:
        fn = re.sub(r'\.blkx?$', '', seg[-1], flags=re.I)
        fn = re.sub(r'^air_', '', fn, flags=re.I)
        if mode: fn = re.sub(r'_' + mode + r'_.*$', '', fn, flags=re.I)
        mapn = fn
    mapn = re.sub(r'^air_', '', mapn, flags=re.I).replace('_', ' ').strip()
    mapn = ' '.join(w[:1].upper() + w[1:] for w in mapn.split() if w)
    mapn = mapn.replace('South Eastern', 'Southeastern').replace('North Eastern', 'Northeastern')
    if not mapn: return None
    if mode == 'historical':
        prefix = '[Operation]'
    else:
        prefix = '[' + ' '.join(w[:1].upper() + w[1:] for w in mode.split('_') if w) + ']'
    return {'prefix': prefix, 'map': mapn, 'label': prefix + ' ' + mapn, 'mode': mode}

def analyze_mission_patch(path, patch):
    ml = mission_label(path)
    if not ml: return None
    hunks, cur = [], None
    for l in str(patch or '').split('\n'):
        if l.startswith('@@'):
            if cur: hunks.append(cur)
            cur = [l]; continue
        if l.startswith('diff '):
            if cur: hunks.append(cur)
            cur = None; continue
        if cur is not None: cur.append(l)
    if cur: hunks.append(cur)
    is_pos = lambda l: bool(re.match(r'^[+-]\s*-?\d+(\.\d+)?\s*,?\s*$', l))
    out = []
    if '/carriers/' in str(path):
        pos = sum(1 for h in hunks for l in h if is_pos(l))
        out.append('изменены позиции и маршруты авианосцев и их эскорта' if pos > 10 else 'изменён состав юнитов авианосной группы')
        return {'label': ml['label'], 'lines': out}
    moved = set()
    for h in hunks:
        add_nums = sum(1 for l in h if re.match(r'^\+\s*-?\d+(\.\d+)?\s*,?\s*$', l))
        has_del_nums = any(l.startswith('-') and not l.startswith('---') and re.match(r'^\s*-?\d+(\.\d+)?\s*,?\s*$', l[1:]) for l in h)
        if has_del_nums and add_nums > 0:
            for l in h:
                m = re.search(r'"unit_class"\s*:\s*"([^"]+)"', l)
                if m:
                    d = decode_unit_class(m.group(1))
                    if d['catPl']: moved.add(d['catPl'])
    added_cls, removed_cls = {}, {}
    for h in hunks:
        for l in h:
            m = re.match(r'^([+-])\s*"unit_class"\s*:\s*"([^"]+)"', l)
            if not m: continue
            (added_cls if m.group(1) == '+' else removed_cls)[m.group(2)] = decode_unit_class(m.group(2))
    for cls in list(added_cls):  # одновременно добавленные и удалённые = перемещённые
        if cls in removed_cls:
            d = added_cls[cls]
            if d['catPl']: moved.add(d['catPl'])
            del added_cls[cls]; del removed_cls[cls]
    if moved: out.append('перемещены: ' + ', '.join(sorted(moved)))
    def group_by_cat(m_):
        by = {}
        for d in m_.values():
            by.setdefault(d['catPl'] or 'юниты', [])
            if d['name'] and d['name'] not in by[d['catPl'] or 'юниты']: by[d['catPl'] or 'юниты'].append(d['name'])
        return by
    add_cats, rem_cats = group_by_cat(added_cls), group_by_cat(removed_cls)
    if add_cats:
        parts = []
        for cat, names in add_cats.items():
            parts.append(f"{cat} ({', '.join(names[:2])} и др.)" if len(names) > 2 else f"{cat} ({', '.join(names)})")
        out.append('добавлены: ' + ', '.join(parts))
    if rem_cats:
        out.append('убраны: ' + ', '.join(f"{cat} ({', '.join(names[:3])})" for cat, names in rem_cats.items()))
    if not out: out.append('изменена расстановка юнитов на карте')
    return {'label': ml['label'], 'lines': out}

def build_changelog(files, rep, cmp_root=None):
    """Собирает структурированный changelog: группировка одинаковых изменений"""
    by_entity = {}
    def add_lines(kind, entity, label, lines, classes=None):
        if not entity or not lines: return
        e = by_entity.setdefault(entity, {'kind': kind, 'entity': entity, 'label': label, 'lines': [], 'classes': []})
        for l in lines:
            if l not in e['lines']: e['lines'].append(l)
        for c in (classes or []):
            if c not in e['classes']: e['classes'].append(c)
    # 1) загрузка (weaponpresets)
    for f in files:
        if f['status'] == 'modified' and 'weaponpresets/' in f['filename'] and f.get('patch'):
            ac = prettify_aircraft_from_preset(f['filename'].split('/')[-1])
            r = analyze_weapon_preset_patch(f['patch'])
            if r and r['lines']: add_lines('loadout', ac, 'изменения загрузки', r['lines'])
    # 2) классы брони
    armor_file = next((f for f in files if f['status'] == 'modified'
                       and re.search(r'damage_model/armor_classes\.blkx?$', f['filename']) and f.get('patch')), None)
    if armor_file:
        by_change = {}
        for c in analyze_armor_classes_patch(armor_file['patch']):
            key = (c['matKey'], c['thickness'] or '', ' '.join(c['lines']))
            g = by_change.setdefault(key, {'material': c['material'], 'thickness': c['thickness'], 'parts': [], 'lines': c['lines'], 'classes': []})
            if c['part'] and c['part'] not in g['parts']: g['parts'].append(c['part'])
            g['classes'].append(c['name'])
        for g in by_change.values():
            entity = f"{g['thickness']} мм {g['material']}" if g['thickness'] else g['material']
            add_lines('armor', entity, ', '.join(g['parts']), g['lines'], g['classes'])
    # 3) миссии
    for f in files:
        if re.search(r'mis\.vromfs\.bin_u/.*missions/', f['filename']) and f.get('patch'):
            m = analyze_mission_patch(f['filename'], f['patch'])
            if m and m['lines']: add_lines('mission', m['label'], '', m['lines'])
    for f in files:
        if f['status'] == 'added' and '_mirror_' in f['filename']:
            base_p = f['filename'].replace('_mirror_', '_')
            if any(x['filename'] == base_p and x['status'] == 'modified' for x in files):
                ml = mission_label(base_p)
                if ml: add_lines('mission', ml['label'], '', ['добавлена зеркальная версия карты'])
    # 4) прочие значимые изменения (баффы/нерфы)
    for f in files:
        if (f['status'] == 'modified' and f.get('patch') and 'weaponpresets/' not in f['filename']
                and 'missions/' not in f['filename'] and 'armor_classes' not in f['filename']):
            an = analyze_file(f)
            if an['verdict'] != 'change' and an['changes']:
                add_lines('generic', prettify_name(f['short_path'].split('/')[-1]), '', [d['text'] for d in an['changes'][:5]])
    # слияние записей с одинаковым текстом изменений
    merged = {}
    for e in by_entity.values():
        key = (e['kind'], e['label'], ' '.join(e['lines']))
        g = merged.setdefault(key, {'kind': e['kind'], 'entities': [], 'label': e['label'], 'lines': e['lines'], 'classes': []})
        if e['entity'] not in g['entities']: g['entities'].append(e['entity'])
        for c in e['classes']:
            if c not in g['classes']: g['classes'].append(c)
    order = {'loadout': 0, 'armor': 1, 'mission': 2, 'generic': 3}
    entries = sorted(merged.values(), key=lambda g: (order.get(g['kind'], 9), -len(g['entities'])))
    # локальный скан дампа: какие юниты используют изменённые классы брони
    if cmp_root is not None:
        fm_dirs = [vd / 'gamedata' / 'flightmodels' for vd in sorted(pathlib.Path(cmp_root).glob('*.vromfs.bin_u'))
                   if (vd / 'gamedata' / 'flightmodels').is_dir()]
        for g in entries:
            if g['kind'] != 'armor' or not g['classes']: continue
            names = set()
            for d in fm_dirs:
                for fp in d.iterdir():
                    if not fp.is_file() or not re.search(r'\.blkx?$', fp.name, re.I): continue
                    try: content = fp.read_text(encoding='utf-8', errors='replace')
                    except OSError: continue
                    if any(f'"{c}"' in content for c in g['classes'][:8]):
                        names.add(prettify_name(fp.stem))
            if names: g['affected'] = sorted(names)
    return entries

def join_entities(entities, cap=15):
    lst = list(entities[:cap]); more = len(entities) - len(lst)
    m = re.match(r'^(\[[^\]]+\])\s', lst[0]) if lst else None
    if m and all(x.startswith(m.group(1) + ' ') for x in lst):
        out = ', '.join([lst[0]] + [x[len(m.group(1)) + 1:] for x in lst[1:]])
    else:
        out = ', '.join(lst)
    return out + (f' и ещё {more}' if more > 0 else '')

def changelog_markdown(entries):
    if not entries: return ''
    L = ['## 🗒️ Changelog (структурированный авто-анализ)', '']
    for e in entries:
        L.append(f"- **{join_entities(e['entities'], 15)}**")
        if e['label']: L.append(f"  - {e['label']}:")
        for l in e['lines'][:25]: L.append(f"    - {l}")
        if len(e['lines']) > 25: L.append(f"    - … ещё {len(e['lines']) - 25}")
        if e.get('affected'):
            aff = e['affected']
            tail = ', '.join(aff[:60]) + (' …' if len(aff) > 60 else '')
            L.append(f"  - затронуто юнитов: {len(aff)} — {tail}")
    L.append('')
    return '\n'.join(L)

def render_changelog_html(entries):
    if not entries: return ''
    h = ['<div class="rep-section"><div class="rep-sec-title">🗒️ Changelog <span class="cnt">структурированный авто-анализ</span></div><ul class="cl">']
    for e in entries:
        h.append(f'<li><span class="cl-entity">{esc(join_entities(e["entities"], 15))}</span><ul>')
        if e['label']:
            h.append(f'<li><span class="cl-label">{esc(e["label"])}:</span><ul>')
        else:
            h.append('<li><ul>')
        for l in e['lines'][:20]:
            h.append(f'<li>{esc(l)}</li>')
        if len(e['lines']) > 20:
            h.append(f'<li class="cl-more">… ещё {len(e["lines"]) - 20}</li>')
        h.append('</ul></li>')
        if e.get('affected'):
            aff = e['affected']
            tail = ', '.join(aff[:60]) + (' …' if len(aff) > 60 else '')
            h.append(f'<li><span class="cl-label">затронуто юнитов: {len(aff)}</span> — {esc(tail)}</li>')
        h.append('</ul></li>')
    h.append('</ul></div>')
    return ''.join(h)


def build_markdown(rep, changelog=None):
    """Готовый отчёт в виде Markdown (текст)"""
    L = []
    L.append('# 📋 Отчёт об изменениях — War Thunder Datamine')
    L.append('')
    L.append(f"**Версии:** {rep['base']} → {rep['cmp']}  ")
    L.append(f"**Сгенерировано:** {datetime.now().strftime('%d.%m.%Y, %H:%M:%S')} (авто-анализ 🤖)")
    L.append('')
    L.append('## 📊 Итого')
    L.append('')
    L.append('| Показатель | Значение |')
    L.append('|---|---|')
    L.append(f"| 🆕 Добавлено файлов | **{rep['stats']['added']}** |")
    L.append(f"| 🔧 Изменено файлов | **{rep['stats']['modified']}** |")
    L.append(f"| 🗑️ Удалено файлов | **{rep['stats']['deleted']}** |")
    L.append(f"| **Всего** | **{rep['stats']['total']}** |")
    if changelog:
        cl = changelog_markdown(changelog).rstrip('\n')
        if cl.strip():
            L.append('')
            L.append(cl)
    if rep['highlights']:
        L.append('')
        L.append('## ⭐ Главное (авто-анализ)')
        L.append('')
        for h in rep['highlights'][:30]:
            L.append(f"- {h['ico']} {h['text']}")
    cats = sorted(rep['categories'].values(), key=lambda c: -len(c['files']))
    for crep in cats:
        L.append('')
        L.append(f"## {crep['cat']['title']} — {len(crep['files'])} файл(ов)")
        if rep['binary'].get(crep['cat']['id']):
            L.append('')
            L.append(f"_Обновлено двоичных файлов (без текстового diff): {rep['binary'][crep['cat']['id']]}_")
        for fr in crep['files'][:100]:
            L.append('')
            L.append(f"### {VERDICT_LABEL[fr['verdict']]} — {prettify_name(fr['file']['short_path'].split('/')[-1])} — `{fr['file']['short_path']}`")
            if fr['note']:
                L.append(f"> {fr['note']}")
                continue
            for d in fr['changes'][:40]:
                L.append(f"- {d['text']}")
            if len(fr['changes']) > 40:
                L.append(f"- … ещё {len(fr['changes']) - 40} изменений")
    if rep['addedFiles']:
        L.append('')
        L.append(f"## 🆕 Добавленные файлы ({len(rep['addedFiles'])})")
        L.append('')
        for x in rep['addedFiles'][:200]:
            L.append(f"- `{x['file']['short_path']}`")
        if len(rep['addedFiles']) > 200:
            L.append(f"- … ещё {len(rep['addedFiles']) - 200}")
    if rep['deletedFiles']:
        L.append('')
        L.append(f"## 🗑️ Удалённые файлы ({len(rep['deletedFiles'])})")
        L.append('')
        for x in rep['deletedFiles'][:200]:
            L.append(f"- `{x['file']['short_path']}`")
        if len(rep['deletedFiles']) > 200:
            L.append(f"- … ещё {len(rep['deletedFiles']) - 200}")
    L.append('')
    L.append('---')
    L.append('*Отчёт сгенерирован автоматически — wt_mine.py 🤖*')
    return '\n'.join(L)

REPORT_CSS = """body{background:#0f0f0f;color:#e6e6e6;font-family:Inter,Segoe UI,Arial,sans-serif;font-size:13px;margin:0;padding:24px}
.wrap{max-width:960px;margin:0 auto}
.rep-head{background:#1c1c1c;border:1px solid #2b2b2b;border-radius:12px;padding:16px 18px;margin-bottom:14px}
.rep-title{font-size:17px;font-weight:800}
.rep-vers{color:#9a9a9a;font-size:12.5px;margin-top:4px}.rep-vers b{color:#e6e6e6}
.rep-stats{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
.rep-chip{font-size:11.5px;font-weight:700;padding:5px 11px;border-radius:99px;border:1px solid #2b2b2b;background:#232323;color:#9a9a9a}
.rep-chip b{font-size:13px;color:#e6e6e6}
.rep-chip.added{color:#7fc97f;border-color:rgba(127,201,127,.3);background:rgba(127,201,127,.12)}
.rep-chip.modified{color:#d6b55a;border-color:rgba(214,181,90,.3);background:rgba(214,181,90,.12)}
.rep-chip.deleted{color:#e07a7a;border-color:rgba(224,122,122,.3);background:rgba(224,122,122,.12)}
.rep-section{background:#1c1c1c;border:1px solid #2b2b2b;border-radius:12px;padding:14px 16px;margin-bottom:14px}
.rep-sec-title{font-size:13px;font-weight:800;display:flex;align-items:center;gap:8px;margin-bottom:10px}
.rep-sec-title .cnt{margin-left:auto;font-size:11px;color:#6b6b6b;font-weight:600}
.rep-high{display:flex;gap:9px;align-items:flex-start;padding:7px 10px;border-radius:7px;background:#0f0f0f;border:1px solid #2b2b2b;margin-bottom:6px;font-size:12.5px;line-height:1.4}
.rep-file{display:flex;align-items:center;gap:8px;padding:7px 10px;border-radius:7px;background:#0f0f0f;border:1px solid #2b2b2b;margin-bottom:4px;flex-wrap:wrap}
.rep-file .fname{font-family:ui-monospace,Menlo,monospace;font-size:12px;font-weight:700}
.rep-file .fdelta{font-size:11px;color:#6b6b6b;font-family:ui-monospace,monospace}
.rep-filepath{font-size:10.5px;color:#6b6b6b;font-family:ui-monospace,monospace;padding:0 10px;margin:-2px 0 6px;word-break:break-all}
.vbadge{font-size:10px;font-weight:800;padding:2px 8px;border-radius:99px;white-space:nowrap}
.vbadge.buff{background:rgba(127,201,127,.12);color:#7fc97f;border:1px solid rgba(127,201,127,.3)}
.vbadge.nerf{background:rgba(224,122,122,.12);color:#e07a7a;border:1px solid rgba(224,122,122,.3)}
.vbadge.change{background:rgba(214,181,90,.12);color:#d6b55a;border:1px solid rgba(214,181,90,.3)}
.rep-changes{margin:0 0 10px 4px;padding-left:12px;border-left:2px solid #2b2b2b;display:flex;flex-direction:column;gap:3px}
.rep-ch{font-size:12px;color:#9a9a9a;font-family:ui-monospace,Menlo,monospace;line-height:1.45;word-break:break-word}
.rep-ch .pname{color:#e6e6e6;font-weight:600}.rep-ch .pv{color:#9a9a9a}.rep-ch .arrow{color:#6b6b6b}
.rep-ch .pdelta{font-weight:700}.rep-ch .pdelta.delta-up{color:#7fc97f}.rep-ch .pdelta.delta-down{color:#e07a7a}
.rep-ch.add{color:#7fc97f}.rep-ch.del{color:#e07a7a}
.rep-more{font-size:11px;color:#6b6b6b;font-style:italic;margin-top:2px}
.rep-list{display:flex;flex-direction:column;gap:4px}
.rep-empty{color:#6b6b6b;font-size:13px;padding:40px;text-align:center}
ul.cl{margin:0;padding-left:4px;list-style:none}
ul.cl li{position:relative;padding-left:14px;margin-bottom:2px;color:#bdbdbd;font-size:12.5px;line-height:1.55}
ul.cl ul{padding-left:6px;margin-top:2px}
ul.cl>li{margin-bottom:10px}
ul.cl>li>.cl-entity{color:#e6e6e6;font-weight:700;font-size:13px}
ul.cl .cl-label{color:#e6e6e6;font-weight:600}
ul.cl li::before{content:"•";position:absolute;left:2px;color:#666}
ul.cl>li::before{content:"▸";color:#4f9cf9}
.cl-more{color:#777;font-style:italic;font-size:11.5px}
.rep-foot{color:#6b6b6b;font-size:11px;text-align:center;margin-top:4px}"""

def esc(s):
    return str(s if s is not None else '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')

def build_html(rep, changelog=None):
    """Готовый отчёт в виде standalone HTML"""
    s = rep['stats']
    h = []
    h.append('<div class="rep-head">')
    h.append('<div class="rep-title">📋 Отчёт об изменениях</div>')
    h.append(f'<div class="rep-vers"><b>{esc(rep["base"])}</b> → <b>{esc(rep["cmp"])}</b> • сгенерировано {esc(datetime.now().strftime("%d.%m.%Y, %H:%M:%S"))} • авто-анализ 🤖</div>')
    h.append('<div class="rep-stats">')
    h.append(f'<span class="rep-chip added">🆕 Добавлено <b>{s["added"]}</b></span>')
    h.append(f'<span class="rep-chip modified">🔧 Изменено <b>{s["modified"]}</b></span>')
    h.append(f'<span class="rep-chip deleted">🗑️ Удалено <b>{s["deleted"]}</b></span>')
    h.append(f'<span class="rep-chip">Всего <b>{s["total"]}</b></span>')
    h.append('</div></div>')
    if changelog:
        clh = render_changelog_html(changelog)
        if clh: h.append(clh)
    if rep['highlights']:
        h.append(f'<div class="rep-section"><div class="rep-sec-title">⭐ Главное <span class="cnt">авто-анализ • {len(rep["highlights"])}</span></div>')
        for hl in rep['highlights'][:30]:
            h.append(f'<div class="rep-high"><span class="ico">{hl["ico"]}</span><span>{esc(hl["text"])}</span></div>')
        h.append('</div>')
    cats = sorted(rep['categories'].values(), key=lambda c: -len(c['files']))
    for crep in cats:
        h.append(f'<div class="rep-section"><div class="rep-sec-title">{crep["cat"]["icon"]} {esc(crep["cat"]["title"])} <span class="cnt">{len(crep["files"])} файл(ов)</span></div>')
        if rep['binary'].get(crep['cat']['id']):
            h.append(f'<div class="rep-file"><span class="fname">Обновлено двоичных файлов: {rep["binary"][crep["cat"]["id"]]}</span><span class="fdelta">текстовый diff недоступен</span></div>')
        for fr in crep['files'][:100]:
            vb = {'buff': 'buff', 'nerf': 'nerf'}.get(fr['verdict'], 'change')
            h.append(f'<div class="rep-file"><span class="fname">{esc(prettify_name(fr["file"]["short_path"].split("/")[-1]))}</span>'
                     f'<span class="vbadge {vb}">{esc(VERDICT_LABEL[fr["verdict"]])}</span>'
                     f'<span class="fdelta">+{fr["file"].get("additions") or 0} −{fr["file"].get("deletions") or 0}</span></div>')
            h.append(f'<div class="rep-filepath">{esc(fr["file"]["short_path"])}</div>')
            if fr['note']:
                h.append(f'<div class="rep-changes"><div class="rep-ch">{esc(fr["note"])}</div></div>')
            elif fr['changes']:
                h.append('<div class="rep-changes">')
                for d in fr['changes'][:30]:
                    if d['kind'] == 'value' and d['pct'] is not None:
                        dir_cls = 'delta-up' if float(d['newV'].split()[0] if d['newV'] else 0) > float(d['oldV'].split()[0] if d['oldV'] else 0) else 'delta-down'
                        delta_full = f"({d['deltaStr']}, {d['pctStr'].strip()})" if d['pctStr'] else f"({d['deltaStr']})"
                        tag_html = f' <span class="vbadge {"buff" if d["score"] > 0 else "nerf"}">{d["tag"]}</span>' if d['tag'] else ''
                        h.append(f'<div class="rep-ch"><span class="pname">{esc(d["name"])}</span> <span class="pv">{esc(d["oldV"])}</span> <span class="arrow">→</span> <span class="pv">{esc(d["newV"])}</span> <span class="pdelta {dir_cls}">{esc(delta_full)}</span>{tag_html}</div>')
                    elif d['kind'] == 'value':
                        h.append(f'<div class="rep-ch"><span class="pname">{esc(d["name"])}</span> <span class="pv">{esc(d["oldV"])}</span> <span class="arrow">→</span> <span class="pv">{esc(d["newV"])}</span></div>')
                    elif d['kind'] == 'add':
                        h.append(f'<div class="rep-ch add">➕ {esc(d["text"])}</div>')
                    elif d['kind'] == 'del':
                        h.append(f'<div class="rep-ch del">➖ {esc(d["text"])}</div>')
                    else:
                        h.append(f'<div class="rep-ch">{esc(d["text"])}</div>')
                if len(fr['changes']) > 30:
                    h.append(f'<div class="rep-more">… ещё {len(fr["changes"]) - 30} изменений</div>')
                h.append('</div>')
        if len(crep['files']) > 100:
            h.append(f'<div class="rep-more">… показано 100 из {len(crep["files"])} файлов</div>')
        h.append('</div>')
    if rep['addedFiles']:
        h.append(f'<div class="rep-section"><div class="rep-sec-title">🆕 Добавленные файлы <span class="cnt">{len(rep["addedFiles"])}</span></div><div class="rep-list">')
        for x in rep['addedFiles'][:150]:
            h.append(f'<div class="rep-file"><span class="fname">{esc(x["file"]["short_path"])}</span></div>')
        if len(rep['addedFiles']) > 150:
            h.append(f'<div class="rep-more">… ещё {len(rep["addedFiles"]) - 150} файлов</div>')
        h.append('</div></div>')
    if rep['deletedFiles']:
        h.append(f'<div class="rep-section"><div class="rep-sec-title">🗑️ Удалённые файлы <span class="cnt">{len(rep["deletedFiles"])}</span></div><div class="rep-list">')
        for x in rep['deletedFiles'][:150]:
            h.append(f'<div class="rep-file"><span class="fname">{esc(x["file"]["short_path"])}</span></div>')
        if len(rep['deletedFiles']) > 150:
            h.append(f'<div class="rep-more">… ещё {len(rep["deletedFiles"]) - 150} файлов</div>')
        h.append('</div></div>')
    h.append(f'<div class="rep-foot">Отчёт сгенерирован автоматически • {esc(rep["base"])} → {esc(rep["cmp"])} • wt_mine.py 🤖</div>')
    body = '\n'.join(h)
    return ('<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Отчёт {esc(rep["base"])} → {esc(rep["cmp"])}</title>'
            f'<style>{REPORT_CSS}</style></head><body><div class="wrap">{body}</div></body></html>')

# =====================================================================

def ext_of(path):
    return (str(path).rsplit('.', 1)[-1] if '.' in str(path) else '').lower()

def short_path_of(path):
    p = str(path)
    if '.vromfs.bin_u/' in p:
        return '/' + p.split('.vromfs.bin_u/', 1)[1]
    return '/' + p

def build_file_entries(res, base_root, cmp_root, max_analyze=300):
    """Превращает результат compare_dumps в список файлов как в HTML-приложении,
    с текстовыми patch для изменённых файлов"""
    files = []
    for x in res['added']:
        # для добавленных .blk читаем содержимое как pseudo-patch (+строки), чтобы вытащить ключевые ТТХ
        patch = None
        if re.search(r'\.blk[cs]?$', x['path'], re.I) and x['size'] < 2_000_000:
            try:
                # добавленный файл лежит в НОВОМ дампе (cmp_root)
                pa = cmp_root / x['path']
                content = pa.read_text(errors='replace').splitlines()
                patch = '\n'.join('+' + l for l in content)
            except Exception:
                patch = None
        files.append({'filename': x['path'], 'short_path': short_path_of(x['path']), 'status': 'added',
                      'additions': None, 'deletions': 0, 'patch': patch, 'ext': ext_of(x['path']), 'vromfs': x['vromfs']})
    for x in res['deleted']:
        files.append({'filename': x['path'], 'short_path': short_path_of(x['path']), 'status': 'deleted',
                      'additions': 0, 'deletions': None, 'patch': None, 'ext': ext_of(x['path']), 'vromfs': x['vromfs']})
    for x in res['modified'][:max_analyze]:
        pa = base_root / x['path']
        pb = cmp_root / x['path']
        patch, add, dele = None, 0, 0
        try:
            # бинарные файлы (текстуры и т.п.) не diff-им — как GitHub, который не даёт для них patch
            if ext_of(x['path']) in IMG_EXTS:
                patch = None
            elif pa.exists() and pb.exists() and pa.stat().st_size < 2_000_000 and pb.stat().st_size < 2_000_000:
                ta = pa.read_text(errors='replace').splitlines()
                tb = pb.read_text(errors='replace').splitlines()
                diff_lines = list(difflib.unified_diff(ta, tb, fromfile=str(pa), tofile=str(pb), lineterm=''))
                patch = '\n'.join(diff_lines)
                add = sum(1 for l in diff_lines if l.startswith('+') and not l.startswith('+++'))
                dele = sum(1 for l in diff_lines if l.startswith('-') and not l.startswith('---'))
        except Exception:
            patch = None
        files.append({'filename': x['path'], 'short_path': short_path_of(x['path']), 'status': 'modified',
                      'additions': add, 'deletions': dele, 'patch': patch, 'ext': ext_of(x['path']), 'vromfs': x['vromfs']})
    return files

def main():
    ap = argparse.ArgumentParser(description="War Thunder vromfs miner")
    ap.add_argument("--war-thunder", help="Путь к установке War Thunder (LIVE или DEV)")
    ap.add_argument("--out", help="Куда дампить _u папки")
    ap.add_argument("--wt-tools", default="vromfs_unpacker", help="Путь к vromfs_unpacker бинарю")
    ap.add_argument("--compare", nargs=2, metavar=("BASE","COMPARE"), help="Сравнить два дампа")
    ap.add_argument("--json", help="Сохранить сырой JSON diff сюда")
    ap.add_argument("--md", help="Сохранить готовый отчёт (Markdown) сюда")
    ap.add_argument("--html", help="Сохранить готовый отчёт (standalone HTML) сюда")
    args = ap.parse_args()

    if args.compare:
        base, cmp = pathlib.Path(args.compare[0]), pathlib.Path(args.compare[1])
        print(f"[compare] {base} → {cmp}")
        res = compare_dumps(base, cmp)
        print(f"Added: {res['stats']['added']}, Modified: {res['stats']['modified']}, Deleted: {res['stats']['deleted']}")
        # топ vromfs
        from collections import Counter
        c = Counter([x["vromfs"] for x in res["added"]+res["modified"]+res["deleted"]])
        print("vromfs breakdown:", dict(c.most_common()))

        # 🤖 Авто-анализ: приложение само разбирает изменения и собирает готовый отчёт
        print("[analyze] разбираю изменения .blk (баффы/нерфы)…")
        files = build_file_entries(res, base, cmp)
        rep = analyze_files(files, base.name, cmp.name)
        print("[changelog] семантический анализ: загрузка, броня, миссии…")
        changelog = build_changelog(files, rep, cmp_root=cmp)
        md = build_markdown(rep, changelog)

        if args.json:
            pathlib.Path(args.json).write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"[json] сырой diff сохранён в {args.json}")
        if args.html:
            pathlib.Path(args.html).write_text(build_html(rep, changelog), encoding="utf-8")
            print(f"[html] 📊 готовый отчёт сохранён в {args.html}")
        if args.md:
            pathlib.Path(args.md).write_text(md, encoding="utf-8")
            print(f"[md] 📊 готовый отчёт сохранён в {args.md}")
        if not args.md and not args.html:
            # по умолчанию печатаем готовый отчёт прямо в консоль
            print()
            print(md)
        return

    if args.war_thunder and args.out:
        wt_root = pathlib.Path(args.war_thunder)
        out_root = pathlib.Path(args.out)
        if not wt_root.exists():
            print(f"[error] War Thunder путь не найден: {wt_root}", file=sys.stderr)
            sys.exit(1)
        out_root.mkdir(parents=True, exist_ok=True)
        vromfs_list = find_vromfs(wt_root)
        if not vromfs_list:
            print(f"[error] Не нашёл *.vromfs.bin в {wt_root}. Проверь путь. Ищу: {VROMFS_NAMES}", file=sys.stderr)
            print(f"[hint] Для DEV сервера путь обычно '.../War Thunder DEV' и внутри есть aces.vromfs.bin")
            sys.exit(1)
        print(f"[found] {len(vromfs_list)} vromfs: {[x.name for x in vromfs_list]}")
        for vp in vromfs_list:
            unpack_vromfs(vp, out_root, args.wt_tools)
        idx = index_dump(out_root)
        print(f"[done] проиндексировано {len(idx)} файлов в {out_root}")
        # сохранить version
        (out_root / "version").write_text(wt_root.name + " " + datetime.now().isoformat())
        return

    ap.print_help()

if __name__ == "__main__":
    main()
