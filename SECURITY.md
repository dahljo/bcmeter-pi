# bcMeter DIY Security

This repository is the curated public Raspberry Pi distribution. It contains
the code needed to operate and update a DIY bcMeter, but no private production,
QC, lab-control, cloud-provisioning, deployment, or credential-recovery tools.

## Device access model

- Measurement status, masked configuration, event summaries, CSV logs, and log
  listings are readable without a password.
- Start/stop, calibration, normal configuration, log deletion, device rename,
  and release OTA actions are passwordless on the device AP/operator LAN. These
  writes require a same-origin request and an explicit bcMeter client marker to
  prevent ordinary cross-site browser requests.
- Stored WiFi and service credentials are never returned by the API. Secret
  configuration values are masked rather than serialized to the browser, and
  the credential-bearing JSON files are written atomically with mode `0600`.
- The public API has no arbitrary archive-upload, raw lab-control, QC, shell, or
  remote command endpoint.
- OTA accepts only the configured `dahljo/bcmeter-pi` GitHub Release asset. A
  release-provided SHA-256 digest is mandatory and archive members are checked
  for path traversal, links, and unsupported member types before installation.
  This relies on HTTPS and control of the GitHub release account; SHA-256 alone
  is an integrity check, not a separate publisher signature.

The local device network is the operational trust boundary. Do not expose the
HTTP service directly to the public internet. Report suspected vulnerabilities
to jd@bcmeter.org with the version, image date, hardware model, network exposure,
and reproduction steps.
