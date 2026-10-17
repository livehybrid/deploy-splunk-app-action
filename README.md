# Splunk App CI/CD

Reusable GitHub Actions building blocks for packaging, vetting and publishing
Splunk apps and add-ons. One place to change a gate, every add-on gets it.

This repo gives you two things:

1. **A composite action** (`action.yml`) that builds and packages a UCC add-on.
2. **Reusable workflows** (`.github/workflows/`) for AppInspect and publishing
   (GitHub release and Splunkbase).

Most repos use the reusable workflows, and either the composite action or their
own build step. A complete worked example lives in
[`livehybrid/splunk-app-cicd-pattern`](https://github.com/livehybrid/splunk-app-cicd-pattern).

## The pattern

```
  Commit / PR  ->  ucc-gen build  ->  AppInspect  ->  package  ->  publish
                                     cloud+future     + commit     release /
                                                        hash       Splunkbase
```

## Composite action

Builds a UCC add-on with `ucc-gen`, packages it as a tarball versioned
`<tag>+<short commit hash>`, and uploads it as an artifact named `dist`.

| Input | Description | Required | Default |
|-------|-------------|----------|---------|
| `python-version` | Python version used to build. | No | `3.9` |

It uploads the artifact `dist` for later jobs to consume. It declares no
outputs, and it does **not** run AppInspect: that is the `appinspect-cli`
workflow below.

```yaml
jobs:
  package:
    runs-on: ubuntu-latest
    steps:
      - uses: livehybrid/deploy-splunk-app-action@v1
        with:
          python-version: "3.11"
```

If you need control over the build (a different app directory, permission
normalisation, a custom tarball name), write the build steps in your own repo
and keep using the reusable workflows below. That is what most add-ons in the
estate do, and the example repo shows it.

## Reusable workflows

All of them expect the packaged tarball in an artifact named `dist`, so run
them `needs:` your build job.

### `appinspect-cli.yml`

AppInspect CLI, one job per tag, `fail-fast: false` so you see every tag's
result in one run. No Splunkbase account needed.

| | |
|---|---|
| Inputs | `tags` (required, string) comma separated, e.g. `cloud,future,private_victoria` |
| Secrets | `token` (required) usually `${{ secrets.GITHUB_TOKEN }}`, used to post the results markdown |

`cloud` answers "will this run on Splunk Cloud?". `future` answers "what is
about to break?", which is the tag most people miss. Findings can be waived in
a committed `.appinspect.expect.yaml`.

### `appinspect-api.yml`

The same vetting Splunkbase runs, via the AppInspect API. Runs
`included_tags: private_victoria`, `excluded_tags: offensive`, and uploads
`AppInspect_response.html` as an artifact. Skipped for `dependabot[bot]`, which
gets no secrets.

| | |
|---|---|
| Inputs | none |
| Secrets | `splunkbase_username`, `splunkbase_password` (both required) |

Needs a Splunkbase account, so gate the job if you want forks to stay green.
API-side findings are waived in a committed `.appinspect_api.expect.yaml`.

### `publish.yml`

Creates or updates a GitHub release and attaches every `*.tar.gz` it finds.
Conditioned on `startsWith(github.ref, 'refs/tags/v')`, so a stray commit
cannot ship an add-on.

| | |
|---|---|
| Inputs | none |
| Secrets | none |

### `publish-splunkbase.yml`

Publishes the packaged artifact to the app's existing Splunkbase listing
through the Splunkbase release API. Same flow Splunk use for their own add-ons:

1. Checks the public listing before sending anything: the package's app id
   must match the listing, the listing must not be archived, and the version
   must be newer than the newest release. A version that is already published
   counts as success, so re-running a tag pipeline is a no-op.
2. Logs in at `api.splunk.com` for a JWT (Splunkbase's preferred auth).
3. Uploads the release **hidden**.
4. Waits for Splunkbase's own validation.
5. Only on a pass: makes the release public with release notes, and sets it as
   the listing's latest release.

A failed validation never goes public; it leaves a hidden release you can
delete in the Splunkbase UI. A public release can never be deleted or
replaced, which is why it is the last step.

Runs on a `v*` tag, or for an existing GitHub release via `release_tag`.
Tag-triggered uploads are **off** until the caller repo sets the repository
variable `SPLUNKBASE_PUBLISH_ENABLED=true`, so a fork or a fresh template
never uploads by accident. `dry_run` and `force` run regardless.

| | |
|---|---|
| Inputs | `app_id` (required), `splunk_versions` (required, e.g. `10.6,10.4,10.2,10.0,9.4`), `cim_versions`, `visible` (default `true`), `release_notes` (default links the GitHub release), `release_tag`, `dry_run`, `force`, `package_id` (finish an upload that timed out), `timeout_seconds` (default 1800), `tooling_ref` |
| Secrets | `splunkbase_username`, `splunkbase_password` (the same splunk.com account as the AppInspect API) |

`splunk_versions` has no default on purpose: claim only the versions the app
is tested against. The first release of a brand-new app has to be created in
the Splunkbase UI, because the API can only add releases to a listing.

The publisher is `splunkbase-publish/publish.py`, standard library only, so it
also runs by hand:

```bash
SPLUNKBASE_USERNAME=... SPLUNKBASE_PASSWORD=... python3 splunkbase-publish/publish.py \
  --app-id 4207 --package dist/ --splunk-versions 10.6,10.4,10.2,10.0,9.4 --dry-run
```

### `deploy-scde.yml`

Optional, off by default, and not part of the recommended pattern. Installs the
packaged artifact onto a Splunk Cloud Developer Edition stack via ACS, with
credentials pulled from 1Password at runtime. It runs only when the caller sets
the repository variable `SCDE_DEPLOY_ENABLED=true` or passes `force: true`.
Inputs: `force`, `artifact`, `vault`, `stack_item`. Secret:
`op_service_account_token`.

## End to end

```yaml
name: Splunk App CI/CD

on:
  push:
    branches: ["**"]
    tags: ["v*.*.*"]
  pull_request:

permissions:
  pull-requests: write
  actions: write
  checks: write
  contents: write

jobs:
  package:
    runs-on: ubuntu-latest
    steps:
      - uses: livehybrid/deploy-splunk-app-action@v1
        with:
          python-version: "3.11"

  appinspect:
    needs: package
    uses: livehybrid/deploy-splunk-app-action/.github/workflows/appinspect-cli.yml@main
    with:
      tags: "cloud,future,private_victoria"
    secrets:
      token: "${{ secrets.GITHUB_TOKEN }}"

  appinspect-api:
    needs: appinspect
    # Opt in: set the repo variable APPINSPECT_API=true and add the secrets.
    if: vars.APPINSPECT_API == 'true'
    uses: livehybrid/deploy-splunk-app-action/.github/workflows/appinspect-api.yml@main
    secrets:
      splunkbase_username: ${{ secrets.SPLUNKBASE_USERNAME }}
      splunkbase_password: ${{ secrets.SPLUNKBASE_PASSWORD }}

  publish:
    needs: appinspect
    permissions:
      contents: write
    uses: livehybrid/deploy-splunk-app-action/.github/workflows/publish.yml@main

  publish-splunkbase:
    # Tag runs only, after the GitHub release. Off until the repo variable
    # SPLUNKBASE_PUBLISH_ENABLED=true; app id from the repo variable too.
    needs: publish
    permissions:
      contents: read
    uses: livehybrid/deploy-splunk-app-action/.github/workflows/publish-splunkbase.yml@main
    with:
      app_id: ${{ vars.SPLUNKBASE_APP_ID }}
      splunk_versions: "10.6,10.4,10.2,10.0,9.4"
    secrets:
      splunkbase_username: ${{ secrets.SPLUNKBASE_USERNAME }}
      splunkbase_password: ${{ secrets.SPLUNKBASE_PASSWORD }}
```

To publish a release that already exists on GitHub (or to rehearse with
`dry_run`), add a small dispatch workflow:

```yaml
name: Publish a release to Splunkbase
on:
  workflow_dispatch:
    inputs:
      tag: { description: "Existing release tag, e.g. v1.2.2", required: true }
      dry_run: { type: boolean, default: true }
jobs:
  publish:
    permissions:
      contents: read
    uses: livehybrid/deploy-splunk-app-action/.github/workflows/publish-splunkbase.yml@main
    with:
      app_id: ${{ vars.SPLUNKBASE_APP_ID }}
      splunk_versions: "10.6,10.4,10.2,10.0,9.4"
      release_tag: ${{ inputs.tag }}
      dry_run: ${{ inputs.dry_run }}
      force: true
    secrets:
      splunkbase_username: ${{ secrets.SPLUNKBASE_USERNAME }}
      splunkbase_password: ${{ secrets.SPLUNKBASE_PASSWORD }}
```

## Run the same checks locally

```bash
pip install splunk-add-on-ucc-framework splunk-appinspect
ucc-gen build --source package --ta-version 1.0.0
splunk-appinspect inspect output/<your-app> --included-tags cloud,future
```

Same tags as CI, so a local pass means a CI pass.

## Versioning

`@v1` tracks the v1 line, or pin a patch (`@v1.0.5`). The reusable workflows are
referenced `@main`.
