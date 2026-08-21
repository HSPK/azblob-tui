# Security

Report security issues privately through GitHub's security advisory feature:

https://github.com/HSPK/azblob-tui/security/advisories/new

Do not include Azure credentials, SAS tokens, account keys, access tokens, or
private resource metadata in public issues.

`azblob-tui` obtains Azure Storage tokens through the authenticated Azure CLI
and keeps them only in memory. The application never writes credentials to its
configuration or account cache.
