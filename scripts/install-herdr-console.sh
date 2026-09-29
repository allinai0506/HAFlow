#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
source_dir="${repo_root}/console"
target_dir="${HOME}/.herdr-console"

mkdir -p "${target_dir}"
install -m 755 "${source_dir}/herdr_factory_console.py" "${target_dir}/herdr_factory_console.py"
install -m 755 "${source_dir}/launcher.applescript" "${target_dir}/launcher.applescript"
install -m 755 "${source_dir}/HerdrDashboard.command" "${target_dir}/HerdrDashboard.command"

# Flow Workbench v1 依赖本地离线静态资源（X6 / Dagre），必须随脚本一起部署，
# 否则部署后的 Console 读不到 /static/vendor/*，流程图会显示明确错误。
if [[ -d "${source_dir}/static" ]]; then
  mkdir -p "${target_dir}/static"
  rsync -a --delete "${source_dir}/static/" "${target_dir}/static/"
fi

if [[ "${1:-}" != "--no-restart" ]]; then
  launchctl kickstart -k "gui/$(id -u)/com.user.herdr-factory-console"
fi

echo "共事工厂控制台已同步到 ${target_dir}"
