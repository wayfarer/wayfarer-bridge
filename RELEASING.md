# Releasing `wfb` to PyPI

This document is the maintainer checklist for publishing the [`wfb`](https://pypi.org/project/wfb/) package. It assumes a **manual** upload from your machine using [Twine](https://twine.readthedocs.io/). Do not commit API tokens or passwords; configure credentials with **`~/.pypirc`**, the system keychain, or environment variables as described in the Twine and PyPI documentation.

## Before you start

- Run the test suite: `python3 -m unittest tests.test_wfb tests.test_wfb_chrome_bridge`.
- Decide the new version string (semantic versioning as you prefer: patch / minor / major).
- Confirm the version is **not** already on [PyPI](https://pypi.org/project/wfb/#history) (PyPI rejects duplicate filenames).

## Steps

1. **Bump the version** in [`pyproject.toml`](pyproject.toml) under `[project]` → `version = "X.Y.Z"`. Commit that change on `main` (or your release branch) and push when you are ready.

2. **Create an annotated git tag** that matches the package version, using the usual `v` prefix:

   ```sh
   git tag -a vX.Y.Z -m "wfb X.Y.Z"
   ```

3. **Build** clean artifacts from the repository root:

   ```sh
   rm -rf dist/
   python3 -m pip install --upgrade build
   python3 -m build
   ```

   You should get `dist/wfb-X.Y.Z.tar.gz` and `dist/wfb-X.Y.Z-py3-none-any.whl`.

4. **Validate** the distributions:

   ```sh
   python3 -m pip install --upgrade twine
   python3 -m twine check dist/*
   ```

5. **Upload** to PyPI:

   ```sh
   python3 -m twine upload dist/*
   ```

6. **Push commits and the tag** so Git history matches what was published:

   ```sh
   git push origin main
   git push origin vX.Y.Z
   ```

   Adjust `main` if your default branch differs.

## Optional: TestPyPI

Dry-run against TestPyPI first if you want a smoke test (separate token and index URL; see Twine’s `--repository` / `testpypi` docs).

## If something goes wrong

- If the upload **succeeds** but you forgot to tag or push, add the tag to the published commit and push the tag; do **not** re-upload the same version.
- If the upload **fails** after partially completing, fix the issue and retry; if a given version already exists on PyPI, you must **bump** `version` in `pyproject.toml` and repeat the process with a new number.
