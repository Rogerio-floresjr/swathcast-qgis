# Releasing

## A new version

1. Bump `version=` and add a `changelog=` entry in `swathcast_qgis/metadata.txt`. Bump `version`
   in `CITATION.cff` to match.
2. Run the tests on every supported QGIS:

   ```
   scripts/test.sh
   scripts/test.sh qgis-ltr
   scripts/test.sh qgis-334
   ```

   If a UI string changed, also run `scripts/i18n.sh`.
3. If the interface changed, run `scripts/screenshots.sh` and `scripts/screenshots.sh pt_BR` to
   refresh the README figures.
4. Build the ZIP:

   ```
   scripts/package.sh
   ```

   This writes `dist/swathcast_qgis-<version>.zip`. `LICENSE` goes inside the plugin folder of the
   ZIP, because plugins.qgis.org requires it.
5. Commit, tag and publish:

   ```
   git tag -a v<version> -m "SwathCast for QGIS <version>"
   git push origin main v<version>
   gh release create v<version> dist/swathcast_qgis-<version>.zip \
       --title "SwathCast for QGIS <version>" --notes "<the changelog entry>"
   ```

6. Upload the same ZIP at https://plugins.qgis.org (*My plugins › Upload*). This needs an OSGeo
   account, and a staff member approves the first version.

## plugins.qgis.org checklist

- **Licence:** compatible with GPLv2 or later. This plugin uses GPL-2.0-or-later, and `LICENSE` is
  in the ZIP.
- **`metadata.txt`:**
  - `name`, `description`, `about`, `version`, `qgisMinimumVersion`, `author` and `email`;
  - `homepage`, `repository` and `tracker`, pointing to public pages;
  - `experimental=True` until the first stable version.
- **Size:** the ZIP must stay well under the site's limit. `scripts/package.sh` leaves out tests,
  docs, `.ts` sources and caches.
- **Contents:** no compiled binaries and no third-party libraries.
