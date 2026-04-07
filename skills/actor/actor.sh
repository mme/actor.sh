#!/bin/sh
DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHONPATH="$DIR" exec python3 -m actor "$@"
