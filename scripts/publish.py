#!/usr/bin/env python3
"""Publish a new version of one app to this site.

    python scripts/publish.py <app> [--apk FILE] [--dry-run]

Builds the app's signed release APK from its source repo, checks it, attaches it to
a new release tagged <app>-v<version>, updates the app's page and store.json, and
pushes. Bump versionCode and versionName in the source repo first; the version comes
from the APK.

SpritzStore reads store.json, so it picks up the new version on its own. --apk skips
the build and uses an existing file. --dry-run does the checks and reports the page
changes without publishing anything.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent
WEBSITE = SITE.parent
REPO = "cook1991/cook1991.github.io"
JBR = "C:/Program Files/Android/Android Studio/jbr"
SDK = Path(os.environ.get("ANDROID_HOME") or Path(os.environ["LOCALAPPDATA"]) / "Android/Sdk")

# `signer` pins each app's certificate: a release signed with anything else couldn't
# update existing installs, so it must never go out. `page` holds the text on each
# app's page that tracks the release, as (pattern, replacement) pairs. Apps with no
# `page` are installed through SpritzStore only.
APPS = {
    "spritzstore": dict(
        source="spritzstore", package="net.spritzlive.spritzstore",
        signer="8988fb4882e9300014f2bb0971f3c78fa61208abdbd29fa18e438b45e7c36141",
        apk="SpritzStore-{version}.apk",
    ),
    "identifysong": dict(
        source="identifysongapp", package="net.spritzlive.identifysong",
        signer="304145a77ef397452b27a64426b214488cf0b5cd0bb3ed5393f545e806a513f7",
        apk="IdentifySong-{version}.apk",
    ),
    "securemessenger": dict(
        source="securemessenger", package="net.spritzlive.securemessenger",
        signer="bfb3c0ccd636a7f7d5a1a8240ec96999f8a707fe794e61d8d089aff941466dfd",
        apk="SecureMessenger-{version}.apk",
    ),
    "messages": dict(
        source="smsmmsapp", package="net.spritzlive.smsmms",
        signer="7ebff1da4059ae4f82cff427b9d1859064dc98bda113dadaca31925754b160dd",
        apk="SpritzLive-Messages-{version}.apk", apk_pattern=r"SpritzLive-Messages-[\d.]+\.apk",
        page=[(r"Version [\d.]+ (&middot; Android [\d.]+\+ &middot; ~)\d+(&nbsp;MB)", r"Version {version} \g<1>{mb}\g<2>")],
    ),
    "authvault": dict(
        source="authapp", package="net.spritzlive.authvault",
        signer="1baa3b61f495a4bf545b2939ab65a246b58d03e8af86af85b209ec687c172401",
        apk="AuthVault-v{version}.apk", apk_pattern=r"AuthVault-v[\d.]+\.apk",
        page=[(r"Download APK \(v[\d.]+\)", "Download APK (v{version})"),
              (r"SHA-256: [0-9a-f]{{64}}", "SHA-256: {sha}")],
    ),
    "drb": dict(
        source="bible", package="org.drbo.bible",
        signer="3e7f7172d9b2cf1cb1d75bee824e069d725bbc587fa08b6f7a044950d35fd337",
        apk="douay-rheims-bible-v{version}.apk", apk_pattern=r"douay-rheims-bible-v[\d.]+\.apk",
        page=[(r"about \d+&nbsp;MB", "about {mb}&nbsp;MB")],
    ),
    "skycast": dict(
        source="weatherapp", package="com.spritzlive.weather",
        signer="fe15a5f47945696124b27761a3fdcfbc9af8db652d366ffd3feac21d0ebc8d24",
        apk="SkyCast-v{version}.apk", apk_pattern=r"SkyCast-v[\d.]+\.apk",
        page=[(r"Download APK \(v[\d.]+\)", "Download APK (v{version})")],
    ),
    "odotrack": dict(
        source="speedometer", package="com.spritzlive.speedometer",
        signer="d80c0ac4597de213da0559e9a80117311835742b3850df0db4cf7b228c50e498",
        apk="odotrack-{version}.apk", apk_pattern=r"odotrack-[\d.]+\.apk",
        page=[(r"Version [\d.]+ &middot; ~\d+ MB", "Version {version} &middot; ~{mb} MB"),
              (r"SHA-256: [0-9a-f]{{64}}", "SHA-256: {sha}")],
    ),
}

CREDENTIALS = re.compile(
    rb"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{30,}"
    rb"|AIza[0-9A-Za-z_-]{35}|AKIA[0-9A-Z]{16}|xox[baprs]-[0-9A-Za-z-]{20,}"
    rb"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


def fail(message):
    sys.exit(f"publish: {message}")


def run(*args, cwd=None, env=None):
    done = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        fail(f"{' '.join(map(str, args))} failed:\n{done.stdout}{done.stderr}")
    return done.stdout


def build_tool(name):
    versions = sorted((SDK / "build-tools").iterdir(), key=lambda p: [int(n) for n in re.findall(r"\d+", p.name)])
    for suffix in (".exe", ".bat", ""):
        tool = versions[-1] / f"{name}{suffix}"
        if tool.exists():
            return str(tool)
    fail(f"{name} not found in {versions[-1]}")


def build_apk(source):
    env = dict(os.environ)
    env.setdefault("JAVA_HOME", JBR)
    gradlew = source / ("gradlew.bat" if os.name == "nt" else "gradlew")
    print(f"Building {source.name}...")
    run(str(gradlew), ":app:assembleRelease", cwd=source, env=env)
    return source / "app/build/outputs/apk/release/app-release.apk"


def check_apk(apk, app):
    """Returns the APK's version name and code after refusing anything unsafe to publish."""
    badging = run(build_tool("aapt2"), "dump", "badging", str(apk))
    package = re.search(r"package: name='([^']+)' versionCode='(\d+)' versionName='([^']+)'", badging)
    if not package:
        fail(f"couldn't read the package info from {apk}")
    if package[1] != app["package"]:
        fail(f"{apk} is {package[1]}, expected {app['package']}")

    certs = run(build_tool("apksigner"), "verify", "--print-certs", str(apk))
    signer = re.search(r"Signer #1 certificate SHA-256 digest: ([0-9a-f]{64})", certs)
    if not signer or signer[1] != app["signer"]:
        fail(f"{apk} isn't signed with this app's release key (got {signer and signer[1]})")

    with zipfile.ZipFile(apk) as contents:
        for name in contents.namelist():
            hit = CREDENTIALS.search(contents.read(name))
            if hit:
                fail(f"{name} in {apk} contains what looks like a credential ({hit[0][:6].decode(errors='replace')}...)")
    return package[3], int(package[2])


def update_page(html, slug, app, version, tag, filename, sha, mb):
    html, links = re.subn(
        rf"releases/download/{slug}-v[^/\"]+/{app['apk_pattern']}", f"releases/download/{tag}/{filename}", html
    )
    if not links:
        fail(f"{slug}: no download link found on the page")
    html = re.sub(app["apk_pattern"], filename, html)
    for pattern, new in app["page"]:
        html, count = re.subn(pattern.format(), new.format(version=version, sha=sha, mb=mb), html)
        if not count:
            fail(f"{slug}: page text not found: {pattern}")
    return html


def main():
    parser = argparse.ArgumentParser(description="Publish a new version of one app to this site.")
    parser.add_argument("app", choices=APPS)
    parser.add_argument("--apk", type=Path, help="use this APK instead of building one")
    parser.add_argument("--dry-run", action="store_true", help="check everything, publish nothing")
    args = parser.parse_args()

    slug, app = args.app, APPS[args.app]
    source = WEBSITE / app["source"]

    if not args.dry_run:
        if run("git", "status", "--porcelain", cwd=SITE).strip():
            fail(f"{SITE} has uncommitted changes")
        run("git", "pull", "--ff-only", "--quiet", cwd=SITE)

    apk = args.apk or build_apk(source)
    version, version_code = check_apk(apk, app)
    tag = f"{slug}-v{version}"
    filename = app["apk"].format(version=version)
    data = apk.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    mb = round(len(data) / 1_000_000)

    exists = subprocess.run(["gh", "release", "view", tag, "-R", REPO], capture_output=True).returncode == 0
    if exists and not args.dry_run:
        fail(f"{tag} is already published -- bump the version in {source}")

    manifest_path = SITE / "store.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    current = manifest["apps"].get(slug)
    if current and version_code <= current["versionCode"] and not args.dry_run:
        fail(f"versionCode {version_code} isn't newer than the store's {current['versionCode']}")

    page = SITE / slug / "index.html"
    before = after = None
    if "page" in app:
        with open(page, encoding="utf-8", newline="") as f:
            before = f.read()
        after = update_page(before, slug, app, version, tag, filename, sha, mb)

    print(f"{tag}: {filename}, {mb} MB, sha256 {sha}")
    if args.dry_run:
        changed = sum(a != b for a, b in zip(before.splitlines(), after.splitlines())) if before else 0
        print(f"Dry run: {changed} page line(s) would change. Nothing published.")
        return

    # The release goes first so the page never links to a file that isn't there yet.
    with tempfile.TemporaryDirectory() as tmp:
        named = Path(tmp) / filename
        shutil.copyfile(apk, named)
        run("gh", "release", "create", tag, str(named), "-R", REPO,
            "--title", f"{slug} {version}", "--notes", f"{slug} for Android, version {version}.")

    manifest["apps"][slug] = {
        "versionCode": version_code,
        "versionName": version,
        "url": f"https://github.com/{REPO}/releases/download/{tag}/{filename}",
        "size": len(data),
        "sha256": sha,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    run("git", "add", str(manifest_path), cwd=SITE)
    if after is not None:
        with open(page, "w", encoding="utf-8", newline="") as f:
            f.write(after)
        run("git", "add", str(page), cwd=SITE)
    run("git", "commit", "--quiet", "-m", f"Publish {slug} {version}", cwd=SITE)
    run("git", "push", "--quiet", "origin", "main", cwd=SITE)
    print(f"Published {tag}. SpritzStore and the site update within a few minutes.")


if __name__ == "__main__":
    main()
