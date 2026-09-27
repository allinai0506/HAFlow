#!/bin/bash
# HerdrDashboard 双击入口:拉起控制台服务并打开人类仪表板
set -euo pipefail
uid="$(id -u)"
launchctl kickstart -k "gui/${uid}/com.user.herdr-factory-console" >/dev/null 2>&1 || true
sleep 1
open "http://127.0.0.1:8765/?view=dashboard"
