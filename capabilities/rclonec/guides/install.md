# Installing rclone

rclonec does not vendor rclone. It resolves `rclone` on PATH, so the binary is installed once on the machine and shared by everything on it, updated on its own schedule.

rclone is a single statically linked binary that depends on nothing but the operating system. A package manager adds no value here and takes one away: `rclone selfupdate` maintains a binary installed directly, but should not be used against one a package manager owns.

## Direct install

The official script fetches the right build for the platform and installs the binary with its man page:

```sh
sudo -v ; curl https://rclone.org/install.sh | sudo bash
```

To avoid root, take the archive instead and put the binary anywhere on PATH:

```sh
curl -fsSLO https://downloads.rclone.org/rclone-current-osx-arm64.zip
unzip -q rclone-current-osx-arm64.zip
install -m 755 rclone-*/rclone ~/bin/rclone
```

Substitute the platform in the archive name — `rclone-current-linux-amd64.zip` on a typical server. [rclone's install page](https://rclone.org/install/) lists the builds.

## Verifying the download

Each release publishes checksums signed by the maintainer. Verify both before installing:

```sh
curl -fsSLO https://downloads.rclone.org/v<version>/SHA256SUMS
curl -fsSL https://rclone.org/KEYS | gpg --import
gpg --verify SHA256SUMS
shasum -a 256 rclone-current-<platform>.zip
```

The signature should name the maintainer's key, and the checksum should match the line for the versioned archive. rclone documents the signing key at [release signing](https://rclone.org/release_signing/); the "key is not certified" notice is web-of-trust, and comparing the fingerprint against the published one is the check that matters.

A browser-downloaded archive on macOS carries a quarantine flag that blocks the first run. Fetching with `curl` avoids it; `xattr -d com.apple.quarantine <path>` clears it if it is already there.

## Keeping it current

```sh
rclone selfupdate --check
rclone selfupdate
```

It verifies hash and signature before replacing the running binary.

## Proving it from here

`rclonec doctor` reports the path it resolved and the version it found, alongside each connection's own round-trip. A missing or shadowed binary is diagnosed there rather than inferred from a failing command.
