#!/usr/bin/env python3
"""Run *.list route test cases against a GraphHopper /route endpoint.

usage: run_tests.py HOST [--key KEY] [-v] [--no-color] [--save OUT] [FILE.list ...]
       run_tests.py --compare BASE.jsonl NEW.jsonl [-v]
  HOST  base URL, e.g. https://graphhopper.com/api/1 or https://biketest.graphhopper.org
  FILE  default: all *.list next to this script
  --save OUT  write one JSON line per test (status, metrics, fails); plain `diff` works on two such files
  --compare   list fixed/broken/still failing tests of NEW against BASE (-v: also passing tests with changed metrics)

Test case format (blocks separated by blank lines):
  point=lat,lon&point=lat,lon[&any=query&params]   first line, raw /route query string
  profile=bike
  distance<1400m | distance=5.5km-6.5km | time<56min | time=9h30m-10h30m | ascend<100m | descend>...
  low_stress_part>75        % of distance with bike LTS 1-2 as in graphhopper-maps (road_class, cycleway, urban_density)
  bike_network_part>70      % of distance with bike_network != missing
  contains=lat,lon;lat,lon  route passes within 10 m of each point
  avoids=lat,lon;lat,lon    route stays more than 10 m away from each point
  comment=...               printed on failure
Operators: <, <=, >, >=, =range (a-b) or =single value (±10 %).
"""
import argparse, glob, json, math, operator, os, re, sys, urllib.request

LTS1 = {'cycleway', 'path', 'footway', 'pedestrian', 'living_street', 'bridleway', 'track', 'corridor'}
DETAILS = ['road_class', 'cycleway', 'urban_density', 'bike_network']
UNITS = {'': 1, 'm': 1, 'km': 1000, 's': 1, 'min': 60, 'h': 3600}
RADIUS = 10  # m, for contains/avoids
OPS = {'<': operator.lt, '<=': operator.le, '>': operator.gt, '>=': operator.ge}


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
    k, op, v = re.fullmatch(r'(\w+)\s*(<=|>=|<|>|=)\s*(.*)', l).groups()
    if k in ('comment', 'contains', 'avoids'):
        t[k] = v if k == 'comment' else [tuple(map(float, p.split(','))) for p in v.split(';')]
    elif k == 'profile':
        t['query'] += '&profile=' + v
    elif op != '=':
        t['checks'].append((k, op, num(k, v), v))
    else:  # range a-b or single value ±10 %
        lo, hi = [num(k, x) for x in v.split('-')] if '-' in v else (num(k, v) * 0.9, num(k, v) * 1.1)
        t['checks'] += [(k, '>=', lo, v), (k, '<=', hi, v)]


def load(path):
    tests = []
    for block in re.split(r'\n\s*\n', open(path).read().strip()):
        lines = [l.strip() for l in block.split('\n')]
        t = {'src': f'{os.path.basename(path)}:{len(tests) + 1}', 'query': lines[0], 'checks': [], 'contains': [], 'avoids': [], 'comment': ''}
        for l in lines[1:]:
            try:
                parse_line(t, l)
            except (AttributeError, KeyError, ValueError) as e:
                sys.exit(f"{t['src']}: cannot parse {l!r} ({e})")
        tests.append(t)
    return tests


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
    rc, cw, ud, bn = (per_seg(n, d) for n, d in zip(DETAILS, ('other', 'missing', 'residential', 'missing')))  # order as DETAILS
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


def run(t, args):
    url = f"{args.host.rstrip('/')}/route?{t['query']}&points_encoded=false&instructions=false" + ''.join('&details=' + d for d in args.details)
    try:
        res = json.load(urllib.request.urlopen(url + (f'&key={args.key}' if args.key else ''), timeout=60))
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('host', nargs='?')
    ap.add_argument('files', nargs='*')
    ap.add_argument('--key')
    ap.add_argument('-v', '--verbose', action='store_true', help='print metrics for passed tests too')
    ap.add_argument('--no-color', action='store_true')
    ap.add_argument('--save', metavar='OUT')
    ap.add_argument('--compare', nargs=2, metavar=('BASE', 'NEW'))
    args = ap.parse_args()
    color = {'FAIL': '31', 'ERROR': '35', 'PASS': '32', 'BOLD': '1'} if sys.stdout.isatty() and not args.no_color else {}
    col = lambda c, txt: f'\033[{color[c]}m{txt}\033[0m' if c in color else txt
    if args.compare:
        return compare(*args.compare, args.verbose, col)
    if not args.host:
        ap.error('HOST or --compare BASE NEW required')
    try:  # request only the path details the server knows
        evs = json.load(urllib.request.urlopen(f"{args.host.rstrip('/')}/info" + (f'?key={args.key}' if args.key else ''), timeout=30))['encoded_values']
        args.details = [d for d in DETAILS if d in evs]
    except Exception:
        args.details = DETAILS
    files = args.files or sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), '*.list')))
    tests = [t for f in files for t in load(f)]
    counts = {'PASS': 0, 'FAIL': 0, 'ERROR': 0}
    print(f'{args.host}: {len(tests)} tests from {len(files)} files')
    cur, nl, rows = None, '', []  # nl = newline needed before the next full line
    for t in tests:
        status, msgs, m = run(t, args)
        counts[status] += 1
        rows.append({'src': t['src'], 'status': status, 'metrics': {k: fmt(k, v) for k, v in m.items()}, 'fails': msgs})
        if t['src'].split(':')[0] != cur:
            cur = t['src'].split(':')[0]
            print(nl + col('BOLD', f"{cur} ({sum(x['src'].startswith(cur + ':') for x in tests)})") + ' ', end='', flush=True)
            nl = '\n'
        if status == 'PASS' and not args.verbose:
            print(col('PASS', '.'), end='', flush=True)
            nl = '\n'
            continue
        print(nl + col(status, f'{status:5}') + f" {t['src']:22} {t['query'].split('&profile')[0].replace('%2C', ',')}")
        nl = ''
        if status == 'FAIL' and t['comment']:
            print('      # ' + t['comment'])
        if m:
            print('      ' + ' '.join(f'{k}={fmt(k, v)}' for k, v in m.items()))
        print(*('      ' + col(status, msg) for msg in msgs), sep='\n', end='' if not msgs else '\n')
    print(nl + col('PASS', f"passed={counts['PASS']}") + ' ' + col('FAIL' if counts['FAIL'] else '', f"failed={counts['FAIL']}")
          + ' ' + col('ERROR' if counts['ERROR'] else '', f"errored={counts['ERROR']}"))
    if args.save:
        open(args.save, 'w').write(''.join(json.dumps(r) + '\n' for r in rows))
    sys.exit(1 if counts['FAIL'] or counts['ERROR'] else 0)


def compare(base_file, new_file, verbose, col):
    base = {r['src']: r for r in map(json.loads, open(base_file))}
    diff = {'fixed': 0, 'broken': 0, 'still failing': 0}
    print(f'{new_file} compared to {base_file}:')
    for r in map(json.loads, open(new_file)):
        b = base.get(r['src'])
        if not b:
            continue
        kind = ('fixed' if r['status'] == 'PASS' else 'still failing') if b['status'] != 'PASS' else ('broken' if r['status'] != 'PASS' else 'pass')
        if kind == 'pass' and not verbose:
            continue
        diff[kind] = diff.get(kind, 0) + 1
        print(col('PASS' if kind in ('fixed', 'pass') else 'FAIL', f'{kind:13}') + f" {r['src']:22} "
              + ' '.join(f"{k}:{b['metrics'].get(k)}->{v}" for k, v in r['metrics'].items() if b['metrics'].get(k) != v))
        for msg in r['fails']:
            print('              ' + col(r['status'], msg))
    print(' '.join(f'{k}={v}' for k, v in diff.items()))


if __name__ == '__main__':
    main()
