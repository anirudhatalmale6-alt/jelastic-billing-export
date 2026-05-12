# Jelastic Billing Export - Setup Guide

## 1. Requirements

- Python 3.8 or higher
- Linux VM (Ubuntu, Debian, CentOS, etc.)

## 2. Installation

```bash
# Clone or copy the project files to your server
cd /opt/jelastic-billing    # or any directory you prefer

# Install Python dependencies
pip3 install -r requirements.txt
```

## 3. Configuration

Edit `config.yaml` with your details:

### Jelastic API
1. Log into your SaveInCloud dashboard
2. Go to Settings > API Tokens
3. Generate a new token with billing read permissions
4. Paste the token in config.yaml under `jelastic.token`
5. Set `jelastic.api_url` to your provider URL (e.g. `https://app.saveincloud.com/1.0/`)

### FTP Server
Fill in your FTP host, username, password, and remote directory under the `ftp` section.

### Date Range
- Use `billing.lookback: "7d"` for last 7 days (default)
- Use `billing.lookback: "30d"` for last 30 days
- Or set fixed dates with `billing.start_date` and `billing.end_date`

### CSV Options
- `csv.per_environment: false` = one combined CSV (default)
- `csv.per_environment: true` = one CSV per environment
- Customize the filename with `csv.filename_pattern`

## 4. Test Run

```bash
python3 jelastic_billing_export.py
```

Check the `output/` folder for CSV files and `logs/` for the log file.

## 5. Schedule with Cron (Weekly)

```bash
# Open crontab editor
crontab -e

# Add this line to run every Monday at 6:00 AM
0 6 * * 1 cd /opt/jelastic-billing && /usr/bin/python3 jelastic_billing_export.py >> /dev/null 2>&1
```

Common cron schedules:
- Every Monday at 6 AM: `0 6 * * 1`
- Every Sunday at midnight: `0 0 * * 0`
- First day of each month: `0 6 1 * *`
- Every day at 8 AM: `0 8 * * *`

## 6. Alternative: Systemd Timer

Create `/etc/systemd/system/jelastic-billing.service`:
```ini
[Unit]
Description=Jelastic Billing Export

[Service]
Type=oneshot
WorkingDirectory=/opt/jelastic-billing
ExecStart=/usr/bin/python3 jelastic_billing_export.py
User=your_username
```

Create `/etc/systemd/system/jelastic-billing.timer`:
```ini
[Unit]
Description=Run Jelastic Billing Export Weekly

[Timer]
OnCalendar=Mon 06:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable jelastic-billing.timer
sudo systemctl start jelastic-billing.timer
```

## 7. Environment Variables (Optional)

Instead of putting credentials in config.yaml, use environment variables:

```bash
export JELASTIC_TOKEN="your_token_here"
export FTP_HOST="ftp.example.com"
export FTP_USER="ftpuser"
export FTP_PASS="ftppassword"
```

These override the values in config.yaml.

## 8. Troubleshooting

- Check logs at `./logs/billing_export.log`
- Run with DEBUG logging: set `logging.level: "DEBUG"` in config.yaml
- Test API connection: `curl "https://app.saveincloud.com/1.0/environment/control/rest/getenvs?session=YOUR_TOKEN"`
- If FTP fails, try setting `ftp.use_tls: false`
