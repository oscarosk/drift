#!/usr/bin/env bash
# Downloads the public Kubernetes files OrgGraph is built from.
# Uses sparse checkouts so we only fetch the few files we need, not the whole repos.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data && cd data

# 1. Teams (SIGs), their leads and subprojects
if [ ! -d community ]; then
  git clone --depth 1 --filter=blob:none --sparse https://github.com/kubernetes/community.git
  (cd community && git sparse-checkout set --no-cone /sigs.yaml)
fi

# 2. OWNERS files: who approves / reviews each directory of the main codebase
if [ ! -d k8s ]; then
  git clone --depth 1 --filter=blob:none --no-checkout https://github.com/kubernetes/kubernetes.git k8s
  (cd k8s && git sparse-checkout init --no-cone \
     && printf '/OWNERS_ALIASES\nOWNERS\n' > .git/info/sparse-checkout && git checkout)
fi

# 3. Enhancement proposals (KEPs): the formal decision records
if [ ! -d enh ]; then
  git clone --depth 1 --filter=blob:none --no-checkout https://github.com/kubernetes/enhancements.git enh
  (cd enh && git sparse-checkout init --no-cone \
     && echo 'kep.yaml' > .git/info/sparse-checkout && git checkout)
fi
echo "data ready"
