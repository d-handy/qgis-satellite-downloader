# Documentation assets

Material for the README, the GitHub page, plugins.qgis.org and talks or posters. User
documentation itself lives in the [main README](../README.md).

```
docs/
  assets/
    logo/         hex logos (SVG)
    screenshots/  screenshots of the plugin for the README and plugins.qgis.org
```

## Logos

| File | Use |
|---|---|
| `qgis-satellite-downloader-logo.svg` | The full hex logo: README header, website, posters, slides |
| `qgis-satellite-downloader-icon.svg` | Simplified hex for small sizes (64 px and below), such as icons and avatars |

The README shows the logo the way R packages do, floated right next to the title:

```html
# qgis-satellite-downloader <img src="docs/assets/logo/qgis-satellite-downloader-logo.svg" align="right" height="139" alt="QGIS Satellite Downloader logo" />
```

### Colours

| Colour | Hex | Used for |
|---|---|---|
| Navy | `#1E3550` | Hex background, panel dividers |
| Blue | `#306998` | Earth |
| Light blue | `#5B9BD5` | Solar panels, globe grid |
| Yellow | `#FFD43B` | Hex border, satellite body, download arrow, footprint |
| Off-white | `#EAF0F6` | Panel boom, antenna |

### Rendering note

Both logos clip the globe to the hexagon with `clip-path`. Browsers, GitHub and vector tools
render that correctly; Qt's SVG renderer does not and draws the globe outside the hexagon.
That matters wherever QGIS itself draws the file (plugin icon, menus), and for PNGs exported
through Qt. Export PNGs from a browser or a vector editor (Inkscape) instead.

## Screenshots

Put screenshots in `assets/screenshots/` as PNG, named after what they show
(`simple-mode.png`, `expert-mode-results.png`). Take them in English with a default QGIS
theme and crop them to the plugin window. Avoid places or data that identify a person.
