"""
Downloads the public Kubernetes files OrgGraph is built from (works on Windows, macOS, Linux).
Uses sparse checkouts so only the few files we need are fetched, not the whole repos.

Run from the project root:  python scripts/fetch_data.py
"""
import os
import subprocess

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")


def git(*args, cwd=None):
    print("+ git", " ".join(args))
    subprocess.run(["git", *args], cwd=cwd, check=True)


def sparse_clone(url: str, name: str, patterns: list[str]):
    dest = os.path.join(ROOT, name)
    if os.path.isdir(dest):
        print(f"{name}: already present, skipping")
        return
    git("clone", "--depth", "1", "--filter=blob:none", "--no-checkout", url, name, cwd=ROOT)
    git("sparse-checkout", "init", "--no-cone", cwd=dest)
    git("sparse-checkout", "set", "--no-cone", *patterns, cwd=dest)
    git("checkout", cwd=dest)


def main():
    os.makedirs(ROOT, exist_ok=True)
    # 1. Teams (SIGs), their leads and subprojects
    sparse_clone("https://github.com/kubernetes/community.git", "community", ["/sigs.yaml"])
    # 2. OWNERS files: who approves / reviews each directory of the main codebase
    sparse_clone("https://github.com/kubernetes/kubernetes.git", "k8s", ["/OWNERS_ALIASES", "OWNERS"])
    # 3. Enhancement proposals (KEPs): the formal decision records
    sparse_clone("https://github.com/kubernetes/enhancements.git", "enh", ["kep.yaml"])
    print("data ready")


if __name__ == "__main__":
    main()
