# Installing and running LiveMonitor

These instructions are for the Ubuntu VM. They use the project folder
`/home/ubuntu/livemonitor-server` and run the service as the `ubuntu` user.

## Fresh install

1. Log in to the VM as `ubuntu`, then copy and run these commands. Replace the
   repository URL only if your copy is hosted somewhere else.

   ```bash
   git clone https://github.com/lucifure/livemonitor-server.git /home/ubuntu/livemonitor-server
   cd /home/ubuntu/livemonitor-server
   cp config.example.json config.json
   nano config.json
   bash deploy/setup.sh
   ```

2. In `nano`, replace `CHANGE_ME` with a long private API token. Set the
   channels, `api_bind`, and recording folder for this VM. Save with `Ctrl+O`,
   press Enter, then exit with `Ctrl+X`.

`setup.sh` deliberately stops if `config.json` is missing. It installs Ubuntu
packages, creates `.venv`, installs the pinned Python packages, and starts the
`livemonitor` service.

## Update after a GitHub merge

Run these commands on the VM after a merge:

```bash
cd /home/ubuntu/livemonitor-server
git pull
.venv/bin/python -m pip install -r requirements.txt
sudo systemctl restart livemonitor
sudo systemctl status livemonitor --no-pager
```

If a yt-dlp-only update is needed between normal releases, run:

```bash
cd /home/ubuntu/livemonitor-server
bash deploy/update-ytdlp.sh
```

Use `bash deploy/diagnose-download.sh` to compare direct and WARP downloads for
the configured YouTube channels; add `--wait` to wait for a live stream.

## Check health

Check whether the service is running:

```bash
sudo systemctl status livemonitor --no-pager
```

Check the API without putting the token in your shell history. Paste the token
when prompted, then press Enter:

```bash
read -r -s -p 'API token: ' TOKEN; echo
curl --fail --silent --show-error -H "Authorization: Bearer $TOKEN" http://YOUR_TAILSCALE_IP:8787/status
unset TOKEN
```

Replace `YOUR_TAILSCALE_IP:8787` with the `api_bind` and `api_port` values from
`config.json`. A successful request returns JSON containing `version`,
`channels`, and `disk`.

## Read logs

Show recent messages:

```bash
journalctl -u livemonitor -n 100 --no-pager
```

Follow new messages live (press `Ctrl+C` to stop):

```bash
journalctl -u livemonitor -f
```

## Roll back a bad update

First view the recent commits and choose the last known-good commit ID:

```bash
cd /home/ubuntu/livemonitor-server
git log --oneline -10
```

Then replace `GOOD_COMMIT` below with that ID:

```bash
git reset --hard GOOD_COMMIT
.venv/bin/python -m pip install -r requirements.txt
sudo systemctl restart livemonitor
sudo systemctl status livemonitor --no-pager
```

This does not change `config.json` or recordings because they are not tracked
by Git. Do not use `git clean` unless you are certain you do not need local
files.

## One-command helpers from Termux

Run these from the repository checkout on your phone. Set `LM_HOST` or `LM_KEY`
before a command to override the default VM address or SSH key. Each command
saves its output in `Documents` on shared storage.

- `deploy/vm-deploy.sh` checks that recordings are idle, then runs the VM deploy
  script and shows the latest commit and service state.
- `deploy/vm-restart-if-idle.sh` checks recordings, restarts the service, and
  waits up to 30 seconds for it to become active.
- `deploy/vm-install-cookies.sh <local-cookies-file>` checks cookie domains,
  installs the file with restricted permissions, and updates the VM config.
  Run the restart helper afterward to apply it.
- `deploy/check_cookies_file.py <cookies-file>` checks a Netscape cookie file
  and reports only allowed domains and their counts.
- `deploy/vm-fallback-test.sh <channel-handle>` runs the yt-dlp fallback check
  once without cookies and again with the VM cookie file when present.
