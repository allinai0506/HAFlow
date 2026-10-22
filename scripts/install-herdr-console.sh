#!/usr/bin/env bash
# 共事工厂控制台 —— 部署脚本
#
# 根据已安装 plist 的真实 script 路径识别 release / 工作区服务。
# 纳管清单来自 herdr.service_release.SCRIPTS（唯一权威）。
# 参数合同：Python 解释器 + 一个已知服务脚本 + 该服务声明的参数词表；
# 参数值属宿主状态，逐字保留；未知参数需显式迁移。
# 完整构造 ProgramArguments / HERDR_ROOT / WorkingDirectory，批量验证后逐文件原子替换。
# 指向 releases/<sha> 但不受本脚本管理的服务会使部署失败，除非 --allow-unmanaged 豁免。
# --no-restart 更新配置但不声明运行进程已加载新版本。
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
source_dir="${repo_root}/console"
target_dir="${HOME}/.herdr-console"
releases_dir="${HOME}/.herdr-controller/releases"
agent_dir="${HOME}/Library/LaunchAgents"
domain="gui/$(id -u)"

# 纳管清单的唯一权威是 herdr/service_release.py 的 SCRIPTS；此处不做第二份副本。
# 历史上 bash 数组、Python 字典、测试常量各存一份白名单，带参数的服务因此被
# 静默排除在部署之外，永远钉在旧 release 快照上。
SERVICE_LABELS="$(PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}" python3 -m herdr.service_release --labels)" || {
  echo "无法读取服务纳管清单 (herdr.service_release --labels)" >&2; exit 1; }
SERVICES=(${SERVICE_LABELS})
[[ ${#SERVICES[@]} -gt 0 ]] || { echo "服务纳管清单为空" >&2; exit 1; }
# Bash 3.2 + nounset treats empty arrays as unset; guard every optional expansion.
SNAPSHOT_SERVICES=()
KICKSTART_SERVICES=()
for service in "${SERVICES[@]}"; do
  plist="${agent_dir}/${service}.plist"
  [[ -f "${plist}" ]] || continue
  layout="$(python3 - "${plist}" <<'PY_LAYOUT'
import pathlib, plistlib, sys
args = plistlib.load(open(sys.argv[1], 'rb')).get('ProgramArguments', [])
print('release' if len(args) >= 2 and 'releases' in pathlib.Path(args[1]).parts else 'workspace')
PY_LAYOUT
)"
  if [[ "${layout}" == release ]]; then
    SNAPSHOT_SERVICES+=("${service}")
  else
    KICKSTART_SERVICES+=("${service}")
  fi
done

# 可选：第一个参数指定要部署的 commit（默认 HEAD）
target_sha=""
allow_unmanaged=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sha) target_sha="${2:-}"; shift 2 ;;
    --sha=*) target_sha="${1#*=}"; shift ;;
    --allow-unmanaged) allow_unmanaged+=("${2:-}"); shift 2 ;;
    --allow-unmanaged=*) allow_unmanaged+=("${1#*=}"); shift ;;
    --no-restart) restart=0; shift ;;
    -h|--help)
      awk 'NR>1 && /^#/{sub(/^# ?/,""); print; next} NR>1{exit}' "${BASH_SOURCE[0]}"
      exit 0 ;;
    *) echo "未知参数: $1（可用: --sha <commit> | --no-restart | --allow-unmanaged <label>）" >&2; exit 2 ;;
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

# ---------------------------------------------------------------------------
# 0. 未纳管的钉死服务：在写任何东西之前停下
#
# plist 一旦指向 releases/<sha> 而本脚本又不发布它，它就永远停在旧代际上：
# 不报错、不告警，直到某天该服务代码真的变了才发现。
# 命名空间与清单判定都在 Python 侧，脚本不再复制一份服务前缀。
# ---------------------------------------------------------------------------
if ! drift_report="$(PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}" \
  python3 -m herdr.service_release --drift "${agent_dir}")"; then
  echo "未纳管服务检查失败，拒绝部署" >&2
  exit 1
fi
drift_blocked=0
while read -r service pinned; do
  [[ -n "${service}" ]] || continue
  lag="unknown"
  if git cat-file -e "${pinned}^{commit}" 2>/dev/null; then
    lag="$(git rev-list --count "${pinned}..HEAD" 2>/dev/null || echo unknown)"
  fi
  exempt=0
  for allowed in ${allow_unmanaged[@]+"${allow_unmanaged[@]}"}; do
    [[ "${allowed}" == "${service}" ]] && exempt=1
  done
  if [[ "${exempt}" == "1" ]]; then
    echo "已豁免未纳管服务: ${service}（钉在 ${pinned:0:12}，落后 ${lag} 个提交；本次部署不更新它）"
  else
    echo "错误: ${service} 钉在 release ${pinned:0:12}（落后 ${lag} 个提交），但本脚本不发布它。" >&2
    echo "      加入 herdr/service_release.py 的 SCRIPTS 纳管，或用 --allow-unmanaged ${service} 显式豁免。" >&2
    drift_blocked=1
  fi
done <<< "${drift_report}"
if [[ "${drift_blocked}" == "1" ]]; then
  exit 1
fi

echo "==> 目标 commit: ${short_sha}"

# ---------------------------------------------------------------------------
# 1. 重建 release 快照（git archive = HEAD 的纯净镜像）
#
# 不加 --delete：旧快照是唯一回滚路径，任何情况下都不清空 releases/。
# ---------------------------------------------------------------------------
tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT
git archive --format=tar "${target_sha}" | tar -x -C "${tmp_dir}"
if [[ -d "${release_dir}" ]]; then
  echo "==> 快照已存在，复用: ${release_dir}"
else
  echo "==> 重建快照: ${release_dir}"
  mkdir -p "${release_dir}"
  # 先在临时目录解包成功，再落到 release 目录，避免半成品快照
  rsync -a "${tmp_dir}/" "${release_dir}/"
fi
PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}" python3 -m herdr.service_release --verify-snapshot "${tmp_dir}" "${release_dir}" "${target_sha}"

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

# 全部配置验证成功后才发布；未知用户参数不会被静默删除。
plist_paths=()
for service in "${SERVICES[@]}"; do
  plist="${agent_dir}/${service}.plist"
  [[ -f "${plist}" ]] && plist_paths+=("${plist}")
done
PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}" python3 -m herdr.service_release "${release_dir}" ${plist_paths[@]+"${plist_paths[@]}"}

if [[ "${restart}" != "1" ]]; then
  echo "配置已发布；running_sha / running_import_root=unknown（--no-restart，未重载服务）"
  exit 0
fi

# ---------------------------------------------------------------------------
# 4. 重载服务
#
# 关键：bootout + bootstrap 才会重读 plist；kickstart 不会。
# ---------------------------------------------------------------------------
for service in ${SNAPSHOT_SERVICES[@]+"${SNAPSHOT_SERVICES[@]}"}; do
  launchctl bootout "${domain}/${service}" >/dev/null 2>&1 || true
done
sleep 1
for service in ${SNAPSHOT_SERVICES[@]+"${SNAPSHOT_SERVICES[@]}"} ${KICKSTART_SERVICES[@]+"${KICKSTART_SERVICES[@]}"}; do
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
for service in ${SNAPSHOT_SERVICES[@]+"${SNAPSHOT_SERVICES[@]}"}; do
  pid="$(launchctl list | awk -v s="${service}" '$3==s {print $1; exit}')"
  if [[ -z "${pid}" || "${pid}" == "-" ]]; then
    echo "  ✗ ${service} 未运行" >&2
    status=1
    continue
  fi
  cmdline="$(ps -o command= -p "${pid}" 2>/dev/null || true)"
  if [[ "${cmdline}" == *"${target_sha}"* ]]; then
    echo "  ✓ ${service} (pid ${pid}) 启动命令指向 ${short_sha}（import 根/组件实际版本需服务运行态回执验证）"
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
