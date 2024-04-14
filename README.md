# atomic-update ⚛️
Never let another update break your read-write [openSUSE](https://en.wikipedia.org/wiki/OpenSUSE) system!

## Synopsis
atomic-update is a simple single file program with just one external dependency that can be used to perform atomic (transactional) updates of openSUSE systems with read-write root filesystems such as Tumbleweed and Slowroll. It can also be used on Leap, even though Leap-specific update commands are yet to be added.

atomic-update uses [btrfs subvolumes](https://btrfs.readthedocs.io/en/latest/Subvolumes.html) and [snapper snapshots](http://snapper.io/) to safely perform updates to a new root filesystem snapshot while minimizing any side-effects to your currently running system/snapshot.

### How it works
- On performing an update or running a command using atomic-update, a new root filesystem snapshot is created
- The new snapshot is used to boot an ephemeral container to see which services are in a failed state, for later comparison
- All changes are made against this new snapshot and not to the currently running system's snapshot
- The snapshot is booted again in an ephemeral container to see if the changes broke any new services
- If the changes are successful, the new snapshot is set as the default snapshot. The changes can be either applied live or the system rebooted into the new default snapshot
- If the changes are unsuccessful, the new snapshot is discarded

Performing updates like this have a number of benefits:
- Prevent a broken system due to system crash, power loss, or other unforeseen events during an update
- Prevent updates from impacting the currently running system, causing the currently running programs, including but not limited to the desktop environment to crash
- Prevent non-interactive updates from breaking system due to conflicts/errors causing zypper to abort (the default action on conflicts/errors) in the middle of an update
- Prevent updates from causing an inconsistent system state due to failing scripts during an otherwise successful update
- Avoid having to reboot into read-only grub snapshots to perform rollback

Downsides:
- Updates must be either applied live or the system rebooted shortly thereafter to avoid losing changes made to the old root filesystem.

### Acknowledgements
atomic-update is heavily inspired by the excellent [transactional-update](https://github.com/openSUSE/transactional-update) package for read-only root filesystems. All the credit goes to them 🤗

Even though transactional-update works on read-write systems as of version 4.6.0, it's not officially supported and the lead developer has [stated](https://bugzilla.opensuse.org/show_bug.cgi?id=1221742#c27) support may be removed in the future if there are conflicts with read-only filesystem features.

## Installation
1. Install external dependency for booting snapshots in an ephemeral container to check for issues. `systemd-nspawn` is part of systemd and very small 👼
```
sudo zypper install systemd-container
```

2. Install atomic-update, just a single python script you can read through in a few minutes 📜
```
curl -s https://raw.githubusercontent.com/pavinjosdev/atomic-update/main/atomic-update | sudo tee /usr/bin/atomic-update > /dev/null
sudo chmod 755 /usr/bin/atomic-update
```

## Usage
Type in `atomic-update --help` for usage help.

```
Usage: atomic-update [options] command

atomic-update provides safer transactional operations
for openSUSE systems with read-write root filesystems.

Commands:
  dup                 - Perform distribution upgrade
  run <cmd>           - Run a command in a new snapshot
  rollback [number]   - Set the current or given snapshot as default snapshot

Options:
  --reboot              - Reboot after update
  --apply               - Switch into default snapshot without reboot
  --shell               - Open shell in new snapshot before exiting
  --continue [number]   - Use latest or given snapshot as base
  --no-verify           - Skip verification of snapshot
  --interactive         - Run dup in interactive mode
  --debug               - Enable debug output
  --help                - Print this help and exit
  --version             - Print version number and exit
```

---

## Video examples
1. Perform distribution upgrade on Tumbleweed/Slowroll and apply it live without rebooting

![atomic-dup](https://github.com/pavinjosdev/atomic-update/assets/11430516/29f74398-89ce-4a7f-ae8a-b0d8936ccaa1)

2. Test what happens when an update breaks

![break-snapshot](https://github.com/pavinjosdev/atomic-update/assets/11430516/ece7f041-5028-464b-85f3-40702c306930)

---

## Examples
1. Perform distribution upgrade on Tumbleweed and reboot
```
sudo atomic-update --reboot dup
```

2. Perform distribution upgrade on Tumbleweed and apply it live without rebooting
```
sudo atomic-update --apply dup
```

3. Update packages on Leap and reboot
```
sudo atomic-update --reboot run zypper update
```

4. Drop into a bash shell in a new snapshot
```
sudo atomic-update --shell run true
```

5. Run bash script in a new snapshot and drop into a bash shell in the same snapshot afterward
```
sudo atomic-update --shell run bash -c 'date | awk "{print \$1}" && whoami'
```

6. Troubleshoot a failing dup by running dup interactively and dropping into a bash shell afterward
```
sudo atomic-update --shell --interactive dup
```

7. Continue making updates to the previous snapshot in a new snapshot
```
sudo atomic-update --shell --continue run true
```

> Without `--continue` option, atomic-update would always base the new snapshot from the currently booted snapshot.
Use this option to not lose changes made to a previous snapshot. Option `--apply` implies continue.

8. Rollback to currently booted snapshot
```
sudo atomic-update rollback
```

## Uninstallation
1. Remove atomic-update
```
sudo rm /usr/bin/atomic-update
```

2. Optionally, uninstall `systemd-nspawn`
```
sudo zypper remove systemd-container
```

## Troubleshooting
Specify the `--debug` option for troubleshooting.
atomic-update is intended to catch SIGINT (Ctrl+C) and properly cleanup.
If for some reason it does not cleanup such as when receiving SIGTERM or SIGKILL, current or future operations should not be affected.
atomic-update keeps its working directory in `/tmp/atomic-update_*`, so a reboot would always cleanup.

## Known issues
- When switching to a new snapshot without reboot using the `--apply` option, future updates to the bootloader (prior to a reboot) such as running `update-bootloader` script must be performed from a new atomic snapshot.
