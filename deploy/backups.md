# Database backups

`backup.sh` runs weekly on the VM as the `illusion` user and keeps two copies.

The local copy is the one you will actually use. The laptop copy exists because a VM backup sitting on the same physical machine as the live database is not a backup against hardware failure.

## Setup

Edit `KIOSK_DEST` in the script if the hostname or path differ, then on the VM:

```sh
apk add sqlite rsync
install -m 755 backup.sh /home/illusion/backup.sh
mkdir -p /home/easternedge/illusion-backups        # on the LAPTOP; rsync will not create it
```

The job runs as `illusion`, so it needs `illusion`'s own key on the laptop:

```sh
doas -u illusion mkdir -p /home/illusion/.ssh
doas -u illusion chmod 700 /home/illusion/.ssh
doas -u illusion ssh-keygen -t ed25519 -N "" -f /home/illusion/.ssh/id_ed25519 -C "illusion-backup"
doas su - illusion -c 'ssh-copy-id -i ~/.ssh/id_ed25519.pub peyton@eer-inventory'
```

`-N ""` because cron cannot type a passphrase, and `su - illusion` because that sets `HOME`, so the accepted host key lands in `illusion`'s `known_hosts`. Get that wrong and the job fails forever with `Host key verification failed`, since cron has no terminal to answer the prompt on.

Schedule it:

```sh
rc-update add crond default && rc-service crond start
doas crontab -u illusion -e     # 0 3 * * 0 /home/illusion/backup.sh
```

## Verify

```sh
doas su - illusion -c 'ssh -o BatchMode=yes easternedge@eer-inventory true && echo OK'
doas -u illusion /home/illusion/backup.sh && cat /home/illusion/logs/backup.log
```

`BatchMode=yes` disables every prompt, which is what cron effectively runs with. The log should say `copied to kiosk`, not `FAILED`.

To prove cron itself works without waiting a week, set the schedule a couple of minutes ahead, watch `tail -f /home/illusion/logs/backup.log`, then put `0 3 * * 0` back. Worth doing once: cron runs with a nearly empty environment, and that is the usual reason something works by hand and not on a schedule.

## Restore

```sh
rc-service claws stop
cp /home/illusion/backups/inventory-YYYY-MM-DD.db /home/illusion/inventory.db
rc-service claws start
```

Stop claws first. Copying over a database it has open will corrupt it.

Check a backup any time without restoring:

```sh
sqlite3 /home/illusion/backups/inventory-YYYY-MM-DD.db "SELECT count(*) FROM items;"
```
