#!/bin/sh
# Reload nginx after any certificate renewal (only if config is valid)
nginx -t -q && systemctl reload nginx
