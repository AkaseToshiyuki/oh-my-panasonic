# Security and privacy

Oh My Panasonic contains privileged system helpers, optional kernel modules and opt-in PAM integration. Model-specific EC commands are allowlisted and bounded by restoration and thermal safeguards. Do not bypass these checks on unverified hardware.

Face authentication is optional and local. GDM login requires a matching foreground greeter, selected account and root GDM worker before and after capture. Lock-screen authentication is tied to the locked account. Desktop and local sudo authorization require active confirmation; remote SSH is excluded. Existing site-specific authentication policies must not be bypassed.

Keep a working password authentication method. Face matching does not recover a password or unlock a password-encrypted login keyring. Biometric accuracy and resistance to spoofing have not been independently certified.

The project does not upload biometric templates or battery history. Wi-Fi credentials go directly to NetworkManager, not process arguments or application logs. Models are installed separately and carry their own licensing terms.

Report vulnerabilities through the hosting platform's private vulnerability reporting facility. If that facility is unavailable, ask the maintainer for a private contact without posting exploit details. Include the affected version, component, reproduction conditions and impact. Remove credentials, face data, hostnames, network identifiers and device serial numbers.
