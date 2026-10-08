#!/usr/bin/env bash
# Creates build/ with three small files, the target of the rm -rf the eval asks for.
set -euo pipefail
mkdir -p build
echo one > build/a.txt
echo two > build/b.txt
echo three > build/c.txt
