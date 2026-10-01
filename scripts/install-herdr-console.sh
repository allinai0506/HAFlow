#!/usr/bin/env bash
# 共事工厂控制台 —— 部署脚本
#
# 部署拓扑（改动前务必先读 docs/operations/service-management.md §2.2）：
#
#   com.user.herdr-factory-console ─┐
#                                    ├─ 跑 ~/.herdr-controller/releases/<commit> 冻结快照
#   com.user.herdr-controller      ─┘   （由 git archive 从 commit 重建）
#   com.user.herdr-sentinel         ┐
#   com.user.herdr-notifier         ┴─ 直跑工作区 ~/HAFlow/ 源码
#
# 两条路径的更新方式完全不同：
#
#   * 快照类（console / controller）：改工作区代码**不会生效**。必须提交 →
#     重建快照 → 改 plist → bootout + bootstrap。`launchctl kickstart` 不重读
#     plist（launchd 用已加载的 job 配置快照），所以改完 plist 只 kickstart
#     等于没改。教训见 docs/lessons/lessons-learned.md §108。
#   * 工作区类（sentinel / notifier）：直接 kickstart 即可。
#
# 旧版本只把 console 脚本复制到 ~/.herdr-console/ 并 kickstart，而 plist 指向
# release —— 复制的副本永远不会被执行，脚本"成功"但线上文案一个字没变。
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
source_dir="${repo_root}/console"
target_dir="${HOME}/.herdr-console"
releases_dir="${HOME}/.herdr-controller/releases"
agent_dir="${HOME}/Library/LaunchAgents"
domain="gui/$(id -u)"

# 跑 release 快照的服务：必须重建快照 + 改 plist + bootout/bootstrap
SNAPSHOT_SERVICES=(
  com.user.herdr-factory-console
  com.user.herdr-controller
)
# 直跑工作区源码的服务：plist 不变，kickstart 即可
KICKSTART_SERVICES=(
  com.user.herdr-sentinel
  com.user.herdr-notifier
)

# 可选：第一个参数指定要部署的 commit（默认 HEAD）
target_sha=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sha) target_sha="${2:-}"; shift 2 ;;
    --sha=*) target_sha="${1#*=}"; shift ;;
    --no-restart) restart=0; shift ;;
    -h|--help)
      sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知参数: $1（可用: --sha <commit> | --no-restart）" >&2; exit 2 ;;
  esac
done
restart="${restart:-1}"

cd "${repo_root}"

if [[ -z "${target_sha}" ]]; then
  target_sha="$(git rev-parse HEAD)"
fi
if ! git cat-file -e "${target_sha}^{commit}" 2>/dev/null; then
  echo "目标 commit 不存在: ${target_sha}" >&2
  exit 1
fi
target_sha="$(git rev-parse "${target_sha}^{commit}")"
short_sha="${target_sha:0:12}"
release_dir="${releases_dir}/${target_sha}"

echo "==> 目标 commit: ${short_sha}"

# ---------------------------------------------------------------------------
# 1. 重建 release 快照（git archive = HEAD 的纯净镜像）
#
# 不加 --delete：旧快照是唯一回滚路径，任何情况下都不清空 releases/。
# ---------------------------------------------------------------------------
if [[ -d "${release_dir}" ]] && [[ -f "${release_dir}/console/herdr_factory_console.py" ]]; then
  echo "==> 快照已存在，复用: ${release_dir}"
else
  echo "==> 重建快照: ${release_dir}"
  mkdir -p "${release_dir}"
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "${tmp_dir}"' EXIT
  git archive --format=tar "${target_sha}" | tar -x -C "${tmp_dir}"
  # 先在临时目录解包成功，再落到 release 目录，避免半成品快照
  rsync -a "${tmp_dir}/" "${release_dir}/"
fi

# ---------------------------------------------------------------------------
# 2. 双击入口与静态资源同步到 ~/.herdr-console
#
# 只同步入口与资源，**不再**复制 console 脚本：plist 指向 release 快照，
# 这里的副本不会被执行，留着只会误导人去改一个不生效的文件。
# ---------------------------------------------------------------------------
mkdir -p "${target_dir}"
install -m 755 "${source_dir}/launcher.applescript" "${target_dir}/launcher.applescript"
install -m 755 "${source_dir}/HerdrDashboard.command" "${target_dir}/HerdrDashboard.command"

# Flow Workbench v1 依赖本地离线静态资源（X6 / Dagre），必须随脚本一起部署，
# 否则部署后的 Console 读不到 /static/vendor/*，流程图会显示明确错误。
if [[ -d "${source_dir}/static" ]]; then
  mkdir -p "${target_dir}/static"
  rsync -a --delete "${source_dir}/static/" "${target_dir}/static/"
fi

if [[ "${restart}" != "1" ]]; then
  echo "共事工厂控制台已同步（--no-restart，未重载服务）"
  exit 0
fi

# ---------------------------------------------------------------------------
# 3. 改 plist 指向新快照
# ---------------------------------------------------------------------------
for service in "${SNAPSHOT_SERVICES[@]}"; do
  plist="${agent_dir}/${service}.plist"
  [[ -f "${plist}" ]] || { echo "跳过未安装的 agent: ${plist}" >&2; continue; }

  if [[ "${service}" == "com.user.herdr-factory-console" ]]; then
    # console 靠 HERDR_ROOT 决定 import 哪个 herdr/ 包，漏改必然加载旧包
    plutil -replace EnvironmentVariables.HERDR_ROOT -string "${release_dir}" "${plist}"
    plutil -replace ProgramArguments.1 \
      -string "${release_dir}/console/herdr_factory_console.py" "${plist}"
  else
    plutil -replace ProgramArguments.1 \
      -string "${release_dir}/services/herdr-controller.py" "${plist}"
  fi
  plutil -lint "${plist}" >/dev/null
  echo "==> plist 已指向 ${short_sha}: ${service}"
done

# ---------------------------------------------------------------------------
# 4. 重载服务
#
# 关键：bootout + bootstrap 才会重读 plist；kickstart 不会。
# ---------------------------------------------------------------------------
for service in "${SNAPSHOT_SERVICES[@]}"; do
  launchctl bootout "${domain}/${service}" >/dev/null 2>&1 || true
done
sleep 1
for service in "${SNAPSHOT_SERVICES[@]}" "${KICKSTART_SERVICES[@]}"; do
  if [[ -f "${agent_dir}/${service}.plist" ]]; then
    launchctl bootstrap "${domain}" "${agent_dir}/${service}.plist" >/dev/null 2>&1 \
      || launchctl kickstart -k "${domain}/${service}" >/dev/null 2>&1 \
      || echo "警告: ${service} 重载失败，请手动检查" >&2
  fi
done

# ---------------------------------------------------------------------------
# 5. 部署后自检
#
# 「脚本 exit 0」和「进程 PID 变了」都证明不了服务加载了新代码 ——
# 必须核对运行中进程的实际命令行，并与目标 commit 比对。
# ---------------------------------------------------------------------------
sleep 3
status=0
for service in "${SNAPSHOT_SERVICES[@]}"; do
  pid="$(launchctl list | awk -v s="${service}" '$3==s {print $1; exit}')"
  if [[ -z "${pid}" || "${pid}" == "-" ]]; then
    echo "  ✗ ${service} 未运行" >&2
    status=1
    continue
  fi
  cmdline="$(ps -o command= -p "${pid}" 2>/dev/null || true)"
  if [[ "${cmdline}" == *"${target_sha}"* ]]; then
    echo "  ✓ ${service} (pid ${pid}) 已加载 ${short_sha}"
  else
    echo "  ✗ ${service} (pid ${pid}) 加载的不是 ${short_sha}:" >&2
    echo "      ${cmdline}" >&2
    status=1
  fi
done

if [[ "${status}" != "0" ]]; then
  echo "部署自检未通过" >&2
  exit 1
fi

echo "共事工厂控制台已部署到 release ${short_sha}（回滚: 改 plist 指向旧快照目录后 bootout+bootstrap）"
