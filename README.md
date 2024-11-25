# Splunk App CI/CD GitHub Action

A reusable GitHub Action for automating the packaging, testing, and publishing of Splunk apps.

## Inputs

| Name                 | Description                                        | Required | Default                     |
|----------------------|----------------------------------------------------|----------|-----------------------------|
| `python-version`     | Python version to use in the environment.          | No       | `3.9`                       |
| `included-tags`      | Comma-separated tags for AppInspect scanning.      | Yes      | `cloud,private_victoria,future` |
| `splunkbase-username`| Splunkbase username for AppInspect API.            | Yes      | -                           |
| `splunkbase-password`| Splunkbase password for AppInspect API.            | Yes      | -                           |
| `github-token`| Splunkbase password for AppInspect API.            | Yes      | -                           |

## Outputs

| Name            | Description                                      |
|-----------------|--------------------------------------------------|
| `artifact-path` | Path to the packaged Splunk app artifacts.       |
| `reports-path`  | Path to quality reports generated during testing.|

## Example Usage

```yaml
jobs:
  ci:
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
      actions: write
      checks: write
    steps:
      - name: Run Splunk App CI/CD
        uses: livehybrid/splunk-app-cicd-action@main
        with:
          python-version: "3.9"
          included-tags: "cloud,private_victoria,future"
          splunkbase-username: ${{ secrets.SPLUNKBASE_USERNAME }}
          splunkbase-password: ${{ secrets.SPLUNKBASE_PASSWORD }}
          splunkbase-password: ${{ secrets.GITHUB_TOKEN }}
```
