# walter/firewall — host firewall

| file | installed as | what |
|---|---|---|
| `ufw-rules.sh.tmpl` | `/usr/local/sbin/ufw-rules.sh` (run by `walter/deploy.sh`) | ufw defaults and the INPUT allows |
| `docker-user-rules.sh.tmpl` + `.service` | `/usr/local/sbin/docker-user-rules.sh`, `docker-user-rules.service` | `WALTER-PUBLISHED` chain in DOCKER-USER (Docker-published ports) |

## SSH (22/tcp)

sshd listens on all addresses. ufw admits 22/tcp only from:

| source | rule | used by |
|---|---|---|
| the hypervisor bridge subnet | `allow in on ${BACKEND_LAN_IF} proto tcp from <subnet> to any port 22` | the workstation (`HYPERVISOR_BRIDGE_IP`): operator shells, judge probes and collector, key copies, tunnels |
| `EDGE_WG_IP` on wg0 | `allow in on wg0 proto tcp from ${EDGE_WG_IP} to ${BACKEND_WG_IP} port 22` | nothing at present; a break-glass path through Covenant if the bridge path breaks |

The subnet is not a site setting. `ufw-rules.sh` reads it from the kernel route of `BACKEND_LAN_IF`
(`ip -4 -o route show dev <if> scope link proto kernel`, for example `192.168.122.0/24`). It stops
**before changing any SSH rule** if there is no such route, or if the route does not contain
`HYPERVISOR_BRIDGE_IP`.

Rule order is what keeps this from locking you out. The script first adds both scoped allows, and
only then deletes the legacy any-source `allow 22/tcp` (v4 and v6), and only if that rule is still present.
Re-running it changes nothing. Connections that are already open survive a rule change in any case, because ufw
accepts ESTABLISHED traffic before the user rules.

IPv6: Walter has only link-local v6 addresses, so no v6 SSH allow is added. The legacy rule's
v6 twin is removed with it.

Why not `ListenAddress`: binding sshd to the LAN and wg0 addresses would make sshd depend on wg0
existing at start (a boot-order lockout risk) and on cloud-init's sshd drop-ins. The firewall
scope gives the same exposure with less risk.

Tests: `python3 -m pytest walter/firewall/tests -q -p no:cacheprovider`. They render the template
with `site.env.example` and with the checkout's `site.env` (or `SITE_ENV_REAL=<file>`), then run it
against fake `ufw` and `ip` commands.

## Deploying the SSH scope change safely

Run these from the workstation, in a repo checkout that has `site.env`. `W` is
`${BACKEND_SSH_USER}@${BACKEND_LAN_IP}`. Keep one SSH session to Walter open for the whole procedure.

1. **Pre-check.** The workstation's source address must be inside the subnet that will be allowed:
   ```sh
   ssh W 'echo "$SSH_CONNECTION" | cut -d" " -f1; ip -4 -o route show dev ${BACKEND_LAN_IF} scope link proto kernel'
   ssh W 'sudo ufw status numbered; sudo ufw show added'
   ```
2. **Render and copy.** If the digest is off (`SPARK_DIGEST_HOST` empty), strip its block the way
   `deploy.sh` does:
   ```sh
   scripts/render.sh walter/firewall/ufw-rules.sh.tmpl /tmp/ufw-rules.sh
   # digest off only: sed -i '/^# >>> digest/,/^# <<< digest/d' /tmp/ufw-rules.sh
   scp /tmp/ufw-rules.sh W:/tmp/ufw-rules.sh
   ```
3. **Arm the rollback timer.** In 10 minutes it re-adds the any-source rule unless you cancel it:
   ```sh
   ssh W 'sudo systemd-run --unit=ssh-scope-rollback --on-active=10min /usr/sbin/ufw allow 22/tcp'
   ssh W 'systemctl list-timers ssh-scope-rollback.timer --no-pager'
   ```
4. **Back up and apply:**
   ```sh
   ssh W 'sudo cp -a /usr/local/sbin/ufw-rules.sh /usr/local/sbin/ufw-rules.sh.pre-ssh-scope &&
          sudo install -m 0755 -o root -g root /tmp/ufw-rules.sh /usr/local/sbin/ufw-rules.sh &&
          sudo /usr/local/sbin/ufw-rules.sh'
   ```
   (`walter/deploy.sh` does the same install and run. If you deploy with it, arm the timer first.)
5. **Verify with a new connection.** Don't reuse the open session or a ControlMaster:
   ```sh
   ssh -o ControlPath=none -o ConnectTimeout=10 W 'echo new-session-ok; sudo ufw status numbered'
   ```
   Expect two `22/tcp` rules: one `on <if>` from the subnet and one `on wg0` from `EDGE_WG_IP`.
   There should be no `22/tcp ALLOW IN Anywhere` and no `(v6)` 22 rule. If the judge is installed,
   one of its Walter probes should also succeed.
6. **Cancel the timer** only after step 5 succeeds:
   ```sh
   ssh -o ControlPath=none W 'sudo systemctl stop ssh-scope-rollback.timer; systemctl list-timers ssh-scope-rollback.timer --no-pager'
   ```

**Rollback.** If you are locked out, wait for the timer: within 10 minutes it re-adds `allow 22/tcp`.
From any working session, roll back by hand with:
```sh
sudo ufw allow 22/tcp
sudo install -m 0755 /usr/local/sbin/ufw-rules.sh.pre-ssh-scope /usr/local/sbin/ufw-rules.sh
```
To make the rollback permanent, also revert the change in the repo, because the next `deploy.sh`
installs the scoped script again. The hypervisor console (`virsh console`) works with no network at all.
