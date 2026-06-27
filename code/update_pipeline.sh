#!/usr/bin/env bash
#
# CI orchestration for the update. Keeps generated results off the code branch and runs the
# processing inside the published container via datalad-containers.
#
#   - `main`        holds only the code (this checkout).
#   - `derivatives` is a persistent DataLad dataset on its own branch, cloned standalone
#                   into scratch. The processing is recorded there with
#                   `datalad containers-run`, so every update carries full provenance (the
#                   command, the input subdataset commit, the output diff, and the container
#                   image digest) and history is retained.
#   - `dist`        is the lightweight, force-recreated publication artifact consumed by
#                   downstream users (see README.md).
#
# The published image is used purely as the runtime environment: the code and the dataset
# are bind-mounted in (the image holds no code), and only the image digest is stored in the
# dataset (a small text file), so it stays annex-free and ghcr holds the bytes.
#
# code/update.py and code/compress.py are the actual code and run in any environment; this
# script is only the CI orchestration around them.
#
# Required environment variables:
#   REPO_URL    Authenticated https remote for this repository (clone/push).
#   WORKSPACE   Path to the `main` checkout that holds the code (this repository).
#   IMAGE       Container image reference to run the processing in.
# Optional:
#   LIMIT        Batch size passed to update.py for incremental runs (default: 2000).
#   GITHUB_SHA   Recorded in the provenance message to link results to the code commit.
#   RUNNER_TEMP  Scratch directory for the working clones (default: /tmp).
set -euo pipefail

: "${REPO_URL:?REPO_URL must be set}"
: "${WORKSPACE:?WORKSPACE must be set}"
: "${IMAGE:?IMAGE must be set}"
LIMIT="${LIMIT:-2000}"
GITHUB_SHA="${GITHUB_SHA:-unknown}"

BOT_NAME="github-actions[bot]"
BOT_EMAIL="github-actions[bot]@users.noreply.github.com"

# TODO: if this cache derives from an upstream DataLad dataset, set INPUT_SUBDATASET_URL to
# register it as an input subdataset. It is cloned into the derivatives dataset and pinned
# in the provenance of every run. Leave it empty if this cache has no upstream input
# dataset; the subdataset handling below is then skipped.
INPUT_SUBDATASET_URL=""  # e.g. https://github.com/dandi-cache/<input-dataset-name>.git
INPUT_SUBDATASET_PATH="sourcedata/<input-dataset-name>"

DS="${RUNNER_TEMP:-/tmp}/derivatives-dataset"
DISTDIR="${RUNNER_TEMP:-/tmp}/dist-publish"

# datalad (with the container extension) from the project environment.
datalad() { uv run --project "${WORKSPACE}/envs" datalad "$@"; }

git config --global user.name "${BOT_NAME}"
git config --global user.email "${BOT_EMAIL}"

# The `derivatives` dataset is a standalone clone (not a git worktree): datalad writes the
# input subdataset's config into `.git/config`, which is a file -- not a directory -- in a
# worktree, so subdataset registration fails there.
rm -rf "${DS}" "${DISTDIR}"

# Reuse the persistent `derivatives` dataset branch, or bootstrap a new one.
if git ls-remote --heads "${REPO_URL}" derivatives | grep -q refs/heads/derivatives; then
  echo "Reusing the existing 'derivatives' dataset branch."
  git clone --branch derivatives --single-branch "${REPO_URL}" "${DS}"
  if [ -n "${INPUT_SUBDATASET_URL}" ]; then
    git -C "${DS}" submodule update --init "${INPUT_SUBDATASET_PATH}"
  fi
else
  echo "Bootstrapping a new 'derivatives' DataLad dataset."
  datalad create --no-annex "${DS}"
  if [ -n "${INPUT_SUBDATASET_URL}" ]; then
    datalad clone -d "${DS}" "${INPUT_SUBDATASET_URL}" "${DS}/${INPUT_SUBDATASET_PATH}"
  fi
  datalad save -d "${DS}" -m "Initialize derivatives dataset"
fi

git -C "${DS}" config user.name "${BOT_NAME}"
git -C "${DS}" config user.email "${BOT_EMAIL}"
mkdir -p "${DS}/derivatives"

# Carry the study-level BIDS dataset_description.json (kept on the code branch) onto the
# derivatives dataset so the published dataset is self-describing.
cp "${WORKSPACE}/dataset_description.json" "${DS}/dataset_description.json"
datalad save -d "${DS}" -m "Update dataset_description.json" dataset_description.json || true

# Advance the input subdataset to its latest commit and record the pointer.
if [ -n "${INPUT_SUBDATASET_URL}" ]; then
  git -C "${DS}" submodule update --init --remote "${INPUT_SUBDATASET_PATH}"
  datalad save -d "${DS}" -m "Update input subdataset to latest" "${INPUT_SUBDATASET_PATH}" || true
fi

cd "${DS}"

# Pin the published image digest and register it as a container. Only the digest is stored
# (a small text file), so the dataset stays annex-free; ghcr holds the image bytes.
docker pull "${IMAGE}"
DIGEST=$(docker inspect --format '{{index .RepoDigests 0}}' "${IMAGE}")
mkdir -p .datalad/environments/pipeline
printf '%s\n' "${DIGEST}" > .datalad/environments/pipeline/image
datalad containers-add pipeline --update \
  --image .datalad/environments/pipeline/image \
  --call-fmt 'docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD":/tmp -w /tmp -v "$WORKSPACE/code":/code:ro "$(cat {img})" {cmd}'
datalad save -m "Pin runtime container image to ${DIGEST}" .datalad

# Run the processing inside the published image. The image provides only the environment;
# the code and the dataset are bind-mounted in (see the call format). `--explicit` keeps
# datalad from clearing the outputs first, which is required when the outputs are also prior
# state (input) of the next incremental run.
RUN_INPUT_ARGS=()
if [ -n "${INPUT_SUBDATASET_URL}" ]; then
  RUN_INPUT_ARGS=(--input "${INPUT_SUBDATASET_PATH}")
fi
datalad containers-run -n pipeline --explicit \
  "${RUN_INPUT_ARGS[@]}" \
  --output derivatives \
  -m "Update <cache-name> (code @ ${GITHUB_SHA}; image ${DIGEST})" \
  "python /code/update.py --base-directory /tmp --limit ${LIMIT}"

# Publish the full results to the `derivatives` branch.
git -C "${DS}" push "${REPO_URL}" HEAD:derivatives

# Build and force-publish the consumer-facing `dist` artifact from a fresh repo.
uv run --project "${WORKSPACE}/envs" python "${WORKSPACE}/code/compress.py" --base-directory "${DS}"
mkdir -p "${DISTDIR}/derivatives"
cp "${DS}"/derivatives/*.jsonl.gz "${DISTDIR}/derivatives/"
cp "${WORKSPACE}/dataset_description.json" "${DISTDIR}/dataset_description.json"
git -C "${DISTDIR}" init -q -b dist
git -C "${DISTDIR}" config user.name "${BOT_NAME}"
git -C "${DISTDIR}" config user.email "${BOT_EMAIL}"
git -C "${DISTDIR}" add dataset_description.json derivatives
git -C "${DISTDIR}" commit -q -m "Publish <cache-name>"
git -C "${DISTDIR}" push -f "${REPO_URL}" dist:dist
