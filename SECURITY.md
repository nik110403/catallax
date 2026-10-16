# Security

Catallax is a single-user application meant for a trusted local network. This
repository is a public copy of a private project, updated in batches. There is no
dedicated security contact.

If you run it:

- Keep the default `127.0.0.1` bind unless you trust every machine on the network.
- The app speaks plain HTTP. `SHARE_PASSPHRASE` gates the API with a signed,
  HTTP-only cookie, but the passphrase and cookie cross the wire in the clear
  without TLS. Put a VPN, tunnel or TLS-terminating reverse proxy in front before
  exposing it beyond your machine.
- There is no live-money path; the trading desk is paper-only.
