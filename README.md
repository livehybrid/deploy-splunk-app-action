# Splunk App CI/CD

Reusable GitHub Actions building blocks for packaging, vetting and publishing
Splunk apps and add-ons. One place to change a gate, every add-on gets it.

This repo gives you two things:

1. **A composite action** (`action.yml`) that builds and packages a UCC add-on.
2. **Reusable workflows** (`.github/workflows/`) for AppInspect and publishing.

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
