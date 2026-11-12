# Security

## Reporting a vulnerability

Please do not open a public issue. Report the problem privately through GitHub: on the
repository page, open *Security → Report a vulnerability*. If that is not available, write
to dhandy1@uni-koeln.de.

Include the plugin version, what an attacker could do, and the steps to reproduce it.

## Scope

The plugin reads responses from public STAC catalogues and Nominatim and turns them into
file names, web requests and map layers. Problems where a manipulated catalogue or server
response makes the plugin read or write outside its download folder, fetch non-web
addresses, or run code are in scope.

## Supported versions

Only the latest release receives fixes.
