# Publication privacy

Sentinel's current project credits use **Sentinel contributors**. Original work
remains GPL-3.0-only; the public author credit does not remove third-party notices
or change their licenses. Account-specific paths in templates and tests use
neutral placeholders, while installed assets use their packaged system paths.

## Public identity and contact

Python author metadata omits a personal email. Debian maintainer fields and
changelog trailers use the maintainer's GitHub-provided public no-reply address:
`283042319+Throckmorton05@users.noreply.github.com`. It is not a project support
inbox; use the repository's Issues page for bug reports. This address still
identifies the GitHub account, while keeping its private mailbox out of artifacts.

If a public project domain/contact is adopted later, update `debian/control`,
changelog trailer addresses, and the generated Maintainer field in
`packaging/iso/platform_package.py`. Use the chosen public identity for future
changelog entries as well.

The configured GitHub owner, repository URL, and signed APT channel remain public
identifiers. Removing real names from files does not unlink an existing GitHub
profile. A no-reply email hides the private mailbox but can still identify its
GitHub account. A dedicated pseudonymous account/project identity can reduce
that link. Changing the repository URL also requires changing and verifying the
public update configuration before rebuilding/releasing packages.

For future commits, use repository-local identity settings with values you have
chosen, rather than changing every other project's identity:

```bash
git config user.name 'YOUR_PUBLIC_PSEUDONYM'
git config user.email 'YOUR_EXACT_GITHUB_NOREPLY_OR_PUBLIC_PROJECT_EMAIL'
```

Copy the actual no-reply address from GitHub settings; do not infer its account
ID or assume that every no-reply format belongs to your account. Review profile,
commit/signature identities, release uploader attribution, and public contact
details as part of deciding how pseudonymous publication should be.

## Private extension-signing inputs

The original extension keys live outside the checkout. The default private
directory is `~/.local/share/sentinel-extension-keys/`, overridable with the
absolute external `SENTINEL_EXTENSION_KEY_DIR` environment variable:

| Component | Private filename |
| --- | --- |
| Activity | `activity.pem` |
| Safe Steam | `safe_steam.pem` |
| Safe Crunchyroll | `safe_crunchyroll.pem` |
| Extension Catalog | `extension_catalog.pem` |
| New Tab | `newtab.pem` |

The directory should have mode 0700 and keys mode 0600. Builders reject private
keys inside the project or keys with group/world access. Catalog/New Tab builds
refuse to regenerate a missing key when a public identity already exists.
Changing a key changes the extension identity; retain the originals privately.
Ordinary users/contributors can install the existing public CRX/XML assets
without possessing these private inputs.

For a legacy checkout whose keys still sit in `browser_distribution/`, run as
the normal developer account:

```bash
python3 tools/relocate_extension_keys.py --untrack
```

The helper checks collisions, copies private bytes to the external directory,
verifies the copy, and only then removes the source copy. `--untrack` removes
only those known legacy key paths from the current Git index. It does not alter
past commits, tags, other clones, or any remote. A different existing private
backup causes an error instead of overwriting either key.

Private release GPG keys also remain outside the checkout. The public archive
verification `.gpg` key is intended for distribution. Review its public UID
information before release. The current `.gitignore` protects key and secret
file patterns, but ignored files already tracked in Git still require removal
from the index/history.

## Current files versus history and old artifacts

Editing files and configuring future Git identity does not erase author/committer
names, emails, old file contents, signed-tag identities, or secrets from old
commits. A source archive without `.git` cannot establish that original history
is clean. Choose either a cleaned, new public history while keeping the original
history private, or a reviewed rewrite of every relevant branch/tag/ref.
Neither cleanup removes copies already cloned, downloaded, or cached elsewhere.

Do not make a privacy-cleanup commit and assume the original public history is
safe. Inspect it on the original checkout before publication. History replacement
and force pushes should be a separate deliberate operation after backups/review.

Rebuild DEBs, wheels, `.changes`, `.buildinfo`, and the ISO from the cleaned
source. Previously built artifacts can contain the old maintainer/author metadata.
Do not replace bytes under an already published package version; use a new
revision when replacing an official release. Exclude private archives, patch
bundles, local logs, activity, credentials, editor state, and signing directories
from public release assets.

The source text and screenshot metadata/OCR checks help find known identifiers;
they are not a guarantee of complete anonymity. Third-party author names and
legitimate domain-list entries are not personal project attribution and should
keep their existing notices/content.
