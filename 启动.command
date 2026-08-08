#!/bin/bash
# 双击这个文件即可启动（macOS / Linux）
cd "$(dirname "$0")"

# 找一个可用的 python
PY=""
for c in python3 python; do
  if command -v $c >/dev/null 2>&1; then
    if $c -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" 2>/dev/null; then
      PY=$c; break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo
  echo "===================================================="
  echo "  没找到 Python（需要 3.10 以上）"
  echo "===================================================="
  echo "  去 python.org 下载安装，然后重新双击本文件。"
  echo
  read -p "  按回车键关闭。"
  exit 1
fi

$PY tools/launcher.py
