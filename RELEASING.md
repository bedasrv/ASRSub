# Releasing ASRSub

Git tags are the single source of truth for ASRSub versions. Do not maintain a
second version number in application code or in the Dockerfile.

## Release flow

1. Merge the release changes into `main`.
2. Pull the exact remote tip:

   ```bash
   git switch main
   git pull --ff-only origin main
   ```

3. Create an annotated SemVer tag. Stable releases use `vMAJOR.MINOR.PATCH`;
   prereleases may use `-rc.1`, `-beta.1`, etc.:

   ```bash
   git tag -a v1.0.0 -m "Release v1.0.0"
   git push origin v1.0.0
   ```

The tag workflow rejects malformed versions and tags that do not point to a
commit contained in `main`. It then runs the complete CI suite, builds the
image, publishes the GHCR package, and creates a GitHub release with generated
notes. A release tag is never allowed to bypass the test job.

## Image tags

The package is published as `ghcr.io/bedasrv/asrsub`.

| Source | Published tags | Intended use |
|---|---|---|
| Pull request | none | Build-only validation; no registry writes |
| `main` push | `latest`, `main`, `sha-<short>` | Development/current main |
| `v1.2.3` | `v1.2.3`, `1.2.3`, `1.2`, `1`, `latest`, `sha-<short>` | Stable release |
| `v1.2.3-rc.1` | prerelease version tags and `sha-<short>` | Release candidate; does not move `latest` |

Deploy production with a full version or commit tag, not `latest`:

```bash
docker login ghcr.io
ASRSUB_IMAGE=ghcr.io/bedasrv/asrsub ASRSUB_VERSION=1.2.3 docker compose pull
ASRSUB_IMAGE=ghcr.io/bedasrv/asrsub ASRSUB_VERSION=1.2.3 docker compose up -d
```

`ASRSUB_VERSION` also accepts `v1.2.3` and `sha-<short>`. The Compose file
defaults to the existing local `asrsub:latest` image, so local development is
unchanged.

## CI/CD behavior

- Pull requests run Python compilation, application imports, deterministic dry
  tests, and a Docker build. They never publish packages.
- Pushes to `main` run the same checks and publish `latest`, `main`, and an
  immutable short-SHA image.
- SemVer tag pushes publish versioned images and create the GitHub release.
- Docker Buildx publishes OCI provenance and an SBOM with registry images.
- The GHCR package is created automatically by the first successful publishing
  run. Set its visibility and retention policy in the repository's Packages
  settings according to deployment policy.
