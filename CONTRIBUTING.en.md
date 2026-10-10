# Contributing

[简体中文](CONTRIBUTING.md)

Device integration, build tools and documentation belong in this repository.
Kernel changes belong in `linux-sm8450-liuqin`, on the `liuqin-6.17` branch.
The project build selects an exact kernel commit through `kernel/source.json`.

Keep changes focused and explain the behavior they address. Preserve original
authors and license notices when adapting third-party code. Build and test the
affected components before proposing a change.

For device results, include the model, source revision, image version and steps
to reproduce. Distinguish a successful build from a successful hardware test.
Remove credentials, device addresses, serial numbers and personal data from
logs before sharing them.

Do not change a published release tag. A release must identify its sources and
build inputs, and its installation and recovery instructions must match the
tested images. Kernel and root filesystem updates must preserve user accounts
and files unless an explicitly selected reinstall requires otherwise.
