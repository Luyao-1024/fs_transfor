#!/bin/sh
# FsTransfor 启动脚本: 使用项目 venv(含 paramiko, 复用系统 PyGObject/GTK)
cd "$(dirname "$0")" || exit 1
exec .venv/bin/python main.py "$@"
