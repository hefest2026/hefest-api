#!/bin/sh
set -eu
echo "--- running migrations ---"
uv run tortoise -c hefest.config.TORTOISE_ORM migrate
echo "--- migrations done ---"
exec "$@"
