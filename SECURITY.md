# Security policy

Please report vulnerabilities privately through GitHub Security Advisories.
Do not open a public issue containing access tokens, OAuth data, Home Assistant
entity data, audio recordings, or bridge logs with personal content.

The bridge is designed to keep ChatGPT/Codex OAuth credentials in Codex's own
credential store. Home Assistant receives only a separate bridge bearer token.
Bind the bridge to a trusted interface, use a firewall, and rotate its token if
it is disclosed. TLS termination is required when traffic crosses an untrusted
network.
