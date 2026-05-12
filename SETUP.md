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
1. Log into your SaveInCloud dashboard at app.paas.saveincloud.net.br
2. Go to Settings > API Tokens
3. Generate a new token with billing permissions
4. Paste the token in config.yaml under `jelastic.token`
5. The API URL is already set for SaveInCloud: `https://app.paas.saveincloud.net.br/1.0/`

### Upload Method

The script supports both SFTP (SSH) and FTP. Enable one in config.yaml:

**SFTP (recommended):**
```yaml
sftp:
  enabled: true
  host: "your-server-ip"
  port: 22
  username: "your-user"
  password: "your-password"
  remote_dir: "/home/arthur/"
```

**FTP:**
```yaml
ftp:
  enabled: true
  host: "ftp.example.com"
  port: 21
  username: "ftpuser"
  password: "ftppass"
  use_tls: true
  remote_dir: "/billing/"
```

### Date Range
- `billing.lookback: "7d"` = last 7 days (default)
- `billing.lookback: "30d"` = last 30 days
- `billing.lookback: "1m"` = last month
- Or set fixed dates with `billing.start_date` and `billing.end_date`

The script automatically splits large date ranges into valid API chunks
(HOUR: max 1 day, DAY: max 7 days per request).

### CSV Options
- `csv.per_environment: false` = one combined CSV (default)
- `csv.per_environment: true` = one CSV per environment
- Customize filename: `csv.filename_pattern: "billing_{date}.csv"`
- Available placeholders: {date}, {datetime}, {env}, {start}, {end}

## 4. Test Run

```bash
python3 jelastic_billing_export.py
```

Check the `output/` folder for CSV files and `logs/` for the log file.

For verbose output, set `logging.level: "DEBUG"` in config.yaml.

## 5. Schedule with Cron (Weekly)

```bash
# Open crontab editor
crontab -e

# Run every Monday at 6:00 AM
0 6 * * 1 cd /opt/jelastic-billing && /usr/bin/python3 jelastic_billing_export.py >> /dev/null 2>&1
```

Common cron schedules:
- Every Monday 6 AM: `0 6 * * 1`
- Every Sunday midnight: `0 0 * * 0`
- First of each month: `0 6 1 * *`
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

Enable:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now jelastic-billing.timer
```

## 7. Environment Variables (Optional)

For security, use environment variables instead of config.yaml:

```bash
export JELASTIC_TOKEN="your_token_here"
export SFTP_HOST="your-server-ip"
export SFTP_USER="username"
export SFTP_PASS="password"
```

These override the values in config.yaml.

## 8. Troubleshooting

- Check logs at `./logs/billing_export.log`
- Set `logging.level: "DEBUG"` for detailed API call output
- Test API: `curl "https://app.paas.saveincloud.net.br/1.0/environment/control/rest/getenvs?session=YOUR_TOKEN"`
- If SFTP fails, verify SSH access: `ssh user@host`
- "Interval too large" errors: the script handles this automatically via chunking
- Empty billing data: normal for trial/collaborator accounts with no resource usage
