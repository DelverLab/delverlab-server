# Server releases

Add user-facing changes to `CHANGELOG.md` under `Unreleased` in the same pull request. CI checks the build and smoke-tests the container against PostgreSQL. Merging a pull request does not publish an image.

1. In **Actions → Publish release → Run workflow**, select `main` and choose `patch`, `minor`, or `major`. Leave `resume` blank. The first release is `0.1.0` regardless of bump choice.
2. The workflow closes `Unreleased`, tests the candidate, creates the release commit and tag, and records the tested image ID in a draft GitHub Release before pushing the image to GHCR. It verifies the image by digest, publishes the release notes, and updates `DelverLab/delverlab` with the version tag only (for example, `ghcr.io/delverlab/delverlab-server:0.1.0`). Published tags must not be overwritten. No workflow deploys to an installation automatically.
3. If the first image is private, a package admin must make it public in GitHub Package settings. The workflow refuses to update the install repository until it verifies a public pull.

If the run fails **before** the tag exists, fix the cause and start a new run from current `main`. If the tag already exists, dispatch from current `main` with `resume` set to the existing tag (for example `v0.1.0`). Resume continues that release without moving the tag, creating a new version, or replacing a published image.

The original `v0.1.0` remains valid even though its tagged commit is no longer on `main`: the tag was kept at the original commit to preserve the published image's provenance after correcting commit attribution. New release tags must be reachable from `main`.

## Repository setup

Use a GitHub App installed only on `delverlab-server` and `delverlab`, with **Contents: Read and write**. In the server's `release` environment, set `RELEASE_APP_CLIENT_ID` as a variable and `RELEASE_APP_PRIVATE_KEY` as a secret. Restrict the environment to the `main` branch. The App needs permission to bypass the server's pull-request/check rule for the tested release commit and to set release tags. Keep tag deletion/movement and branch force-push protections separate. Use only this workflow to write stable server image tags in GHCR.

Use a dedicated GHCR publisher account with package write access and a classic PAT with `write:packages` (no `repo` or `delete:packages`). Store the account login in the `release` environment variable `RELEASE_REGISTRY_USERNAME` and the PAT in the `release` environment secret `RELEASE_REGISTRY_TOKEN`. Do not grant repositories write access under the package's **Manage Actions access**. The build uses a project-specific source label so GHCR does not automatically link the repository. If you link the package to the source repository later, leave permission inheritance off. The repository `GITHUB_TOKEN` does not publish to this registry. Protect `main` and restrict publication credentials to the `release` environment.