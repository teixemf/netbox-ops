# Self-upgrade installation and recovery

The upgrade helper performs one verified release step at a time. A prepared
candidate is not an installed release.

The initiating process calls `launch_prepared_release` and then exits. The
helper runs using the verified base Python interpreter, from the private
`/opt/.netbox-ops-handoff` directory. It inherits the initiating process pidfd
and the existing exclusive operations lock. The helper waits for the exact
initiating process to exit before moving any installation directory.

Under that lock, it rechecks the official compatibility plan, selected release,
local installation identity and prepared candidate contents. It switches the
directories on the same filesystem, validates the new installation and only
then removes the previous installation. It retains the original top-level
directory permissions. No installer or NetBox service command runs.

## Recovery

If a handoff is interrupted, do not delete the transaction record, backup,
candidate or handoff directory. The independent recovery command is:

```sh
sudo /opt/.netbox-ops-handoff/recover
```

This works even when `/opt/netbox-ops` is temporarily missing. It uses the
existing lock and transaction record. An unvalidated transaction is rolled
back; a validated transaction only resumes cleanup. If state or directory
identities cannot be verified, recovery stops rather than guessing.

Review `/var/log/netbox-ops/operations.log` for the result. A helper PID is not
proof of a successful upgrade. If there is no remaining handoff directory,
the normal completion or rollback has cleaned up the independent helper.

## Operator commands

```sh
sudo netbox-ops self-upgrade list
sudo netbox-ops self-upgrade show vX.Y.Z
sudo netbox-ops self-upgrade plan vX.Y.Z
sudo netbox-ops self-upgrade install vX.Y.Z
sudo netbox-ops self-upgrade status
```

For a noninteractive installation, pass `--confirm 'UPGRADE vX.Y.Z'` with the
exact target version. Never use an unverified release. The installation command
exits after successful handoff (exit 0 means submitted, not installed).
A mandatory intermediate step is installed separately: check its result and
review the original target again before submitting the next step.

The last result is stored in `/var/lib/netbox-ops/self-upgrade-result.json`.
The TUI and fallback text menu also provide access to the last result.

Exit codes: 0 for a successful read/plan/submission or a completed target
reported by status; 1 for failure; 2 for invalid arguments or missing
noninteractive confirmation; 3 for a blocked or cancelled upgrade; 4 for
a pending/unknown result or a completed intermediate step with more steps
remaining. The status command needs the installed launcher; if it is missing,
use the independent recovery command above.

End-to-end validation on disposable Linux VMs is tracked in #79.
