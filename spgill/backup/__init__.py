# Stdlib imports
import datetime
import itertools
import json
import pathlib
import shlex
import shutil
import sys

# Vendor imports
import click
import colorama
from colorama import Fore, Style

# Local imports
from . import helper, command, config as applicationConfig


@click.group()
@click.pass_context
@click.option(
    "--config",
    "-c",
    type=str,
    help="""Path to backup config file. Defaults to '~/.spgill.backup.yaml'.""",
)
def cli(ctx, config):
    # Initialize colorama (for windows)
    colorama.init()

    # Load the config options and insert it into the context object
    ctx.obj = applicationConfig.loadConfigValues(config)

    # If there are no profiles defined, print error message and exit
    if not len(ctx.obj.get("profiles", {}).keys()):
        helper.printError(
            f"{Fore.RED}Error: No profiles defined in config{Style.RESET_ALL}"
        )


@cli.command(
    name="run", help="""Execute a backup profile with the name PROFILE."""
)
@click.pass_obj
@click.option(
    "--go",
    "-g",
    is_flag=True,
    help="""By default, this command runs in 'dry run' mode. This flag is necessary to actually execute the backup.""",
)
@click.argument("profile", type=str, required=True)
def cli_run(obj, go, profile):
    profileData = helper.getProfileData(obj, profile)

    # Get the repo's data
    helper.printLine("Selected profile:", profile)

    # If in preview mode, print a warning
    if not go:
        helper.printWarning(
            "Warning: Running in dry-run mode. Run tool again with '--go' option to execute backup."
        )

    helper.printLine("Beginning backup...")

    # Add global include/exclude rules, then iterate through each group and add theirs
    include = profileData.get("include", [])
    exclude = profileData.get("exclude", [])
    groups = profileData.get("groups", {})
    for groupName in groups:
        include += groups[groupName].get("include", [])
        exclude += groups[groupName].get("exclude", [])

    # Construct the restic args
    args = [
        *helper.getBaseResticsArgs(obj, profileData),
        "backup",
        *itertools.chain(
            *[["--tag", tag] for tag in profileData.get("tags", [])]
        ),
        *itertools.chain(*[["--exclude", pattern] for pattern in exclude]),
        *include,
        *profileData.get("args", []),
    ]

    # If this is the real deal, execute the backup
    if go:
        command.restic(
            args, _env=helper.getResticEnv(obj, profileData), _fg=True
        )

    # If in preview mode, just print the joined args
    else:
        print(shlex.join(args))


@cli.command(
    name="cli",
    context_settings=dict(
        ignore_unknown_options=True,
        allow_interspersed_args=False,
    ),
    help="""Execute the restic command line directly. Repo, cache, and password args for PROFILE are automatically added before your own RESTIC_ARGS.""",
)
@click.pass_obj
@click.argument("profile", type=str, required=True)
@click.argument("restic_args", nargs=-1, type=click.UNPROCESSED)
def cli_restic(obj, profile, restic_args):
    profileData = helper.getProfileData(obj, profile)

    # Compile everything, with unprocessed args, into a list
    args = [
        *helper.getBaseResticsArgs(obj, profileData),
        *restic_args,
    ]

    # Execute the command
    command.restic(args, _env=helper.getResticEnv(obj, profileData), _fg=True)


@cli.command(
    name="command",
    help="""Write the basic restic command to stdout. Helpful for using a repo in an external script.""",
)
@click.pass_obj
@click.argument("profile", type=str, required=True)
def cli_command(obj, profile):
    profileData = helper.getProfileData(obj, profile)

    if profileData["repo"].startswith("s3"):
        helper.printWarning(
            "Warning: S3 repos require credential environment variables be set before command execution!"
        )

    sys.stdout.write(
        shlex.join(["restic", *helper.getBaseResticsArgs(obj, profileData)])
    )


@cli.command(
    name="dump",
    help=f"""
    Extract and dump snapshots from repo of PROFILE to a tar archive at DESTINATION. If no SNAPSHOTS are given, defaults to 'latest' snapshot. Includes AES256 encyption and Zstd compression.

    {Fore.RED}WARNING{Style.RESET_ALL}: Only works on Linux/macOS
    """,
)
@click.pass_obj
@click.argument("destination", type=str, required=True)
@click.argument("profile", type=str, required=True)
@click.argument("snapshots", nargs=-1, type=str, required=False)
def cli_dump(obj, destination, profile, snapshots):
    profiles = obj.get("profiles", {})

    # Make sure the selected profile is defined
    if profile not in profiles:
        helper.printError(f"Error: No profile '{profile}' defined in config")
    profileData = profiles[profile]

    # Dump config is a nested object. Ensure it exists
    if not (dumpConfig := obj.get("dump", None)):
        helper.printError("Error: No dump options defined in config")

    # Reusable base args for following commands
    repoArgs = helper.getBaseResticsArgs(obj, profileData)
    repoEnv = helper.getResticEnv(obj, profileData)

    dumpDestDir = pathlib.Path(destination).expanduser()
    if not dumpDestDir.exists():
        helper.printError(
            f"Destination directory '{dumpDestDir}' does not exist"
        )

    # Cache directory is optional
    cacheEnabled = "cache" in dumpConfig
    dumpCacheDir = (
        pathlib.Path(
            dumpConfig.get("cache", obj.get("cache", "~"))
        ).expanduser()
        if cacheEnabled
        else dumpDestDir
    )

    dumpPasswordFile = pathlib.Path(dumpConfig.get("passwordFile", None))

    # If no snapshots have been selected, default to latest
    if not len(snapshots):
        snapshots = ["latest"]

    # Print some startup information
    helper.printLine("Selected profile:", profile)
    helper.printLine("Selected snapshots:", ", ".join(snapshots))

    # Iterate through each snapshot that's being dumped
    for snapshotName in snapshots:
        helper.printLine(f"Processing '{snapshotName}':")

        # Fetch information on the latest snapshot
        helper.printNestedLine(f"Querying snapshots for '{snapshotName}'...")
        snapsArgs = [*repoArgs, "--quiet", "snapshots", snapshotName, "--json"]
        snapsCommand = command.restic(snapsArgs, _env=repoEnv)
        if b"null" in snapsCommand.stdout:
            helper.printError(f"Could not find snapshot: '{snapshotName}'")
        latest = json.loads(snapsCommand.stdout)[0]

        # Convert the timestamp to a datetime object
        # Requires the we first round off the milliseconds to three decimal places
        latest["time"] = datetime.datetime.fromisoformat(
            helper.fixTimestamp(latest["time"])
        )

        # Creat timestamp and unique filename for this repo
        timestamp = latest["time"].strftime(r"%Y%m%d%H%M%S")
        repoDirName = pathlib.Path(profileData["repo"]).name
        filename = (
            f"{repoDirName}_{timestamp}_{latest['short_id']}.tar.zst.aes"
        )

        # Make sure the cache and destination files don't exist yet
        dumpCacheFile = dumpCacheDir / filename
        if dumpCacheFile.exists():
            helper.printError(f"Archive already exists at '{dumpCacheFile}'")
        dumpDestFile = dumpDestDir / filename
        if dumpDestFile.exists():
            helper.printError(
                f"Final archive already exists at '{dumpDestFile}'"
            )

        # Inform the user which snapshot is being used
        helper.printNestedLine(
            f"Using snapshot ID '{latest['id'][:8]}' with timestamp '{latest['time']}'"
        )

        # Fetch the size of the latest snapshot
        helper.printNestedLine("Querying snapshot size...")
        statsArgs = [*repoArgs, "--quiet", "stats", latest["id"], "--json"]
        statsCommand = command.restic(statsArgs, _env=repoEnv)
        latestSize = json.loads(statsCommand.stdout)["total_size"]
        helper.printNestedLine(
            f"Archive should be no larger than (approx.) {helper.humanReadable(latestSize)}"
        )

        # Ensure there's enough space in the cache dir and the destination
        dumpCacheUsage = shutil.disk_usage(dumpCacheDir)
        if dumpCacheUsage.free < latestSize:
            helper.printError(
                f"Error: Dump archive needs at least {helper.humanReadable(latestSize)}, "
                f"but directory only has {helper.humanReadable(dumpCacheUsage.free)} free"
            )
        dumpDestUsage = shutil.disk_usage(dumpDestDir)
        if dumpDestUsage.free < latestSize:
            helper.printError(
                f"Error: Dump archive needs at least {helper.humanReadable(latestSize)}, "
                f"but destination directory only has {helper.humanReadable(dumpDestUsage.free)} free"
            )

        # Begin dumping the repo to an archive
        helper.printNestedLine(
            "Creating archive... (compression and encryption enabled)"
        )
        print("Dumping to", dumpCacheFile)
        dumpCommand = command.openSsl(
            command.zStd(
                command.pv(
                    command.restic(
                        *[*repoArgs, "dump", latest["id"], "/"],
                        _env=repoEnv,
                        _piped=True,
                    ),
                    *["-pterbs", str(latestSize)],
                    _err=sys.stderr,
                    _piped=True,
                ),
                *["-c", "-T8"],
                _piped=True,
            ),
            *[
                "enc",
                "-aes-256-cbc",
                "-md",
                "sha512",
                "-pbkdf2",
                "-iter",
                "100000",
                "-pass",
                f"file:{dumpPasswordFile}",
                "-e",
            ],
            _out=str(dumpCacheFile),
        )

        # Detect dump errors
        if dumpCommand.exit_code != 0:
            helper.printError(
                f"Dump command returned with error code {dumpCommand.exit_code}. Aborting."
            )

        # Copy the dump archive to the destination, if cache was enabled
        if cacheEnabled:
            helper.printNestedLine("Moving archive to final destination...")
            dumpSize = dumpCacheFile.stat().st_size
            copyCommand = command.pv(
                *["-pterbs", dumpSize, dumpCacheFile],
                _out=str(dumpDestFile),
                _err=sys.stderr,
            )

            # After copying the dump to the destination, remove the cached dump file
            if copyCommand.exit_code != 0:
                helper.printError(
                    f"Copy command returned with error code {copyCommand.exit_code}. Aborting."
                )
            dumpCacheFile.unlink()

        # Print success message for this repo
        helper.printNestedLine(
            f"{Fore.GREEN}Success!{Style.RESET_ALL} Archive is now available at {dumpDestFile}"
        )


@cli.command(
    name="decrypt",
    context_settings=dict(
        ignore_unknown_options=True,
        allow_interspersed_args=False,
    ),
    help=f"""
    Take an archive at FILE_INPUT, that was previously generated by the dump command, and write the decrypted and decompressed archive to FILE_OUTPUT.

    {Fore.RED}WARNING{Style.RESET_ALL}: Only works on Linux/macOS
    """,
)
@click.pass_obj
@click.argument("file_input", type=click.File("rb"))
@click.argument("file_output", type=click.File("wb"))
def cli_decrypt(obj, file_input, file_output):
    # Ensure output is NOT a terminal
    if hasattr(file_output, "isatty") and file_output.isatty():
        helper.printError("Stdout is a TTY. Try piping this command instead.")

    # Construct arguments for the command chain
    dumpPasswordPath = obj.get("dump", {}).get("passwordFile", "")

    # Pipe openssl to zstd and then stdout
    command.zStd(
        command.openSsl(
            *[
                "enc",
                "-aes-256-cbc",
                "-md",
                "sha512",
                "-pbkdf2",
                "-iter",
                "100000",
                "-pass",
                f"file:{dumpPasswordPath}",
                "-d",
            ],
            _piped=True,
            _in=file_input,
        ),
        *["-dc", "-T8"],
        _out=file_output,
    )


@cli.command(
    name="list", help="""List all backup profiles defined in config file."""
)
@click.pass_obj
def cli_list(obj):
    profiles = obj.get("profiles", {})
    for i, profileName in enumerate(profiles):
        if i > 0:
            print()

        profileData = profiles[profileName]

        helper.printKeyVal("Name", profileName)
        helper.printKeyVal("  repo", profileData["repo"])
        helper.printKeyVal("  include", profileData.get("include", []))
        helper.printKeyVal("  exclude", profileData.get("exclude", []))


if __name__ == "__main__":
    cli()
