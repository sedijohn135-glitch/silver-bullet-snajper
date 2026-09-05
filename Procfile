# Railway / Heroku process definition.
#
# `worker` is the right process type: the bot is a long-running scheduler, not an
# HTTP service. If you deploy it as a `web` service instead, Railway sets PORT and
# the bot automatically exposes /health and /status on it.
worker: python -u main.py
