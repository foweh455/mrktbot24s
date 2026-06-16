# MRKT Bot: deploy 24/7 on Ubuntu 24.04

## 1) Install base packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

## 2) Upload project to server

```bash
cd /opt
sudo mkdir -p mrktbot10
sudo chown -R $USER:$USER /opt/mrktbot10
```

Copy project files into `/opt/mrktbot10` (git clone or scp).

## 3) Create venv and install dependencies

```bash
cd /opt/mrktbot10
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

## 4) Smoke test (manual run)

```bash
cd /opt/mrktbot10
source .venv/bin/activate
python main.py
```

Stop with `Ctrl+C` after checking startup logs.

## 5) Create systemd service

```bash
sudo tee /etc/systemd/system/mrktbot.service > /dev/null << 'EOF'
[Unit]
Description=MRKT NFT Sniper Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/mrktbot10
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/mrktbot10/.venv/bin/python /opt/mrktbot10/main.py
Restart=always
RestartSec=3
KillSignal=SIGINT
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
EOF
```

## 6) Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mrktbot.service
sudo systemctl status mrktbot.service --no-pager
```

## 7) Logs and control

```bash
sudo journalctl -u mrktbot.service -f
sudo systemctl restart mrktbot.service
sudo systemctl stop mrktbot.service
sudo systemctl start mrktbot.service
```

## Important checks

- Run only **one** instance of the bot (no extra manual process in `screen/tmux` if systemd is enabled).
- If bot was paused after purchase, its pause state is now persisted in `runtime_state.json`.
- Telegram polling now auto-recovers after transient errors.
