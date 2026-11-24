#!/bin/sh
cd "$(dirname "$0")"
exec python3 latex_compiler.py "$@"
