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

The CLI/TUI launch, confirmation and final result presentation are handled in
#78. End-to-end validation of this path on disposable Linux VMs is tracked in
#79.
