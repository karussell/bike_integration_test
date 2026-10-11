# Tune Bike Profile

python3 run_tests.py http://localhost:8989 bike --save bike/results/master.jsonl

python3 run_tests.py http://localhost:8989 bike --save bike/results/branch.jsonl

# Compare Results

python3 run_tests.py --compare bike/results/master.jsonl bike/results/branch.jsonl