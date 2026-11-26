"""Install the approved repository batch, retaining individual failure results."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import subprocess
import sys

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", required=True)
    parser.add_argument("--issue", required=True)
    args = parser.parse_args()
    repos = [x["repository"] for x in json.loads(Path(args.matrix).read_text())["include"]]
    if not repos or len(repos) > 250 or len(set(repos)) != len(repos) or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", r) for r in repos):
        raise ValueError("Invalid approved repository batch")
    def install(repo):
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("install-caller.py")),
                                 "--repository", repo, "--operation", "APPLY", "--issue", args.issue],
                                capture_output=True, text=True)
        return repo, result.returncode, result.stdout, result.stderr
    failures = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for repo, code, stdout, stderr in pool.map(install, repos):
            print(f"=== {repo}: {'FAILED' if code else 'SUCCESS'} ===", flush=True)
            print(stdout, stderr, flush=True)
            if code:
                failures.append(repo)
    print(f"Completed {len(repos)} repositories; failures: {failures}", flush=True)
    return bool(failures)

if __name__ == "__main__":
    sys.exit(main())
