#!/usr/bin/env bash
# Build a secret-free NetBox VM and check two disposable boots from its QCOW2.
set -euo pipefail
mode=$1
workdir="$RUNNER_TEMP/netbox-ci"
mkdir -p "$workdir"
cd "$workdir"
cleanup() {
  status=$?
  if (( status != 0 )); then
    if [[ -f serial.log ]]; then tail -n 100 serial.log || true; fi
  fi
  if [[ -f vm.pid ]]; then kill "$(cat vm.pid)" 2>/dev/null || true; fi
}
trap cleanup EXIT
test -c /dev/kvm || { echo '::error::KVM unavailable'; exit 1; }
sudo chmod a+rw /dev/kvm
qmp_powerdown() {
  python3 - <<'PY'
import json
import socket
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
    sock.settimeout(5)
    sock.connect("qmp.sock")
    with sock.makefile("rwb") as f:
        if "QMP" not in json.loads(f.readline()):
            raise RuntimeError("QMP unavailable")
        for command in ("qmp_capabilities", "system_powerdown"):
            f.write((json.dumps({"execute": command}) + "\n").encode())
            f.flush()
            while True:
                response = json.loads(f.readline())
                if "return" in response or "error" in response:
                    break
            if "error" in response:
                raise RuntimeError(response["error"])
PY
}
wait_for_exit() {
  local pid=$1 limit=$2 state
  for (( i=0; i<limit; i++ )); do
    state=$(ps -o stat= -p "$pid" 2>/dev/null || true)
    if [[ -z "$state" || "$state" == Z* ]]; then return 0; fi
    sleep 1
  done
  return 1
}
stop_vm() {
  local pid
  pid=$(cat vm.pid)
  if ! wait_for_exit "$pid" 45; then
    echo '::warning::Requesting ACPI shutdown via QMP'
    qmp_powerdown
    wait_for_exit "$pid" 90 || { echo '::error::VM shutdown failed'; return 1; }
  fi
  rm -f vm.pid qmp.sock
}
boot_vm() {
  local disk=$1 attempt=$2
  rm -f vm.pid qmp.sock serial.log
  ssh-keygen -q -t ed25519 -N '' -f "key-$attempt"
  cat > user-data <<EOF
#cloud-config
users:
  - default
ssh_authorized_keys:
  - $(cat "key-$attempt.pub")
ssh_pwauth: false
EOF
  cat > meta-data <<EOF
instance-id: netbox-ci-$GITHUB_RUN_ID-$attempt
local-hostname: netbox-ci-$attempt
EOF
  if [[ -n "${NETBOX_VM_UPGRADE_SCENARIO:-}" && "$mode" == verify ]]; then
    # Isolate automatic APT transactions before guest provisioning.
    # These settings live only in the disposable overlay.
    cat >> user-data <<'EOF'
package_update: false
package_upgrade: false
bootcmd:
  - [systemctl, mask, --runtime, apt-daily.timer, apt-daily-upgrade.timer, apt-daily.service, apt-daily-upgrade.service]
  - [systemctl, stop, apt-daily.timer, apt-daily-upgrade.timer]
write_files:
  - path: /etc/apt/apt.conf.d/99-netbox-ci-no-periodic
    permissions: '0644'
    content: |
      APT::Periodic::Enable "0";
EOF
  fi
  cloud-localds seed.img user-data meta-data
  qemu-system-x86_64 \
    -machine q35,accel=kvm -cpu host -smp 2 -m 4096 \
    -drive file="$disk",format=qcow2,if=virtio,discard=unmap \
    -drive file=seed.img,format=raw,if=virtio \
    -netdev user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22 \
    -device virtio-net-pci,netdev=net0 \
    -display none -serial file:serial.log -monitor none \
    -qmp unix:qmp.sock,server=on,wait=off \
    -daemonize -pidfile vm.pid
  ssh_opts=(-i "./key-$attempt" -p 2222 -o BatchMode=yes -o ConnectTimeout=3 \
    -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=./known-$attempt")
  ready=false
  for i in $(seq 1 90); do
    if ssh ${ssh_opts[@]} ubuntu@127.0.0.1 true 2>/dev/null; then
      ready=true
      break
    fi
    sleep 3
  done
  [[ "$ready" == true ]] || { echo '::error::VM SSH timeout'; return 1; }
  timeout 240 ssh ${ssh_opts[@]} ubuntu@127.0.0.1 'sudo cloud-init status --wait'
}
ssh_guest() {
  timeout 2400 ssh ${ssh_opts[@]} ubuntu@127.0.0.1 "$@"
}
if [[ "$mode" == build ]]; then
  started=$SECONDS
  image=ubuntu-24.04-server-cloudimg-amd64.img
  url=https://cloud-images.ubuntu.com/releases/noble/release
  curl -fLsS --retry 3 -o "$image" "$url/$image"
  curl -fLsS --retry 3 -o SHA256SUMS "$url/SHA256SUMS"
  grep -F " *$image" SHA256SUMS | sha256sum -c -
  qemu-img create -f qcow2 -F qcow2 -b "$PWD/$image" overlay-build.qcow2
  qemu-img resize overlay-build.qcow2 12G
  boot_vm overlay-build.qcow2 build
  # Only NetBox goes into the baseline. The current netbox-ops checkout is
  # installed afresh for every verification boot.
  scp -i ./key-build -P 2222 -o BatchMode=yes \
    -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=./known-build \
    "$GITHUB_WORKSPACE/scripts/ci/netbox-vm-guest.sh" \
    ubuntu@127.0.0.1:/home/ubuntu/netbox-vm-guest.sh
  ssh_guest 'sudo bash /home/ubuntu/netbox-vm-guest.sh prepare'
  # Refuse to publish when sanitization fails.
  ssh_guest 'sudo bash /home/ubuntu/netbox-vm-guest.sh sanitize'
  qmp_powerdown
  stop_vm
  qemu-img convert -c -O qcow2 overlay-build.qcow2 netbox-ubuntu24.qcow2
  qemu-img check netbox-ubuntu24.qcow2
  sha256sum netbox-ubuntu24.qcow2 > SHA256SUMS.txt
  {
    echo '## NetBox baseline build'
    echo "Image size: $(stat -c %s netbox-ubuntu24.qcow2) bytes"
    echo "Build duration: $((SECONDS-started)) seconds"
    echo 'Contains migrated NetBox schema, but no runtime secrets, SSH keys or netbox-ops checkout.'
  } >> "$GITHUB_STEP_SUMMARY"
  echo "PASS: reusable NetBox baseline built in $((SECONDS-started)) s"
elif [[ "$mode" == verify ]]; then
  sha256sum -c SHA256SUMS.txt
  base_hash=$(sha256sum netbox-ubuntu24.qcow2 | cut -d' ' -f1)

  # Ship the current PR checkout to each disposable VM, not the baseline.
  tar -C "$GITHUB_WORKSPACE" --exclude=.git --exclude=.github \
    --exclude=venv --exclude=__pycache__ -czf source.tar.gz .
  attempts=(1 2)
  scenario="${NETBOX_VM_UPGRADE_SCENARIO:-}"
  if [[ "${NETBOX_VM_COVERAGE:-0}" == 1 ]]; then
    [[ "${NETBOX_VM_E2E:-0}" == 1 && -z "$scenario" && "${NETBOX_VM_SELF_UPGRADE_E2E:-0}" != 1 ]] || {
      echo 'Coverage pilot requires the baseline E2E scenario only' >&2
      exit 2
    }
  fi
  if [[ -n "$scenario" ]]; then
    case "$scenario" in
      netbox|plugins|combined|cross-minor|failure) attempts=(1) ;;
      *) echo 'Invalid upgrade test scenario' >&2; exit 2 ;;
    esac
  fi
  for attempt in "${attempts[@]}"; do
    started=$SECONDS
    qemu-img create -f qcow2 -F qcow2 -b "$PWD/netbox-ubuntu24.qcow2" "overlay-$attempt.qcow2"
    boot_vm "overlay-$attempt.qcow2" "$attempt"
    scp -i "./key-$attempt" -P 2222 -o BatchMode=yes \
      -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=./known-$attempt" \
      "$GITHUB_WORKSPACE/scripts/ci/netbox-vm-guest.sh" source.tar.gz \
      ubuntu@127.0.0.1:/home/ubuntu/
    ssh_guest 'sudo bash /home/ubuntu/netbox-vm-guest.sh provision'
    if [[ -n "$scenario" ]]; then
      ssh_guest 'printf "%s\\n" netbox-ci-e2e | sudo tee /run/netbox-ops-ci-e2e >/dev/null'
      ssh_guest "sudo bash /opt/netbox-ops/scripts/ci/netbox-vm-upgrade-e2e.sh $scenario"
    elif (( attempt == 1 )); then
      if [[ "${NETBOX_VM_E2E:-0}" == 1 ]]; then
        ssh_guest 'printf "%s\\n" netbox-ci-e2e | sudo tee /run/netbox-ops-ci-e2e >/dev/null'
        if [[ "${NETBOX_VM_COVERAGE:-0}" == 1 ]]; then
          ssh_guest 'sudo bash /opt/netbox-ops/scripts/ci/netbox-vm-coverage.sh setup'
        fi
        ssh_guest 'sudo bash /opt/netbox-ops/scripts/ci/netbox-vm-e2e.sh'
        if [[ "${NETBOX_VM_COVERAGE:-0}" == 1 ]]; then
          ssh_guest 'sudo bash /opt/netbox-ops/scripts/ci/netbox-vm-coverage.sh collect'
          mkdir -p "$workdir/coverage-e2e"
          scp -i "./key-$attempt" -P 2222 -o BatchMode=yes \
            -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=./known-$attempt" \
            'ubuntu@127.0.0.1:/run/netbox-ops-coverage-export/*' "$workdir/coverage-e2e/"
        fi
      fi
      ssh_guest 'sudo touch /var/tmp/ci-isolation-marker'
    else
      ssh_guest 'sudo test ! -e /var/tmp/ci-isolation-marker'
    fi
    # Self-upgrade runs only in the isolated second boot.
    if (( attempt == 2 )) && [[ "${NETBOX_VM_SELF_UPGRADE_E2E:-0}" == 1 ]]; then
      from_tag="${NETBOX_VM_SELF_UPGRADE_FROM:-}"
      target_tag="${NETBOX_VM_SELF_UPGRADE_TARGET:-}"
      ssh_guest 'printf "%s\\n" netbox-ci-e2e | sudo tee /run/netbox-ops-ci-e2e >/dev/null'
      if [[ -n "$from_tag" || -n "$target_tag" ]]; then
        if [[ ! "$from_tag" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ||
              ! "$target_tag" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
          echo 'Invalid official self-upgrade release pair' >&2
          exit 2
        fi
        ssh_guest "sudo /usr/bin/python3 -I -B /opt/netbox-ops/scripts/ci/netbox-vm-self-upgrade.py --official-from $from_tag --official-target $target_tag"
      else
        ssh_guest 'sudo /usr/bin/python3 -I -B /opt/netbox-ops/scripts/ci/netbox-vm-self-upgrade.py'
      fi
    fi
    test "$(sha256sum netbox-ubuntu24.qcow2 | cut -d' ' -f1)" = "$base_hash"
    ssh_guest 'sudo systemctl poweroff --no-block' || true
    stop_vm
    echo "PASS: isolated NetBox boot $attempt in $((SECONDS-started)) s"
  done
  sha256sum -c SHA256SUMS.txt
  {
    echo '## Reusable NetBox baseline verified'
    if [[ -n "$scenario" ]]; then
      echo "PASS: $scenario upgrade checks in disposable overlay; baseline hash unchanged."
    else
      echo 'Two isolated boots succeeded using the current checkout, with a pre-migrated NetBox schema.'
      echo 'The base image hash remained unchanged; the second boot has no marker from the first.'
    fi
  } >> "$GITHUB_STEP_SUMMARY"
else
  echo "Usage: $0 build|verify" >&2
  exit 2
fi
