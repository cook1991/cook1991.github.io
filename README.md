# SpritzLive Apps

Landing pages and APK downloads for SpritzLive's Android apps, served at
<https://cook1991.github.io/>. Each app's page lives in its own folder, and its APK is
attached to a release tagged `<app>-v<version>`. App source stays in separate repos.

## Publishing a new version

Bump `versionCode` and `versionName` in the app's source repo, commit, then:

```
python scripts/publish.py <app>
```

It builds the signed APK, refuses one with the wrong package or signing key, creates the
release, updates the app's page, and pushes. Add `--dry-run` to check without publishing.
