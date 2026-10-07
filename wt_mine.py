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
    """mig_35.blk → MiG-35, f_4e_phantom.blk → F-4E Phantom"""
    t = re.sub(r'\.(blk[cs]?|dds|png|jpe?g|webp|tga|svg)$', '', str(base), flags=re.I).replace('_', ' ').strip()
    if not t:
        return str(base)
    toks = [NAME_FIX.get(tok.lower(), tok[:1].upper() + tok[1:]) for tok in t.split()]
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
            base.update(kind='delRaw' if direction == 'del' else 'addRaw', raw=p['raw'])
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
        if cat['id'] in UNIT_CAT_IDS and re.search(r'\.blk[cs]?$', f['filename'], re.I):
            nm = prettify_name(f['short_path'].split('/')[-1])
            info = added_file_info(f)
            rep['highlights'].append({'ico': '🆕', 'text': f"Новый юнит/FM: {nm}" + (f" — {', '.join(info)}" if info else ''), 'file': f['filename']})
        elif cat['id'] in ('tex', 'img'):
            rep['highlights'].append({'ico': '🎨', 'text': f"Новая текстура/скин: {f['short_path'].split('/')[-1]}", 'file': f['filename']})

    for f in deleted:
        cat = categorize(f['filename'], f.get('ext', ''))
        rep['deletedFiles'].append({'file': f, 'cat': cat})
        if cat['id'] in UNIT_CAT_IDS and re.search(r'\.blk[cs]?$', f['filename'], re.I):
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

def build_markdown(rep):
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
.rep-foot{color:#6b6b6b;font-size:11px;text-align:center;margin-top:4px}"""

def esc(s):
    return str(s if s is not None else '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')

def build_html(rep):
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
        md = build_markdown(rep)

        if args.json:
            pathlib.Path(args.json).write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"[json] сырой diff сохранён в {args.json}")
        if args.html:
            pathlib.Path(args.html).write_text(build_html(rep), encoding="utf-8")
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
