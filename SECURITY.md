# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| 0.1.x | Yes (current) |
| < 0.1.0 | No |

---

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

### Preferred: GitHub Security Advisory

Use [GitHub's private vulnerability reporting](https://github.com/hinanohart/tab-conductor/security/advisories/new) to submit a security advisory directly to the maintainers. This keeps the report private until a fix is available.

### Alternative: Email

If the private advisory form is unavailable, use the contact details on the maintainer's GitHub profile. Do **not** include the vulnerability details in a public issue.

Include:
- Description of the vulnerability
- Steps to reproduce
- Affected version(s)
- Potential impact assessment
- (Optional) Suggested fix

---

## Disclosure Policy

tab-conductor follows a **90-day coordinated disclosure policy**:

1. You report the vulnerability privately.
2. Maintainers acknowledge receipt within 5 business days.
3. Maintainers investigate and develop a fix.
4. A patched release is published within 90 days of the initial report.
5. After the patch is available (or 90 days, whichever comes first), you are free to disclose publicly.

---

## Security Architecture Summary

tab-conductor implements a **4-layer security model**:

| Layer | Countermeasure |
|---|---|
| **Prompt level** | `--append-system-prompt` instructs workers not to output secrets or override supervisor |
| **Tool level** | `--disallowedTools` baseline blocks `sudo *`, `curl * \| sh`, `rm -rf /*`, and credential file reads |
| **State level** | JSON Schema 2020-12 strict validation; optional HMAC-SHA256 signing (`hmac_signing: true`) |
| **Network/Env level** | `SecretFilter` regex scrub before any write; filtered environment inheritance; bugreport redaction |

Full threat model: [docs/SECURITY_THREAT_MODEL.md](docs/SECURITY_THREAT_MODEL.md)

---

## Scope

The following are **in scope** for security reports:

- Secret leakage through state.json, log files, or bugreport packages
- State tampering enabling cost cap bypass or unauthorized worker spawning
- Prompt injection enabling workers to read or exfiltrate credential files
- Path traversal in `secret_filter.py` or `state.py` symlink handling
- HMAC bypass when `hmac_signing: true`

The following are **out of scope**:

- Vulnerabilities in the `claude` CLI itself (report to Anthropic)
- Vulnerabilities in the operating system or Python interpreter
- Social engineering attacks
