# Mycelium

Mycelium lets two Apple silicon Macs share the work of running one language model. Each Mac runs part of
the model, and you chat with the result.

This is **v0.2**, a rewrite of the Python v0.1.4 app in Rust. It ships as one native macOS app with no
Python inside. v0.1.4's history is kept on the [`legacy/v0.1.4`](../../tree/legacy/v0.1.4) branch.

## Install

Download the latest release from the [Releases](../../releases) page. Check the zip's SHA-256 against the
`.sha256` file beside it, unzip it, and follow `QUICKSTART-MACOS.md` in the unzipped folder. The release
notes list the requirements and known issues.

You need two Apple silicon Macs with macOS 15.6.1 or later; the owner Mac needs macOS 26.

## Repository layout

- `crates/`: the product libraries and the desktop app (`crates/desktop`).
- `test-support/`: shared test infrastructure (obligations registry, budget model, simulation).
- `xtask/`: workspace tooling: lints, gates, release checks and repository audits.

The v0.1.4 Python app served as the behaviour reference during the port. It was only ever driven from the
outside, as a process and HTTP endpoint, so this repository holds no Python.

## Contributing

Enable the repository's Git hooks once per clone:

```sh
git config core.hooksPath .githooks
```

They refuse a commit that adds a personal home-folder path (`/Users/<name>`, `/home/<name>/`) or your
account name, in files or in the commit message. Write `~/` or `$HOME` instead. The same rule runs in the
test suite (`cargo test -p xtask --test no_home_paths`) and as `cargo xtask no-home-paths`. Allowed
placeholder names are listed in `xtask/data/home-path-placeholders.txt`.

The Rust toolchain is pinned in `rust-toolchain.toml`. Building the app needs the pinned native MLX
inputs described in `crates/desktop/`.

## License

AGPL-3.0-only (see `LICENSE`). Every source file carries an SPDX header.
