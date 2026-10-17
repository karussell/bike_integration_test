#!/usr/bin/env python3
"""Run *.list route test cases against a GraphHopper /route endpoint.

usage: run_tests.py HOST [DIR|FILE.list ...] [--key KEY] [-v] [--no-color] [--save OUT] [--skip LISTS]
       run_tests.py --compare BASE.jsonl NEW.jsonl [-v] [--skip LISTS]
  HOST  base URL, e.g. https://graphhopper.com/api/1 or https://biketest.graphhopper.org
  DIR   profile folder with *.list files, e.g. bike; default: all */*.list next to this script
  --skip LISTS  comma separated list names to leave out, e.g. world,israel (run and compare)
Cases with a point outside the server bbox (/info) are not requested and counted as out_of_bbox;
compare ignores cases that are out of bbox on either side.
  --save OUT  write one JSON line per test (status, metrics, fails); plain `diff` works on two such files
  --compare   list fixed/broken/still failing tests of NEW against BASE (-v: also passing tests with changed metrics)

Test case format (blocks separated by blank lines):
  point=lat,lon&point=lat,lon   first line, raw /route query string
  &profile=bike                 lines starting with & are appended to the query string
  distance<1400m | distance=5.5km-6.5km | time<56min | time=9h30m-10h30m | ascend<100m | descend>...
  low_stress_part>75        % of distance with bike LTS 1-2 as in graphhopper-maps (road_class, cycleway, urban_density)
  bike_network_part>70      % of distance with bike_network != missing
  contains=lat,lon;lat,lon  route passes within 10 m of each point
  avoids=lat,lon;lat,lon    route stays more than 10 m away from each point
  comment=...               printed on failure
  skip=true                 do not run this case (a comment explaining why is required); compare ignores it
  # ...                     lines starting with # are ignored
Operators: <, <=, >, >=, =range (a-b) or =single value (±10 %).
"""
import argparse, glob, json, math, operator, os, re, sys, urllib.request

LTS1 = {'cycleway', 'path', 'footway', 'pedestrian', 'living_street', 'bridleway', 'track', 'corridor'}
DETAILS = {'road_class': 'other', 'cycleway': 'missing', 'urban_density': 'residential', 'bike_network': 'missing'}  # with defaults
UNITS = {'': 1, 'm': 1, 'km': 1000, 's': 1, 'min': 60, 'h': 3600}
RADIUS = 10  # m, for contains/avoids
HERE = os.path.dirname(os.path.abspath(__file__))
OPS = {'<': operator.lt, '<=': operator.le, '>': operator.gt, '>=': operator.ge}
COLORS = {'FAIL': '31', 'ERROR': '35', 'PASS': '32', 'SKIP': '33', 'WARN': '33', 'BOLD': '1'}
OUT = 'OUT_OF_BBOX'


def col(c, txt):
    return f'\033[{COLORS[c]}m{txt}\033[0m' if c in COLORS and col.on else txt


def num(key, s):
    if key == 'time':  # 9h30m, 56min, 4m, 30s
        m = re.fullmatch(r'(?:(\d+)h)?(?:(\d+)(?:m|min))?(?:(\d+)s)?', s)
        if not m or not any(m.groups()):
            raise ValueError(f'time needs a unit (h, m, min, s): {s!r}')
        return sum(int(v or 0) * f for v, f in zip(m.groups(), (3600, 60, 1)))
    m = re.fullmatch(r'([\d.]+)\s*(\w*)', s)
    if not m or (key == 'distance' and not m.group(2)):
        raise ValueError(f'{key} needs a unit (m, km): {s!r}')
    return float(m.group(1)) * UNITS[m.group(2)]


def parse_line(t, l):
    if l.startswith('&'):
        t['query'] += l
        return
    k, op, v = re.fullmatch(r'(\w+)\s*(<=|>=|<|>|=)\s*(.*)', l).groups()
    if k in ('contains', 'avoids'):
        t[k] = [tuple(map(float, p.split(','))) for p in v.split(';')]
    elif k == 'comment':
        t[k] = v
    elif k == 'skip':
        t[k] = v == 'true'
    elif op != '=':
        t['checks'].append((k, op, num(k, v), v))
    else:  # range a-b or single value ±10 %
        lo, hi = [num(k, x) for x in v.split('-')] if '-' in v else (num(k, v) * 0.9, num(k, v) * 1.1)
        t['checks'] += [(k, '>=', lo, v), (k, '<=', hi, v)]


def load(path):
    tests = []
    for block in re.split(r'\n\s*\n', open(path).read().strip()):
        lines = [l.strip() for l in block.split('\n') if not l.lstrip().startswith('#')]
        if not lines:
            continue
        t = {'file': os.path.relpath(path, HERE), 'src': f'{os.path.relpath(path, HERE)}:{len(tests) + 1}', 'query': lines[0],
             'points': lines[0].replace('%2C', ','), 'checks': [], 'contains': [], 'avoids': [], 'comment': '', 'skip': False}
        t['coords'] = [tuple(map(float, c)) for c in re.findall(r'point=(-?[\d.]+),(-?[\d.]+)', t['points'])]
        for l in lines[1:]:
            try:
                parse_line(t, l)
            except (AttributeError, KeyError, ValueError) as e:
                sys.exit(f"{t['src']}: cannot parse {l!r} ({e})")
        if t['skip'] and not t['comment']:
            sys.exit(f"{t['src']}: skip=true requires a comment explaining why")
        tests.append(t)
    return tests


def list_name(path):  # bike/world.list:3 -> world
    return os.path.basename(path.rsplit(':', 1)[0]).removesuffix('.list')


def list_files(paths, skip):
    files = []
    for p in paths:
        files += sorted(glob.glob(os.path.join(p, '*', '*.list')) or glob.glob(os.path.join(p, '*.list'))) if os.path.isdir(p) else [p]
    files = [f for f in files if list_name(f) not in skip]
    return files or sys.exit(f'no *.list files found in {paths}')


def xy(lon, lat, ref_lat):  # local equirectangular projection in metres
    return (lon * math.cos(math.radians(ref_lat)) * 111320, lat * 111320)


def seg_dist(p, a, b):  # distance of p=(lat,lon) to segment a-b of (lon,lat) points
    (px, py), (ax, ay), (bx, by) = xy(p[1], p[0], p[0]), xy(*a, p[0]), xy(*b, p[0])
    dx, dy = bx - ax, by - ay
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy))) if dx or dy else 0
    return math.hypot(ax + t * dx - px, ay + t * dy - py)


def bike_lts(rc, cw, rural):  # port of classifyBikeLTS in graphhopper-maps
    if cw in ('track', 'separate') or rc in LTS1:
        return 1
    if rc in ('trunk', 'motorway'):
        return 4
    if rc in ('primary', 'secondary'):
        return 3 if cw == 'lane' or (cw == 'missing' and not rural) else 4
    if rc == 'tertiary':
        return 2 if cw == 'lane' and not rural else 3
    return 3 if rc == 'steps' or (rc == 'unclassified' and rural) else 2


def metrics(path):
    pts = path['points']['coordinates']
    seg = [math.dist(xy(*a, a[1]), xy(*b, a[1])) for a, b in zip(pts, pts[1:])]
    per_seg = lambda name, default: [v for i, j, v in path['details'].get(name, [(0, len(seg), default)]) for _ in range(j - i)]
    rc, cw, ud, bn = (per_seg(n, d) for n, d in DETAILS.items())
    part = lambda ok: 100 * sum(d for d, o in zip(seg, ok) if o) / sum(seg)
    return {'distance': path['distance'], 'time': path['time'] / 1000, 'ascend': path.get('ascend', 0), 'descend': path.get('descend', 0),
            'low_stress_part': part(bike_lts(r, c, u == 'rural') <= 2 for r, c, u in zip(rc, cw, ud)),
            'bike_network_part': part(b != 'missing' for b in bn)}, pts


def fmt(k, x):
    if k == 'time':
        return f'{int(x // 3600)}h{int(x % 3600 // 60):02d}m' if x >= 3600 else f'{x / 60:.1f}min'
    if k == 'distance':
        return f'{x / 1000:.2f}km' if x >= 1000 else f'{x:.0f}m'
    return f'{x:.0f}' + ('m' if k in ('ascend', 'descend') else '%')


def get(args, path, query):
    url = f"{args.host.rstrip('/')}/{path}?{query}" + (f'&key={args.key}' if args.key else '')
    return json.load(urllib.request.urlopen(url, timeout=60))


def run(t, args, details, bbox):
    if t['skip']:
        return 'SKIP', [], {}
    if not (t['checks'] or t['contains'] or t['avoids']):
        return 'ERROR', ['no assertion'], {}
    if bbox and any(not (bbox[1] <= lat <= bbox[3] and bbox[0] <= lon <= bbox[2]) for lat, lon in t['coords']):
        return OUT, [], {}
    try:
        res = get(args, 'route', t['query'] + '&points_encoded=false&instructions=false' + ''.join('&details=' + d for d in details))
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='replace')
        return 'ERROR', [f'HTTP {e.code}: ' + (json.loads(body).get('message', body) if body.startswith('{') else body)[:200]], {}
    except Exception as e:
        return 'ERROR', [str(e)], {}
    m, pts = metrics(res['paths'][0])
    fails = [f"{k} too {'large' if op[0] == '<' else 'small'}: {k}{op}{raw} but {fmt(k, m[k])}"
             for k, op, v, raw in t['checks'] if not OPS[op](m[k], v)]
    for key, bad in (('contains', lambda d: d > RADIUS), ('avoids', lambda d: d <= RADIUS)):
        for c in t[key]:
            d = min(seg_dist(c, a, b) for a, b in zip(pts, pts[1:]))
            if bad(d):
                fails.append(f"not {key}: {c[0]},{c[1]} {'missed' if key == 'contains' else 'passed'} by {d:.0f}m")
    return ('FAIL' if fails else 'PASS'), fails, m


def run_all(args):
    try:
        if args.save:
            os.makedirs(os.path.dirname(args.save) or '.', exist_ok=True)
        out = open(args.save, 'w') if args.save else None
    except OSError as e:
        sys.exit(f'cannot write {args.save}: {e.strerror}')
    try:  # request only the path details the server knows, skip cases outside its bbox
        info = get(args, 'info', '')
        details, bbox = [d for d in DETAILS if d in info['encoded_values']], info.get('bbox')
    except Exception:
        details, bbox = list(DETAILS), None
    files = list_files(args.files or [HERE], args.skip)
    tests = [t for f in files for t in load(f)]
    counts, rows, cur, nl = {'PASS': 0, 'FAIL': 0, 'ERROR': 0, OUT: 0, 'SKIP': 0}, [], None, ''  # nl: newline needed before the next full line
    print(f'{args.host}: {len(tests)} tests from {len(files)} files')
    for t in tests:
        status, msgs, m = run(t, args, details, bbox)
        counts[status] += 1
        rows.append({'src': t['src'], 'query': t['points'], 'status': status, 'metrics': {k: fmt(k, v) for k, v in m.items()}, 'fails': msgs})
        if t['file'] != cur:
            cur = t['file']
            print(nl + col('BOLD', f"{cur} ({sum(x['file'] == cur for x in tests)})") + ' ', end='', flush=True)
            nl = '\n'
        if status in ('PASS', OUT) and not args.verbose:
            print(col('PASS', '.') if status == 'PASS' else '-', end='', flush=True)
            nl = '\n'
            continue
        print(nl + col(status, f'{status:5}') + f" {t['src']:27} {t['points']}")
        nl = ''
        if status in ('FAIL', 'SKIP') and t['comment']:
            print('      # ' + t['comment'])
        if m:
            print('      ' + ' '.join(f'{k}={fmt(k, v)}' for k, v in m.items()))
        for msg in msgs:
            print('      ' + col(status, msg))
    print(nl + col('PASS', f"passed={counts['PASS']}") + (f" out_of_bbox={counts[OUT]}" if counts[OUT] else '')
          + (col('SKIP', f" skipped={counts['SKIP']}") if counts['SKIP'] else '')
          + (col('ERROR', f" errored={counts['ERROR']}") if counts['ERROR'] else '') + ' ' + col('FAIL' if counts['FAIL'] else '', f"failed={counts['FAIL']}"))
    if out:
        out.write(''.join(json.dumps(r) + '\n' for r in rows))
    sys.exit(1 if counts['FAIL'] or counts['ERROR'] else 0)


def compare(base_file, new_file, verbose, skip):
    base = {r['src']: r for r in map(json.loads, open(base_file)) if list_name(r['src']) not in skip}
    new = [r for r in map(json.loads, open(new_file)) if list_name(r['src']) not in skip]
    diff = {'fixed': 0, 'now broken': 0, 'still failing': 0}
    print(f'{new_file} compared to {base_file}:')
    per_file = lambda rows: {f: sum(r['src'].rsplit(':', 1)[0] == f for r in rows) for f in {r['src'].rsplit(':', 1)[0] for r in rows}}
    nb, nn = per_file(base.values()), per_file(new)
    for f in sorted(nb.keys() | nn.keys()):
        if nb.get(f, 0) != nn.get(f, 0):
            print(col('WARN', f'warning: {f} has {nb.get(f, 0)} cases in {base_file} but {nn.get(f, 0)} in {new_file}'))
    out = [r['src'] for r in new if {OUT, 'SKIP'} & {r['status'], base.get(r['src'], {}).get('status')}]
    base = {k: v for k, v in base.items() if k not in out}
    new = [r for r in new if r['src'] not in out]
    for r in new:
        b = base.get(r['src'])
        if not b:
            continue
        ok_before, ok_now = b['status'] == 'PASS', r['status'] == 'PASS'
        kind = 'pass' if ok_before and ok_now else 'fixed' if ok_now else 'now broken' if ok_before else 'still failing'
        if kind == 'pass' and not verbose:
            continue
        diff[kind] = diff.get(kind, 0) + 1
        changed = '' if kind == 'fixed' else ' '.join(f"{k}:{b['metrics'].get(k)}->{v}" for k, v in r['metrics'].items() if b['metrics'].get(k) != v)
        print(col({'fixed': 'PASS', 'pass': 'PASS', 'now broken': 'FAIL'}.get(kind, 'WARN'), f'{kind:13}') + f" {r['src']:27} {changed}")
        if not ok_now:
            print('              ' + r.get('query', ''))
        for msg in r['fails']:
            print('              ' + col(r['status'], msg))
    passed = lambda rows: sum(r['status'] == 'PASS' for r in rows)
    print(' '.join(col({'fixed': 'PASS', 'now broken': 'FAIL', 'still failing': 'WARN'}.get(k, ''), f'{k}={v}') for k, v in diff.items()) + f' | passed {passed(base.values())}/{len(base)} -> {passed(new)}/{len(new)}'
          + f' | out_of_bbox or skipped on one side={len(out)}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('host', nargs='?')
    ap.add_argument('files', nargs='*')
    ap.add_argument('--key')
    ap.add_argument('-v', '--verbose', action='store_true', help='print metrics for passed tests too')
    ap.add_argument('--no-color', action='store_true')
    ap.add_argument('--save', metavar='OUT')
    ap.add_argument('--compare', nargs=2, metavar=('BASE', 'NEW'))
    ap.add_argument('--skip', metavar='LISTS', default='', help='comma separated list names to leave out, e.g. world,israel')
    args = ap.parse_args()
    args.skip = {x.strip().removesuffix('.list') for x in args.skip.split(',') if x.strip()}
    col.on = sys.stdout.isatty() and not args.no_color
    if args.compare:
        missing = [f for f in args.compare if not os.path.isfile(f)]
        return compare(*args.compare, args.verbose, args.skip) if not missing else sys.exit(f'no such file: {missing[0]}')
    if not args.host:
        ap.error('HOST or --compare BASE NEW required')
    run_all(args)


if __name__ == '__main__':
    main()
