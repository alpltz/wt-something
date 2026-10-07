#!/usr/bin/env python3
"""
wt_mine.py — локальный датамайнер War Thunder vromfs

Делает то же что и gszabi99 бот, но на твоём ПК:
 - находит *.vromfs.bin в папке War Thunder (LIVE и DEV)
 - распаковывает через wt-tools (vromfs_unpacker)
 - индексирует и сравнивает дампы

Использование:
  # Замайнить LIVE установку
  python wt_mine.py --war-thunder "C:/Games/WarThunder" --out ./dump_live

  # Замайнить DEV установку
  python wt_mine.py --war-thunder "C:/Games/WarThunder DEV" --out ./dump_dev

  # Сравнить два дампа (как в приложении)
  python wt_mine.py --compare ./dump_2.58.0.28 ./dump_2.59.0.16 --json diff.json --html report.html

Требования:
  wt-tools: https://github.com/kotiq/wt-tools (скомпилировать или скачать бинарь)
  python 3.10+
"""
import argparse, os, sys, json, hashlib, subprocess, pathlib, shutil, difflib
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

def main():
    ap = argparse.ArgumentParser(description="War Thunder vromfs miner")
    ap.add_argument("--war-thunder", help="Путь к установке War Thunder (LIVE или DEV)")
    ap.add_argument("--out", help="Куда дампить _u папки")
    ap.add_argument("--wt-tools", default="vromfs_unpacker", help="Путь к vromfs_unpacker бинарю")
    ap.add_argument("--compare", nargs=2, metavar=("BASE","COMPARE"), help="Сравнить два дампа")
    ap.add_argument("--json", help="Сохранить JSON diff сюда")
    ap.add_argument("--html", help="Сохранить HTML отчёт сюда")
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
        # пример diff для первого modified
        if res["modified"]:
            first = res["modified"][0]["path"]
            p1 = base / first
            p2 = cmp / first
            if p1.exists() and p2.exists():
                try:
                    t1 = p1.read_text(errors="replace").splitlines()
                    t2 = p2.read_text(errors="replace").splitlines()
                    diff = list(difflib.unified_diff(t1, t2, fromfile=str(base), tofile=str(cmp), lineterm=""))[:40]
                    print("\n".join(diff))
                except:
                    print(f"[binary] {first}")
        if args.json:
            pathlib.Path(args.json).write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"[json] saved to {args.json}")
        if args.html:
            html = f"""<html><body style="font-family:monospace;background:#111;color:#ddd;padding:20px">
<h2>War Thunder diff {base} → {cmp}</h2>
<p>Added {res['stats']['added']} Modified {res['stats']['modified']} Deleted {res['stats']['deleted']}</p>
<h3>Added</h3><ul>{"".join(f"<li>{x['path']} ({x['size']} B)</li>" for x in res['added'][:100])}</ul>
<h3>Modified</h3><ul>{"".join(f"<li>{x['path']}</li>" for x in res['modified'][:100])}</ul>
</body></html>"""
            pathlib.Path(args.html).write_text(html, encoding="utf-8")
            print(f"[html] saved to {args.html}")
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
