#!/usr/bin/env python3
#
# SPDX-License-Identifier: GPL-3.0-only
#
# Copyright (C) 2024  Pavin Joseph <https://github.com/pavinjosdev>
#
# atomic-update is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 3
# as published by the Free Software Foundation.
#
# atomic-update is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with atomic-update; if not, see <http://www.gnu.org/licenses/>.

import os
import sys
import time
import json
import signal
import logging
import tempfile
import subprocess
from shlex import quote
import xml.etree.ElementTree as ET

# Constants
VERSION = "0.1.0"
ZYPPER_PID_FILE = "/run/zypp.pid"
VALID_CMD = ["dup", "run", "rollback"]
VALID_OPT = ["--reboot", "--apply", "--shell", "--continue", "--no-verify", \
            "--interactive", "--debug", "--help", "--version"]

# Command help/usage info
help_text = """
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
"""

################################

# Function to get output and exit code of shell command
def shell_exec(command):
    res = subprocess.run(command, shell=True, capture_output=True, encoding="utf8", errors="replace")
    output = res.stdout + res.stderr
    return output.strip(), res.returncode

# Function to get snapper root config name
def get_snapper_root_config():
    config_json = shell_exec("snapper --jsonout list-configs")[0]
    config = json.loads(config_json)
    for item in config["configs"]:
        if item["subvolume"] == "/":
            return item["config"]

# Function to get snapper active and default snapshots
def get_snaps(snapper_root_config):
    snaps_json = shell_exec(f"snapper --jsonout -c {snapper_root_config} list --disable-used-space")[0]
    snaps = json.loads(snaps_json)
    active_snap, default_snap = (None,)*2
    for item in snaps[snapper_root_config]:
        active_snap = item["number"] if item["active"] else active_snap
        default_snap = item["number"] if item["default"] else default_snap
    return active_snap, default_snap

# Function to get latest atomic snapshot of status
# valid status: created, pending, finished
def get_atomic_snap(snapper_root_config, status):
    snaps_json = shell_exec(f"snapper --jsonout -c {snapper_root_config} list --disable-used-space")[0]
    snaps = json.loads(snaps_json)
    snaps[snapper_root_config].reverse()
    for item in snaps[snapper_root_config]:
        try:
            if item["userdata"]["atomic"] == status:
                return item["number"]
        except:
            pass

# Function to verify snapshot by booting it up as a container
def verify_snapshot():
    logging.debug("Booting container")
    cmd = ["systemd-nspawn", "--directory", TMP_MOUNT_DIR, "--ephemeral", "--boot"]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    logging.debug("Getting container id")
    container_id = None
    for _ in range(10):
        out, ret = shell_exec("LC_ALL=C machinectl --quiet --no-pager -o json list")
        containers = json.loads(out)
        for container in containers:
            if ( container["class"] == "container" and container["service"] == "systemd-nspawn" and
            container["machine"].startswith(f"{TMP_MOUNT_DIR.split('/').pop()}") ):
                container_id = container["machine"]
                break
        if container_id:
            break
        time.sleep(1)
    logging.debug(f"Container ID = {container_id}")
    if not container_id:
        logging.error("Could not bootup ephemeral container from snapshot. Cancelling task...")
        cleanup()
        sys.exit()
    logging.debug("Waiting for container bootup to finish...")
    startup_finished = False
    for _ in range(120):
        out, ret = shell_exec(f"LC_ALL=C machinectl --quiet shell {container_id} /usr/bin/bash -c 'systemd-analyze time'")
        if out.find("Startup finished") != -1:
            startup_finished = True
            break
        if startup_finished:
            break
        time.sleep(1)
    if not startup_finished:
        logging.error("Timeout waiting for bootup of ephemeral container from snapshot. Cancelling task...")
        cleanup()
        sys.exit()
    logging.debug("Getting failed systemd units")
    out, ret = shell_exec(f"LC_ALL=C machinectl --quiet shell {container_id} /usr/bin/bash -c 'systemctl --quiet --no-pager -o json --failed | cat -'")
    out = json.loads(out)
    failed_units = [item["unit"] for item in out]
    logging.debug(f"Number of failed units = {len(failed_units)}")
    logging.debug(f"Failed units = {', '.join(failed_units)}")
    logging.debug("Stopping container...")
    shell_exec(f"machinectl stop {container_id}")
    return failed_units

# Function to cleanup on SIGINT or successful completion
def cleanup():
    logging.info("Cleaning up...")
    logging.debug("Stopping ephemeral systemd-nspawn containers...")
    out, ret = shell_exec("LC_ALL=C machinectl --quiet --no-pager -o json list")
    containers = json.loads(out)
    for container in containers:
        if ( container["class"] == "container" and container["service"] == "systemd-nspawn" and
        container["machine"].startswith(f"{TMP_MOUNT_DIR.split('/').pop()}") ):
            container_id = container["machine"]
            shell_exec(f"machinectl stop {container_id}")
    logging.debug("Cleaning up temp mounts...")
    umount_command = f"""
LC_ALL=C mount -l | grep '{TMP_MOUNT_DIR}' | awk '{{print $3}}' | awk '{{print length, $0}}' | sort -rn | awk '{{print $2}}' | awk '{{system("umount " $0)}}';
"""
    while True:
        out, ret = shell_exec(umount_command)
        if out == "" and ret == 0:
            break
        time.sleep(0.01)
    logging.debug("Cleaning up temp dirs...")
    shell_exec(f"rmdir {quote(TMP_MOUNT_DIR)}")
    shell_exec(f"rmdir {quote(TMP_DIR)}")
    logging.debug("Cleaning up unfinished snapshots...")
    snapper_root_config = get_snapper_root_config()
    for status in ["created", "pending"]:
        snap_num = get_atomic_snap(snapper_root_config, status)
        if snap_num:
            shell_exec(f"snapper -c {snapper_root_config} delete {snap_num}")

# Function to handle SIGINT
def sigint_handler(signum, frame):
    signal.signal(signum, signal.SIG_IGN) # ignore additional signals
    cleanup()
    sys.exit(0)

################################

# Handle SIGINT gracefully
signal.signal(signal.SIGINT, sigint_handler)

# Parse command, options, and args
COMMAND = ""
OPT = []
ARG = []
for index, item in enumerate(sys.argv):
    if index == 0:
        continue
    if item.startswith("--"):
        if item in VALID_OPT:
            OPT.append(item)
        else:
            print(f"Invalid option {item!r}. See usage below.\n")
            print(help_text.strip())
            sys.exit(1)
    else:
        if item in VALID_CMD:
            COMMAND = item
            ARG = sys.argv[index+1:]
            break
        else:
            print(f"Invalid command {item!r}. See usage below.\n")
            print(help_text.strip())
            sys.exit(1)

# Print help
if "--help" in OPT:
    print(help_text.strip())
    sys.exit()

# Print version
if "--version" in OPT:
    print(f"atomic-update v{VERSION}")
    sys.exit()

# Validate command
if not COMMAND:
    print(f"No valid command provided. See usage below.\n")
    print(help_text.strip())
    sys.exit(1)
if COMMAND == "run" and not ARG:
    print(f"No argument provided for command {COMMAND!r}. See usage below.\n")
    print(help_text.strip())
    sys.exit(1)

DEBUG = True if "--debug" in OPT else False
CONFIRM = True if "--interactive" in OPT else False
REBOOT = True if "--reboot" in OPT else False
APPLY = True if "--apply" in OPT else False
SHELL = True if "--shell" in OPT else False
CONTINUE = True if "--continue" in OPT else False
NO_VERIFY = True if "--no-verify" in OPT else False

# Setup logging
logging.basicConfig(
    stream=sys.stdout,
    format="%(asctime)s: %(levelname)s: %(message)s",
    level=logging.DEBUG if DEBUG else logging.INFO,
)

# check if there's a snapshot number provided to continue from
continue_num = None
if "--continue" in OPT:
    try:
        continue_num = int(sys.argv[sys.argv.index("--continue") + 1])
        if not continue_num in range(1, 999999):
            logging.error("Invalid value for option '--continue'. Must be between 1 to 999999 (inclusive)")
            sys.exit(1)
    except ValueError:
        logging.debug("No numerical value provided for option '--continue'")
        pass
    except IndexError:
        logging.debug("No value provided for option '--continue'")
        pass

if continue_num:
    ret = os.system(f"btrfs subvolume list / | grep '@/.snapshots/{continue_num}/snapshot'")
    if ret != 0:
        logging.error(f"Provided snapshot {continue_num} for option '--continue' does not exist")
        sys.exit(1)

# check if there's a snapshot number provided for rollback
rollback_num = None
if COMMAND == "rollback":
    try:
        rollback_num = int(ARG[0])
        if not rollback_num in range(1, 999999):
            logging.error("Invalid snapshot number provided for rollback. Must be between 1 to 999999 (inclusive)")
            sys.exit(1)
    except ValueError:
        logging.debug("Invalid value provided as snapshot number for rollback")
        sys.exit(1)
    except IndexError:
        logging.debug("No snapshot number provided for rollback")
        pass

if rollback_num:
    ret = os.system(f"btrfs subvolume list / | grep '@/.snapshots/{rollback_num}/snapshot'")
    if ret != 0:
        logging.error(f"Provided snapshot {rollback_num} for rollback does not exist")
        sys.exit(1)

# Bail out if we're not root
if os.getuid() != 0:
    logging.error("Bailing out, program must be run with root privileges")
    sys.exit(2)

# Bail out if required dependecies are not available
programs = ["zypper", "snapper", "btrfs", "echo", "ps", "sed", "awk", "bash", "sort", \
            "env", "chroot", "mount", "umount", "rmdir", "findmnt", "systemd-nspawn", \
            "systemctl", "machinectl"]
for program in programs:
    if not shell_exec(f"command -v {program}"):
        logging.error(f"Bailing out, missing required dependecy {program!r} in PATH ({os.environ.get('PATH')}) " \
            f"for user {os.environ.get('USER')!r}. The following programs " \
            f"are required for atomic-update to function: {', '.join(programs)}"
        )
        sys.exit(3)

# Check if zypper is already running
pid = None
pid_program = None
if os.path.isfile(ZYPPER_PID_FILE):
    with open(ZYPPER_PID_FILE, "r") as f:
        pid = f.read().strip()
        try:
            pid = int(pid)
        except ValueError:
            pid = None
        if pid:
            pid_program = shell_exec(f"ps -p {pid} | sed '1d' | awk '{{print $4}}'")
            if pid_program:
                msg = f"zypper is already invoked by the application with pid {pid} ({pid_program}).\n" \
                "Close this application before trying again."
                logging.error(msg)
                sys.exit(4)

# Create secure temp dir
TMP_DIR = tempfile.mkdtemp(dir="/tmp", prefix="atomic-update_")
TMP_MOUNT_DIR = f"{TMP_DIR}/rootfs"
os.makedirs(TMP_MOUNT_DIR, mode=0o700, exist_ok=True)

# Handle commands: dup, run
if COMMAND in ["dup", "run"]:
    logging.info(f"Starting atomic {'distribution upgrade' if COMMAND == 'dup' else 'transaction'}...")
    # get snapper root config name
    snapper_root_config = get_snapper_root_config()
    logging.debug(f"Snapper root config name: {snapper_root_config}")
    if not snapper_root_config:
        logging.error("No snapper config found for root '/'. Configure snapper and try again.")
        sys.exit(5)
    # get active and default snapshot number
    active_snap, default_snap = get_snaps(snapper_root_config)
    logging.debug(f"Active snapshot number: {active_snap}, Default snapshot number: {default_snap}")
    base_snap = active_snap
    if CONTINUE or APPLY:
        base_snap = default_snap
        if continue_num:
            base_snap = continue_num
    # warn user when rebasing from old snapshot
    # thus losing changes to snapshots made in the interim
    if not continue_num and base_snap != default_snap:
        logging.warn(f"This snapshot is being created from a different base ({base_snap}) " \
                    f"than the previous default snapshot ({default_snap}) and does not " \
                    f"contain the changes from the latter.")
    # create new read-write snapshot to perform atomic update in
    out, ret = shell_exec(f"snapper -c {snapper_root_config} create -c number " \
                          f"-d 'Atomic update of #{base_snap}' " \
                          f"-u 'atomic=created' --from {base_snap} --read-write")
    if ret != 0:
        logging.error(f"Could not create read-write snapshot to perform atomic update in")
        sys.exit(6)
    # get latest atomic snapshot number we just created
    atomic_snap = get_atomic_snap(snapper_root_config, "created")
    logging.debug(f"Latest atomic snapshot number: {atomic_snap}")
    logging.info(f"Using snapshot {base_snap} as base for new snapshot {atomic_snap}")
    snap_subvol = f"@/.snapshots/{atomic_snap}/snapshot"
    snap_dir = snap_subvol.lstrip("@")
    # update atomic snapshot status
    shell_exec(f"snapper -c {snapper_root_config} modify -u 'atomic=pending' {atomic_snap}")
    # check the latest atomic snapshot exists as btrfs subvolume
    out, ret = shell_exec(f"LC_ALL=C btrfs subvolume list / | grep '{snap_subvol}'")
    if ret != 0:
        logging.error(f"Could not find latest atomic snapshot subvolume {snap_subvol}. Discarding snapshot {atomic_snap}")
        shell_exec(f"snapper -c {snapper_root_config} delete {atomic_snap}")
        sys.exit(7)
    # find the device where root fs resides
    out, ret = shell_exec("LC_ALL=C findmnt --json /")
    if ret != 0:
        logging.error(f"Could not find root filesystem device. Discarding snapshot {atomic_snap}")
        shell_exec(f"snapper -c {snapper_root_config} delete {atomic_snap}")
        sys.exit(8)
    out = json.loads(out)["filesystems"][0]
    rootfs_device = out["source"].split("[")[0]
    logging.debug(f"Btrfs root device: {rootfs_device}")
    # populate temp dir with atomic snapshot mounts
    logging.info("Setting up temp mounts...")
    commands = f"""
mount -o subvol={snap_subvol} {rootfs_device} {TMP_MOUNT_DIR};
for i in dev proc run sys; do mount --rbind --make-rslave /$i {TMP_MOUNT_DIR}/$i; done;
chroot {TMP_MOUNT_DIR} mount -a;
"""
    shell_exec(commands)
    # verify snapshot prior to performing update
    if not NO_VERIFY:
        logging.info("Verifying snapshot prior to update...")
        pre_failed_units = verify_snapshot()
    if COMMAND == "dup":
        # check if dup has anything to do
        logging.info("Checking for packages to upgrade")
        xml_output, ret = shell_exec(f"LC_ALL=C zypper --root {TMP_MOUNT_DIR} --non-interactive --no-cd --xmlout dist-upgrade --dry-run")
        docroot = ET.fromstring(xml_output)
        for item in docroot.iter('install-summary'):
            num_pkgs = int(item.attrib["packages-to-change"])
        if not num_pkgs:
            logging.info("Nothing to do. Exiting...")
            cleanup()
            sys.exit()
        logging.info("Performing distribution upgrade within chroot...")
        ret = os.system(f"zypper --root {TMP_MOUNT_DIR} {'' if CONFIRM else '--non-interactive'} --no-cd dist-upgrade")
        if ret != 0:
            logging.error(f"Zypper returned exit code {ret}. Discarding snapshot {atomic_snap}")
            shell_exec(f"snapper -c {snapper_root_config} delete {atomic_snap}")
            cleanup()
            sys.exit(9)
        logging.info(f"Distribution upgrade completed successfully")
    elif COMMAND == "run":
        exec_cmd = ' '.join(ARG)
        logging.info(f"Running command {exec_cmd!r} within chroot...")
        ret = os.system(f"chroot {TMP_MOUNT_DIR} {exec_cmd}")
        if ret != 0:
            logging.error(f"Command returned exit code {ret}. Discarding snapshot {atomic_snap}")
            shell_exec(f"snapper -c {snapper_root_config} delete {atomic_snap}")
            cleanup()
            sys.exit(9)
        logging.info("Command run successfully")
    if SHELL:
        logging.info(f"Opening bash shell within chroot of snapshot {atomic_snap}")
        logging.info("Continue with 'exit 0' or discard with 'exit 1'")
        command = f"""
chroot {TMP_MOUNT_DIR} bash -c "export PS1='atomic-update:\${{PWD}} # '; exec bash"
"""
        ret = os.system(command)
        if ret != 0:
            logging.error(f"Shell returned exit code {ret}. Discarding snapshot {atomic_snap}")
            shell_exec(f"snapper -c {snapper_root_config} delete {atomic_snap}")
            cleanup()
            sys.exit()
    # verify snapshot after update
    if not NO_VERIFY:
        logging.info("Verifying snapshot post update...")
        post_failed_units = verify_snapshot()
        new_failed_units = list( set(post_failed_units) - set(pre_failed_units) )
        if new_failed_units:
            logging.error(f"Discarding snapshot {atomic_snap} as the following new " \
                          f"systemd units have failed since update: {', '.join(new_failed_units)}")
            shell_exec(f"snapper -c {snapper_root_config} delete {atomic_snap}")
            cleanup()
            sys.exit()
    # on success, update atomic snapshot status
    shell_exec(f"snapper -c {snapper_root_config} modify -u 'atomic=finished' {atomic_snap}")
    # on success, set new snapshot as the default
    logging.info(f"Setting snapshot {atomic_snap} ({snap_dir}) as the new default")
    shell_exec(f"snapper -c {snapper_root_config} modify --default {atomic_snap}")
    # perform cleanup
    cleanup()
    if REBOOT:
        logging.info("Rebooting now...")
        os.system("systemctl reboot")
        sys.exit()
    if APPLY:
        logging.info(f"Using default snapshot {atomic_snap} to replace running system...")
        logging.info("Applying /usr...")
        command = f"mount --bind --make-rslave {snap_dir}/usr /usr"
        logging.debug(command)
        os.system(command)
        # find subvols under /usr and mount them
        out, ret = shell_exec("LC_ALL=C btrfs subvolume list / | grep -v snapshots | grep '@/usr' | awk '{print $9}'")
        for subvol in out.split("\n"):
            subdir = subvol.lstrip("@")
            command = f"mount -o subvol={subvol} {rootfs_device} {subdir}"
            logging.debug(command)
            os.system(command)
        logging.info("Applying /etc...")
        command = f"mount --bind --make-rslave {snap_dir}/etc /etc"
        logging.debug(command)
        os.system(command)
        logging.info("Applying /boot...")
        command = f"mount --bind --make-rslave {snap_dir}/boot /boot"
        logging.debug(command)
        os.system(command)
        # find subvols under /boot and mount them
        out, ret = shell_exec("LC_ALL=C btrfs subvolume list / | grep -v snapshots | grep '@/boot' | awk '{print $9}'")
        for subvol in out.split("\n"):
            subdir = subvol.lstrip("@")
            command = f"mount -o subvol={subvol} {rootfs_device} {subdir}"
            logging.debug(command)
            os.system(command)
        # mount ESP if it exists
        out, ret = shell_exec("LC_ALL=C findmnt --json /boot/efi")
        if ret == 0:
            out = json.loads(out)["filesystems"][0]
            command = f"mount {out['source']} {out['target']}"
            logging.debug(command)
            os.system(command)
        logging.info("Executing systemctl daemon-reexec...")
        os.system("systemctl daemon-reexec")
        logging.info("Executing systemd-tmpfiles --create...")
        os.system("systemd-tmpfiles --create")
        logging.info("Applied default snapshot as new base for running system")
        logging.info("Running processes will not be restarted automatically")
        logging.info("Until the next reboot, bootloader changes must be made from a new atomic snapshot")
        sys.exit()

# Handle command: rollback
elif COMMAND == "rollback":
    invalid_opts = OPT.copy()
    invalid_opts.remove("--debug") if "--debug" in OPT else None
    if invalid_opts:
        logging.warn(f"Options {', '.join(invalid_opts)!r} do not apply to rollback command")
    if rollback_num:
        logging.info(f"Rolling back to snapshot {rollback_num}")
        os.system(f"snapper rollback -c number {rollback_num}")
    else:
        logging.info("Rolling back to currently booted snapshot")
        os.system("snapper rollback -c number")

# If we're here, remind user to reboot
logging.info("Please reboot your machine to activate the changes and avoid data loss")
sys.exit()
