# Publishing

GitHub releases are created automatically when a `v*` tag is pushed.

## PyPI trusted publishing

The release workflow is prepared for PyPI OIDC trusted publishing. Before
enabling it, create a pending publisher on PyPI with these exact values:

- PyPI project: `azblob-tui`
- GitHub owner: `HSPK`
- GitHub repository: `azblob-tui`
- Workflow: `release.yml`
- Environment: `pypi`

Then enable the repository variable:

```bash
gh variable set PYPI_PUBLISH --repo HSPK/azblob-tui --body true
```

The next `v*` tag publishes the wheel and source distribution without storing
a PyPI token in GitHub.

## Release checklist

1. Update the version in `pyproject.toml` and `src/azure_blob_tui/__init__.py`.
2. Update `CHANGELOG.md`.
3. Run:

   ```bash
   python -m unittest discover -s tests -v
   uv build
   uvx --from twine twine check dist/*
   ```

4. Commit and push `main`.
5. Tag and push:

   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```
