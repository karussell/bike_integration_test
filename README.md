# Test expected routes and properties for different branches

python3 run_tests.py http://localhost:8988 bike --save bike/results/master.jsonl

python3 run_tests.py http://localhost:8989 bike --save bike/results/branch.jsonl

Where first parameter is the http endpoint and the second is the folder containing list files with
expected values

# List Format

One test case per block, blocks separated by blank lines. The first line is the raw `/route` query string,
every further line is one assertion. Lines starting with `&` are appended to the query string, `comment` is printed on failure:

```
point=51.196964,13.309157&point=51.195156,13.374142
  &profile=bike
  distance=5.5km-6.5km
  time=20m-22m
  ascend<100m
  low_stress_part>75
  bike_network_part>70
  contains=51.186047,13.33314;51.192417,13.359429
  avoids=51.19,13.35
  comment=avoid secondary even if hilly detour
```

| key | meaning |
|---|---|
| `point=...&point=...` | first line, passed as is to `/route` |
| `&profile=bike` | lines starting with `&` are appended to the query string |
| `distance`, `time`, `ascend`, `descend` | distance in `m`/`km`, time in `h`/`m`/`min`/`s` (unit required for distance and time), ascend/descend in m |
| `low_stress_part` | % of distance with bike LTS 1-2 as in graphhopper-maps (road_class, cycleway, urban_density) |
| `bike_network_part` | % of distance with `bike_network != missing` |
| `contains=lat,lon;lat,lon` | route passes within 10 m of each point |
| `avoids=lat,lon;lat,lon` | route stays more than 10 m away from each point |
| `comment=...` | printed when the case fails |
| `skip=true` | case is not run and listed as SKIP, compare ignores it; a `comment` explaining why is required |

Operators: `<`, `<=`, `>`, `>=`, `=a-b` (inclusive range) or `=value` (±10 %).

# Compare Results

python3 run_tests.py --compare bike/results/master.jsonl bike/results/branch.jsonl