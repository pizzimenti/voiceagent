#!/usr/bin/env bash
# Build voiceagent from the working tree and publish into bradley-local.
#
# After this runs:
#   sudo pacman -Syu voiceagent
#
# First-time migration off a foreign (pacman -U) install:
#   sudo pacman -Rns voiceagent
#   sudo pacman -Sy voiceagent

set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/.local/share/pacman-localrepo}"
REPO_NAME="${REPO_NAME:-bradley-local}"

cd "$(dirname "$(readlink -f "$0")")/.."

if [[ ! -d "$REPO_DIR" ]]; then
  echo "error: local repo dir not found: $REPO_DIR" >&2
  exit 1
fi

# Do NOT pass --cleanbuild here. makepkg sets srcdir=${startdir}/src
# and --cleanbuild rm -rf's it before build() runs. In this repo, src/
# IS the working tree (src/voiceagent/, src/vendor/, etc.), so cleanbuild
# would wipe the source code. The PKGBUILD's own build() already removes
# stale build/dist/egg-info before rebuilding — that's enough.
makepkg -f

shopt -s nullglob
pkgs=( ./voiceagent-*.pkg.tar.zst )
shopt -u nullglob

if (( ${#pkgs[@]} == 0 )); then
  echo "error: no voiceagent-*.pkg.tar.zst produced by makepkg" >&2
  exit 1
fi

mv -f "${pkgs[@]}" "$REPO_DIR/"

cd "$REPO_DIR"
# -R: drop the previous voiceagent-*.pkg.tar.zst from disk after updating the db
repo-add -R "$REPO_NAME.db.tar.gz" "${pkgs[@]##*/}"

echo
echo "Published to $REPO_NAME:"
printf '  %s\n' "${pkgs[@]##*/}"
echo
echo "Next: sudo pacman -Syu voiceagent"
