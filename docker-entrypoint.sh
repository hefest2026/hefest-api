#!/bin/sh
set -e
uv run tortoise -c hefest.config.TORTOISE_ORM migrate
exec "$@"
