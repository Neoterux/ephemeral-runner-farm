---
name: Bug report
about: Something isn't working
title: ''
labels: bug
assignees: ''
---

**What happened**

**What you expected**

**Repro steps**

**Environment**
- Host OS / version:
- podman version:
- Python version on the host (`cat /etc/sp-runner-python`):
- Single host or multi-host:

**Logs**
<!-- agent:   sudo -u ghrunner XDG_RUNTIME_DIR=/run/user/$(id -u ghrunner) journalctl --user -u sp-runner-agent -n 100
     manager: same, unit sp-runner-manager
     a slot:  ... journalctl --user -u 'sp-runner@1.service' -n 100
     Redact tokens / fingerprints / hostnames as needed. -->
```
```
